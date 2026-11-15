import { Readable } from 'node:stream';
import { createHash } from 'node:crypto';
import { UploadsService } from './uploads.service';
import { AuthUser } from '../auth/auth.types';

const actor: AuthUser = { id: 'user', role: 'OPERATOR', tenantId: 'tenant', projectIds: ['project'], email: '', displayName: '', mustChangePassword: false };
const dataset = { id: 'dataset', name: 'test01', tenantId: 'tenant', projectId: 'project' };
const content = Buffer.from('dataset content');
const checksum = createHash('sha256').update(content).digest('hex');
const input = { datasetId: dataset.id, robotId: 'robot', originalName: 'logs/test.txt', mimeType: 'text/plain', fileSize: content.length, checksum, recognitionType: 'unclassified', schemaVersion: '1.0', capturedAt: '2026-09-22T00:00:00Z' };
function setup() {
  const tx = { $queryRaw: jest.fn(), dataset: { update: jest.fn(), findUniqueOrThrow: jest.fn().mockResolvedValue(dataset) }, datasetFolder: { findUnique: jest.fn().mockResolvedValue(null), upsert: jest.fn().mockResolvedValue({ id: 'folder' }) }, datasetVersion: { findFirst: jest.fn().mockResolvedValue(null), create: jest.fn().mockResolvedValue({ id: 'draft', version: 1 }), updateMany: jest.fn().mockResolvedValue({ count: 1 }) }, datasetVersionFile: { createMany: jest.fn() }, robotFile: { findFirst: jest.fn(), findUnique: jest.fn(), create: jest.fn().mockResolvedValue({ id: 'file' }) }, uploadSession: { update: jest.fn(), findFirst: jest.fn(), findUnique: jest.fn(), create: jest.fn().mockImplementation(({data}) => Promise.resolve({ ...data, parts: [] })) } };
  const prisma = { dataset: { findFirst: jest.fn().mockResolvedValue(dataset) }, robot: { findFirst: jest.fn().mockResolvedValue({ id: 'robot', tenantId: 'tenant', projectId: 'project' }), upsert: jest.fn().mockResolvedValue({ id: 'robot' }), findUniqueOrThrow: jest.fn().mockResolvedValue({ externalId: 'source' }) }, robotFile: { findFirst: jest.fn().mockResolvedValue(null) }, uploadSession: { create: jest.fn().mockImplementation(({data}) => Promise.resolve({ ...data, parts: [] })), findUnique: jest.fn(), update: jest.fn(), updateMany: jest.fn().mockResolvedValue({ count: 1 }) }, $transaction: jest.fn(async fn => fn(tx)) };
  const storage = { exists: jest.fn().mockResolvedValue(false), putObject: jest.fn(), composeObject: jest.fn(), getObject: jest.fn().mockImplementation(() => Promise.resolve(Readable.from([content]))), deleteObject: jest.fn().mockResolvedValue(undefined), deleteObjects: jest.fn().mockResolvedValue(undefined) };
  const service = new UploadsService(prisma as never, {} as never, { get: jest.fn().mockResolvedValue(100000) } as never, storage as never);
  tx.uploadSession.findUnique.mockImplementation(async ({ where }) => where.id ? ({ ...await prisma.uploadSession.findUnique(), status: 'FINALIZING' }) : null);
  return { service, prisma, storage, tx };
}
describe('Dataset-bound uploads', () => {
  it('resolves the source within the selected project and locks the destination at session creation', async () => {
    const {service, prisma} = setup();
    await service.defaults(actor, dataset.id);
    expect(prisma.robot.upsert).toHaveBeenCalledWith(expect.objectContaining({ create: expect.objectContaining({ projectId: 'project', tenantId: 'tenant' }) }));
    const session = await service.create(actor, input);
    expect(session.finalObjectKey).toBe('test01/logs/test.txt');
    expect(session.folderPath).toBe('logs');
    expect(session.datasetId).toBe(dataset.id);
    expect(prisma.robotFile.findFirst).toHaveBeenCalledWith({ where: { checksum, datasetId: dataset.id, folderPath: 'logs' }, select: { id: true } });
  });
  it('rejects an inaccessible dataset before creating storage objects', async () => {
    const {service, prisma, storage} = setup(); prisma.dataset.findFirst.mockResolvedValue(null);
    await expect(service.create(actor, input)).rejects.toThrow('无访问权限');
    expect(prisma.dataset.findFirst).toHaveBeenCalledWith({ where: { id: dataset.id, deletingAt: null, tenantId: 'tenant', projectId: { in: ['project'] } } });
    expect(storage.putObject).not.toHaveBeenCalled();
  });
  it('rejects a source from another project', async () => {
    const {service, prisma} = setup(); prisma.robot.findFirst.mockResolvedValue({ id: 'robot', tenantId: 'tenant', projectId: 'other' });
    await expect(service.create(actor, input)).rejects.toThrow('同一个项目');
  });
  it.each(['DRAFT', 'FROZEN'])('adds the completed file to a writable draft when latest is %s', async status => {
    const {service, prisma, tx, storage} = setup();
    const created = await service.create(actor, input);
    prisma.uploadSession.findUnique.mockResolvedValue({ ...created, fileSize: BigInt(content.length), capturedAt: new Date(input.capturedAt), status: 'UPLOADING', parts: [{ objectKey: 'uploads/part', partNumber: 1, size: content.length }] });
    tx.datasetVersion.findFirst.mockResolvedValue({ id: 'old', status, version: 4 });
    storage.exists.mockResolvedValue(true);
    await service.finalize(created.id);
    expect(storage.composeObject).toHaveBeenCalledWith(created.finalObjectKey, ['uploads/part'], 'text/plain');
    expect(tx.robotFile.create).toHaveBeenCalledWith(expect.objectContaining({ data: expect.objectContaining({ datasetId: dataset.id }) }));
    expect(tx.datasetVersionFile.createMany).toHaveBeenCalledWith({ data: [{ datasetVersionId: status === 'DRAFT' ? 'old' : 'draft', robotFileId: 'file' }], skipDuplicates: true });
    if (status === 'FROZEN') expect(tx.datasetVersion.create).toHaveBeenCalledWith({ data: { datasetId: dataset.id, version: 5 } });
  });
  it('uses the selected nested directory and scopes checksum matching to that directory', async () => {
    const { service, tx, prisma } = setup();
    tx.datasetFolder.findUnique.mockImplementation(({where}) => Promise.resolve(where.datasetId_path.path === 'images/raw' ? { id: 'nested' } : null));
    const result = await service.create(actor, { ...input, folderPath: 'images/raw', originalName: 'photo.txt' });
    expect(result.finalObjectKey).toBe('test01/images/raw/photo.txt');
    expect(prisma.robotFile.findFirst).toHaveBeenCalledWith({ where: { checksum, datasetId: dataset.id, folderPath: 'images/raw' }, select: { id: true } });
  });
  it('rejects missing target folders instead of silently placing files at the root', async () => {
    const { service, storage } = setup();
    await expect(service.create(actor, { ...input, folderPath: 'missing' })).rejects.toThrow('目标文件夹不存在');
    expect(storage.putObject).not.toHaveBeenCalled();
  });
  it('reserves a new filename when the destination already exists', async () => {
    const { service, tx } = setup(); tx.robotFile.findUnique.mockResolvedValue({ id: 'older-file' });
    const result = await service.create(actor, input);
    expect(result.finalObjectKey).toMatch(/^test01\/logs\/test__[a-f0-9-]+\.txt$/);
  });

});
