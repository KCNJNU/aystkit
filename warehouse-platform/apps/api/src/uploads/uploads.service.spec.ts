import { BadRequestException, ConflictException, ForbiddenException } from '@nestjs/common';
import { UploadStatus, UserRole } from '@prisma/client';
import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { MAX_UPLOAD_PARTS, UPLOAD_CHUNK_SIZE, UploadsService } from './uploads.service';

describe('UploadsService', () => {
  const content = Buffer.from('industrial resumable upload\n');
  const checksum = createHash('sha256').update(content).digest('hex');
  const session = {
    id: '00000000-0000-0000-0000-000000000010', ownerId: '00000000-0000-0000-0000-000000000020', robotId: '00000000-0000-0000-0000-000000000030',
    status: UploadStatus.UPLOADING, originalName: 'scan.log', fileType: 'log', mimeType: 'text/plain', fileSize: BigInt(content.length), checksum,
    recognitionType: 'acceptance', schemaVersion: '2.1', capturedAt: new Date('2026-08-16T00:00:00Z'), metadata: null,
    chunkSize: 8 * 1024 * 1024, totalParts: 1, maxFileSize: 2_147_483_648n, tempPrefix: 'uploads/session/parts/', finalObjectKey: null,
    robotFileId: null, errorMessage: null, expiresAt: new Date(Date.now() + 60_000), completedAt: null, createdAt: new Date(), updatedAt: new Date(),
    parts: [{ id: 'part', uploadSessionId: 'session', partNumber: 1, objectKey: 'uploads/session/parts/000001', size: content.length, checksum, createdAt: new Date() }],
  };
  const prisma = {
    uploadSession: { findUnique: jest.fn(), update: jest.fn(), updateMany: jest.fn(), create: jest.fn() }, uploadPart: { upsert: jest.fn() },
    robot: { findFirst: jest.fn(), findUnique: jest.fn(), findUniqueOrThrow: jest.fn() }, robotFile: { findFirst: jest.fn() }, job: { findFirst: jest.fn() }, $transaction: jest.fn(),
  };
  const jobs = { create: jest.fn() };
  const settings = { get: jest.fn().mockResolvedValue(2_147_483_648) };
  const storage = { putObject: jest.fn(), getObject: jest.fn(), deleteObject: jest.fn(), exists: jest.fn(), getPresignedDownloadUrl: jest.fn(), checkHealth: jest.fn(), statObject: jest.fn(), listObjects: jest.fn(), composeObject: jest.fn(), deleteObjects: jest.fn() };
  const service = new UploadsService(prisma as never, jobs as never, settings as never, storage);

  beforeEach(() => { jest.clearAllMocks(); prisma.uploadSession.findUnique.mockResolvedValue(session); prisma.uploadSession.update.mockResolvedValue(session); prisma.uploadSession.updateMany.mockResolvedValue({ count: 1 }); storage.exists.mockResolvedValue(false); prisma.$transaction.mockImplementation(async fn => fn({ $queryRaw: jest.fn(), robotFile: { count: jest.fn().mockResolvedValue(0) }, recycledFile: { count: jest.fn().mockResolvedValue(0) } })); });

  it('rejects a part when its SHA-256 does not match before ObjectStorage work', async () => {
    await expect(service.putPart(session.id, 1, `bytes 0-${content.length - 1}/${content.length}`, '0'.repeat(64), content, session.ownerId, UserRole.OPERATOR)).rejects.toBeInstanceOf(ConflictException);
    expect(storage.putObject).not.toHaveBeenCalled();
  });

  it('accepts an arbitrary file extension as binary without format-specific validation', async () => {
    prisma.robot.findFirst.mockResolvedValue({ id: session.robotId });
    prisma.robotFile.findFirst.mockResolvedValue(null);
    prisma.uploadSession.create.mockImplementation(({ data }) => Promise.resolve({ ...session, ...data, parts: [] }));

    const result = await service.create(session.ownerId, {
      robotId: session.robotId,
      originalName: 'diagnostics/archive.custom-format',
      mimeType: 'application/octet-stream',
      fileSize: content.length,
      checksum,
      recognitionType: 'unclassified',
      schemaVersion: '1.0',
      capturedAt: '2026-08-16T00:00:00Z',
      metadata: { relativePath: 'diagnostics/archive.custom-format' },
    });

    expect(result.fileType).toBe('binary');
    expect(result.originalName).toBe('diagnostics/archive.custom-format');
    expect(result.maxFileSize).toBe('0');
    expect(settings.get).not.toHaveBeenCalled();
  });

  it('retains the composed object when the database cannot confirm whether it was registered', async () => {
    prisma.robot.findUniqueOrThrow.mockResolvedValue({ externalId: 'CR-TEST' }); prisma.robotFile.findFirst.mockResolvedValue(null);
    storage.composeObject.mockResolvedValue(undefined); storage.getObject.mockResolvedValue(Readable.from([content])); storage.deleteObject.mockResolvedValue(undefined);
    const databaseError = new Error('database unavailable'); prisma.$transaction.mockRejectedValue(databaseError);
    await expect(service.finalize(session.id)).rejects.toBe(databaseError);
    const finalObjectKey = storage.composeObject.mock.calls[0][0] as string;
    expect(finalObjectKey).toMatch(/^raw\/CR-TEST\/2026\/08\/16\/.+\.log$/);
    expect(storage.deleteObject).not.toHaveBeenCalled();
    expect(prisma.uploadSession.updateMany).toHaveBeenCalledWith(expect.objectContaining({ where: expect.objectContaining({ id: session.id }), data: expect.objectContaining({ status: UploadStatus.FAILED }) }));
  });

  it('aborts by deleting exactly the persisted temporary part keys', async () => {
    storage.deleteObjects.mockResolvedValue(undefined);
    await service.abort(session.id, session.ownerId, UserRole.OPERATOR);
    expect(storage.deleteObjects).toHaveBeenCalledWith(['uploads/session/parts/000001']);
    expect(prisma.uploadSession.updateMany).toHaveBeenCalledWith(expect.objectContaining({ where: expect.objectContaining({ id: session.id }), data: { status: UploadStatus.ABORTED, errorMessage: null } }));
  });

  it('allows SUPER_ADMIN to manage an upload session owned by another user', async () => {
    storage.deleteObjects.mockResolvedValue(undefined);
    await service.abort(session.id, '00000000-0000-0000-0000-000000000099', UserRole.SUPER_ADMIN);
    expect(storage.deleteObjects).toHaveBeenCalledWith(['uploads/session/parts/000001']);
  });

  it('does not grant another non-admin user access to an upload session', async () => {
    await expect(service.abort(session.id, '00000000-0000-0000-0000-000000000099', UserRole.OPERATOR)).rejects.toBeInstanceOf(ForbiddenException);
    expect(storage.deleteObjects).not.toHaveBeenCalled();
  });

  it('returns the existing finalize job when a FINALIZING upload is retried', async () => {
    const existingJob = { id: 'existing-finalize-job', status: 'QUEUED', steps: [] };
    prisma.uploadSession.findUnique.mockResolvedValue({ ...session, status: UploadStatus.FINALIZING });
    prisma.job.findFirst.mockResolvedValue(existingJob);

    await expect(service.complete(session.id, session.ownerId, UserRole.OPERATOR)).resolves.toBe(existingJob);
    expect(jobs.create).not.toHaveBeenCalled();
  });


  const createInput = (fileSize: number, originalName = 'capture.mkv') => ({
    robotId: session.robotId, originalName, mimeType: 'application/octet-stream',
    fileSize, checksum, recognitionType: 'unclassified', schemaVersion: '1.0',
    capturedAt: '2026-08-16T00:00:00Z',
  });

  it('creates a resumable upload larger than2GiB with zero meaning no per-file policy limit', async () => {
    const fileSize = 3 * 1024 ** 3 + 17;
    prisma.robot.findFirst.mockResolvedValue({ id: session.robotId });
    prisma.robotFile.findFirst.mockResolvedValue(null);
    prisma.uploadSession.create.mockImplementation(({ data }) => Promise.resolve({ ...session, ...data, parts: [] }));
    const result = await service.create(session.ownerId, createInput(fileSize));
    expect(result.fileSize).toBe(String(fileSize));
    expect(result.maxFileSize).toBe('0');
    expect(result.chunkSize).toBe(8 * 1024 ** 2);
    expect(result.totalParts).toBe(385);
    expect(storage.putObject).not.toHaveBeenCalled();
    expect(storage.composeObject).not.toHaveBeenCalled();
  });

  it('allows a typed large upload when its administrator policy is unlimited', async () => {
    settings.get.mockResolvedValueOnce(0);
    prisma.robot.findFirst.mockResolvedValue({ id: session.robotId });
    prisma.robotFile.findFirst.mockResolvedValue(null);
    prisma.uploadSession.create.mockImplementation(({ data }) => Promise.resolve({ ...session, ...data, parts: [] }));
    const result = await service.create(session.ownerId, createInput(3 * 1024 ** 3, 'capture.log'));
    expect(result.maxFileSize).toBe('0');
    expect(result.fileType).toBe('log');
    expect(settings.get).toHaveBeenCalledWith('upload.logMaxBytes');
  });

  it('retains an explicitly configured per-type limit', async () => {
    settings.get.mockResolvedValueOnce(2 * 1024 ** 3);
    await expect(service.create(session.ownerId, createInput(3 * 1024 ** 3, 'capture.log')))
      .rejects.toBeInstanceOf(ConflictException);
    expect(prisma.uploadSession.create).not.toHaveBeenCalled();
  });

  it.each([0, -1, .5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])(
    'rejects an imprecise or invalid declared byte length %s before any object storage work', async fileSize => {
      await expect(service.create(session.ownerId, createInput(fileSize))).rejects.toBeInstanceOf(BadRequestException);
      expect(prisma.uploadSession.create).not.toHaveBeenCalled();
      expect(storage.putObject).not.toHaveBeenCalled();
    });

  it('validates a final part beyond the2GiB offset without buffering preceding parts', async () => {
    const tail = Buffer.alloc(17, 42), fileSize = 3 * 1024 ** 3 + tail.length;
    const partChecksum = createHash('sha256').update(tail).digest('hex');
    prisma.uploadSession.findUnique.mockResolvedValue({ ...session, fileSize: BigInt(fileSize),
      totalParts: 385, maxFileSize: 0n, parts: [] });
    await service.putPart(session.id, 385, `bytes ${3 * 1024 ** 3}-${fileSize - 1}/${fileSize}`,
      partChecksum, tail, session.ownerId, UserRole.OPERATOR);
    expect(storage.putObject).toHaveBeenCalledWith({ objectKey: 'uploads/session/parts/000385',
      content: tail, contentType: 'application/octet-stream' });
    expect(prisma.uploadPart.upsert).toHaveBeenCalledWith(expect.objectContaining({
      create: expect.objectContaining({ partNumber: 385, size: 17, checksum: partChecksum }) }));
  });


  it('rejects a file beyond the object-storage part count before creating a session', async () => {
    await expect(service.create(session.ownerId,
      createInput(MAX_UPLOAD_PARTS * UPLOAD_CHUNK_SIZE + 1))).rejects.toThrow('10000 parts');
    expect(prisma.robot.findFirst).not.toHaveBeenCalled();
    expect(prisma.uploadSession.create).not.toHaveBeenCalled();
    expect(storage.composeObject).not.toHaveBeenCalled();
  });

  it('accepts the final representable transport part without increasing per-request memory', async () => {
    prisma.robot.findFirst.mockResolvedValue({ id: session.robotId });
    prisma.robotFile.findFirst.mockResolvedValue(null);
    prisma.uploadSession.create.mockImplementation(({ data }) => Promise.resolve({ ...session, ...data, parts: [] }));
    const result = await service.create(session.ownerId, createInput(MAX_UPLOAD_PARTS * UPLOAD_CHUNK_SIZE));
    expect(result.totalParts).toBe(MAX_UPLOAD_PARTS);
    expect(result.chunkSize).toBe(UPLOAD_CHUNK_SIZE);
    expect(result.maxFileSize).toBe('0');
  });

  it.each([['json', 50], ['yaml', 10]] as const)(
    'limits %s syntax-parser memory separately and cleans up a rejected assembled object', async (kind, mebibytes) => {
      const bytes = mebibytes * 1024 ** 2 + 1;
      prisma.uploadSession.findUnique.mockResolvedValue({ ...session, originalName: `capture.${kind}`,
        fileType: kind, fileSize: BigInt(bytes), parts: [{ ...session.parts[0], size: bytes }] });
      prisma.robot.findUniqueOrThrow.mockResolvedValue({ externalId: 'CR-TEST' });
      const block = Buffer.alloc(1024 ** 2, 32);
      async function* chunks() {
        for (let count = 0; count < mebibytes; count += 1) yield block;
        yield Buffer.from('x');
      }
      const stream = Readable.from(chunks());
      storage.getObject.mockResolvedValue(stream);
      storage.deleteObject.mockResolvedValue(undefined);
      await expect(service.finalize(session.id)).rejects.toThrow('parser memory budget');
      expect(stream.destroyed).toBe(true);
      expect(prisma.$transaction).toHaveBeenCalled();
      expect(storage.deleteObject).toHaveBeenCalledWith(expect.stringContaining(`.${kind}`));
      expect(prisma.uploadSession.updateMany).toHaveBeenCalledWith(expect.objectContaining({
        data: expect.objectContaining({ status: UploadStatus.FAILED }) }));
    });

  it.each([['json', '{"camera":"synthetic"}'], ['yaml', 'camera: synthetic\n']] as const)(
    'preserves %s syntax validation within its parser budget', async (kind, text) => {
      const body = Buffer.from(text);
      const inspection = service as unknown as { inspectObject(key: string, type: string): Promise<{size: bigint;checksum:string}> };
      storage.getObject.mockResolvedValue(Readable.from([body.subarray(0, 3), body.subarray(3)]));
      await expect(inspection.inspectObject('isolated-fixture', kind)).resolves.toEqual({
        size: BigInt(body.length), checksum: createHash('sha256').update(body).digest('hex') });
      storage.getObject.mockResolvedValue(Readable.from([Buffer.from(kind === 'json' ? '{invalid' : '[1,2]')]));
      await expect(inspection.inspectObject('isolated-fixture', kind)).rejects.toBeInstanceOf(ConflictException);
    });
});
