import { UploadStatus } from '@prisma/client';
import { StorageOperationsService } from './storage-operations.service';

function setup(status: UploadStatus, expiresAt: Date) {
  const objectKey = 'dataset/video.mkv';
  const tx = { $queryRaw: jest.fn(async query => query?.sql?.includes('FROM "UploadSession"') ? [{ id: 'upload-a', status, expiresAt }] : []),
    robotFile: { count: jest.fn(async () => 0) }, recycledFile: { count: jest.fn(async () => 0) },
    datasetVersion: { count: jest.fn(async () => 0) }, datasetFolder: { count: jest.fn(async () => 0) },
    uploadSession: { findMany: jest.fn(async () => [{ id: 'upload-a', status, expiresAt, finalObjectKey: objectKey, tempPrefix: 'uploads/upload-a/parts/' }]) },
  };
  const prisma = { $transaction: jest.fn(async fn => fn(tx)),
    reconciliationIssue: { findMany: jest.fn(async () => [{ id: 'issue', objectKey }]), updateMany: jest.fn() } };
  const storage = { deleteObjects: jest.fn() };
  return { tx, prisma, storage, service: new StorageOperationsService(prisma as never, {} as never, storage as never) };
}

describe('active upload object protection', () => {
  it('does not report composed output or parts of an active upload as orphan objects', async () => {
    const run = { update: jest.fn(async () => ({ requestedById: null })) };
    const prisma = { reconciliationRun: run,
      robotFile: { findMany: jest.fn(async () => []) }, recycledFile: { findMany: jest.fn(async () => []) },
      datasetVersion: { findMany: jest.fn(async () => []) }, dataset: { findMany: jest.fn(async () => []) },
      datasetFolder: { findMany: jest.fn(async () => []) },
      uploadSession: { findMany: jest.fn(async () => [{ tempPrefix: 'uploads/upload-a/parts/', finalObjectKey: 'dataset/video.mkv' }]) },
      reconciliationIssue: { deleteMany: jest.fn(), createMany: jest.fn() }, $transaction: jest.fn(),
    };
    prisma.$transaction.mockImplementation(async fn => fn(prisma));
    const storage = { listObjects: jest.fn(async () => ({ items: [
      { objectKey: 'dataset/video.mkv', size: 10 }, { objectKey: 'uploads/upload-a/parts/000001', size: 10 },
    ] })) };
    const service = new StorageOperationsService(prisma as never, {} as never, storage as never);
    await expect(service.executeScan('run')).resolves.toMatchObject({ issueCount: 0 });
    expect(prisma.reconciliationIssue.createMany).not.toHaveBeenCalled();
  });

  it.each([UploadStatus.CREATED, UploadStatus.UPLOADING, UploadStatus.FINALIZING, UploadStatus.FAILED])('keeps %s upload output out of orphan deletion', async status => {
    const { service, storage } = setup(status, new Date(Date.now() + 60_000));
    await expect(service.executeOrphanDelete(['issue'])).rejects.toThrow('上传');
    expect(storage.deleteObjects).not.toHaveBeenCalled();
  });

  it('protects a finalizing output even after its original upload expiry', async () => {
    const { service, storage } = setup(UploadStatus.FINALIZING, new Date(0));
    await expect(service.executeOrphanDelete(['issue'])).rejects.toThrow('上传');
    expect(storage.deleteObjects).not.toHaveBeenCalled();
  });

  it('locks matching upload sessions before rechecking file indexes after completion', async () => {
    const { service, storage, tx } = setup(UploadStatus.COMPLETED, new Date(0));
    tx.robotFile.count.mockResolvedValue(1);
    await expect(service.executeOrphanDelete(['issue'])).rejects.toThrow('文件');
    const lockCall = tx.$queryRaw.mock.calls.findIndex(([query]) => query?.sql?.includes('FROM "UploadSession"'));
    expect(lockCall).toBeGreaterThanOrEqual(0);
    expect(tx.$queryRaw.mock.invocationCallOrder[lockCall]).toBeLessThan(tx.robotFile.count.mock.invocationCallOrder[0]);
    expect(storage.deleteObjects).not.toHaveBeenCalled();
  });

  it('does not block confirmed orphan cleanup for an expired failed session', async () => {
    const { service, storage } = setup(UploadStatus.FAILED, new Date(0));
    await expect(service.executeOrphanDelete(['issue'])).resolves.toMatchObject({ deleted: 1 });
    expect(storage.deleteObjects).toHaveBeenCalledWith(['dataset/video.mkv']);
  });
});
