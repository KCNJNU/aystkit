import { BadRequestException, ConflictException, NotFoundException } from '@nestjs/common';
import { validate } from 'class-validator';
import { AuthUser } from '../auth/auth.types';
import { FilesService } from './files.service';
import { DetachFileReferencesDto } from './dto/detach-file-references.dto';

const actor = { role: 'OPERATOR', tenantId: 'tenant-a', projectIds: ['project-a'] } as AuthUser;
const versionId = '00000000-0000-4000-8000-000000000001';
describe('File reference review and detachment', () => {
  const tx = { $queryRaw: jest.fn(), datasetVersion: { findMany: jest.fn(), update: jest.fn() }, datasetVersionFile: { deleteMany: jest.fn(), findMany: jest.fn(), count: jest.fn() } };
  const prisma = { $transaction: jest.fn(), robotFile: { findFirst: jest.fn() }, datasetVersionFile: { findMany: jest.fn(), count: jest.fn() } };
  const storage = { deleteObject: jest.fn() };
  const service = new FilesService(prisma as never, storage as never);
  const input = { confirmed: true, versionIds: [versionId] };
  beforeEach(() => {
    jest.resetAllMocks();
    prisma.robotFile.findFirst.mockResolvedValue({ id: 'file', originalName: 'test.json' });
    prisma.$transaction.mockImplementation(arg => typeof arg === 'function' ? arg(tx) : Promise.all(arg));
    tx.datasetVersion.findMany.mockResolvedValue([{ id: versionId, status: 'DRAFT', updatedAt: new Date() }]);
    tx.datasetVersionFile.deleteMany.mockResolvedValue({ count: 1 });
    tx.datasetVersionFile.findMany.mockResolvedValue([{ robotFile: { fileSize: 42n } }]);
    tx.datasetVersionFile.count.mockResolvedValue(0);
  });
  it('reviews only scoped references, explains protected versions, and counts hidden references without leaking names', async () => {
    prisma.datasetVersionFile.findMany.mockResolvedValue(['DRAFT', 'FROZEN'].map((status, i) => ({ createdAt: new Date(), datasetVersion: { id: String(i), version: i + 1, status, dataset: { id: 'dataset', name: '测试集' } } })));
    prisma.datasetVersionFile.count.mockResolvedValue(3);
    const result = await service.references('file', actor);
    expect(result.restrictedCount).toBe(1);
    expect(result.references.map(ref => ref.canDetach)).toEqual([true, false]);
    expect(prisma.datasetVersionFile.findMany).toHaveBeenCalledWith(expect.objectContaining({ where: { robotFileId: 'file', datasetVersion: { dataset: { deletingAt: null, tenantId: 'tenant-a', projectId: { in: ['project-a'] } } } } }));
  });
  it('requires explicit confirmation without changing any data', async () => {
    await expect(service.detachReferences('file', { ...input, confirmed: false }, actor)).rejects.toBeInstanceOf(BadRequestException);
    expect(prisma.$transaction).not.toHaveBeenCalled();
  });
  it('rejects files outside the source scope', async () => {
    prisma.robotFile.findFirst.mockResolvedValue(null);
    await expect(service.detachReferences('file', input, actor)).rejects.toBeInstanceOf(NotFoundException);
    expect(prisma.$transaction).not.toHaveBeenCalled();
  });
  it('rejects stale or inaccessible target versions without deleting any membership', async () => {
    tx.datasetVersion.findMany.mockResolvedValue([]);
    await expect(service.detachReferences('file', input, actor)).rejects.toBeInstanceOf(ConflictException);
    expect(tx.datasetVersionFile.deleteMany).not.toHaveBeenCalled();
    expect(tx.datasetVersion.findMany).toHaveBeenCalledWith(expect.objectContaining({ where: expect.objectContaining({ dataset: { deletingAt: null, tenantId: 'tenant-a', projectId: { in: ['project-a'] } } }) }));
  });
  it.each(['FROZEN', 'PUBLISHED', 'ARCHIVED'])('rechecks %s under a lock and refuses mutation', async status => {
    tx.datasetVersion.findMany.mockResolvedValue([{ id: versionId, status }]);
    await expect(service.detachReferences('file', input, actor)).rejects.toBeInstanceOf(ConflictException);
    expect(tx.$queryRaw.mock.invocationCallOrder[0]).toBeLessThan(tx.datasetVersion.findMany.mock.invocationCallOrder[0]);
    expect(tx.datasetVersionFile.deleteMany).not.toHaveBeenCalled();
  });
  it('updates counts and the optimistic freeze timestamp atomically while retaining the original file', async () => {
    const future = new Date(Date.now() + 10000);
    tx.datasetVersion.findMany.mockResolvedValue([{ id: versionId, status: 'DRAFT', updatedAt: future }]);
    await expect(service.detachReferences('file', input, actor)).resolves.toEqual({ detachedCount: 1, remainingCount: 0 });
    expect(tx.datasetVersionFile.deleteMany).toHaveBeenCalledWith({ where: { robotFileId: 'file', datasetVersionId: { in: [versionId] } } });
    expect(tx.datasetVersion.update).toHaveBeenCalledWith({ where: { id: versionId }, data: { fileCount: 1, totalSize: 42n, updatedAt: new Date(future.getTime() + 1) } });
    expect(storage.deleteObject).not.toHaveBeenCalled();
  });
  it('rejects malformed confirmation and version lists at the HTTP DTO boundary', async () => {
    for (const values of [{ versionIds: [versionId] }, { confirmed: 'true', versionIds: [versionId] }, { confirmed: true, versionIds: [] }, { confirmed: true, versionIds: ['bad-id'] }, { confirmed: true, versionIds: [versionId, versionId] }]) {
      expect((await validate(Object.assign(new DetachFileReferencesDto(), values))).length).toBeGreaterThan(0);
    }
    expect(await validate(Object.assign(new DetachFileReferencesDto(), input))).toEqual([]);
  });
});
