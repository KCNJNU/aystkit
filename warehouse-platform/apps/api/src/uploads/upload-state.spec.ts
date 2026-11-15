import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { UploadStatus, UserRole } from '@prisma/client';
import { UploadsService } from './uploads.service';

function setup(status: UploadStatus = UploadStatus.UPLOADING) {
  const content = Buffer.from('archive');
  const checksum = createHash('sha256').update(content).digest('hex');
  let current = { id: 'upload', ownerId: 'operator', robotId: 'robot', status, expiresAt: new Date(Date.now() + 60_000),
    robotFileId: null as string | null, finalObjectKey: 'dataset/video.mkv', originalName: 'video.mkv', fileType: 'binary', mimeType: 'video/x-matroska',
    capturedAt: new Date(), fileSize: BigInt(content.length), checksum, chunkSize: 8 * 1024 ** 2, totalParts: 1, tempPrefix: 'uploads/upload/parts/',
    metadata: null, folderPath: '', parts: [{ partNumber: 1, objectKey: 'uploads/upload/parts/000001', size: content.length, checksum }] };
  const updateMany = jest.fn(async ({ where, data }) => {
    if (where.status?.in && !where.status.in.includes(current.status)) return { count: 0 };
    current = { ...current, ...data }; return { count: 1 };
  });
  const uploadSession = { findUnique: jest.fn(async () => ({ ...current })),
    update: jest.fn(async ({ data }) => { current = { ...current, ...data }; return current; }), updateMany };
  const robot = { findUniqueOrThrow: jest.fn(async () => ({ externalId: 'robot', tenantId: null, projectId: null })) };
  const robotFile = { findFirst: jest.fn(async () => null), create: jest.fn(async () => ({ id: 'file' })), count: jest.fn(async () => 0) };
  const prisma = { uploadSession, uploadPart: { upsert: jest.fn() }, robot, robotFile,
    recycledFile: { count: jest.fn(async () => 0) }, $queryRaw: jest.fn(async () => []),
    job: { findFirst: jest.fn(async () => null) }, $transaction: jest.fn() };
  prisma.$transaction.mockImplementation(async fn => fn(prisma));
  const jobs = { create: jest.fn(async () => ({ id: 'job' })) };
  const storage = { composeObject: jest.fn(async () => undefined), putObject: jest.fn(), deleteObjects: jest.fn(async () => undefined), deleteObject: jest.fn(async () => undefined),
    exists: jest.fn(async key => !key.startsWith('uploads/')), getObject: jest.fn(async () => Readable.from([content])) };
  const service = new UploadsService(prisma as never, jobs as never, {} as never, storage as never);
  return { service, prisma, storage, jobs, content, checksum, state: () => current };
}

describe('upload terminal state protection', () => {
  it.each([UploadStatus.ABORTED, UploadStatus.EXPIRED])('never finalizes a %s session', async status => {
    const f = setup(status);
    await expect(f.service.finalize('upload')).rejects.toThrow(/aborted|expired/);
    expect(f.storage.composeObject).not.toHaveBeenCalled();
    expect(f.prisma.robotFile.create).not.toHaveBeenCalled();
  });

  it('rejects abort while finalization owns the parts', async () => {
    const f = setup(UploadStatus.FINALIZING);
    await expect(f.service.abort('upload', 'operator', UserRole.OPERATOR)).rejects.toThrow('finalizing');
    expect(f.storage.deleteObjects).not.toHaveBeenCalled();
    expect(f.state().status).toBe(UploadStatus.FINALIZING);
  });

  it('does not resurrect an aborted session when an earlier part write completes', async () => {
    const f = setup();
    let release!: () => void;
    let writing!: () => void;
    const started = new Promise<void>(resolve => { writing = resolve; });
    f.storage.putObject.mockImplementation(() => new Promise(resolve => { writing(); release = () => resolve(undefined); }));
    const part = f.service.putPart('upload', 1, `bytes 0-${f.content.length - 1}/${f.content.length}`, f.checksum, f.content, 'operator', UserRole.OPERATOR);
    await started;
    await f.service.abort('upload', 'operator', UserRole.OPERATOR);
    release();
    await expect(part).rejects.toThrow();
    expect(f.state().status).toBe(UploadStatus.ABORTED);
  });

  it('keeps completed finalization idempotent without reading parts or objects', async () => {
    const f = setup(UploadStatus.COMPLETED);
    f.prisma.uploadSession.findUnique.mockResolvedValue({ ...f.state(), robotFileId: 'file' });
    await expect(f.service.finalize('upload')).resolves.toEqual({ robotFileId: 'file', uploadSessionId: 'upload' });
    expect(f.storage.composeObject).not.toHaveBeenCalled();
  });

  it('does not compensate an object accepted by another execution after verification fails', async () => {
    const f = setup();
    f.storage.getObject.mockImplementation(async () => {
      await f.prisma.uploadSession.update({ data: { status: UploadStatus.COMPLETED, robotFileId: 'accepted-file' } });
      f.prisma.robotFile.count.mockResolvedValue(1);
      throw new Error('connection closed after a different execution committed');
    });
    await expect(f.service.finalize('upload')).rejects.toThrow('connection closed');
    expect(f.storage.deleteObject).not.toHaveBeenCalled();
    expect(f.state().status).toBe(UploadStatus.COMPLETED);
  });

  it('protects accepted data moved to the recycle bin before stale compensation', async () => {
    const f = setup();
    f.storage.getObject.mockRejectedValue(new Error('connection closed'));
    f.prisma.recycledFile.count.mockResolvedValue(1);
    await expect(f.service.finalize('upload')).rejects.toThrow('connection closed');
    expect(f.storage.deleteObject).not.toHaveBeenCalled();
  });

  it('reuses the accepted file when another execution completes before registration', async () => {
    const f = setup();
    f.storage.getObject.mockImplementation(async () => {
      await f.prisma.uploadSession.update({ data: { status: UploadStatus.COMPLETED, robotFileId: 'accepted-file' } });
      return Readable.from([f.content]);
    });
    await expect(f.service.finalize('upload')).resolves.toMatchObject({ robotFileId: 'accepted-file' });
    expect(f.prisma.robotFile.create).not.toHaveBeenCalled();
    expect(f.storage.deleteObject).not.toHaveBeenCalled();
  });

  it('does not register an object removed by cleanup before the registration lock', async () => {
    const f = setup();
    f.storage.exists.mockResolvedValue(false);
    await expect(f.service.finalize('upload')).rejects.toThrow('disappeared');
    expect(f.prisma.robotFile.create).not.toHaveBeenCalled();
  });

  it('creates the completion job in the transaction that acquires FINALIZING', async () => {
    const f = setup();
    await f.service.complete('upload', 'operator', UserRole.OPERATOR);
    expect(f.jobs.create).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ resourceId: 'upload' }), f.prisma);
    expect(f.prisma.$transaction).toHaveBeenCalledTimes(1);
    expect(f.state().status).toBe(UploadStatus.FINALIZING);
  });
});
