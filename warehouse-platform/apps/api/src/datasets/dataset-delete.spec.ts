import { DatasetsService } from './datasets.service';
import { StorageOperationsService } from '../storage/storage-operations.service';
import { AuthUser } from '../auth/auth.types';
const actor: AuthUser = { id: 'user', role: 'OPERATOR', tenantId: 'tenant', projectIds: ['project'], displayName: '', email: '', mustChangePassword: false };
const dataset = { id: 'dataset', name: 'test01', tenantId: 'tenant', projectId: 'project', deletingAt: null };
function setup() {
  const tx = {
    $queryRaw: jest.fn(),
    recycledFile: { count: jest.fn().mockResolvedValue(0) },
    dataset: { findFirst: jest.fn().mockResolvedValue(dataset), findUniqueOrThrow: jest.fn().mockResolvedValue(dataset), update: jest.fn() },
    uploadSession: { findMany: jest.fn().mockResolvedValue([]), deleteMany: jest.fn() },
    datasetVersion: { findMany: jest.fn().mockResolvedValue([{ id: 'version', manifestKey: 'manifests/dataset/v1.json' }]), deleteMany: jest.fn() },
    robotFile: { findMany: jest.fn().mockResolvedValue([{ id: 'owned-file', objectKey: 'test01/upload/file.txt' }]), deleteMany: jest.fn() },
    datasetFolder: { findMany: jest.fn().mockResolvedValue([]) },
    datasetVersionFile: { count: jest.fn().mockResolvedValue(0) },
    job: { count: jest.fn().mockResolvedValue(0), create: jest.fn().mockResolvedValue({ id: 'job' }) },
  };
  const prisma = { $transaction: jest.fn(async fn => fn(tx)) };
  const storage = { deleteObjects: jest.fn(), listObjects: jest.fn().mockResolvedValue({ items: [] }) };
  return { tx, storage, service: new DatasetsService(prisma as never, storage as never) };
}
describe('Dataset deletion', () => {
  it('keeps the recovery destination when a dataset contains recycled files', async () => {
    const { tx, service } = setup(); tx.recycledFile.count.mockResolvedValue(1);
    await expect(service.delete('dataset', 'test01', actor)).rejects.toThrow('回收站');
    expect(tx.robotFile.deleteMany).not.toHaveBeenCalled();
  });
  it('checks tenant and project scope before any deletion', async () => {
    const {tx, service} = setup(); tx.dataset.findFirst.mockResolvedValue(null);
    await expect(service.delete('dataset', 'test01', actor)).rejects.toThrow('无访问权限');
    expect(tx.dataset.findFirst).toHaveBeenCalledWith({ where: { id: 'dataset', deletingAt: null, tenantId: 'tenant', projectId: { in: ['project'] } } });
    expect(tx.job.create).not.toHaveBeenCalled();
  });
  it('requires an exact name confirmation', async () => {
    const {tx, service} = setup();
    await expect(service.delete('dataset', 'other', actor)).rejects.toThrow('不匹配');
    expect(tx.dataset.update).not.toHaveBeenCalled();
  });
  it('blocks unfinished uploads', async () => {
    const {tx, service} = setup(); tx.uploadSession.findMany.mockResolvedValue([{ status: 'UPLOADING', parts: [], expiresAt: new Date(Date.now() + 60000) }]);
    await expect(service.delete('dataset', 'test01', actor)).rejects.toThrow('未完成的上传');
    expect(tx.datasetVersion.deleteMany).not.toHaveBeenCalled();
  });
  it('identifies an abandoned empty upload without cancelling it or revealing storage internals', async () => {
    const { tx, service } = setup();
    const updatedAt = new Date(Date.now() - 60 * 60 * 1000), expiresAt = new Date(Date.now() + 60 * 60 * 1000);
    tx.uploadSession.findMany.mockResolvedValue([{ id: 'empty-upload', ownerId: actor.id, originalName: 'metadata.yaml',
      status: 'CREATED', parts: [], createdAt: updatedAt, updatedAt, expiresAt,
      checksum: 'private-checksum', tempPrefix: 'private-temp', metadata: { private: true } }]);
    try {
      await service.delete('dataset', 'test01', actor);
      throw new Error('expected conflict');
    } catch (error) {
      expect((error as { getResponse: () => unknown }).getResponse()).toEqual({
        code: 'DATASET_UPLOADS_PENDING', message: '数据集仍有未完成的上传，请先完成或取消上传任务',
        blockingUploads: [{ id: 'empty-upload', originalName: 'metadata.yaml', status: 'CREATED', partCount: 0,
          createdAt: updatedAt, updatedAt, expiresAt, canAbortEmpty: true }],
      });
    }
    expect(tx.uploadSession.deleteMany).not.toHaveBeenCalled();
    expect(tx.dataset.update).not.toHaveBeenCalled();
    expect(tx.job.create).not.toHaveBeenCalled();
  });
  it.each([
    ['CREATED', 0, 1, actor.id],
    ['CREATED', 1, 60, actor.id],
    ['UPLOADING', 0, 60, actor.id],
    ['FINALIZING', 0, 60, actor.id],
    ['FAILED', 0, 60, actor.id],
    ['CREATED', 0, 60, 'other-owner'],
  ])('does not offer empty recovery for %s with %s parts after %s minutes owned by %s', async (status, count, minutes, ownerId) => {
    const { tx, service } = setup();
    const updatedAt = new Date(Date.now() - Number(minutes) * 60000);
    tx.uploadSession.findMany.mockResolvedValue([{ id: 'upload', ownerId, originalName: 'file', status,
      parts: Array.from({ length: Number(count) }, () => ({ objectKey: 'part' })),
      createdAt: updatedAt, updatedAt, expiresAt: new Date(Date.now() + 60000) }]);
    const conflict = await service.delete('dataset', 'test01', actor).catch(error => error);
    expect(conflict.getResponse()).toMatchObject({ code: 'DATASET_UPLOADS_PENDING', blockingUploads: [{ canAbortEmpty: false }] });
    expect(tx.dataset.update).not.toHaveBeenCalled();
  });
  it('offers administrators explicit recovery of old empty sessions from another owner', async () => {
    const { tx, service } = setup();
    const updatedAt = new Date(Date.now() - 60 * 60000);
    tx.uploadSession.findMany.mockResolvedValue([{ id: 'upload', ownerId: 'other-owner', originalName: 'file',
      status: 'CREATED', parts: [], createdAt: updatedAt, updatedAt, expiresAt: new Date(Date.now() + 60000) }]);
    const conflict = await service.delete('dataset', 'test01', { ...actor, role: 'SYSTEM_ADMIN' }).catch(error => error);
    expect(conflict.getResponse()).toMatchObject({ blockingUploads: [{ canAbortEmpty: true }] });
    expect(tx.uploadSession.deleteMany).not.toHaveBeenCalled();
  });
  it('keeps finalizing uploads protected after expiry and excludes completed sessions from blockers', async () => {
    const { tx, service } = setup();
    const date = new Date(Date.now() - 60000);
    tx.uploadSession.findMany.mockResolvedValue([
      { id: 'finalizing', status: 'FINALIZING', originalName: 'video.mkv', parts: [], createdAt: date, updatedAt: date, expiresAt: date },
      { id: 'completed', status: 'COMPLETED', parts: [], expiresAt: date },
      { id: 'expired', status: 'CREATED', parts: [], expiresAt: date },
    ]);
    const conflict = await service.delete('dataset', 'test01', actor).catch(error => error);
    expect(conflict.getResponse()).toMatchObject({ blockingUploads: [{ id: 'finalizing', canAbortEmpty: false }] });
  });
  it('blocks queued and running background operations', async () => {
    const {tx, service} = setup(); tx.job.count.mockResolvedValue(1);
    await expect(service.delete('dataset', 'test01', actor)).rejects.toThrow('后台任务');
    expect(tx.datasetVersion.deleteMany).not.toHaveBeenCalled();
  });
  it('protects owned files referenced by another dataset', async () => {
    const {tx, service} = setup(); tx.datasetVersionFile.count.mockResolvedValue(1);
    await expect(service.delete('dataset', 'test01', actor)).rejects.toThrow('其他数据集');
    expect(tx.robotFile.deleteMany).not.toHaveBeenCalled();
    expect(tx.job.create).not.toHaveBeenCalled();
  });

  it('adds only generated folder-cache copies from every scoped page to the same deletion job', async () => {
    const { tx, service, storage } = setup();
    const prefix = 'test01/.folder-downloads/';
    const first = prefix + 'a'.repeat(64) + '/' + 'b'.repeat(64) + '.tar.zst';
    const second = prefix + 'c'.repeat(64) + '/' + 'd'.repeat(64) + '.tar.zst';
    storage.listObjects.mockResolvedValueOnce({ items: [
      { objectKey: first, size: 12 },
      { objectKey: prefix + 'user-file.txt', size: 3 },
      { objectKey: prefix + '../original.txt', size: 3 },
    ], nextCursor: 'page-2' }).mockResolvedValueOnce({ items: [
      { objectKey: first, size: 12 }, { objectKey: second, size: 30 },
    ] });
    await service.delete('dataset', 'test01', actor);
    expect(storage.listObjects.mock.calls).toEqual([
      [{ prefix, cursor: undefined, limit: 1000 }], [{ prefix, cursor: 'page-2', limit: 1000 }],
    ]);
    expect(tx.job.create).toHaveBeenCalledWith({ data: expect.objectContaining({
      payload: { datasetId: 'dataset', datasetName: 'test01', objectKeys: [
        'test01/upload/file.txt', 'manifests/dataset/v1.json', 'test01/.dataset', first, second,
      ] },
    }) });
    expect(storage.deleteObjects).not.toHaveBeenCalled();
  });
  it.each(['other/.folder-downloads/cache.tar.zst', 'test01/.folder-downloads-other/cache.tar.zst'])
    ('rejects an out-of-scope storage result %s before committing any deletion', async objectKey => {
      const { tx, service, storage } = setup();
      storage.listObjects.mockResolvedValue({ items: [{ objectKey, size: 1 }] });
      await expect(service.delete('dataset', 'test01', actor)).rejects.toThrow('范围无效');
      expect(tx.dataset.update).not.toHaveBeenCalled();
      expect(tx.robotFile.deleteMany).not.toHaveBeenCalled();
      expect(tx.job.create).not.toHaveBeenCalled();
      expect(storage.deleteObjects).not.toHaveBeenCalled();
    });
  it.each([['a', 'a'], ['a', 'b', 'a']])('rejects a looping storage cursor %j without partial deletion', async (...cursors) => {
    const { tx, service, storage } = setup();
    for (const nextCursor of cursors) storage.listObjects.mockResolvedValueOnce({ items: [], nextCursor });
    await expect(service.delete('dataset', 'test01', actor)).rejects.toThrow('分页无效');
    expect(storage.listObjects).toHaveBeenCalledTimes(cursors.length);
    expect(tx.dataset.update).not.toHaveBeenCalled();
    expect(tx.job.create).not.toHaveBeenCalled();
  });
  it('retains all metadata when scoped cache enumeration fails', async () => {
    const { tx, service, storage } = setup();
    storage.listObjects.mockRejectedValueOnce(new Error('storage unavailable'));
    await expect(service.delete('dataset', 'test01', actor)).rejects.toThrow('storage unavailable');
    expect(tx.dataset.update).not.toHaveBeenCalled();
    expect(tx.datasetVersion.deleteMany).not.toHaveBeenCalled();
    expect(tx.job.create).not.toHaveBeenCalled();
  });
  it('bounds a stalled storage listing within the existing transaction budget', async () => {
    jest.useFakeTimers();
    try {
      const { tx, service, storage } = setup();
      storage.listObjects.mockReturnValueOnce(new Promise(() => {}));
      const result = expect(service.delete('dataset', 'test01', actor)).rejects.toThrow('读取超时');
      await jest.advanceTimersByTimeAsync(10_001);
      await result;
      expect(tx.dataset.update).not.toHaveBeenCalled();
      expect(tx.job.create).not.toHaveBeenCalled();
    } finally { jest.useRealTimers(); }
  });

  it('commits an exact cleanup inventory with metadata removal and leaves borrowed files alone', async () => {
    const {tx, service, storage} = setup();
    await service.delete('dataset', 'test01', actor);
    expect(tx.robotFile.findMany).toHaveBeenCalledWith(expect.objectContaining({ where: { datasetId: 'dataset' } }));
    expect(tx.robotFile.deleteMany).toHaveBeenCalledWith({ where: { datasetId: 'dataset' } });
    expect(tx.job.create).toHaveBeenCalledWith({ data: expect.objectContaining({ type: 'DATASET_DELETE', requestedById: actor.id, payload: { datasetId: 'dataset', datasetName: 'test01', objectKeys: ['test01/upload/file.txt', 'manifests/dataset/v1.json', 'test01/.dataset'] } }) });
    expect(storage.deleteObjects).not.toHaveBeenCalled();
  });
});
describe('Retryable dataset storage cleanup', () => {
  function worker() {
    const prisma = { dataset: { findUnique: jest.fn().mockResolvedValue({ ...dataset, deletingAt: new Date() }), delete: jest.fn() } };
    const storage = { deleteObjects: jest.fn().mockResolvedValue(undefined) };
    return { prisma, storage, service: new StorageOperationsService(prisma as never, {} as never, storage as never) };
  }
  it('retains the name reservation on storage failure and can retry the same exact keys', async () => {
    const {prisma, storage, service} = worker(); storage.deleteObjects.mockRejectedValueOnce(new Error('offline'));
    await expect(service.executeDatasetDelete('dataset', ['test01/.dataset'])).rejects.toThrow('存储清理');
    expect(prisma.dataset.delete).not.toHaveBeenCalled();
    await service.executeDatasetDelete('dataset', ['test01/.dataset']);
    expect(prisma.dataset.delete).toHaveBeenCalledWith({ where: { id: 'dataset' } });
  });
  it('refuses to clean an active dataset', async () => {
    const {prisma, storage, service} = worker(); prisma.dataset.findUnique.mockResolvedValue(dataset);
    await expect(service.executeDatasetDelete('dataset', ['test01/.dataset'])).rejects.toThrow('尚未确认');
    expect(storage.deleteObjects).not.toHaveBeenCalled();
  });
  it('is idempotent after the dataset has already been removed', async () => {
    const {prisma, storage, service} = worker(); prisma.dataset.findUnique.mockResolvedValue(null);
    await expect(service.executeDatasetDelete('dataset', ['test01/.dataset'])).resolves.toEqual({ datasetId: 'dataset', deleted: true });
    expect(storage.deleteObjects).not.toHaveBeenCalled();
  });
});
