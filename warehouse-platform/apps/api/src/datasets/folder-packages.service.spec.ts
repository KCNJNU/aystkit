import { ConfigService } from '@nestjs/config';
import { UserRole } from '@prisma/client';
import { execFile } from 'node:child_process';
import * as childProcess from 'node:child_process';
import { createHash } from 'node:crypto';
import { EventEmitter } from 'node:events';
import * as fs from 'node:fs/promises';
import { mkdtemp, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { PassThrough, Readable } from 'node:stream';
import { promisify } from 'node:util';
import { AuthUser } from '../auth/auth.types';
import { ArchiveRecordingsService } from './archive-recordings.service';

const actor: AuthUser = { id: 'user', role: UserRole.OPERATOR, email: '', displayName: '',
  tenantId: 'tenant', projectIds: ['project'], mustChangePassword: false };
const dataset = { id: 'dataset', name: '地库_2026-11-02', tenantId: 'tenant', projectId: 'project',
  metadata: { type: 'garage-native-archive' }, deletingAt: null, updatedAt: new Date('2026-11-02T00:00:00Z') };
type Row = Record<string, unknown>;
const runFile = promisify(execFile);

// Prisma 6 PostgreSQL startsWith uses LIKE: % and _ remain patterns unless
// escaped. Keep this model close to SQL semantics rather than JS startsWith.
function likePrefix(actual: string, pattern: string) {
  let expression = '^';
  const literal = (char: string) => char.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  for (let index = 0; index < pattern.length; index++) {
    const char = pattern[index];
    if (char === '\\' && index + 1 < pattern.length) expression += literal(pattern[++index]);
    else if (char === '%') expression += '[\\s\\S]*';
    else if (char === '_') expression += '[\\s\\S]';
    else expression += literal(char);
  }
  return new RegExp(expression).test(actual);
}

function file(id: string, folderPath: string, originalName: string, text: string) {
  const content = Buffer.from(text);
  return { id, datasetId: dataset.id, robotId: 'robot', folderPath, originalName,
    objectKey: `objects/${id}`, fileSize: BigInt(content.length),
    checksum: createHash('sha256').update(content).digest('hex'), content,
    recognitionType: 'training-data', fileType: 'binary', mimeType: 'application/octet-stream',
    schemaVersion: '1', metadata: {}, capturedAt: new Date('2026-11-02T01:00:00Z'),
    createdAt: new Date('2026-11-02T01:00:00Z'), updatedAt: new Date('2026-11-02T01:00:00Z') };
}

// These fakes enforce actual query predicates. Returning the entire fixture for
// every query would conceal the A/AB prefix and cross-dataset regressions.
function matches(row: Row, where: Row = {}): boolean {
  return Object.entries(where).every(([key, value]) => {
    if (value === undefined) return true;
    if (key === 'OR') return (value as Row[]).some(item => matches(row, item));
    if (key === 'AND') return (Array.isArray(value) ? value : [value]).every(item => matches(row, item as Row));
    if (key === 'NOT') return !(Array.isArray(value) ? value : [value]).some(item => matches(row, item as Row));
    const actual = row[key];
    if (value !== null && typeof value === 'object' && !(value instanceof Date)) {
      const filter = value as Row;
      return Object.entries(filter).every(([operator, operand]) => {
        if (operator === 'equals') return actual === operand;
        if (operator === 'in') return (operand as unknown[]).includes(actual);
        if (operator === 'notIn') return !(operand as unknown[]).includes(actual);
        if (operator === 'not') return actual !== operand;
        if (operator === 'startsWith') return typeof actual === 'string' && likePrefix(actual, String(operand));
        if (operator === 'mode') return true;
        throw new Error(`Unsupported fixture filter ${operator}`);
      });
    }
    return actual === value;
  });
}

function setup(base: string) {
  const state = { dataset: { ...dataset } as typeof dataset | null,
    files: [file('direct', 'A', 'video.mkv', 'direct video bytes'),
      file('nested', 'A/slot', 'video.mkv', 'nested video bytes'),
      file('meta', 'A/slot/nested', 'manifest.json', '{"training":true}'),
      file('neighbor', 'AB', 'neighbor.mkv', 'not in A'),
      file('outside', '', 'root.txt', 'root only')],
    folders: ['A', 'A/slot', 'A/slot/nested', 'A/empty', 'A/empty/child', 'AB', 'empty', 'empty/child']
      .map(path => ({ id: `folder-${path}`, datasetId: dataset.id, path,
        parentPath: path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '',
        name: path.split('/').at(-1)!, objectKey: `folders/${path}` })),
  };
  const query = (rows: Row[], args: { where?: Row; take?: number } = {}) => {
    const found = rows.filter(row => matches(row, args.where));
    return (args.take === undefined ? found : found.slice(0, args.take)).map(row => structuredClone(row));
  };
  const prisma = {
    dataset: { findFirst: jest.fn(async (args: { where?: Row } = {}) =>
      state.dataset && matches(state.dataset, args.where) ? state.dataset : null) },
    datasetFolder: {
      findUnique: jest.fn(async (args: { where: { datasetId_path: { datasetId: string; path: string } } }) =>
        state.folders.find(row => matches(row, args.where.datasetId_path)) ?? null),
      findFirst: jest.fn(async (args: { where?: Row } = {}) => query(state.folders, args)[0] ?? null),
      findMany: jest.fn(async (args: { where?: Row; take?: number } = {}) => query(state.folders, args)),
    },
    robotFile: {
      findMany: jest.fn(async (args: { where?: Row; take?: number } = {}) => query(state.files, args)),
      count: jest.fn(async (args: { where?: Row } = {}) => query(state.files, args).length),
    },
    $transaction: jest.fn(async (operation: unknown): Promise<unknown> => typeof operation === 'function'
      ? operation(prisma) : Promise.all(operation as Promise<unknown>[])),
  };
  const cache = new Map<string, Buffer>();
  const modified = new Map<string, Date>();
  let published = 0;
  let afterRead: ((key: string) => void) | undefined;
  const storage = {
    exists: jest.fn(async (key: string) => cache.has(key)),
    getObject: jest.fn(async (key: string) => {
      const row = state.files.find(source => source.objectKey === key);
      if (!row) throw new Error('fixture object missing');
      const bytes = Buffer.from(row.content);
      afterRead?.(key);
      const split = Math.max(1, Math.floor(bytes.length / 2));
      return Readable.from([bytes.subarray(0, split), bytes.subarray(split)]);
    }),
    putObject: jest.fn(async (input: { objectKey: string; content: Readable; contentLength: number }) => {
      expect(Buffer.isBuffer(input.content)).toBe(false);
      const chunks: Buffer[] = [];
      for await (const chunk of input.content) chunks.push(Buffer.from(chunk));
      const bytes = Buffer.concat(chunks);
      expect(input.contentLength).toBe(bytes.length);
      cache.set(input.objectKey, bytes);
      modified.set(input.objectKey, new Date(Date.now() - 1000 + ++published));
    }),
    getPresignedDownloadUrl: jest.fn(async (key: string) => `/objects/${key}`),
    deleteObject: jest.fn(async (key: string) => { cache.delete(key); modified.delete(key); }),
    deleteObjects: jest.fn(async (keys: string[]) => {
      for (const key of keys) { cache.delete(key); modified.delete(key); }
    }),
    listObjects: jest.fn(async (input: {prefix?: string; cursor?: string; limit: number}) => {
      const keys=[...cache.keys()].filter(key => key.startsWith(input.prefix ?? '')
        && (!input.cursor || key > input.cursor)).sort();
      const page=keys.slice(0,input.limit);
      return { items: page.map(objectKey => ({ objectKey, size: cache.get(objectKey)!.length,
        lastModified: modified.get(objectKey) })), nextCursor: keys.length > page.length ? page.at(-1) : undefined };
    }),
  };
  const service = new ArchiveRecordingsService(prisma as never, {} as never, storage as never,
    new ConfigService({ ARCHIVE_VIDEO_WORK_DIR: base }));
  const download = (path = 'A', user = actor) => (service as unknown as {
    folderPackage: (datasetId: string, path: string, user: AuthUser) => Promise<{url: string; fileName: string}>;
  }).folderPackage(dataset.id, path, user);
  const prepare = async (path = 'A', user = actor, retry = false, taskId?: string) => {
    const result = await service.folderPackage(dataset.id, path, user, { background: true, retry, taskId });
    if (!('state' in result)) throw new Error('Expected background response state');
    return result;
  };
  return { state, prisma, storage, cache, modified, service, download, prepare,
    onRead: (callback: (key: string) => void) => { afterRead = callback; } };
}

async function unpack(work: string, cache: Map<string, Buffer>) {
  const packed = join(work, 'received.tar.zst');
  expect(cache.size).toBe(1);
  await writeFile(packed, [...cache.values()][0]);
  const members = (await runFile('tar', ['--zstd', '-tf', packed], { encoding: 'utf8' })).stdout.trim().split('\n');
  const bytes = async (member: string) => (await runFile('tar', ['--zstd', '-xOf', packed, member], { encoding: 'buffer' })).stdout;
  return { members, bytes, manifest: JSON.parse((await bytes('./manifest.json')).toString()) };
}

const tasksOf = (service: ArchiveRecordingsService) => (service as unknown as {
  folderPackageTasks: Map<string, { id: string; state: string }>;
}).folderPackageTasks;

async function waitForTask(service: ArchiveRecordingsService, id: string) {
  for (let poll = 0; poll < 200; poll++) {
    const task = [...tasksOf(service).values()].find(row => row.id === id);
    if (task && task.state !== 'processing') return task;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw new Error('Background package did not settle');
}

describe('recursive folder download snapshots', () => {
  let work: string, staging: string;
  beforeEach(async () => { work = await mkdtemp(join(tmpdir(), 'folder-package-test-')); staging = join(work, 'staging'); });
  afterEach(async () => { jest.restoreAllMocks(); await rm(work, { recursive: true, force: true }); });

  it('models real unescaped and escaped SQL wildcard prefixes in its database fake', () => {
    expect(likePrefix('AB/child','A_/')).toBe(true);
    expect(likePrefix('AB/child','A\\_/')).toBe(false);
    expect(likePrefix('A_/child','A\\_/')).toBe(true);
    expect(likePrefix('ABC/child','A%/')).toBe(true);
    expect(likePrefix('ABC/child','A\\%/')).toBe(false);
    expect(likePrefix('A%/child','A\\%/')).toBe(true);
  });

  it.each(['A_', 'A%'])('uses literal SQL prefixes for the valid folder name %j', async path => {
    const fixture=setup(staging);
    fixture.state.files=[file('wanted',`${path}/child`,'radar.zst','selected training bytes'),
      file('neighbor','AB/child','other.zst','must not enter the package')];
    fixture.state.folders=[path,`${path}/child`,'AB','AB/child'].map(folder => ({
      id:`folder-${folder}`,datasetId:dataset.id,path:folder,name:folder.split('/').at(-1)!,
      parentPath:folder.includes('/') ? folder.slice(0,folder.lastIndexOf('/')) : '',objectKey:`folders/${folder}` }));
    await fixture.download(path);
    const packed=await unpack(work,fixture.cache);
    expect(packed.members.filter(name => !name.endsWith('/')).sort()).toEqual([
      './manifest.json',`./files/${path}/child/radar.zst`,
    ].sort());
    expect(await packed.bytes(`./files/${path}/child/radar.zst`)).toEqual(fixture.state.files[0].content);
    expect(fixture.storage.getObject).toHaveBeenCalledTimes(1);
  });

  it('makes a real tar/zstd containing every selected recursive file, never neighboring prefixes', async () => {
    const fixture = setup(staging);
    const result = await fixture.download();
    expect(result).toMatchObject({ url: expect.stringContaining('/objects/'), fileName: expect.stringMatching(/\.tar\.zst$/) });
    expect(result.fileName).toMatch(/\d{8}_\d{6}/);
    const packed = await unpack(work, fixture.cache);
    expect(packed.members.filter(name => !name.endsWith('/')).sort()).toEqual([
      './manifest.json', './files/A/video.mkv', './files/A/slot/video.mkv', './files/A/slot/nested/manifest.json',
    ].sort());
    for (const row of fixture.state.files.filter(row => row.folderPath === 'A' || row.folderPath.startsWith('A/'))) {
      const suffix = [row.folderPath.slice(1), row.originalName].filter(Boolean).join('/').replace(/^\//, '');
      expect(await packed.bytes(`./files/A/${suffix}`)).toEqual(row.content);
      expect(JSON.stringify(packed.manifest)).toContain(row.checksum);
    }
    expect(packed.members).toContain('./files/A/empty/child/');
    expect(await readdir(staging)).toEqual([]);
    expect(fixture.storage.getObject).toHaveBeenCalledTimes(3);
  });

  it('keeps the tar output outside its immutable input tree', async () => {
    const fixture = setup(staging);
    const method = (fixture.service as unknown as { createTarZstd: (dir: string, output: string) => Promise<void> }).createTarZstd.bind(fixture.service);
    jest.spyOn(fixture.service as never, 'createTarZstd' as never).mockImplementation((async (dir: string, output: string) => {
      expect(relative(dir, output)).toMatch(/^\.\.[/\\]/);
      await method(dir, output);
    }) as never);
    await fixture.download();
  });

  it('creates a valid archive for a known empty folder, preserving its empty descendants', async () => {
    const fixture = setup(staging);
    await fixture.download('empty');
    const packed = await unpack(work, fixture.cache);
    expect(packed.members.filter(name => !name.endsWith('/'))).toEqual(['./manifest.json']);
    expect(packed.members).toContain('./files/empty/child/');
    expect(fixture.storage.getObject).not.toHaveBeenCalled();
  });

  it('recursively packages an inner folder using its own basename', async () => {
    const fixture = setup(staging);
    await fixture.download('A/slot');
    const packed = await unpack(work, fixture.cache);
    expect(packed.members.filter(name => !name.endsWith('/')).sort()).toEqual([
      './manifest.json', './files/slot/video.mkv', './files/slot/nested/manifest.json',
    ].sort());
  });

  it('supports the dataset root and preserves each top-level directory', async () => {
    const fixture = setup(staging);
    await fixture.download('');
    const packed = await unpack(work, fixture.cache);
    expect(packed.members.filter(name => !name.endsWith('/')).sort()).toEqual([
      './manifest.json', ...fixture.state.files.map(row =>
        `./files/${dataset.name}/${[row.folderPath,row.originalName].filter(Boolean).join('/')}`),
    ].sort());
  });

  it('reserves a third copy for MinIO publication before opening any source', async () => {
    const fixture = setup(staging);
    const total = fixture.state.files.filter(row => row.folderPath === 'A' || row.folderPath.startsWith('A/'))
      .reduce((sum,row) => sum + row.fileSize, 0n);
    jest.spyOn(fs,'statfs').mockResolvedValue({ bavail: 64n*1024n*1024n + total*2n + 1n, bsize: 1n } as never);
    await expect(fixture.download()).rejects.toMatchObject({ status: 503 });
    expect(fixture.storage.getObject).not.toHaveBeenCalled();
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
  });

  it.each(['files','folders'] as const)('rejects over 10000 %s without silently truncating the snapshot', async kind => {
    const fixture = setup(staging);
    if (kind === 'files') fixture.state.files = Array.from({length:10001},(_,index) => ({
      ...fixture.state.files[0],id:`file-${index}`,objectKey:`object-${index}` }));
    else fixture.state.folders = [fixture.state.folders[0], ...Array.from({length:10000},(_,index) => ({
      ...fixture.state.folders[0],id:`folder-${index}`,path:`A/child-${index}`,name:`child-${index}`,parentPath:'A' }))];
    await expect(fixture.download()).rejects.toThrow('上限');
    expect(fixture.storage.getObject).not.toHaveBeenCalled();
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
  });

  it.each(['../A', '/A', 'A//slot', 'A/../AB', 'A\\slot', 'A/slot\n'])('rejects invalid query paths %j before object access', async path => {
    const fixture = setup(staging);
    await expect(fixture.download(path)).rejects.toThrow();
    expect(fixture.storage.getObject).not.toHaveBeenCalled();
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
  });

  it('returns 404 for a nonexistent folder rather than an empty successful package', async () => {
    const fixture = setup(staging);
    await expect(fixture.download('missing')).rejects.toMatchObject({ status: 404 });
    expect(fixture.storage.getObject).not.toHaveBeenCalled();
  });

  it.each([{ tenantId: 'other' }, { projectIds: ['other'] }])('enforces tenant and project access even before cache lookup: %j', async scope => {
    const fixture = setup(staging);
    await fixture.download();
    fixture.storage.exists.mockClear(); fixture.storage.getPresignedDownloadUrl.mockClear();
    await expect(fixture.download('A', { ...actor, ...scope })).rejects.toMatchObject({ status: 404 });
    expect(fixture.storage.exists).not.toHaveBeenCalled();
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
  });

  it('rejects a deleting dataset and never signs a previously cached package', async () => {
    const fixture = setup(staging);
    await fixture.download();
    fixture.state.dataset = null;
    fixture.storage.getPresignedDownloadUrl.mockClear();
    await expect(fixture.download()).rejects.toMatchObject({ status: 404 });
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
  });

  it('rejects duplicate destination names without overwriting either source', async () => {
    const fixture = setup(staging);
    fixture.state.files.push(file('duplicate', 'A', 'video.mkv', 'different content'));
    await expect(fixture.download()).rejects.toMatchObject({ status: 409 });
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
  });

  it('rejects a file/directory name collision before publishing', async () => {
    const fixture = setup(staging);
    fixture.state.files.push(file('collision', 'A', 'slot', 'cannot overwrite folder'));
    await expect(fixture.download()).rejects.toMatchObject({ status: 409 });
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
  });

  it.each(['../escape', '/absolute', 'bad\\name'])('rejects malformed stored filenames %j rather than sanitizing them', async name => {
    const fixture = setup(staging);
    fixture.state.files[0].originalName = name;
    await expect(fixture.download()).rejects.toThrow();
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
  });

  it.each(['checksum', 'fileSize'] as const)('refuses to publish when source bytes disagree with %s', async field => {
    const fixture = setup(staging);
    if (field === 'checksum') fixture.state.files[0].checksum = '0'.repeat(64);
    else fixture.state.files[0].fileSize += 1n;
    await expect(fixture.download()).rejects.toThrow();
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
    expect(await readdir(staging)).toEqual([]);
  });

  it('aborts if a selected file is deleted while its immutable bytes are staged', async () => {
    const fixture = setup(staging);
    fixture.onRead(key => { fixture.state.files = fixture.state.files.filter(row => row.objectKey !== key); });
    await expect(fixture.download()).rejects.toMatchObject({ status: 409 });
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
  });

  it('aborts if a selected file moves during staging, even though its bytes and checksum are unchanged', async () => {
    const fixture = setup(staging);
    fixture.onRead(key => { const row = fixture.state.files.find(item => item.objectKey === key)!; row.folderPath = 'AB'; });
    await expect(fixture.download()).rejects.toMatchObject({ status: 409 });
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
  });

  it('detects a changed display name while staging even when objectKey and bytes stay identical', async () => {
    const fixture = setup(staging);
    fixture.onRead(key => { fixture.state.files.find(row => row.objectKey === key)!.originalName='renamed.mkv'; });
    await expect(fixture.download()).rejects.toMatchObject({ status: 409 });
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
  });

  it('invalidates a cached package after a display-name change without needing dataset.updatedAt', async () => {
    const fixture = setup(staging);
    const before=await fixture.download();
    fixture.state.files[0].originalName='renamed.mkv';
    const after=await fixture.download();
    expect(after.url).not.toBe(before.url);
    expect(fixture.storage.putObject).toHaveBeenCalledTimes(2);
  });

  it('allows later uploads, then invalidates its cache on the next download despite unchanged dataset.updatedAt', async () => {
    const fixture = setup(staging);
    let inserted = false;
    fixture.onRead(() => { if (!inserted) { inserted = true; fixture.state.files.push(file('new', 'A', 'later.json', 'later upload')); } });
    const first = await fixture.download();
    expect(fixture.storage.putObject).toHaveBeenCalledTimes(1);
    const second = await fixture.download();
    expect(second.url).not.toBe(first.url);
    expect(fixture.storage.putObject).toHaveBeenCalledTimes(2);
    const third = await fixture.download();
    expect(third.url).toBe(second.url);
    expect(fixture.storage.putObject).toHaveBeenCalledTimes(2);
  });

  it('does not sign a package when publication concurrently deletes the dataset', async () => {
    const fixture = setup(staging);
    const publish = fixture.storage.putObject.getMockImplementation()!;
    fixture.storage.putObject.mockImplementation(async input => { await publish(input); fixture.state.dataset = null; });
    await expect(fixture.download()).rejects.toThrow();
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
  });

  it('keeps only the current and one previous package after three content generations', async () => {
    const fixture=setup(staging);
    const first=await fixture.download();
    const firstKey=first.url.slice('/objects/'.length);
    const prefix=firstKey.slice(0,firstKey.lastIndexOf('/')+1);
    // A different folder's valid cache is outside this directory's lifecycle.
    const neighbor=await fixture.download('AB');
    const neighborKey=neighbor.url.slice('/objects/'.length);
    fixture.state.files.push(file('generation-two','A','second.json','second generation'));
    const second=await fixture.download();
    fixture.state.files.push(file('generation-three','A','third.json','third generation'));
    const third=await fixture.download();
    expect([...fixture.cache.keys()].filter(key => key.startsWith(prefix)).sort()).toEqual([
      second.url.slice('/objects/'.length),third.url.slice('/objects/'.length),
    ].sort());
    expect(fixture.cache.has(firstKey)).toBe(false);
    expect(fixture.cache.has(neighborKey)).toBe(true);
    expect(fixture.storage.listObjects).toHaveBeenCalledWith(expect.objectContaining({prefix,limit:1000}));
  });

  it('removes expired previous archives when building a new generation', async () => {
    const fixture=setup(staging);
    const first=await fixture.download();
    const firstKey=first.url.slice('/objects/'.length);
    fixture.modified.set(firstKey,new Date(Date.now()-2*3600_000));
    fixture.state.files.push(file('fresh','A','new.json','fresh generation'));
    const current=await fixture.download();
    expect([...fixture.cache.keys()]).toEqual([current.url.slice('/objects/'.length)]);
  });

  it('reads every object-list page before retaining the newest predecessor', async () => {
    const fixture=setup(staging);
    const first=await fixture.download();
    const key=first.url.slice('/objects/'.length);
    const prefix=key.slice(0,key.lastIndexOf('/')+1);
    const older=prefix+'1'.repeat(64)+'.tar.zst';
    const newest=prefix+'2'.repeat(64)+'.tar.zst';
    fixture.cache.set(older,Buffer.from('old cache')); fixture.modified.set(older,new Date(Date.now()-2000));
    fixture.cache.set(newest,Buffer.from('newest predecessor')); fixture.modified.set(newest,new Date(Date.now()-100));
    const list=fixture.storage.listObjects.getMockImplementation()!;
    // Short pages exercise the same cursor contract as the production 1000 cap.
    fixture.storage.listObjects.mockImplementation(input => list({...input,limit:1}));
    fixture.state.files.push(file('new-generation','A','fresh.json','newest generation'));
    const current=await fixture.download();
    expect([...fixture.cache.keys()].sort()).toEqual([newest,current.url.slice('/objects/'.length)].sort());
    expect(fixture.storage.listObjects.mock.calls.some(([input]) => input.cursor !== undefined)).toBe(true);
    expect(fixture.storage.deleteObjects.mock.calls.flatMap(([keys]) => keys)).toContain(older);
  });

  it('rejects an object-list response outside the exact cache prefix without deleting it', async () => {
    const fixture=setup(staging);
    fixture.storage.listObjects.mockResolvedValue({ items:[{objectKey:'objects/direct',size:10,
      lastModified:new Date(0)}],nextCursor:undefined });
    await expect(fixture.download()).rejects.toThrow();
    expect(fixture.storage.deleteObjects).not.toHaveBeenCalled();
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
  });

  it('refuses a repeated listing cursor instead of looping or deleting a partial inventory', async () => {
    const fixture=setup(staging);
    fixture.storage.listObjects.mockResolvedValue({items:[],nextCursor:'repeated-cursor'});
    await expect(fixture.download()).rejects.toThrow();
    expect(fixture.storage.listObjects.mock.calls.length).toBeLessThanOrEqual(3);
    expect(fixture.storage.deleteObjects).not.toHaveBeenCalled();
  });

  it('returns a short processing response and shares one generation across simultaneous polls', async () => {
    const fixture = setup(staging);
    let entered!: () => void, release!: () => void;
    const started = new Promise<void>(resolve => { entered = resolve; });
    const held = new Promise<void>(resolve => { release = resolve; });
    const generate = (fixture.service as unknown as { createTarZstd: (dir: string, output: string) => Promise<void> }).createTarZstd.bind(fixture.service);
    const tar = jest.spyOn(fixture.service as never, 'createTarZstd' as never).mockImplementation((async (dir: string, output: string) => {
      entered(); await held; await generate(dir, output);
    }) as never);
    const results = await Promise.all([fixture.prepare(), fixture.prepare(), fixture.prepare()]);
    expect(results).toEqual(Array(3).fill(expect.objectContaining({ state: 'processing', retryAfterMs: 2000,
      taskId: expect.stringMatching(/^[a-f0-9]{64}$/) })));
    expect(new Set(results.map(result => result.taskId)).size).toBe(1);
    await started;
    expect(tar).toHaveBeenCalledTimes(1);
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
    expect(await fixture.prepare()).toMatchObject({ state: 'processing', retryAfterMs: 2000 });
    release();
    let ready: Awaited<ReturnType<typeof fixture.prepare>> | undefined;
    for (let poll = 0; poll < 200; poll++) {
      await new Promise(resolve => setTimeout(resolve, 5));
      ready = await fixture.prepare();
      if (ready.state === 'ready') break;
    }
    expect(ready).toMatchObject({ state: 'ready', url: expect.stringContaining('/objects/'), fileName: expect.stringMatching(/\.tar\.zst$/) });
    expect(tar).toHaveBeenCalledTimes(1);
    expect(fixture.storage.putObject).toHaveBeenCalledTimes(1);
    expect(await readdir(staging)).toEqual([]);
  });

  it('reports a background failure on every poll until an explicit retry and releases the slot', async () => {
    const fixture = setup(staging);
    fixture.storage.getObject.mockRejectedValueOnce(new Error('private storage failure details'));
    expect(await fixture.prepare()).toMatchObject({ state: 'processing', retryAfterMs: 2000 });
    await new Promise(resolve => setTimeout(resolve, 25));
    for (let poll = 0; poll < 2; poll++) {
      await expect(fixture.prepare()).rejects.toMatchObject({ status: 503,
        response: { code: 'FOLDER_PACKAGE_FAILED', message: expect.stringContaining('原始文件已保留') } });
    }
    expect(fixture.storage.getObject).toHaveBeenCalledTimes(1);
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
    expect(await readdir(staging)).toEqual([]);
    expect(await fixture.prepare('A', actor, true)).toMatchObject({ state: 'processing', retryAfterMs: 2000 });
    let ready: Awaited<ReturnType<typeof fixture.prepare>> | undefined;
    for (let poll = 0; poll < 200; poll++) {
      await new Promise(resolve => setTimeout(resolve, 5));
      ready = await fixture.prepare();
      if (ready.state === 'ready') break;
    }
    expect(ready).toMatchObject({ state: 'ready' });
  });

  it('reauthorizes processing polls rather than treating knowledge of a folder as a task token', async () => {
    const fixture = setup(staging);
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    const read = fixture.storage.getObject.getMockImplementation()!;
    fixture.storage.getObject.mockImplementation(async key => { await held; return read(key); });
    expect(await fixture.prepare()).toMatchObject({ state: 'processing' });
    await expect(fixture.prepare('A', { ...actor, tenantId: 'other' })).rejects.toMatchObject({ status: 404 });
    release();
    for (let poll = 0; poll < 200; poll++) {
      await new Promise(resolve => setTimeout(resolve, 5));
      if ((await fixture.prepare()).state === 'ready') return;
    }
    throw new Error('fixture background package did not finish');
  });

  it('keeps polling its original snapshot while later uploads are reserved for the next click', async () => {
    const fixture = setup(staging);
    let entered!: () => void, release!: () => void;
    const started = new Promise<void>(resolve => { entered = resolve; });
    const held = new Promise<void>(resolve => { release = resolve; });
    const generate = (fixture.service as unknown as { createTarZstd: (dir: string, output: string) => Promise<void> }).createTarZstd.bind(fixture.service);
    jest.spyOn(fixture.service as never, 'createTarZstd' as never).mockImplementationOnce((async (dir: string, output: string) => {
      entered(); await held; await generate(dir, output);
    }) as never);
    const first = await fixture.prepare('A', actor, true);
    await started;
    fixture.state.files.push(file('later', 'A', 'later.json', 'later capture upload'));
    expect(await fixture.prepare('A', actor, false, first.taskId)).toMatchObject({ state: 'processing', taskId: first.taskId });
    release();
    let ready: Awaited<ReturnType<typeof fixture.prepare>> | undefined;
    for (let poll = 0; poll < 200; poll++) {
      await new Promise(resolve => setTimeout(resolve, 5));
      ready = await fixture.prepare('A', actor, false, first.taskId);
      if (ready.state === 'ready') break;
    }
    expect(ready).toMatchObject({ state: 'ready', taskId: first.taskId });
    const packed = await unpack(work, fixture.cache);
    expect(packed.members).not.toContain('./files/A/later.json');
    expect(fixture.storage.putObject).toHaveBeenCalledTimes(1);
    // The next explicit click sees the new upload and chooses a new identity.
    const next = await fixture.prepare('A', actor, true);
    expect(next.taskId).not.toBe(first.taskId);
    for (let poll = 0; poll < 200; poll++) {
      await new Promise(resolve => setTimeout(resolve, 5));
      if ((await fixture.prepare('A', actor, false, next.taskId)).state === 'ready') return;
    }
    throw new Error('fixture next background snapshot did not finish');
  });

  it('binds a task to its dataset path and returns a clear retry error after server state expires', async () => {
    const fixture = setup(staging);
    await fixture.download();
    const first = await fixture.prepare('A', actor, true);
    expect(first.state).toBe('ready');
    await expect(fixture.prepare('AB', actor, false, first.taskId)).rejects.toMatchObject({ status: 409,
      response: { code: 'FOLDER_PACKAGE_EXPIRED' } });
    await expect(fixture.prepare('A', actor, false, '0'.repeat(64))).rejects.toMatchObject({ status: 409,
      response: { code: 'FOLDER_PACKAGE_EXPIRED' } });
    fixture.state.files[0].originalName = 'moved-name.mkv';
    await expect(fixture.prepare('A', actor, false, first.taskId)).rejects.toMatchObject({ status: 409 });
  });
  it('assigns a distinct task after a dataset rename even when the source files are unchanged', async () => {
    const fixture = setup(staging);
    await fixture.download();
    const first = await fixture.prepare('A', actor, true);
    fixture.state.dataset!.name = '重命名的数据集';
    const next = await fixture.prepare('A', actor, true);
    expect(next.taskId).not.toBe(first.taskId);
    await expect(fixture.prepare('A', actor, false, first.taskId)).rejects.toMatchObject({ status: 409 });
    for (let poll = 0; poll < 200; poll++) {
      await new Promise(resolve => setTimeout(resolve, 5));
      if ((await fixture.prepare('A', actor, false, next.taskId)).state === 'ready') return;
    }
    throw new Error('fixture renamed dataset snapshot did not finish');
  });

  it('polls processing without inventories while rechecking authorization, deletion, name and directory', async () => {
    const fixture = setup(staging);
    let entered!: () => void, release!: () => void;
    const started = new Promise<void>(resolve => { entered = resolve; });
    const held = new Promise<void>(resolve => { release = resolve; });
    const read = fixture.storage.getObject.getMockImplementation()!;
    fixture.storage.getObject.mockImplementationOnce(async key => { entered(); await held; return read(key); });
    const first = await fixture.prepare();
    await started;
    fixture.prisma.robotFile.findMany.mockClear(); fixture.prisma.datasetFolder.findMany.mockClear();
    try {
      for (let poll = 0; poll < 3; poll++)
        expect(await fixture.prepare('A', actor, false, first.taskId)).toMatchObject({ state: 'processing', taskId: first.taskId });
      await expect(fixture.prepare('A', { ...actor, tenantId: 'other' }, false, first.taskId)).rejects.toMatchObject({ status: 404 });
      await expect(fixture.prepare('A', { ...actor, projectIds: ['other'] }, false, first.taskId)).rejects.toMatchObject({ status: 404 });
      fixture.state.dataset!.deletingAt = new Date() as never;
      await expect(fixture.prepare('A', actor, false, first.taskId)).rejects.toMatchObject({ status: 404 });
      fixture.state.dataset!.deletingAt = null;
      fixture.state.dataset!.name = 'renamed';
      await expect(fixture.prepare('A', actor, false, first.taskId)).rejects.toMatchObject({ status: 409 });
      fixture.state.dataset!.name = dataset.name;
      const folders = fixture.state.folders;
      fixture.state.folders = folders.filter(folder => folder.path !== 'A');
      await expect(fixture.prepare('A', actor, false, first.taskId)).rejects.toMatchObject({ status: 404 });
      fixture.state.folders = folders;
      expect(fixture.prisma.robotFile.findMany).not.toHaveBeenCalled();
      expect(fixture.prisma.datasetFolder.findMany).not.toHaveBeenCalled();
    } finally {
      fixture.state.dataset = { ...dataset };
      release(); await waitForTask(fixture.service, first.taskId!);
    }
    fixture.prisma.robotFile.findMany.mockClear();
    expect(await fixture.prepare('A', actor, false, first.taskId)).toMatchObject({ state: 'ready' });
    expect(fixture.prisma.robotFile.findMany).toHaveBeenCalled();
  });

  it('still rejects a changed source at publication after a lightweight processing poll', async () => {
    const fixture = setup(staging);
    let entered!: () => void, release!: () => void;
    const started = new Promise<void>(resolve => { entered = resolve; });
    const held = new Promise<void>(resolve => { release = resolve; });
    const read = fixture.storage.getObject.getMockImplementation()!;
    fixture.storage.getObject.mockImplementationOnce(async key => { entered(); await held; return read(key); });
    const first = await fixture.prepare();
    await started;
    fixture.state.files[0].originalName = 'changed-while-reading.mkv';
    fixture.prisma.robotFile.findMany.mockClear();
    expect(await fixture.prepare('A', actor, false, first.taskId)).toMatchObject({ state: 'processing' });
    expect(fixture.prisma.robotFile.findMany).not.toHaveBeenCalled();
    release();
    expect((await waitForTask(fixture.service, first.taskId!)).state).toBe('failed');
    fixture.prisma.robotFile.findMany.mockClear();
    await expect(fixture.prepare('A', actor, false, first.taskId)).rejects.toMatchObject({ status: 409 });
    expect(fixture.prisma.robotFile.findMany).not.toHaveBeenCalled();
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
    expect(await readdir(staging)).toEqual([]);
  });

  it('recovers an exact ready cache after task memory is lost without reading or regenerating sources', async () => {
    const fixture = setup(staging);
    await fixture.download();
    const first = await fixture.prepare();
    tasksOf(fixture.service).clear();
    const tar = jest.spyOn(fixture.service as never, 'createTarZstd' as never);
    fixture.storage.getObject.mockClear(); fixture.storage.putObject.mockClear();
    fixture.prisma.robotFile.findMany.mockClear();
    expect(await fixture.prepare('A', actor, false, first.taskId)).toMatchObject({ state: 'ready', taskId: first.taskId });
    expect(fixture.prisma.robotFile.findMany).toHaveBeenCalled();
    expect(tasksOf(fixture.service).size).toBe(1);
    expect(fixture.storage.getObject).not.toHaveBeenCalled(); expect(fixture.storage.putObject).not.toHaveBeenCalled();
    expect(tar).not.toHaveBeenCalled();
    fixture.state.files[0].originalName = 'changed-after-recovery.mkv';
    await expect(fixture.prepare('A', actor, false, first.taskId)).rejects.toMatchObject({ status: 409 });
  });

  it.each(['changed', 'missing', 'unauthorized'] as const)('does not generate during %s cache recovery', async reason => {
    const fixture = setup(staging);
    await fixture.download();
    const first = await fixture.prepare();
    tasksOf(fixture.service).clear();
    const tar = jest.spyOn(fixture.service as never, 'createTarZstd' as never);
    fixture.storage.getObject.mockClear(); fixture.storage.putObject.mockClear(); fixture.storage.getPresignedDownloadUrl.mockClear();
    if (reason === 'changed') fixture.state.files.push(file('late', 'A', 'late.mkv', 'late source bytes'));
    if (reason === 'missing') fixture.cache.clear();
    await expect(fixture.prepare('A', reason === 'unauthorized' ? { ...actor, tenantId: 'other' } : actor, false, first.taskId))
      .rejects.toMatchObject({ status: reason === 'unauthorized' ? 404 : 409 });
    expect(tar).not.toHaveBeenCalled(); expect(tasksOf(fixture.service).size).toBe(0);
    expect(fixture.storage.getObject).not.toHaveBeenCalled(); expect(fixture.storage.putObject).not.toHaveBeenCalled();
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
  });

  it('gives folder source opening a 60-second idle budget without buffering the file', async () => {
    jest.useFakeTimers({ doNotFake: ['nextTick', 'queueMicrotask', 'setImmediate'] });
    const fixture = setup(staging);
    const read = fixture.storage.getObject.getMockImplementation()!;
    let entered!: () => void;
    const started = new Promise<void>(resolve => { entered = resolve; });
    fixture.storage.getObject.mockImplementationOnce(async key => {
      entered(); await new Promise(resolve => setTimeout(resolve, 20_000)); return read(key);
    });
    try {
      const pending = fixture.download();
      await started; await jest.advanceTimersByTimeAsync(20_000);
      await expect(pending).resolves.toMatchObject({ url: expect.stringContaining('/objects/') });
      expect(await readdir(staging)).toEqual([]);
    } finally { jest.useRealTimers(); }
  });

  it('uses lossless level 1 only for folder packages and restores every source checksum', async () => {
    const fixture = setup(staging);
    const spawn = jest.spyOn(childProcess, 'spawn');
    await fixture.download();
    const zstd = spawn.mock.calls.filter(([command]) => command === 'zstd');
    expect(zstd).toHaveLength(1);
    expect(zstd[0][1]).toEqual(['-T1', '-1', '-q', '-o', expect.any(String)]);
    const packed = await unpack(work, fixture.cache);
    for (const row of fixture.state.files.filter(row => row.folderPath === 'A' || row.folderPath.startsWith('A/'))) {
      const suffix = [row.folderPath.slice(1), row.originalName].filter(Boolean).join('/').replace(/^\//, '');
      expect(createHash('sha256').update(await packed.bytes(`./files/A/${suffix}`)).digest('hex')).toBe(row.checksum);
    }
  });

  it('queues different background folders and runs only one real compression at a time', async () => {
    const fixture = setup(staging);
    const firstStarted = deferred(), firstRelease = deferred();
    const order: string[] = [];
    let active = 0, maximumActive = 0;
    const generate = (fixture.service as unknown as {
      createTarZstd: (dir: string, output: string, level: 1 | 3) => Promise<void>;
    }).createTarZstd.bind(fixture.service);
    jest.spyOn(fixture.service as never, 'createTarZstd' as never).mockImplementation((async (dir: string, output: string, level: 1 | 3) => {
      const manifest = JSON.parse(await fs.readFile(join(dir, 'manifest.json'), 'utf8'));
      order.push(manifest.path); maximumActive = Math.max(maximumActive, ++active);
      try {
        if (manifest.path === 'A') { firstStarted.resolve(); await firstRelease.promise; }
        await generate(dir, output, level);
      } finally { active--; }
    }) as never);
    const first = await fixture.prepare('A');
    await firstStarted.promise;
    const second = await fixture.prepare('AB'), third = await fixture.prepare('empty');
    try {
      await waitForQueue(fixture.service, 2);
      expect(second).toMatchObject({ state: 'processing' }); expect(third).toMatchObject({ state: 'processing' });
      expect(await fixture.prepare('AB', actor, false, second.taskId)).toMatchObject({ state: 'processing', taskId: second.taskId });
      expect(order).toEqual(['A']); expect(fixture.storage.getObject).not.toHaveBeenCalledWith('objects/neighbor');
    } finally { firstRelease.resolve(); }
    expect((await waitForTask(fixture.service, first.taskId!)).state).toBe('ready');
    expect((await waitForTask(fixture.service, second.taskId!)).state).toBe('ready');
    expect((await waitForTask(fixture.service, third.taskId!)).state).toBe('ready');
    expect(order).toEqual(['A', 'AB', 'empty']); expect(maximumActive).toBe(1);
    expect(fixture.storage.putObject).toHaveBeenCalledTimes(3); expect(await readdir(staging)).toEqual([]);
  });

  it('releases a failed background folder to the next queued folder while preserving sources', async () => {
    const fixture = setup(staging);
    const firstStarted = deferred(), firstRelease = deferred();
    const generate = (fixture.service as unknown as {
      createTarZstd: (dir: string, output: string, level: 1 | 3) => Promise<void>;
    }).createTarZstd.bind(fixture.service);
    const tar = jest.spyOn(fixture.service as never, 'createTarZstd' as never).mockImplementationOnce((async () => {
      firstStarted.resolve(); await firstRelease.promise; throw new Error('fixture compression failure');
    }) as never).mockImplementation((async (dir: string, output: string, level: 1 | 3) => generate(dir, output, level)) as never);
    const first = await fixture.prepare('A'); await firstStarted.promise;
    const next = await fixture.prepare('AB');
    try { await waitForQueue(fixture.service, 1); } finally { firstRelease.resolve(); }
    expect((await waitForTask(fixture.service, first.taskId!)).state).toBe('failed');
    expect((await waitForTask(fixture.service, next.taskId!)).state).toBe('ready');
    await expect(fixture.prepare('A', actor, false, first.taskId)).rejects.toMatchObject({ status: 503,
      response: { code: 'FOLDER_PACKAGE_FAILED' } });
    expect(tar).toHaveBeenCalledTimes(2); expect(fixture.state.files).toHaveLength(5);
    expect(fixture.storage.putObject).toHaveBeenCalledTimes(1); expect(await readdir(staging)).toEqual([]);
  });

  it('revalidates a queued folder before reading its sources after a move while waiting', async () => {
    const fixture = setup(staging);
    const firstStarted = deferred(), firstRelease = deferred();
    const generate = (fixture.service as unknown as {
      createTarZstd: (dir: string, output: string, level: 1 | 3) => Promise<void>;
    }).createTarZstd.bind(fixture.service);
    const tar = jest.spyOn(fixture.service as never, 'createTarZstd' as never).mockImplementationOnce((async (dir: string, output: string, level: 1 | 3) => {
      firstStarted.resolve(); await firstRelease.promise; await generate(dir, output, level);
    }) as never);
    const first = await fixture.prepare('A'); await firstStarted.promise;
    const next = await fixture.prepare('AB');
    try {
      await waitForQueue(fixture.service, 1);
      fixture.state.files.find(row => row.id === 'neighbor')!.originalName = 'moved.mkv';
    } finally { firstRelease.resolve(); }
    expect((await waitForTask(fixture.service, first.taskId!)).state).toBe('ready');
    expect((await waitForTask(fixture.service, next.taskId!)).state).toBe('failed');
    await expect(fixture.prepare('AB', actor, false, next.taskId)).rejects.toMatchObject({ status: 409 });
    expect(fixture.storage.getObject).not.toHaveBeenCalledWith('objects/neighbor');
    expect(tar).toHaveBeenCalledTimes(1); expect(await readdir(staging)).toEqual([]);
  });

  it('downloads an overnight durable snapshot after task memory is lost and later files arrive', async () => {
    const fixture = setup(staging);
    const saved = await fixture.service.capturePreparedFolderSnapshot(dataset.id, 'A', actor);
    const control = { signal: new AbortController().signal, check: jest.fn(async () => undefined) };
    await fixture.service.runPreparedFolderPackage(dataset.id, 'A', actor, saved as never, control);
    Object.assign(fixture.prisma, { preparedFolderPackage: { findFirst: jest.fn(async () => ({
      ...saved, snapshot: saved, state: 'READY', path: 'A', datasetId: dataset.id,
    })), findMany: jest.fn(async () => [{ objectKey: saved.objectKey }]) } });
    tasksOf(fixture.service).clear();
    fixture.state.files.push(file('overnight', 'A', 'evening.mkv', 'new evening capture'));
    fixture.storage.getObject.mockClear(); fixture.storage.putObject.mockClear();
    const result = await fixture.service.folderPackage(dataset.id, 'A', actor, { background: true, taskId: saved.taskId });
    expect(result).toMatchObject({ state: 'ready', taskId: saved.taskId, fileName: saved.fileName });
    await expect(fixture.service.validatePreparedFolderPackage(dataset.id, 'A', actor, saved as never)).resolves.toBeUndefined();
    expect(fixture.storage.getObject).not.toHaveBeenCalled(); expect(fixture.storage.putObject).not.toHaveBeenCalled();
    const packed = await unpack(work, fixture.cache);
    expect(packed.manifest.snapshotAt).toBe(saved.at);
    expect(packed.members).not.toContain('./files/A/evening.mkv');
    expect(control.check).toHaveBeenCalledWith(true); expect(control.check).toHaveBeenCalledWith(false);
  });

  it.each(['file_deleted', 'file_renamed', 'file_metadata', 'folder_deleted', 'dataset_renamed', 'scope_revoked'] as const)(
    'rejects a durable ready snapshot after %s without regenerating it', async change => {
      const fixture = setup(staging);
      const saved = await fixture.service.capturePreparedFolderSnapshot(dataset.id, 'A', actor);
      await fixture.service.runPreparedFolderPackage(dataset.id, 'A', actor, saved as never,
        { signal: new AbortController().signal, check: async () => undefined });
      Object.assign(fixture.prisma, { preparedFolderPackage: { findFirst: jest.fn(async () => ({ ...saved, snapshot: saved })) } });
      if (change === 'file_deleted') fixture.state.files = fixture.state.files.filter(row => row.id !== 'direct');
      if (change === 'file_renamed') fixture.state.files[0].originalName = 'changed.mkv';
      if (change === 'file_metadata') fixture.state.files[0].metadata = { altered: true };
      if (change === 'folder_deleted') fixture.state.folders = fixture.state.folders.filter(row => row.path !== 'A/slot');
      if (change === 'dataset_renamed') fixture.state.dataset!.name = 'renamed';
      const viewer = change === 'scope_revoked' ? { ...actor, projectIds: [] } : actor;
      fixture.storage.getObject.mockClear(); fixture.storage.putObject.mockClear();
      await expect(fixture.service.folderPackage(dataset.id, 'A', viewer, { background: true, taskId: saved.taskId }))
        .rejects.toMatchObject({ status: change === 'scope_revoked' ? 404 : 409 });
      expect(fixture.storage.getObject).not.toHaveBeenCalled(); expect(fixture.storage.putObject).not.toHaveBeenCalled();
    });

  it('pins the durable ready cache beyond predecessor retention while newer snapshots are built', async () => {
    const fixture = setup(staging);
    const saved = await fixture.service.capturePreparedFolderSnapshot(dataset.id, 'A', actor);
    await fixture.service.runPreparedFolderPackage(dataset.id, 'A', actor, saved as never,
      { signal: new AbortController().signal, check: async () => undefined });
    fixture.modified.set(saved.objectKey, new Date(Date.now() - 24 * 3600_000));
    Object.assign(fixture.prisma, { preparedFolderPackage: { findMany: jest.fn(async () => [{ objectKey: saved.objectKey }]) } });
    for (const id of ['later-one', 'later-two']) {
      fixture.state.files.push(file(id, 'A', id + '.mkv', id));
      await fixture.download();
      expect(fixture.cache.has(saved.objectKey)).toBe(true);
    }
    expect(fixture.cache.size).toBe(3); expect(fixture.state.files).toHaveLength(7);
  });

  it('validates only saved members when later uploads exceed the new-package inventory limit', async () => {
    const fixture = setup(staging);
    const saved = await fixture.service.capturePreparedFolderSnapshot(dataset.id, 'A', actor);
    await fixture.service.runPreparedFolderPackage(dataset.id, 'A', actor, saved as never,
      { signal: new AbortController().signal, check: async () => undefined });
    fixture.state.files.push(...Array.from({ length: 10001 }, (_, index) => ({ ...fixture.state.files[0],
      id: `later-${index}`, objectKey: `objects/later-${index}`, originalName: `later-${index}.mkv` })));
    fixture.storage.getObject.mockClear();
    await expect(fixture.service.validatePreparedFolderPackage(dataset.id, 'A', actor, saved as never)).resolves.toBeUndefined();
    expect(fixture.storage.getObject).not.toHaveBeenCalled();
    expect(fixture.prisma.robotFile.findMany).toHaveBeenLastCalledWith(expect.objectContaining({
      where: { datasetId: dataset.id, id: { in: saved.files.map(item => item.id) } },
    }));
    await expect(fixture.service.capturePreparedFolderSnapshot(dataset.id, 'A', actor)).rejects.toThrow('上限');
  });

  it('rejects a tampered durable identity before accessing any source object', async () => {
    const fixture = setup(staging);
    const saved = await fixture.service.capturePreparedFolderSnapshot(dataset.id, 'A', actor);
    const altered = { ...saved, objectKey: 'unrelated/cache.tar.zst' };
    await expect(fixture.service.runPreparedFolderPackage(dataset.id, 'A', actor, altered as never,
      { signal: new AbortController().signal, check: async () => undefined })).rejects.toMatchObject({ status: 409 });
    expect(fixture.storage.getObject).not.toHaveBeenCalled(); expect(fixture.storage.putObject).not.toHaveBeenCalled();
  });
});

function deferred() {
  let resolve!: () => void;
  return { promise: new Promise<void>(done => { resolve = done; }), resolve: () => resolve() };
}

type PackageSlot = {
  withPackageSlot: (operation: () => Promise<void>, wait?: boolean, signal?: AbortSignal) => Promise<void>;
  packageWaiters: Array<{ expiresAt: number }>;
  packaging: boolean;
};

async function waitForQueue(service: ArchiveRecordingsService, size: number) {
  for (let poll = 0; poll < 200; poll++) {
    if ((service as unknown as PackageSlot).packageWaiters.length === size) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw new Error('Package queue did not reach expected size');
}

describe('background package FIFO slot', () => {
  const timeout = 30 * 60_000;
  const slot = () => new ArchiveRecordingsService({} as never, {} as never, {} as never,
    new ConfigService()) as unknown as PackageSlot;
  afterEach(() => { jest.restoreAllMocks(); jest.useRealTimers(); });

  it('reserves each handoff fairly and keeps synchronous legacy requests busy', async () => {
    const service = slot(), firstRelease = deferred(), secondRelease = deferred();
    const order: string[] = [];
    const first = service.withPackageSlot(async () => { order.push('first'); await firstRelease.promise; });
    const second = service.withPackageSlot(async () => { order.push('second'); await secondRelease.promise; }, true);
    const third = service.withPackageSlot(async () => { order.push('third'); }, true);
    const legacy = jest.fn(async () => undefined);
    await expect(service.withPackageSlot(legacy)).rejects.toMatchObject({ status: 503 });
    firstRelease.resolve(); await first;
    await expect(service.withPackageSlot(legacy)).rejects.toMatchObject({ status: 503 });
    const fourth = service.withPackageSlot(async () => { order.push('fourth'); }, true);
    expect(order).toEqual(['first', 'second']); expect(legacy).not.toHaveBeenCalled();
    secondRelease.resolve(); await Promise.all([second, third, fourth]);
    expect(order).toEqual(['first', 'second', 'third', 'fourth']);
    expect(service.packaging).toBe(false); expect(service.packageWaiters).toEqual([]);
  });

  it('releases the slot when the active owner and a queued owner both fail', async () => {
    const service = slot(), firstRelease = deferred();
    const first = service.withPackageSlot(async () => { await firstRelease.promise; throw new Error('first failure'); });
    const firstRejected = expect(first).rejects.toThrow('first failure');
    const second = service.withPackageSlot(async () => { throw new Error('second failure'); }, true);
    const secondRejected = expect(second).rejects.toThrow('second failure');
    const next = jest.fn(async () => undefined);
    const third = service.withPackageSlot(next, true);
    firstRelease.resolve(); await Promise.all([firstRejected, secondRejected, third]);
    expect(next).toHaveBeenCalledTimes(1); expect(service.packaging).toBe(false);
    expect(service.packageWaiters).toEqual([]);
  });

  it('removes an expired waiter without releasing the active owner or skipping a later waiter', async () => {
    jest.useFakeTimers();
    const service = slot(), firstRelease = deferred(), expiredWork = jest.fn(async () => undefined);
    const first = service.withPackageSlot(async () => { await firstRelease.promise; });
    const expired = service.withPackageSlot(expiredWork, true);
    const expiredRejected = expect(expired).rejects.toMatchObject({ status: 503,
      response: { code: 'FOLDER_PACKAGE_QUEUE_TIMEOUT' } });
    await jest.advanceTimersByTimeAsync(timeout / 2);
    const nextWork = jest.fn(async () => undefined);
    const next = service.withPackageSlot(nextWork, true);
    await jest.advanceTimersByTimeAsync(timeout / 2); await expiredRejected;
    expect(service.packaging).toBe(true); expect(service.packageWaiters).toHaveLength(1);
    await expect(service.withPackageSlot(async () => undefined)).rejects.toMatchObject({ status: 503 });
    expect(expiredWork).not.toHaveBeenCalled(); expect(nextWork).not.toHaveBeenCalled();
    firstRelease.resolve(); await Promise.all([first, next]);
    expect(nextWork).toHaveBeenCalledTimes(1); expect(service.packaging).toBe(false);
    expect(service.packageWaiters).toEqual([]); expect(jest.getTimerCount()).toBe(0);
  });

  it('also checks an expired deadline at handoff when its timer callback was delayed', async () => {
    jest.useFakeTimers();
    const service = slot(), firstRelease = deferred(), expiredWork = jest.fn(async () => undefined);
    const first = service.withPackageSlot(async () => { await firstRelease.promise; });
    const expired = service.withPackageSlot(expiredWork, true);
    const expiredRejected = expect(expired).rejects.toMatchObject({ response: { code: 'FOLDER_PACKAGE_QUEUE_TIMEOUT' } });
    jest.setSystemTime(Date.now() + timeout + 1);
    const nextWork = jest.fn(async () => undefined);
    const next = service.withPackageSlot(nextWork, true);
    firstRelease.resolve(); await Promise.all([first, expiredRejected, next]);
    expect(expiredWork).not.toHaveBeenCalled(); expect(nextWork).toHaveBeenCalledTimes(1);
    expect(service.packaging).toBe(false); expect(service.packageWaiters).toEqual([]); expect(jest.getTimerCount()).toBe(0);
  });

  it('bounds the queue to eight waiting jobs without disrupting accepted FIFO jobs', async () => {
    jest.useFakeTimers();
    const service = slot(), firstRelease = deferred(), order: number[] = [];
    const first = service.withPackageSlot(async () => { await firstRelease.promise; });
    const accepted = Array.from({ length: 8 }, (_, index) => service.withPackageSlot(async () => { order.push(index); }, true));
    const refused = jest.fn(async () => undefined);
    await expect(service.withPackageSlot(refused, true)).rejects.toMatchObject({ status: 503,
      response: { code: 'FOLDER_PACKAGE_QUEUE_FULL' } });
    expect(service.packageWaiters).toHaveLength(8); expect(refused).not.toHaveBeenCalled();
    firstRelease.resolve(); await Promise.all([first, ...accepted]);
    expect(order).toEqual([0, 1, 2, 3, 4, 5, 6, 7]); expect(service.packaging).toBe(false);
    expect(service.packageWaiters).toEqual([]); expect(jest.getTimerCount()).toBe(0);
  });

  it('removes an aborted queued durable job without disturbing the active or later owner', async () => {
    jest.useFakeTimers();
    const service = slot(), firstRelease = deferred(), controller = new AbortController();
    const first = service.withPackageSlot(async () => firstRelease.promise);
    const skipped = jest.fn(async () => undefined);
    const queued = service.withPackageSlot(skipped, true, controller.signal);
    const rejected = expect(queued).rejects.toThrow('cancelled');
    const nextWork = jest.fn(async () => undefined);
    const next = service.withPackageSlot(nextWork, true);
    controller.abort(); await rejected;
    expect(service.packaging).toBe(true); expect(service.packageWaiters).toHaveLength(1);
    firstRelease.resolve(); await Promise.all([first, next]);
    expect(skipped).not.toHaveBeenCalled(); expect(nextWork).toHaveBeenCalledTimes(1);
    expect(service.packaging).toBe(false); expect(jest.getTimerCount()).toBe(0);
  });
});

function processFixture() {
  const process = Object.assign(new EventEmitter(), { stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    kill: jest.fn<boolean, [string]>(() => true) });
  let closed = false;
  process.kill.mockImplementation(signal => {
    if (!closed) { closed = true; process.stdout.destroy(); process.stdin.destroy(); process.emit('close', null, signal); }
    return true;
  });
  return process;
}

describe('package compression progress deadlines', () => {
  afterEach(() => { jest.restoreAllMocks(); jest.useRealTimers(); });

  function compression() {
    jest.useFakeTimers({ doNotFake: ['nextTick', 'queueMicrotask', 'setImmediate'] });
    const tar = processFixture(), zstd = processFixture();
    jest.spyOn(childProcess, 'spawn').mockReturnValueOnce(tar as never).mockReturnValueOnce(zstd as never);
    let outputBytes = 0;
    jest.spyOn(fs, 'stat').mockImplementation(async () => ({ size: outputBytes } as never));
    const service = new ArchiveRecordingsService({} as never, {} as never, {} as never, new ConfigService());
    const controller = new AbortController();
    const pending = (service as unknown as { createTarZstd: (dir: string, output: string, level: 1 | 3, signal: AbortSignal) => Promise<void> })
      .createTarZstd('/unused-input', '/unused-output', 3, controller.signal);
    return { tar, zstd, pending, controller, grow: () => { outputBytes += 100; } };
  }

  it('allows real input progress to continue beyond 120 seconds', async () => {
    const { tar, zstd, pending } = compression();
    await jest.advanceTimersByTimeAsync(119_000);
    tar.stdout.write(Buffer.from('progress'));
    await jest.advanceTimersByTimeAsync(119_000);
    expect(tar.kill).not.toHaveBeenCalled(); expect(zstd.kill).not.toHaveBeenCalled();
    tar.stdout.end(); tar.emit('close', 0, null); zstd.emit('close', 0, null);
    await pending;
    expect(jest.getTimerCount()).toBe(0);
  });

  it('counts output growth while the compressor finishes after input is exhausted', async () => {
    const { tar, zstd, pending, grow } = compression();
    tar.stdout.end(Buffer.from('input')); tar.emit('close', 0, null);
    await jest.advanceTimersByTimeAsync(100_000);
    grow(); await jest.advanceTimersByTimeAsync(102_000);
    expect(zstd.kill).not.toHaveBeenCalled();
    zstd.emit('close', 0, null); await pending;
    expect(jest.getTimerCount()).toBe(0);
  });

  it('terminates and reaps children after 120 seconds without any progress', async () => {
    const { tar, zstd, pending } = compression();
    const rejection = expect(pending).rejects.toThrow('压缩包生成超时');
    await jest.advanceTimersByTimeAsync(120_000); await rejection;
    expect(tar.kill).toHaveBeenCalledWith('SIGTERM'); expect(zstd.kill).toHaveBeenCalledWith('SIGTERM');
    expect(jest.getTimerCount()).toBe(0);
  });

  it('keeps a bounded total lifetime even when input continues progressing', async () => {
    const { tar, zstd, pending } = compression();
    const rejection = expect(pending).rejects.toThrow('压缩包生成超过总时限');
    const activity = setInterval(() => { if (!tar.stdout.destroyed) tar.stdout.write(Buffer.from('progress')); }, 60_000);
    await jest.advanceTimersByTimeAsync(30 * 60_000); clearInterval(activity); await rejection;
    expect(tar.kill).toHaveBeenCalledWith('SIGTERM'); expect(zstd.kill).toHaveBeenCalledWith('SIGTERM');
    expect(jest.getTimerCount()).toBe(0);
  });

  it('terminates and reaps both compression children when the durable owner loses its lease', async () => {
    const { tar, zstd, pending, controller } = compression();
    const rejection = expect(pending).rejects.toThrow('cancelled');
    controller.abort(); await rejection;
    expect(tar.kill).toHaveBeenCalledWith('SIGTERM'); expect(zstd.kill).toHaveBeenCalledWith('SIGTERM');
    expect(jest.getTimerCount()).toBe(0);
  });
});


describe('whole capture-session uploaded snapshots', () => {
  const robotA = '11111111-1111-4111-8111-111111111111';
  const robotB = '22222222-2222-4222-8222-222222222222';
  const sessionA = 'a'.repeat(32), sessionB = 'b'.repeat(32);
  let work: string;
  beforeEach(async () => { work = await mkdtemp(join(tmpdir(), 'capture-session-test-')); });
  afterEach(async () => { jest.restoreAllMocks(); await rm(work, { recursive: true, force: true }); });
  const prepareFixture = (base: string) => {
    const fixture = setup(base);
    const member = (id: string, path: string, name: string, token = sessionA, robotId = robotA) => ({
      ...file(id, path, name, `bytes of ${id}`), robotId, metadata: { captureSessionId: token, closeReason: 'rotation' },
    });
    fixture.state.files = [member('camera', '前相机/slot', 'video.mkv'),
      member('lidar', 'MID360/slot', 'scan.db3.zst'), member('yaml', 'MID360/slot', 'metadata.yaml'),
      member('next', '前相机/next', 'video.mkv', sessionB),
      member('foreign', '前相机/foreign', 'video.mkv', sessionA, robotB)];
    return fixture;
  };
  it('creates an actual archive across camera/lidar/YAML only for the exact selected robot/session', async () => {
    const fixture = prepareFixture(work);
    const result = await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true });
    expect(result).toMatchObject({ state: 'processing' });
    const taskId = 'taskId' in result ? result.taskId! : '';
    expect((await waitForTask(fixture.service, taskId)).state).toBe('ready');
    const ready = await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true, taskId });
    expect(ready).toMatchObject({ state: 'ready', taskId });
    expect(fixture.storage.getObject).toHaveBeenCalledTimes(3);
    const archive = await unpack(work, fixture.cache);
    expect(archive.manifest).toMatchObject({ type: 'capture-session-download', robotId: robotA,
      captureSessionId: sessionA, uploadedSnapshot: true, fileCount: 3 });
    expect(archive.manifest.files.map((item: {id: string}) => item.id).sort()).toEqual(['camera', 'lidar', 'yaml']);
    const root = `采集_${sessionA.slice(0, 12)}_${robotA.slice(0, 8)}`;
    expect(archive.members).toEqual(expect.arrayContaining([
      `./files/${root}/前相机/slot/video.mkv`, `./files/${root}/MID360/slot/scan.db3.zst`, `./files/${root}/MID360/slot/metadata.yaml`,
    ]));
    expect((await archive.bytes(`./files/${root}/MID360/slot/scan.db3.zst`)).toString()).toBe('bytes of lidar');
    expect(fixture.state.files.every(row => (row.metadata as Record<string, unknown>).closeReason === 'rotation')).toBe(true);
  });
  it('retains the clicked membership while later uploads arrive; another click gets a different snapshot', async () => {
    const fixture = prepareFixture(work);
    const first = await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true });
    const taskId = 'taskId' in first ? first.taskId! : '';
    await waitForTask(fixture.service, taskId);
    fixture.state.files.push({ ...file('later', 'MID360/slot', 'later.json', '{}'), robotId: robotA,
      metadata: { captureSessionId: sessionA, closeReason: 'rotation' } });
    expect(await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true, taskId }))
      .toMatchObject({ state: 'ready', taskId });
    expect((await unpack(work, fixture.cache)).manifest.fileCount).toBe(3);
    const second = await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true });
    expect('taskId' in second && second.taskId).not.toBe(taskId);
    await waitForTask(fixture.service, 'taskId' in second ? second.taskId! : '');
  });
  it('rejects foreign session/robot tokens and folder endpoint replay without publishing a new package', async () => {
    const fixture = prepareFixture(work);
    const first = await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true });
    const taskId = 'taskId' in first ? first.taskId! : '';
    await waitForTask(fixture.service, taskId);
    fixture.storage.getPresignedDownloadUrl.mockClear();
    await expect(fixture.service.captureSessionPackage(dataset.id, sessionB, robotA, actor, { background: true, taskId }))
      .rejects.toMatchObject({ status: 409 });
    await expect(fixture.service.captureSessionPackage(dataset.id, sessionA, robotB, actor, { background: true, taskId }))
      .rejects.toMatchObject({ status: 409 });
    await expect(fixture.service.folderPackage(dataset.id, '', actor, { background: true, taskId }))
      .rejects.toMatchObject({ status: 409 });
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
    expect(fixture.cache.size).toBe(1);
  });
  it('reauthorizes list and ready presigning, and rejects deleted or reassigned members', async () => {
    const fixture = prepareFixture(work);
    const initial = await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true });
    const taskId = 'taskId' in initial ? initial.taskId! : '';
    await waitForTask(fixture.service, taskId);
    const unauthorized = { ...actor, projectIds: ['other-project'] };
    fixture.storage.getPresignedDownloadUrl.mockClear();
    await expect(fixture.service.captureSessions(dataset.id, robotA, unauthorized)).rejects.toMatchObject({ status: 404 });
    await expect(fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, unauthorized, { background: true, taskId }))
      .rejects.toMatchObject({ status: 404 });
    (fixture.state.files[0].metadata as Record<string, unknown>).captureSessionId = sessionB;
    await expect(fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true, taskId }))
      .rejects.toMatchObject({ status: 409 });
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
  });
  it('reports exact session summaries and unassigned files without requiring end proofs', async () => {
    const fixture = prepareFixture(work);
    fixture.state.files.push({ ...file('legacy', '前相机/legacy', 'video.mkv', 'legacy'), robotId: robotA,
      metadata: { captureSessionId: '', closeReason: 'rotation' } });
    const result = await fixture.service.captureSessions(dataset.id, robotA, actor);
    expect(result.sessions).toHaveLength(2);
    expect(result.sessions.find(item => item.sessionId === sessionA)).toMatchObject({ robotId: robotA,
      fileCount: 3, sensorCount: 2, snapshot: true });
    expect(result.unassigned).toMatchObject({ fileCount: 1 });
    await expect(fixture.service.captureSessionPackage(dataset.id, sessionA, '', actor, { background: true }))
      .rejects.toMatchObject({ status: 400 });
    await expect(fixture.service.captureSessions(dataset.id, 'all', actor)).rejects.toMatchObject({ status: 400 });
  });
  it('does not publish an archive when a selected source is deleted or moved into a different session during generation', async () => {
    const fixture = prepareFixture(work);
    fixture.onRead(key => {
      if (key === 'objects/camera') (fixture.state.files[0].metadata as Record<string, unknown>).captureSessionId = sessionB;
    });
    const result = await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true });
    const taskId = 'taskId' in result ? result.taskId! : '';
    expect((await waitForTask(fixture.service, taskId)).state).toBe('failed');
    expect(fixture.storage.putObject).not.toHaveBeenCalled();
    expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
    expect(fixture.state.files).toHaveLength(5);
  });
  it('reauthorizes processing polls and never starts a second compression when polled repeatedly', async () => {
    const fixture = prepareFixture(work);
    const actualTar = (fixture.service as unknown as { createTarZstd: (input: string, output: string, level: 1 | 3) => Promise<void> })
      .createTarZstd.bind(fixture.service);
    let release!: () => void, began!: () => void;
    const started = new Promise<void>(resolve => { began = resolve; });
    const gate = new Promise<void>(resolve => { release = resolve; });
    const tar = jest.spyOn(fixture.service as never, 'createTarZstd' as never).mockImplementation((async (input: string, output: string, level: 1 | 3) => {
      began(); await gate; await actualTar(input, output, level);
    }) as never);
    const result = await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA, actor, { background: true });
    const taskId = 'taskId' in result ? result.taskId! : '';
    await started;
    try {
      await expect(fixture.service.captureSessionPackage(dataset.id, sessionA, robotA,
        { ...actor, projectIds: ['other-project'] }, { background: true, taskId })).rejects.toMatchObject({ status: 404 });
      for (let poll = 0; poll < 3; poll++) expect(await fixture.service.captureSessionPackage(dataset.id, sessionA, robotA,
        actor, { background: true, taskId })).toMatchObject({ state: 'processing', taskId });
      expect(tar).toHaveBeenCalledTimes(1);
      expect(fixture.storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
    } finally { release(); await waitForTask(fixture.service, taskId); }
  });
});
