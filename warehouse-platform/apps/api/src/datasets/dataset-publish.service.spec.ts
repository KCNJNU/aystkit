import { ConflictException } from '@nestjs/common';
import { DatasetVersionStatus } from '@prisma/client';
import { Readable } from 'node:stream';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatasetPublishService } from './dataset-publish.service';

const checksum = (content: Buffer) => createHash('sha256').update(content).digest('hex');

describe('DatasetPublishService', () => {
  const manifest = Buffer.from('{"datasetId":"dataset-id","version":1,"createdAt":"2026-08-12T00:00:00.000Z","files":[]}\n');
  const raw = Buffer.from('{"ok":true}');
  const prisma = { datasetVersion: { findUnique: jest.fn(), update: jest.fn() } };
  const storage = { getObject: jest.fn() };
  const dvc = { publish: jest.fn(), checkRemoteHealth: jest.fn(), resolveWorkspace: jest.fn() };
  const service = new DatasetPublishService(prisma as never, storage as never, dvc as never);
  const frozen = { id: 'version-id', datasetId: 'dataset-id', status: DatasetVersionStatus.FROZEN, manifestKey: 'manifests/dataset-id/v1/manifest.json', manifestChecksum: checksum(manifest), dataset: { name: 'nightly scan' }, version: 1, files: [{ robotFile: { id: 'file-id', objectKey: 'raw/CR-001/scan.json', originalName: 'scan.json', fileSize: BigInt(raw.length), checksum: checksum(raw) } }] };
  let workspace: string;

  beforeEach(async () => { jest.clearAllMocks(); workspace = await mkdtemp(join(tmpdir(), 'dataset-publish-')); dvc.resolveWorkspace.mockImplementation((path: string) => join(workspace, path)); storage.getObject.mockImplementation(async (key: string) => Readable.from([key.startsWith('manifests/') ? manifest : raw])); dvc.publish.mockResolvedValue({ dvcPath: 'data/dataset-workspace/nightly-scan-dataset-id/1.dvc', dvcHash: 'dvc-hash', gitCommit: 'commit', gitTag: 'dataset-dataset-id-v1.0.0' }); dvc.checkRemoteHealth.mockResolvedValue({ configured: true, reachable: true }); prisma.datasetVersion.update.mockResolvedValue({}); });
  afterEach(async () => { await rm(workspace, { recursive: true, force: true }); });

  it('downloads and validates frozen files before DVC publication', async () => {
    prisma.datasetVersion.findUnique.mockResolvedValue(frozen);
    await service.publish('version-id');
    expect(dvc.publish).toHaveBeenCalledWith(join('data', 'dataset-workspace', 'nightly-scan-dataset-id', '1'), 'nightly scan', 1, expect.any(Function), 'dataset-id', undefined);
    expect(prisma.datasetVersion.update).toHaveBeenCalledWith({ where: { id: 'version-id' }, data: expect.objectContaining({ status: DatasetVersionStatus.PUBLISHED, gitTag: 'dataset-dataset-id-v1.0.0' }) });
    expect(dvc.checkRemoteHealth.mock.invocationCallOrder[0]).toBeLessThan(prisma.datasetVersion.update.mock.invocationCallOrder[0]);
  });

  it('streams verified binary snapshots to disk without a complete-file buffer', async () => {
    const chunk = Buffer.alloc(64 * 1024, 3);
    const hash = createHash('sha256');
    for (let i = 0; i < 64; i += 1) hash.update(chunk);
    const file = { id: 'video-file', objectKey: 'raw/video.mkv', originalName: 'video.mkv',
      fileSize: BigInt(chunk.length) * 64n, checksum: hash.digest('hex') };
    prisma.datasetVersion.findUnique.mockResolvedValue({ ...frozen, files: [{ robotFile: file }] });
    storage.getObject.mockImplementation(async key => key.startsWith('manifests/') ? Readable.from([manifest])
      : Readable.from((function* () { for (let i = 0; i < 64; i += 1) yield chunk; })()));
    await service.publish('version-id', async (_name, action) => {
      if (_name !== 'Workspace Snapshot') return action();
      const concat = jest.spyOn(Buffer, 'concat').mockImplementation(() => { throw new Error('Unexpected complete-file buffer'); });
      try { return await action(); } finally { concat.mockRestore(); }
    });
    const target = join(workspace, 'data/dataset-workspace/nightly-scan-dataset-id/1/files/video-file.mkv');
    const actual = await readFile(target);
    expect(actual.length).toBe(Number(file.fileSize));
    expect(checksum(actual)).toBe(file.checksum);
    expect(await readdir(join(target, '..'))).toEqual(['video-file.mkv']);
  });

  it('preserves an existing verified snapshot and removes only its partial file after a mismatch', async () => {
    const path = join(workspace, 'data/dataset-workspace/nightly-scan-dataset-id/1/files');
    await mkdir(path, { recursive: true });
    const original = Buffer.from('existing verified snapshot');
    await writeFile(join(path, 'file-id.json'), original);
    prisma.datasetVersion.findUnique.mockResolvedValue({ ...frozen,
      files: [{ robotFile: { ...frozen.files[0].robotFile, fileSize: BigInt(raw.length + 1) } }] });
    await expect(service.publish('version-id')).rejects.toThrow('Size mismatch');
    expect(await readFile(join(path, 'file-id.json'))).toEqual(original);
    expect(await readdir(path)).toEqual(['file-id.json']);
    expect(dvc.publish).not.toHaveBeenCalled();
  });

  it('keeps different Chinese datasets in separate workspaces at the same version', async () => {
    prisma.datasetVersion.findUnique.mockResolvedValueOnce({ ...frozen, datasetId: 'dataset-a', dataset: { name: '地库甲' } })
      .mockResolvedValueOnce({ ...frozen, datasetId: 'dataset-b', dataset: { name: '地库乙' } });
    await service.publish('version-a');
    await service.publish('version-b');
    expect(dvc.resolveWorkspace.mock.calls.map(call => call[0])).toEqual([
      join('data', 'dataset-workspace', 'dataset-dataset-a', '1'),
      join('data', 'dataset-workspace', 'dataset-dataset-b', '1'),
    ]);
    expect(dvc.publish.mock.calls.map(call => call[4])).toEqual(['dataset-a', 'dataset-b']);
  });

  it('rejects non-frozen versions before reading objects or invoking DVC', async () => {
    prisma.datasetVersion.findUnique.mockResolvedValue({ ...frozen, status: DatasetVersionStatus.DRAFT });
    await expect(service.publish('version-id')).rejects.toBeInstanceOf(ConflictException);
    expect(storage.getObject).not.toHaveBeenCalled();
    expect(dvc.publish).not.toHaveBeenCalled();
  });

  it('keeps a version frozen when the DVC remote cannot be recovered', async () => {
    prisma.datasetVersion.findUnique.mockResolvedValue(frozen);
    dvc.checkRemoteHealth.mockResolvedValue({ configured: true, reachable: false });

    await expect(service.publish('version-id')).rejects.toBeInstanceOf(ConflictException);
    expect(prisma.datasetVersion.update).not.toHaveBeenCalled();
  });

  it('revalidates manifest integrity and remote health for an already published version', async () => {
    const published = { ...frozen, status: DatasetVersionStatus.PUBLISHED, gitTag: 'dataset-nightly-scan-v1.0.0', dvcPath: 'data/dataset-workspace/nightly-scan/1.dvc' };
    prisma.datasetVersion.findUnique.mockResolvedValue(published);

    await expect(service.publish('version-id')).resolves.toBe(published);
    expect(dvc.checkRemoteHealth).toHaveBeenCalled();
    expect(dvc.publish).not.toHaveBeenCalled();
    expect(prisma.datasetVersion.update).not.toHaveBeenCalled();
  });
});
