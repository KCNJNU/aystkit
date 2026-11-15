import { ConfigService } from '@nestjs/config';
import { UserRole } from '@prisma/client';
import * as childProcess from 'node:child_process';
import { EventEmitter } from 'node:events';
import * as fs from 'node:fs/promises';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { PassThrough, Readable } from 'node:stream';
import { AuthUser } from '../auth/auth.types';
import { ArchiveRecordingsService } from './archive-recordings.service';
import { groupArchiveRecordings } from './archive-recording-groups';
import { source as videoSource } from './archive-recording-test-fixtures';

const actor: AuthUser = { id: 'user', role: UserRole.OPERATOR, email: '', displayName: '', tenantId: 'tenant', projectIds: ['project'], mustChangePassword: false };
const dataset = { id: 'dataset', name: '地库_2026-11-02', metadata: { type: 'garage-native-archive' } };
const id = 'a'.repeat(32), sensor = '雷达MID360';
const bytes = Buffer.from('compressed-lidar-fixture'), packed = Buffer.from('small-packaged-fixture');

function setup(base: string) {
  const source = { id: 'file', folderPath: `${sensor}/slot/${id}`, objectKey: 'lidar', originalName: 'part.db3.zst', fileSize: BigInt(bytes.length),
    checksum: 'b'.repeat(64), metadata: { segmentId: id, relativePath: 'compressed-bag/part.db3.zst',
      startedAt: '2026-11-02T01:00:00Z', endAt: '2026-11-02T01:00:01Z' } };
  const prisma = { dataset: { findFirst: jest.fn(async () => dataset) }, datasetFolder: { findMany: jest.fn(async () => [{ path: sensor }]) },
    robotFile: { findMany: jest.fn(async () => [source]), findFirst: jest.fn(async () => null) } };
  const storage = { exists: jest.fn(async () => false), getObject: jest.fn(async () => Readable.from([bytes])),
    putObject: jest.fn(async (input: { content: Buffer | Readable; contentLength?: number }) => {
      expect(Buffer.isBuffer(input.content)).toBe(false);
      expect(input.content).toBeInstanceOf(Readable);
      expect(input.contentLength).toBe(packed.length);
      const received: Buffer[] = [];
      for await (const chunk of input.content as Readable) received.push(Buffer.from(chunk));
      expect(Buffer.concat(received)).toEqual(packed);
    }), getPresignedDownloadUrl: jest.fn(async () => '/objects/package') };
  const service = new ArchiveRecordingsService(prisma as never, {} as never, storage as never, new ConfigService({ ARCHIVE_VIDEO_WORK_DIR: base }));
  const groups = jest.spyOn(service as never, 'groups' as never) as unknown as jest.Mock;
  groups.mockResolvedValue([]);
  const tar = jest.spyOn(service as never, 'createTarZstd' as never) as unknown as jest.Mock;
  tar.mockImplementation(async (scratch: string, path: string) => {
    expect(await readFile(join(scratch, 'compressed-bag/part.db3.zst'))).toEqual(bytes);
    expect(JSON.parse(await readFile(join(scratch, 'manifest.json'), 'utf8'))).toMatchObject({ recordingId: id, sensor });
    await writeFile(path, packed);
  });
  return { service, storage, tar, groups, prisma };
}

function processFixture() {
  const process = Object.assign(new EventEmitter(), { stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    kill: jest.fn<boolean, [string]>(() => true) });
  let closed = false;
  process.kill.mockImplementation(signal => {
    if (!closed) {
      closed = true;
      process.stdout.destroy(); process.stdin.destroy();
      process.emit('close', null, signal);
    }
    return true;
  });
  return process;
}

describe('bounded archive package publication', () => {
  let work: string;
  beforeEach(async () => { work = await mkdtemp(join(tmpdir(), 'archive-package-test-')); });
  afterEach(async () => { jest.restoreAllMocks(); jest.useRealTimers(); await rm(work, { recursive: true, force: true }); });

  it('creates a fresh work directory and streams the finished file with its exact length', async () => {
    const base = join(work, 'fresh-base'), { service, storage } = setup(base);
    expect(await service.package(dataset.id, id, actor)).toMatchObject({ url: '/objects/package', fileName: expect.stringMatching(/\.tar\.zst$/) });
    expect(storage.putObject).toHaveBeenCalledTimes(1);
    expect(await readdir(base)).toEqual([]);
  });

  it.each(['radar', 'video'])('repeatedly creates a real %s tar/zstd package without changing its input tree', async kind => {
    const base = join(work, 'staging'), { service, storage, tar, groups } = setup(base);
    let recordingId = id;
    const member = kind === 'radar' ? './compressed-bag/part.db3.zst' : './video.mkv';
    if (kind === 'video') {
      const video = videoSource('video', 100000, 101000, 'garage_disabled');
      const group = groupArchiveRecordings(dataset.id, sensor, [video])[0];
      recordingId = group.id;
      groups.mockResolvedValue([group]);
    }
    tar.mockRestore();
    const helper = (service as unknown as { createTarZstd: (directory: string, output: string) => Promise<void> }).createTarZstd.bind(service);
    jest.spyOn(service as never, 'createTarZstd' as never).mockImplementation((async (directory: string, output: string) => {
      const before = await fs.stat(directory, { bigint: true });
      // Deterministically catch the old layout, independently of tar timing.
      expect(relative(directory, output)).toMatch(/^\.\.[/\\]/);
      await helper(directory, output);
      const after = await fs.stat(directory, { bigint: true });
      expect(after.mtimeNs).toBe(before.mtimeNs);
    }) as never);
    const received = join(work, 'download.tar.zst');
    storage.putObject.mockImplementation(async input => {
      const chunks: Buffer[] = [];
      for await (const chunk of input.content as Readable) chunks.push(Buffer.from(chunk));
      const archive = Buffer.concat(chunks);
      expect(input.contentLength).toBe(archive.length);
      await writeFile(received, archive);
    });
    for (let attempt = 0; attempt < 3; attempt++) {
      await expect(service.package(dataset.id, recordingId, actor)).resolves.toMatchObject({ url: '/objects/package' });
      const members = childProcess.execFileSync('tar', ['--zstd', '-tf', received], { encoding: 'utf8' }).trim().split('\n');
      expect(members.filter(name => !name.endsWith('/')).sort()).toEqual([member, './manifest.json'].sort());
      expect(childProcess.execFileSync('tar', ['--zstd', '-xOf', received, member])).toEqual(bytes);
      const manifest = JSON.parse(childProcess.execFileSync('tar', ['--zstd', '-xOf', received, './manifest.json'], { encoding: 'utf8' }));
      expect(manifest).toMatchObject({ recordingId, datasetId: dataset.id, sensor });
      expect(await readdir(base)).toEqual([]);
    }
    expect(storage.putObject).toHaveBeenCalledTimes(3);
  });

  it('allows one package at a time and releases the slot after publication', async () => {
    const { service, tar, storage } = setup(work);
    let release!: () => void, entered!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    const started = new Promise<void>(resolve => { entered = resolve; });
    tar.mockImplementation(async (_scratch: string, path: string) => { entered(); await held; await writeFile(path, packed); });
    const first = service.package(dataset.id, id, actor);
    await started;
    await expect(service.package(dataset.id, id, actor)).rejects.toThrow('已有压缩包正在生成');
    expect(storage.getObject).toHaveBeenCalledTimes(1);
    release(); await first;
    await expect(service.package(dataset.id, id, actor)).resolves.toMatchObject({ url: '/objects/package' });
  });

  it('cleans temporary files and releases the slot when streamed storage publication fails', async () => {
    const { service, storage } = setup(work);
    let failedStream: Readable | undefined;
    storage.putObject.mockImplementationOnce(async input => { failedStream = input.content as Readable; throw new Error('storage offline'); });
    await expect(service.package(dataset.id, id, actor)).rejects.toThrow('storage offline');
    expect(failedStream?.destroyed).toBe(true);
    expect(await readdir(work)).toEqual([]);
    expect(storage.getPresignedDownloadUrl).not.toHaveBeenCalled();
    await expect(service.package(dataset.id, id, actor)).resolves.toMatchObject({ url: '/objects/package' });
  });

  it('rejects insufficient temporary space before reading source objects', async () => {
    const { service, storage, tar } = setup(work);
    jest.spyOn(fs, 'statfs').mockResolvedValue({ bavail: 1n, bsize: 1n } as never);
    await expect(service.package(dataset.id, id, actor)).rejects.toThrow('临时磁盘空间不足');
    expect(storage.getObject).not.toHaveBeenCalled(); expect(tar).not.toHaveBeenCalled();
  });

  it('preserves originals and cleans staging when a source read fails', async () => {
    const { service, storage, tar } = setup(work);
    storage.getObject.mockResolvedValueOnce(new Readable({ read() { this.destroy(new Error('source unavailable')); } }));
    await expect(service.package(dataset.id, id, actor)).rejects.toThrow('source unavailable');
    expect(storage.putObject).not.toHaveBeenCalled(); expect(tar).not.toHaveBeenCalled();
    expect(await readdir(work)).toEqual([]);
  });

  it('bounds zstd to one thread and terminates both children on timeout', async () => {
    jest.useFakeTimers({ doNotFake: ['nextTick', 'queueMicrotask', 'setImmediate'] });
    const tar = processFixture(), zstd = processFixture();
    const spawn = jest.spyOn(childProcess, 'spawn').mockReturnValueOnce(tar as never).mockReturnValueOnce(zstd as never);
    const service = new ArchiveRecordingsService({} as never, {} as never, {} as never, new ConfigService());
    const generate = (service as unknown as { createTarZstd: (dir: string, path: string) => Promise<void> }).createTarZstd(work, join(work, 'package.tar.zst'));
    const rejection = expect(generate).rejects.toThrow('压缩包生成超时');
    expect(spawn.mock.calls[1][1]).toEqual(['-T1', '-q', '-o', join(work, 'package.tar.zst')]);
    await jest.advanceTimersByTimeAsync(120_000);
    await rejection;
    expect(tar.kill).toHaveBeenCalledWith('SIGTERM'); expect(zstd.kill).toHaveBeenCalledWith('SIGTERM');
    expect(jest.getTimerCount()).toBe(0);
  });

  it('propagates pipe failure, terminates children and clears the timeout', async () => {
    const tar = processFixture(), zstd = processFixture();
    jest.spyOn(childProcess, 'spawn').mockReturnValueOnce(tar as never).mockReturnValueOnce(zstd as never);
    const service = new ArchiveRecordingsService({} as never, {} as never, {} as never, new ConfigService());
    const generate = (service as unknown as { createTarZstd: (dir: string, path: string) => Promise<void> }).createTarZstd(work, join(work, 'package.tar.zst'));
    const rejection = expect(generate).rejects.toThrow('pipe interrupted');
    tar.stdout.destroy(new Error('pipe interrupted'));
    await rejection;
    expect(tar.kill).toHaveBeenCalledWith('SIGTERM'); expect(zstd.kill).toHaveBeenCalledWith('SIGTERM');
  });

  it.each(['tar', 'zstd'])('identifies missing %s without leaving its companion running', async command => {
    const tar = processFixture(), zstd = processFixture();
    jest.spyOn(childProcess, 'spawn').mockReturnValueOnce(tar as never).mockReturnValueOnce(zstd as never);
    const service = new ArchiveRecordingsService({} as never, {} as never, {} as never, new ConfigService());
    const generate = (service as unknown as { createTarZstd: (dir: string, path: string) => Promise<void> }).createTarZstd(work, join(work, 'package.tar.zst'));
    const rejection = expect(generate).rejects.toMatchObject({ status: 503,
      response: { code: 'FOLDER_PACKAGE_COMMAND_MISSING', message: expect.stringContaining(command) } });
    (command === 'tar' ? tar : zstd).emit('error', Object.assign(new Error('spawn private path ENOENT'), { code: 'ENOENT' }));
    await rejection;
    expect(tar.kill).toHaveBeenCalledWith('SIGTERM'); expect(zstd.kill).toHaveBeenCalledWith('SIGTERM');
  });

  it('escalates the timeout when children ignore SIGTERM', async () => {
    jest.useFakeTimers({ doNotFake: ['nextTick', 'queueMicrotask', 'setImmediate'] });
    const tar = processFixture(), zstd = processFixture();
    for (const child of [tar, zstd]) {
      const terminate = child.kill.getMockImplementation()!;
      child.kill.mockImplementation(signal => signal === 'SIGTERM' ? true : terminate(signal));
    }
    jest.spyOn(childProcess, 'spawn').mockReturnValueOnce(tar as never).mockReturnValueOnce(zstd as never);
    const service = new ArchiveRecordingsService({} as never, {} as never, {} as never, new ConfigService());
    const generate = (service as unknown as { createTarZstd: (dir: string, path: string) => Promise<void> }).createTarZstd(work, join(work, 'package.tar.zst'));
    const rejection = expect(generate).rejects.toThrow('压缩包生成超时');
    await jest.advanceTimersByTimeAsync(121_000);
    await rejection;
    expect(tar.kill).toHaveBeenCalledWith('SIGKILL'); expect(zstd.kill).toHaveBeenCalledWith('SIGKILL');
    expect(jest.getTimerCount()).toBe(0);
  });

  it('waits for both successful child exits and the complete pipe before resolving', async () => {
    const tar = processFixture(), zstd = processFixture();
    jest.spyOn(childProcess, 'spawn').mockReturnValueOnce(tar as never).mockReturnValueOnce(zstd as never);
    const service = new ArchiveRecordingsService({} as never, {} as never, {} as never, new ConfigService());
    const generate = (service as unknown as { createTarZstd: (dir: string, path: string) => Promise<void> }).createTarZstd(work, join(work, 'package.tar.zst'));
    tar.stdout.end(Buffer.from('bounded-tar-stream'));
    tar.emit('close', 0, null); zstd.emit('close', 0, null);
    await generate;
    expect(tar.kill).not.toHaveBeenCalled(); expect(zstd.kill).not.toHaveBeenCalled();
  });
});
