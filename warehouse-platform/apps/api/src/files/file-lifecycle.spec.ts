import { ConflictException, NotFoundException } from '@nestjs/common';
import { Prisma } from '@prisma/client';
import { AuthUser } from '../auth/auth.types';
import { FileLifecycleService } from './file-lifecycle.service';

const user = { id: 'user', role: 'OPERATOR', tenantId: 'tenant', projectIds: ['project'] } as AuthUser;
const dataset = { id: 'dataset', name: '测试集', tenantId: 'tenant', projectId: 'project', deletingAt: null };
const file = { id: 'file', datasetId: 'dataset', robotId: 'robot', folderPath: '', originalName: 'sample.json', objectKey: '测试集/sample.json', fileSize: 12n, checksum: 'sha', fileType: 'json', mimeType: 'application/json', metadata: { note: '原始元信息' }, recognitionType: 'sample', schemaVersion: '1', capturedAt: new Date(), createdAt: new Date(), updatedAt: new Date() };
const snapshot = JSON.parse(JSON.stringify({ file, tagIds: ['tag'] }, (_, value) => typeof value === 'bigint' ? value.toString() : value));
function setup() {
  const entry = { ...file, tenantId: 'tenant', projectId: 'project', snapshot, purgingAt: null };
  const tx = {
    $queryRaw: jest.fn(),
    dataset: { findFirst: jest.fn().mockResolvedValue(dataset) },
    robot: { findFirst: jest.fn().mockResolvedValue({ id: 'robot' }) },
    robotFile: { findFirst: jest.fn().mockImplementation(async ({ where }) => where.id === 'file' ? { ...file, robot: { tenantId: 'tenant', projectId: 'project' }, tags: [{ tagId: 'tag' }] } : null), create: jest.fn(), update: jest.fn(), delete: jest.fn() },
    recycledFile: { findFirst: jest.fn().mockResolvedValue(entry), findUnique: jest.fn().mockResolvedValue(null), create: jest.fn(), update: jest.fn().mockResolvedValue(entry), delete: jest.fn(), deleteMany: jest.fn() },
    datasetVersionFile: { count: jest.fn().mockResolvedValue(0) },
    datasetVersion: { findMany: jest.fn().mockResolvedValue([]), count: jest.fn().mockResolvedValue(0), update: jest.fn() },
    datasetFolder: { findUnique: jest.fn().mockResolvedValue(null) },
    uploadSession: { findFirst: jest.fn().mockResolvedValue(null) },
    tag: { findMany: jest.fn().mockResolvedValue([{ id: 'tag' }]) },
  };
  const prisma = { ...tx, $transaction: jest.fn(async callback => callback(tx)) };
  const storage = { exists: jest.fn().mockResolvedValue(false), composeObject: jest.fn(), statObject: jest.fn().mockResolvedValue({ size: 12 }), deleteObject: jest.fn().mockResolvedValue(undefined) };
  const refs = { references: jest.fn().mockResolvedValue({ total: 0, references: [], restrictedCount: 0 }) };
  return { tx, prisma, storage, refs, service: new FileLifecycleService(prisma as never, storage as never, refs as never) };
}
describe('Recoverable file lifecycle', () => {
  it('atomically archives metadata and tags without deleting the storage object', async () => {
    const { service, tx, storage } = setup();
    await service.recycle('file', user);
    expect(tx.recycledFile.create).toHaveBeenCalledWith({ data: expect.objectContaining({ id: 'file', snapshot, tenantId: 'tenant', projectId: 'project', reviewAt: expect.any(Date) }) });
    expect(tx.recycledFile.create.mock.invocationCallOrder[0]).toBeLessThan(tx.robotFile.delete.mock.invocationCallOrder[0]);
    expect(storage.deleteObject).not.toHaveBeenCalled();
  });
  it('refuses referenced files and a racing foreign key without touching storage', async () => {
    const { service, tx, storage } = setup(); tx.datasetVersionFile.count.mockResolvedValue(1);
    await expect(service.recycle('file', user)).rejects.toBeInstanceOf(ConflictException);
    expect(tx.recycledFile.create).not.toHaveBeenCalled();
    tx.datasetVersionFile.count.mockResolvedValue(0); tx.robotFile.delete.mockRejectedValue(new Prisma.PrismaClientKnownRequestError('foreign key', { code: 'P2003', clientVersion: 'test' }));
    await expect(service.recycle('file', user)).rejects.toThrow('引用已变化');
    expect(storage.deleteObject).not.toHaveBeenCalled();
  });
  it('does not act on files outside the source project', async () => {
    const { service, tx, prisma } = setup(); tx.robotFile.findFirst.mockResolvedValue(null);
    await expect(service.recycle('file', user)).rejects.toBeInstanceOf(NotFoundException);
    expect(tx.robotFile.findFirst).toHaveBeenCalledWith(expect.objectContaining({ where: { id: 'file', robot: { tenantId: 'tenant', projectId: { in: ['project'] } } } }));
    expect(prisma.$transaction).not.toHaveBeenCalled();
  });
  it('returns a blocked impact without mutating references', async () => {
    const { service, refs, tx } = setup(); refs.references.mockResolvedValue({ total: 2, references: [], restrictedCount: 2 });
    expect(await service.impact('file', user)).toMatchObject({ canRecycle: false, canRelocate: false });
    expect(tx.robotFile.delete).not.toHaveBeenCalled();
  });
  it('restores the same ID, metadata and remaining tags without recreating version references', async () => {
    const { service, tx, storage } = setup(); storage.exists.mockResolvedValue(true);
    await expect(service.restore('file', user)).resolves.toEqual({ id: 'file', restored: true });
    expect(tx.robotFile.create).toHaveBeenCalledWith({ data: expect.objectContaining({ id: 'file', fileSize: 12n, metadata: file.metadata, tags: { create: [{ tagId: 'tag' }] } }) });
    expect(tx.recycledFile.delete).toHaveBeenCalled(); expect(storage.deleteObject).not.toHaveBeenCalled();
  });
  it('retains the recovery record on destination conflict or a missing object', async () => {
    const { service, tx } = setup(); tx.robotFile.findFirst.mockResolvedValue(file as never);
    await expect(service.restore('file', user)).rejects.toThrow('同名');
    tx.robotFile.findFirst.mockResolvedValue(null);
    await expect(service.restore('file', user)).rejects.toThrow('存储对象缺失');
    expect(tx.recycledFile.delete).not.toHaveBeenCalled();
  });
  it('prevents restoration once permanent cleanup has begun', async () => {
    const { service, tx } = setup(); tx.recycledFile.findFirst.mockResolvedValue({ ...file, snapshot, purgingAt: new Date() } as never);
    await expect(service.restore('file', user)).rejects.toThrow('正在彻底清理');
    expect(tx.robotFile.create).not.toHaveBeenCalled();
  });
  it('persists cleanup intent and retains the row when MinIO fails, allowing retry', async () => {
    const { service, tx, storage } = setup(); storage.deleteObject.mockRejectedValueOnce(new Error('offline'));
    await expect(service.purge('file', user)).rejects.toThrow('offline');
    expect(tx.recycledFile.update).toHaveBeenCalledWith({ where: { id: 'file' }, data: { purgingAt: expect.any(Date) } });
    expect(tx.recycledFile.deleteMany).not.toHaveBeenCalled();
    await expect(service.purge('file', user)).resolves.toMatchObject({ purged: true });
    expect(tx.recycledFile.deleteMany).toHaveBeenCalled();
  });
});
describe('Storage path synchronization', () => {
  const input = { originalName: 'renamed.json', folderPath: '', confirmed: true as const };
  it('copies and verifies before committing metadata, then removes the previous object', async () => {
    const { service, tx, storage } = setup();
    await expect(service.relocate('file', input, user)).resolves.toMatchObject({ objectKey: '测试集/renamed.json', cleanupPending: false });
    expect(storage.composeObject).toHaveBeenCalledWith('测试集/renamed.json', [file.objectKey], file.mimeType);
    expect(storage.statObject.mock.invocationCallOrder[0]).toBeLessThan(tx.robotFile.update.mock.invocationCallOrder[0]);
    expect(tx.robotFile.update.mock.invocationCallOrder[0]).toBeLessThan(storage.deleteObject.mock.invocationCallOrder[0]);
    expect(storage.deleteObject).toHaveBeenCalledWith(file.objectKey);
  });
  it.each(['FROZEN', 'PUBLISHED', 'ARCHIVED'])('protects %s version paths', async status => {
    const { service, tx, storage } = setup(); tx.datasetVersion.findMany.mockResolvedValue([{ id: 'version', status }] as never); tx.datasetVersion.count.mockResolvedValue(1);
    await expect(service.relocate('file', input, user)).rejects.toThrow('受保护');
    expect(storage.composeObject).not.toHaveBeenCalled();
  });
  it('invalidates optimistic freezing of draft references', async () => {
    const { service, tx } = setup(); const timestamp = new Date(Date.now() + 5000);
    tx.datasetVersion.findMany.mockResolvedValue([{ id: 'version', status: 'DRAFT', updatedAt: timestamp }] as never); tx.datasetVersion.count.mockResolvedValue(1);
    await service.relocate('file', input, user);
    expect(tx.datasetVersion.update).toHaveBeenCalledWith({ where: { id: 'version' }, data: { updatedAt: new Date(timestamp.getTime() + 1) } });
  });
  it('refuses overwrites and path traversal', async () => {
    const { service, storage } = setup(); storage.exists.mockResolvedValue(true);
    await expect(service.relocate('file', input, user)).rejects.toThrow('占用');
    await expect(service.relocate('file', { ...input, folderPath: '../elsewhere' }, user)).rejects.toThrow();
    await expect(service.relocate('file', { ...input, originalName: 'different.png' }, user)).rejects.toThrow('扩展名');
    expect(storage.composeObject).not.toHaveBeenCalled();
  });
  it('cleans only the new copy on verification failure, retaining the original', async () => {
    const { service, storage, tx } = setup(); storage.statObject.mockResolvedValue({ size: 9 });
    await expect(service.relocate('file', input, user)).rejects.toThrow('校验失败');
    expect(storage.deleteObject).toHaveBeenCalledWith('测试集/renamed.json');
    expect(storage.deleteObject).not.toHaveBeenCalledWith(file.objectKey); expect(tx.robotFile.update).not.toHaveBeenCalled();
  });
  it('reports old-copy cleanup failure after the new location has committed', async () => {
    const { service, storage } = setup(); storage.deleteObject.mockRejectedValue(new Error('offline'));
    await expect(service.relocate('file', input, user)).resolves.toMatchObject({ cleanupPending: true });
  });
});
