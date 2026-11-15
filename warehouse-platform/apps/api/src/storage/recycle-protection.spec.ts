import { StorageOperationsService } from './storage-operations.service';

describe('Recycled object protection', () => {
  it('rejects a stale orphan cleanup after an object enters the recycle bin', async () => {
    const tx = { $queryRaw: jest.fn().mockResolvedValue([]), robotFile: { count: jest.fn().mockResolvedValue(0) }, recycledFile: { count: jest.fn().mockResolvedValue(1) }, datasetVersion: { count: jest.fn().mockResolvedValue(0) }, datasetFolder: { count: jest.fn().mockResolvedValue(0) } };
    const prisma = { $transaction: jest.fn(async fn => fn(tx)), reconciliationIssue: { findMany: jest.fn().mockResolvedValue([{ id: 'issue', objectKey: 'dataset/file.json' }]), updateMany: jest.fn() } };
    const storage = { deleteObjects: jest.fn() };
    await expect(new StorageOperationsService(prisma as never, {} as never, storage as never).executeOrphanDelete(['issue'])).rejects.toThrow('回收站');
    expect(storage.deleteObjects).not.toHaveBeenCalled();
    expect(prisma.reconciliationIssue.updateMany).not.toHaveBeenCalled();
  });
});
