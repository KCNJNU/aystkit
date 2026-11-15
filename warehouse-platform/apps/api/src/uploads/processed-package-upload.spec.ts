import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { ConflictException } from '@nestjs/common';
import { UploadStatus } from '@prisma/client';
import { UploadsService } from './uploads.service';
import { validateProcessedPackage } from './processed-package';

jest.mock('./processed-package', () => ({
  ...jest.requireActual('./processed-package'), validateProcessedPackage: jest.fn(),
}));

const robotId = '365c77c6-0029-4406-a504-739014d0a672';
const folder = '处理好的数据_20261112_153045';

function fixture(folderPath = folder, originalName = 'processed.zip') {
  const content = Buffer.from('isolated package bytes');
  const checksum = createHash('sha256').update(content).digest('hex');
  const dataset = { id: 'dataset', name: '地库_2026-11-12', tenantId: 'tenant', projectId: 'project',
    deletingAt: null, metadata: { robotId } };
  let session = { id: 'upload', ownerId: 'operator', robotId, datasetId: dataset.id,
    folderPath, originalName, fileType: 'binary', mimeType: 'application/zip',
    fileSize: BigInt(content.length), checksum, status: UploadStatus.FINALIZING,
    capturedAt: new Date('2026-11-12T08:00:00Z'), metadata: null, robotFileId: null as string | null,
    totalParts: 1, chunkSize: 8 * 1024 ** 2, finalObjectKey: 'fixture/object.zip',
    parts: [{ partNumber: 1, objectKey: 'uploads/part', size: content.length, checksum }],
  };
  const prisma = {
    robot: { findFirst: jest.fn(async () => ({ id: robotId, tenantId: 'tenant', projectId: 'project' })),
      findUniqueOrThrow: jest.fn(async () => ({ externalId: 'fixture-robot' })) },
    dataset: { findFirst: jest.fn(async () => dataset), findUniqueOrThrow: jest.fn(async () => dataset),
      update: jest.fn() },
    datasetFolder: { findUnique: jest.fn(async ({ where }) =>
      where.datasetId_path.path.endsWith('.zip') ? null : { id: 'folder' }), upsert: jest.fn(async () => ({ id: 'folder' })) },
    uploadSession: { findUnique: jest.fn(async () => ({ ...session })),
      findUniqueOrThrow: jest.fn(async () => ({ ...session })),
      findFirst: jest.fn(async () => null), create: jest.fn(async ({ data }) => ({ ...data, parts: [] })),
      updateMany: jest.fn(async ({ data }) => { session = { ...session, ...data }; return { count: 1 }; }),
      update: jest.fn(async ({ data }) => { session = { ...session, ...data }; return session; }) },
    robotFile: { findFirst: jest.fn(async () => null), findUnique: jest.fn(async () => null),
      count: jest.fn(async () => 0), create: jest.fn(async () => ({ id: 'accepted-file' })) },
    recycledFile: { count: jest.fn(async () => 0) },
    datasetVersion: { findFirst: jest.fn(async () => ({ id: 'draft', status: 'DRAFT' })),
      updateMany: jest.fn(async () => ({ count: 1 })) },
    datasetVersionFile: { createMany: jest.fn(async () => ({ count: 1 })) },
    $queryRaw: jest.fn(async () => []), $transaction: jest.fn(),
  };
  prisma.$transaction.mockImplementation(async callback => callback(prisma));
  const storage = { getObject: jest.fn(async () => Readable.from([content])),
    composeObject: jest.fn(async () => undefined), exists: jest.fn(async () => true),
    putObject: jest.fn(), deleteObject: jest.fn(async () => undefined), deleteObjects: jest.fn(async () => undefined) };
  const settings = { get: jest.fn(async () => 0) };
  const service = new UploadsService(prisma as never, {} as never, settings as never, storage as never);
  return { service, prisma, storage, session: () => session,
    input: { datasetId: dataset.id, folderPath, robotId, originalName, mimeType: 'application/zip',
      fileSize: content.length, checksum, recognitionType: 'unclassified', schemaVersion: '1.0',
      capturedAt: '2026-11-12T08:00:00Z' } };
}

describe('processed package upload enforcement', () => {
  beforeEach(() => jest.mocked(validateProcessedPackage).mockReset());

  it.each([
    [folder, 'before.jpg'], [`${folder}/nested`, 'result.json'], ['', `${folder}/before.jpg`],
  ])('rejects loose files at the effective destination %s / %s before storage', async (path, name) => {
    const f = fixture(path, name);
    await expect(f.service.create('operator', f.input)).rejects.toThrow(/ZIP/);
    expect(f.prisma.uploadSession.create).not.toHaveBeenCalled();
    expect(f.storage.putObject).not.toHaveBeenCalled();
  });

  it.each([8 * 1024 ** 3 + 1, 64 * 1024 ** 3])('allows a processed ZIP declared as %s bytes without allocating its content', async size => {
    const f = fixture();
    await expect(f.service.create('operator', { ...f.input, fileSize: size }))
      .resolves.toMatchObject({ fileSize: String(size), maxFileSize: '0' });
    expect(f.prisma.uploadSession.create).toHaveBeenCalledWith(expect.objectContaining({
      data: expect.objectContaining({ fileSize: BigInt(size), chunkSize: 8 * 1024 ** 2 }),
    }));
  });

  it('does not add package-content validation to original sensor uploads', async () => {
    const f = fixture('原相机前/原相机前_14:00-14:01/capture', 'video.mkv');
    await expect(f.service.finalize('upload')).resolves.toMatchObject({ robotFileId: 'accepted-file' });
    expect(validateProcessedPackage).not.toHaveBeenCalled();
    expect(f.prisma.robotFile.create).toHaveBeenCalled();
  });

  it('validates the finished object before publishing a processed file or draft reference', async () => {
    const f = fixture();
    jest.mocked(validateProcessedPackage).mockResolvedValue({ beforeImages: 1, afterImages: 1, jsonFiles: 1 });
    await expect(f.service.finalize('upload')).resolves.toMatchObject({ robotFileId: 'accepted-file' });
    expect(validateProcessedPackage).toHaveBeenCalledWith(f.storage, 'fixture/object.zip', f.input.fileSize);
    expect(jest.mocked(validateProcessedPackage).mock.invocationCallOrder[0])
      .toBeLessThan(f.prisma.robotFile.create.mock.invocationCallOrder[0]);
    expect(f.session().status).toBe(UploadStatus.COMPLETED);
  });

  it('rejects old loose-file sessions at finalization even if the browser or create check was bypassed', async () => {
    const f = fixture(folder, 'result.json');
    await expect(f.service.finalize('upload')).rejects.toThrow(/ZIP/);
    expect(f.prisma.robotFile.create).not.toHaveBeenCalled();
    expect(f.prisma.datasetVersionFile.createMany).not.toHaveBeenCalled();
    expect(f.storage.deleteObject).toHaveBeenCalledWith('fixture/object.zip');
    expect(f.session().status).toBe(UploadStatus.FAILED);
  });

  it('does not register incomplete packages and preserves the reason for the upload queue', async () => {
    const f = fixture();
    jest.mocked(validateProcessedPackage).mockRejectedValue(new ConflictException('压缩包缺少：处理后图像、.json 文件'));
    await expect(f.service.finalize('upload')).rejects.toThrow('压缩包缺少');
    expect(f.prisma.robotFile.create).not.toHaveBeenCalled();
    expect(f.prisma.datasetVersionFile.createMany).not.toHaveBeenCalled();
    expect(f.storage.deleteObject).toHaveBeenCalledWith('fixture/object.zip');
    expect(f.prisma.uploadSession.updateMany).toHaveBeenLastCalledWith(expect.objectContaining({
      data: { status: UploadStatus.FAILED, errorMessage: '压缩包缺少：处理后图像、.json 文件' },
    }));
  });
});
