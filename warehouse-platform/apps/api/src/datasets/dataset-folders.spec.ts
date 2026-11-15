import { DatasetsService } from './datasets.service';
import { DatasetContentsQueryDto } from './dto/dataset-folder.dto';
import { AuthUser } from '../auth/auth.types';
import { uploadDestination, validateFolderPath } from '../storage/dataset-path';
const user: AuthUser = { id: 'user', role: 'OPERATOR', tenantId: 'tenant', projectIds: ['project'], email: '', displayName: '', mustChangePassword: false };
const dataset = { id: 'dataset', name: 'test02' };
function setup() {
  const folders = new Map<string, { id: string; path: string; name: string; parentPath: string }>();
  const tx = { $queryRaw: jest.fn(), dataset: { findFirst: jest.fn().mockResolvedValue(dataset) }, datasetFolder: {
    findUnique: jest.fn(({where}) => Promise.resolve(folders.get(where.datasetId_path.path) ?? null)),
    findUniqueOrThrow: jest.fn(({where}) => Promise.resolve(folders.get(where.datasetId_path.path))),
    upsert: jest.fn(({create}) => { const folder = { id: create.path, ...create }; folders.set(create.path, folder); return Promise.resolve(folder); }),
    findMany: jest.fn(({where}) => Promise.resolve([...folders.values()].filter(folder => folder.parentPath === where.parentPath))),
  }, robotFile: { findFirst: jest.fn().mockResolvedValue(null), findMany: jest.fn().mockResolvedValue([{ id: 'file', fileSize: 4n }]), count: jest.fn().mockResolvedValue(1) }, uploadSession: { findFirst: jest.fn().mockResolvedValue(null) } };
  const prisma = { ...tx, $transaction: jest.fn(arg => typeof arg === 'function' ? arg(tx) : Promise.all(arg)) };
  const storage = { putObject: jest.fn().mockResolvedValue(undefined) };
  return { folders, tx, storage, service: new DatasetsService(prisma as never, storage as never) };
}
describe('Dataset folders', () => {
  it('creates nested empty folders with matching object-storage markers', async () => {
    const {service, storage} = setup();
    await service.createFolder('dataset', '', '图片', user);
    await service.createFolder('dataset', '图片', '原始数据', user);
    expect(storage.putObject).toHaveBeenCalledWith(expect.objectContaining({ objectKey: 'test02/图片/.folder' }));
    expect(storage.putObject).toHaveBeenCalledWith(expect.objectContaining({ objectKey: 'test02/图片/原始数据/.folder' }));
  });
  it('rejects a duplicate name in the same parent', async () => {
    const {service} = setup(); await service.createFolder('dataset', '', 'images', user);
    await expect(service.createFolder('dataset', '', 'images', user)).rejects.toThrow('同名文件夹');
  });
  it('allows the same child name in different parents', async () => {
    const {service, folders} = setup();
    for (const parent of ['images', 'labels']) { await service.createFolder('dataset', '', parent, user); await service.createFolder('dataset', parent, 'raw', user); }
    expect(folders.has('images/raw')).toBe(true); expect(folders.has('labels/raw')).toBe(true);
  });
  it('rejects a missing parent and inaccessible dataset before storage work', async () => {
    const {service, tx, storage} = setup();
    await expect(service.createFolder('dataset', 'missing', 'raw', user)).rejects.toThrow('上级文件夹不存在');
    tx.dataset.findFirst.mockResolvedValue(null);
    await expect(service.createFolder('dataset', '', 'raw', user)).rejects.toThrow('无访问权限');
    expect(storage.putObject).not.toHaveBeenCalled();
  });
  it('rejects names occupied by files or in-flight uploads', async () => {
    const {service, tx} = setup(); tx.robotFile.findFirst.mockResolvedValue({ id: 'file' });
    await expect(service.createFolder('dataset', '', 'raw', user)).rejects.toThrow('同名文件');
    tx.robotFile.findFirst.mockResolvedValue(null); tx.uploadSession.findFirst.mockResolvedValue({ id: 'upload' });
    await expect(service.createFolder('dataset', '', 'raw', user)).rejects.toThrow('正在上传');
  });
  it('browses only immediate child folders and files at the requested path', async () => {
    const {service, tx} = setup();
    await service.createFolder('dataset', '', 'images', user); await service.createFolder('dataset', 'images', 'raw', user);
    const result = await service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: 'images' }), user);
    expect(result.folders.map(folder => folder.path)).toEqual(['images/raw']);
    expect(result.files[0].fileSize).toBe('4');
    expect(tx.robotFile.findMany).toHaveBeenCalledWith(expect.objectContaining({ where: { datasetId: 'dataset', folderPath: 'images' } }));
  });
  it('reports storage failure instead of reporting folder creation success', async () => {
    const {service, storage} = setup(); storage.putObject.mockRejectedValue(new Error('storage offline'));
    await expect(service.createFolder('dataset', '', 'raw', user)).rejects.toThrow('storage offline');
  });
  it.each(['../escape', '/absolute', 'images//raw', 'images/../raw', 'images/.folder', 'images\\raw'])('rejects unsafe path %s', path => {
    expect(() => validateFolderPath(path)).toThrow();
  });
  it('preserves uploaded directory hierarchy within the selected folder', () => {
    expect(uploadDestination('images/raw', 'batch/sub/file.txt')).toEqual({ folderPath: 'images/raw/batch/sub', name: 'file.txt' });
  });
});


const archiveTimePath = '原相机前/原相机前_16:04-16:05';
const segmentA = 'a'.repeat(32), segmentB = 'b'.repeat(32);
type FolderCondition = { parentPath?: string | { in: string[] }; path?: { notIn: string[] }; OR?: FolderCondition[] };
function archiveSetup() {
  const value = setup();
  value.tx.dataset.findFirst.mockResolvedValue({ ...dataset, metadata: { type: 'garage-native-archive' } });
  const add = (path: string) => value.folders.set(path, { id: path, path, name: path.split('/').at(-1)!, parentPath: path.split('/').slice(0, -1).join('/') });
  add('原相机前'); add(archiveTimePath);
  const matches = (folder: { path: string; parentPath: string }, condition: FolderCondition): boolean => {
    if (condition.OR) return condition.OR.some(part => matches(folder, part));
    const parent = condition.parentPath;
    return (typeof parent === 'string' ? folder.parentPath === parent : !parent || parent.in.includes(folder.parentPath))
      && (!condition.path?.notIn || !condition.path.notIn.includes(folder.path));
  };
  value.tx.datasetFolder.findMany.mockImplementation(({ where }) => Promise.resolve(
    [...value.folders.values()].filter(folder => matches(folder, where as FolderCondition)).sort((a, b) => a.name.localeCompare(b.name) || a.path.localeCompare(b.path))));
  const files: Array<{ id: string; datasetId: string; folderPath: string; originalName: string; fileSize: bigint;
    capturedAt?: Date; metadata?: Record<string, unknown> }> = [];
  type FileCondition = { datasetId?: string; id?: { gt: string }; folderPath?: string | { in?: string[]; startsWith?: string }; OR?: FileCondition[] };
  const fileMatches = (file: typeof files[number], where: FileCondition): boolean =>
    (!where.datasetId || file.datasetId === where.datasetId) && (!where.id?.gt || file.id > where.id.gt)
    && (!where.OR || where.OR.some(part => fileMatches(file, part)))
    && (!where.folderPath || (typeof where.folderPath === 'string' ? file.folderPath === where.folderPath
      : where.folderPath.in ? where.folderPath.in.includes(file.folderPath)
        : file.folderPath.startsWith(where.folderPath.startsWith!.replace(/\\(.)/g, '$1'))));
  const matchingFiles = (where: FileCondition) => files.filter(file => fileMatches(file, where));
  value.tx.robotFile.findMany.mockImplementation(({ where, skip = 0, take = 50, select }) => Promise.resolve(
    matchingFiles(where).sort((a, b) => select ? a.id.localeCompare(b.id) : a.originalName.localeCompare(b.originalName) || a.id.localeCompare(b.id)).slice(skip, skip + take)));
  value.tx.robotFile.count.mockImplementation(({ where }) => Promise.resolve(matchingFiles(where).length));
  return { ...value, add, files };
}

describe('Archive time-slot directory projection', () => {
  it('opens a single segment video directly without moving or renaming the file', async () => {
    const { service, add, files, tx, storage } = archiveSetup();
    const realPath = archiveTimePath + '/' + segmentA;
    add(realPath); files.push({ id: 'video-a', datasetId: 'dataset', folderPath: realPath, originalName: 'video.mkv', fileSize: 123n });
    const result = await service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: archiveTimePath }), user);
    expect(result).toMatchObject({ path: archiveTimePath, view: { kind: 'archive-time-slot', collapsedSegments: 1 }, total: 1, folders: [] });
    expect(result.files).toEqual([{ id: 'video-a', datasetId: 'dataset', folderPath: realPath, originalName: 'video.mkv', fileSize: '123' }]);
    expect(tx.robotFile.count).toHaveBeenCalledWith({ where: { datasetId: 'dataset', folderPath: { in: [archiveTimePath, realPath] } } });
    expect(storage.putObject).not.toHaveBeenCalled(); expect(tx.datasetFolder.upsert).not.toHaveBeenCalled();
  });
  it('combines every segment and direct file with one correct paginated total', async () => {
    const { service, add, files, tx } = archiveSetup();
    const paths = [segmentA, segmentB].map(id => archiveTimePath + '/' + id); paths.forEach(add);
    files.push({ id: 'notes', datasetId: 'dataset', folderPath: archiveTimePath, originalName: 'notes.txt', fileSize: 1n });
    for (let i = 0; i < 55; i++) files.push({ id: 'file-' + String(i).padStart(2, '0'), datasetId: 'dataset', folderPath: paths[i % 2], originalName: 'video.mkv', fileSize: 2n });
    files.push({ id: 'other-dataset', datasetId: 'not-dataset', folderPath: paths[0], originalName: 'secret.mkv', fileSize: 3n });
    const query = Object.assign(new DatasetContentsQueryDto(), { path: archiveTimePath, page: 2 });
    const result = await service.contents('dataset', query, user);
    expect(result.total).toBe(56); expect(result.files).toHaveLength(6);
    expect(result.view).toEqual({ kind: 'archive-time-slot', collapsedSegments: 2 });
    expect(result.files.some(file => file.id === 'other-dataset')).toBe(false);
    expect(tx.robotFile.findMany).toHaveBeenCalledWith(expect.objectContaining({ skip: 50, take: 50,
      where: { datasetId: 'dataset', folderPath: { in: [archiveTimePath, ...paths] } }, orderBy: [{ originalName: 'asc' }, { id: 'asc' }] }));
  });
  it('keeps user folders and lifts only the immediate children of hidden segment folders', async () => {
    const { service, add } = archiveSetup();
    const segment = archiveTimePath + '/' + segmentA; add(segment); add(segment + '/compressed-bag'); add(segment + '/compressed-bag/nested');
    add(archiveTimePath + '/notes'); add(archiveTimePath + '/notes/nested');
    const result = await service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: archiveTimePath }), user);
    expect(result.folders.map(folder => folder.path)).toEqual([segment + '/compressed-bag', archiveTimePath + '/notes']);
    expect(result.folders[0].name).toBe('compressed-bag');
  });
  it('preserves ordinary dataset folders and old links into a real segment path', async () => {
    const { service, add, files, tx } = archiveSetup();
    const segment = archiveTimePath + '/' + segmentA; add(segment);
    files.push({ id: 'video', datasetId: 'dataset', folderPath: segment, originalName: 'video.mkv', fileSize: 1n });
    let result = await service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: segment }), user);
    expect(result.files).toHaveLength(1); expect(result.view).toBeUndefined();
    tx.dataset.findFirst.mockResolvedValue(dataset);
    result = await service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: archiveTimePath }), user);
    expect(result.folders.map(folder => folder.path)).toEqual([segment]); expect(result.files).toEqual([]); expect(result.view).toBeUndefined();
  });
  it('does not flatten other archive levels or non-generated child folder names', async () => {
    const { service, add } = archiveSetup();
    add(archiveTimePath + '/manual-folder');
    let result = await service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: archiveTimePath }), user);
    expect(result.view).toBeUndefined(); expect(result.folders[0].name).toBe('manual-folder');
    add('原相机前/' + segmentA);
    result = await service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: '原相机前' }), user);
    expect(result.view).toBeUndefined(); expect(result.folders.some(folder => folder.name === segmentA)).toBe(true);
  });
  it('denies inaccessible or missing paths before any file query or storage call', async () => {
    const { service, tx, storage } = archiveSetup();
    tx.dataset.findFirst.mockResolvedValue(null);
    await expect(service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: archiveTimePath }), user)).rejects.toThrow('无访问权限');
    expect(tx.datasetFolder.findMany).not.toHaveBeenCalled(); expect(tx.robotFile.findMany).not.toHaveBeenCalled();
    tx.dataset.findFirst.mockResolvedValue({ ...dataset, metadata: { type: 'garage-native-archive' } });
    await expect(service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: archiveTimePath + '/missing' }), user)).rejects.toThrow('文件夹不存在');
    expect(tx.robotFile.findMany).not.toHaveBeenCalled(); expect(storage.putObject).not.toHaveBeenCalled();
  });
  it('adds source capture dates to visible archive folders without changing their paths or names', async () => {
    const { service, add, files, tx } = archiveSetup();
    tx.dataset.findFirst.mockResolvedValue({ ...dataset, metadata: { type: 'garage-native-archive', day: '2026-11-03' } });
    add('雷达MID360'); add('原相机前邻居'); add(archiveTimePath + '/' + segmentA);
    files.push({ id: 'camera', datasetId: 'dataset', folderPath: archiveTimePath + '/' + segmentA,
      originalName: 'video.mkv', fileSize: 1n, capturedAt: new Date('2026-11-03T00:00:00Z'),
      metadata: { startedAt: '2026-11-01T15:59:00Z', endAt: '2026-11-01T16:01:00Z' } },
    { id: 'neighbor', datasetId: 'dataset', folderPath: '原相机前邻居', originalName: 'other.mkv', fileSize: 1n,
      capturedAt: new Date('2026-10-01T00:00:00Z') },
    { id: 'other-dataset', datasetId: 'other-dataset', folderPath: '原相机前', originalName: 'private.mkv', fileSize: 1n,
      capturedAt: new Date('2026-01-01T00:00:00Z') });
    const result = await service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: '' }), user);
    expect(result.folders).toEqual(expect.arrayContaining([
      expect.objectContaining({ path: '原相机前', name: '原相机前', captureDateRange: { from: '2026-11-01', to: '2026-11-02' } }),
      expect.objectContaining({ path: '雷达MID360', name: '雷达MID360', captureDateRange: { from: '2026-11-03', to: '2026-11-03' } }),
    ]));
  });
  it('reduces every metadata page rather than truncating a folder capture range at 1000 files', async () => {
    const { service, files, tx } = archiveSetup();
    for (let i = 0; i < 1001; i++) files.push({ id: String(i).padStart(5, '0'), datasetId: 'dataset', folderPath: archiveTimePath,
      originalName: 'part.mkv', fileSize: 1n, capturedAt: new Date(i < 1000 ? '2026-11-01T00:00:00Z' : '2026-11-02T00:00:00Z') });
    const result = await service.contents('dataset', Object.assign(new DatasetContentsQueryDto(), { path: '' }), user);
    expect(result.folders[0]).toMatchObject({ path: '原相机前', captureDateRange: { from: '2026-11-01', to: '2026-11-02' } });
    expect(tx.robotFile.findMany).toHaveBeenCalledWith(expect.objectContaining({
      select: { id: true, folderPath: true, metadata: true, capturedAt: true }, take: 1000,
      where: expect.objectContaining({ datasetId: 'dataset', id: { gt: '00999' } }),
    }));
  });
});
