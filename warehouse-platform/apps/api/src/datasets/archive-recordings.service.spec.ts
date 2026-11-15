import { ConfigService } from '@nestjs/config';
import { JobStatus, JobType, UserRole } from '@prisma/client';
import * as childProcess from 'node:child_process';
import { createHash } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PassThrough, Readable } from 'node:stream';
import { AuthUser } from '../auth/auth.types';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { ObjectStorage } from '../storage/object-storage';
import { JobExecutorService } from '../worker/job-executor.service';
import { groupArchiveRecordings } from './archive-recording-groups';
import { ArchiveAssemblyBusyError, ArchiveAssemblyCancelledError, ArchiveRecordingsService } from './archive-recordings.service';
import { source } from './archive-recording-test-fixtures';

const actor: AuthUser = { id: 'user-a', role: UserRole.VIEWER, email: '', displayName: '', tenantId: 'tenant-a', projectIds: ['project-a'], mustChangePassword: false };
const dataset = { id: 'dataset-a', name: '地库_2026-10-31', tenantId: 'tenant-a', projectId: 'project-a', metadata: { type: 'garage-native-archive' }, deletingAt: null };
const first = source('a', 100000, 101000), second = source('b', 101040, 102000, 'garage_disabled');
const group = groupArchiveRecordings(dataset.id, '前相机', [first, second])[0];
const payload = { datasetId: dataset.id, path: '前相机', recordingId: group.id };
const output = Buffer.from('assembled-video-packets');
const result = { schemaVersion: 1, sha256: createHash('sha256').update(output).digest('hex'), size: String(output.length), frameCount: 4, durationSeconds: 2.04,
  codec: 'mjpeg', width: 960, height: 720, sourceCount: 2, indexSha256: '1'.repeat(64), losslessRemux: true, packetSha256Validated: true, timecodeScaleNs: 1000,
  attachments: [{ name: 'index.jsonl', sha256: '1'.repeat(64), bytes: 200 }], gaps: [] };

class FakeHelperService extends ArchiveRecordingsService {
  override async runHelper(input: string, path: string, resultPath: string, check: () => Promise<void>) {
    await check();
    const config = JSON.parse(await readFile(input, 'utf8'));
    expect(config.sources.map((item: {id: string}) => item.id)).toEqual(['a', 'b']);
    await writeFile(path, output); await writeFile(resultPath, JSON.stringify(result));
  }
}

function setup(work: string) {
  const objects = new Map<string, Buffer>([['raw/a', Buffer.from('source-a')], ['raw/b', Buffer.from('source-b')]]);
  const storage = {
    statObject: jest.fn(async (key: string) => { const value = objects.get(key); if (!value) throw new Error('Missing object'); return { objectKey: key, size: value.length }; }),
    getObject: jest.fn(async (key: string) => Readable.from([objects.get(key)!])),
    putObject: jest.fn(async ({ objectKey, content }: {objectKey: string; content: Buffer}) => { objects.set(objectKey, Buffer.from(content)); }),
    composeObject: jest.fn(async (key: string, parts: string[]) => { objects.set(key, Buffer.concat(parts.map(part => objects.get(part)!))); }),
    deleteObjects: jest.fn(async (keys: string[]) => { keys.forEach(key => objects.delete(key)); }),
    deleteObject: jest.fn(async (key: string) => { objects.delete(key); }),
  };
  const tx = { $queryRaw: jest.fn(), dataset: { findUnique: jest.fn(async () => dataset) }, job: { findUnique: jest.fn(async () => ({ lockedBy: 'worker-a', status: JobStatus.RUNNING, cancelRequested: false })) },
    robotFile: { findMany: jest.fn(async () => [first, second]), findFirst: jest.fn(async () => null), findUnique: jest.fn(async () => null), create: jest.fn(async (input: {data: object}) => ({ id: 'export-a', ...input.data })) },
    uploadSession: { findFirst: jest.fn(async () => null) }, datasetFolder: { upsert: jest.fn(async () => ({ id: 'folder-a' })) } };
  const prisma = { dataset: { findFirst: jest.fn(async () => dataset), findMany: jest.fn(async () => []) }, datasetFolder: { findMany: jest.fn(async () => [{ path: '前相机' }]) },
    robotFile: { findFirst: jest.fn(async () => null), findUnique: jest.fn(async () => null), findMany: jest.fn(async () => []) },
    job: { findMany: jest.fn(async () => []), findUnique: jest.fn(async () => ({ lockedBy: 'worker-a', status: JobStatus.RUNNING, cancelRequested: false })) },
    $transaction: jest.fn(async (fn: (client: typeof tx) => Promise<unknown>) => fn(tx)) };
  const jobs = { acquireLease: jest.fn(async () => true), releaseLease: jest.fn(async () => undefined), heartbeatLease: jest.fn(async () => ({ count: 1 })), create: jest.fn(async () => ({ id: 'job-new' })), retry: jest.fn() };
  const service = new FakeHelperService(prisma as unknown as PrismaService, jobs as unknown as JobsService, storage as unknown as ObjectStorage, new ConfigService({ ARCHIVE_VIDEO_WORK_DIR: work }));
  const groups = jest.spyOn(service as never, 'groups' as never) as unknown as jest.Mock;
  groups.mockResolvedValue([group]);
  return { service, prisma, jobs, storage, objects, tx, groups };
}

describe('archive continuous recording service', () => {
  let work: string;
  beforeEach(async () => { work = await mkdtemp(join(tmpdir(), 'archive-assembly-test-')); });
  afterEach(async () => { await rm(work, { recursive: true, force: true }); });
  it('scopes viewer reads to tenant and project, with no enqueue side effects', async () => {
    const { service, prisma, jobs } = setup(work);
    const listed = await service.list(dataset.id, '前相机', actor);
    expect(prisma.dataset.findFirst).toHaveBeenCalledWith({ where: { id: dataset.id, deletingAt: null, tenantId: actor.tenantId, projectId: { in: actor.projectIds } } });
    expect(listed.recordings[0]).toMatchObject({ state: 'pending', partCount: 2, file: null });
    expect(jobs.create).not.toHaveBeenCalled();
  });
  it('returns an already closed single part as ready without copying', async () => {
    const { service, groups, storage } = setup(work);
    groups.mockResolvedValue(groupArchiveRecordings(dataset.id, '前相机', [second]));
    expect((await service.list(dataset.id, '前相机', actor)).recordings[0]).toMatchObject({ state: 'ready', partCount: 1, file: { id: 'b', fileSize: '8' }, downloadableVideos: [] });
    expect(storage.composeObject).not.toHaveBeenCalled();
  });
  it('waits for an explicit end marker without claiming an unfinished rotation is still collecting', async () => {
    const { service, groups } = setup(work);
    groups.mockResolvedValue(groupArchiveRecordings(dataset.id, '前相机', [first]));
    expect((await service.list(dataset.id, '前相机', actor)).recordings[0]).toMatchObject({ state: 'waiting_end', file: null, closed: false });
  });
  it('does not expose an export or enqueue assembly before the end marker exists', async () => {
    const { service, groups, prisma, jobs } = setup(work);
    const unfinished = groupArchiveRecordings(dataset.id, '前相机', [first])[0];
    groups.mockResolvedValue([unfinished]);
    prisma.robotFile.findMany.mockResolvedValue([{ ...first, metadata: { recordingId: unfinished.id } } as never]);
    expect((await service.list(dataset.id, '前相机', actor)).recordings[0]).toMatchObject({ state: 'waiting_end', file: null, closed: false });
    await expect(service.requestAssembly(dataset.id, unfinished.id, actor)).rejects.toThrow('采集尚未结束');
    await expect(service.package(dataset.id, unfinished.id, actor)).rejects.toThrow('采集尚未封装完成');
    expect(jobs.create).not.toHaveBeenCalled();
  });
  it('keeps legacy segments with a missing close reason waiting for end confirmation', async () => {
    const { service, groups } = setup(work);
    groups.mockResolvedValue(groupArchiveRecordings(dataset.id, '前相机', [source('legacy', 0, 1000, '')]));
    expect((await service.list(dataset.id, '前相机', actor)).recordings[0]).toMatchObject({ state: 'waiting_end', file: null, closed: false });
  });
  it('offers an uploaded rotation video for download without changing closure, identity or starting work', async () => {
    const { service, groups, prisma, tx, jobs, storage } = setup(work);
    const unfinished = groupArchiveRecordings(dataset.id, '前相机', [first])[0];
    groups.mockResolvedValue([unfinished]);
    const listed = (await service.list(dataset.id, '前相机', actor)).recordings[0];
    expect(listed).toMatchObject({ id: unfinished.id, state: 'waiting_end', file: null, closed: false,
      downloadableVideos: [{ id: first.id, originalName: 'video.mkv', fileSize: '8',
        startedAt: (first.metadata as Record<string, unknown>).startedAt, endAt: (first.metadata as Record<string, unknown>).endAt }] });
    expect(Object.keys(listed.downloadableVideos[0]).sort()).toEqual(['endAt', 'fileSize', 'id', 'originalName', 'startedAt']);
    expect(prisma.robotFile.findMany).toHaveBeenCalledTimes(1); // Existing export lookup only.
    expect(prisma.$transaction).not.toHaveBeenCalled();
    expect(tx.uploadSession.findFirst).not.toHaveBeenCalled();
    expect(jobs.create).not.toHaveBeenCalled();
    for (const request of Object.values(storage)) expect(request).not.toHaveBeenCalled();
  });
  it('keeps multiple committed rotation videos ordered and downloadable while complete assembly remains gated', async () => {
    const { service, groups, jobs } = setup(work);
    const rotation = { ...second, metadata: { ...second.metadata as object, closeReason: 'rotation' } };
    const unfinished = groupArchiveRecordings(dataset.id, '前相机', [rotation, first])[0];
    groups.mockResolvedValue([unfinished]);
    const listed = (await service.list(dataset.id, '前相机', actor)).recordings[0];
    expect(listed.downloadableVideos.map(video => video.id)).toEqual(['a', 'b']);
    expect(listed).toMatchObject({ id: unfinished.id, closed: false, file: null });
    await expect(service.requestAssembly(dataset.id, unfinished.id, actor)).rejects.toThrow('采集尚未结束');
    await expect(service.package(dataset.id, unfinished.id, actor)).rejects.toThrow('采集尚未封装完成');
    expect(jobs.create).not.toHaveBeenCalled();
  });
  it('keeps a verified complete export as the main download without a duplicate parts list', async () => {
    const { service, prisma } = setup(work);
    prisma.robotFile.findMany.mockResolvedValue([{ ...first, id: 'complete-export', metadata: { recordingId: group.id } } as never]);
    expect((await service.list(dataset.id, '前相机', actor)).recordings[0]).toMatchObject({
      state: 'ready', closed: true, file: { id: 'complete-export' }, downloadableVideos: [],
    });
  });
  it('does not offer invalid groups or foreign, staged-looking or derived rows as saved-video downloads', async () => {
    const { service, groups } = setup(work);
    const bad = groupArchiveRecordings(dataset.id, '前相机', [source('bad', 100000, 101000, 'rotation', undefined, { receivedFrames: 0 })])[0];
    groups.mockResolvedValue([bad]);
    expect((await service.list(dataset.id, '前相机', actor)).recordings[0]).toMatchObject({ state: 'failed', downloadableVideos: [] });
    const unfinished = groupArchiveRecordings(dataset.id, '前相机', [first])[0];
    groups.mockResolvedValue([{ ...unfinished, sources: [first,
      { ...first, id: 'foreign-dataset', datasetId: 'other-dataset' }, { ...first, id: 'foreign-robot', robotId: 'other-robot' },
      { ...first, id: 'staging', originalName: 'video.mkv.partial' }, { ...first, id: 'empty', fileSize: 0n },
      { ...first, id: 'derived', metadata: { ...first.metadata as object, archiveRole: 'continuous-export' } }] }]);
    expect((await service.list(dataset.id, '前相机', actor)).recordings[0].downloadableVideos.map(video => video.id)).toEqual(['a']);
  });
  it('keeps the lightweight saved-video projection bounded to the existing sensor source limit', async () => {
    const { service, groups } = setup(work);
    const unfinished = groupArchiveRecordings(dataset.id, '前相机', [first])[0];
    groups.mockResolvedValue([{ ...unfinished, sources: Array.from({ length: 5001 }, (_, index) => ({ ...first, id: `part-${index}` })) }]);
    const listed = (await service.list(dataset.id, '前相机', actor)).recordings[0];
    expect(listed.downloadableVideos).toHaveLength(5000);
    expect(listed.downloadableVideos.at(-1)?.id).toBe('part-4999');
  });
  it('accepts the ROS/FFV1 frames index and camera-info attachments without renaming them', () => {
    const { service } = setup(work);
    const ros = { ...result, codec: 'ffv1', attachments: [{ name: 'frames.index.jsonl', sha256: result.indexSha256, bytes: 200 }, { name: 'camera-info.cdr.jsonl', sha256: '2'.repeat(64), bytes: 50 }] };
    const validate = (service as unknown as {validateResult: (value: unknown, group: unknown) => unknown}).validateResult.bind(service);
    expect(validate(ros, group)).toBe(ros);
    expect(() => validate({ ...ros, attachments: [...ros.attachments, { name: 'index.jsonl', sha256: result.indexSha256, bytes: 200 }] }, group)).toThrow('完整性验证');
  });
  it('uses DB deduplication to avoid duplicate assembly jobs', async () => {
    const { service, prisma, jobs } = setup(work);
    prisma.job.findUnique.mockResolvedValue({ id: 'job-old', status: JobStatus.QUEUED } as never);
    expect(await service.requestAssembly(dataset.id, group.id, { ...actor, role: UserRole.OPERATOR })).toEqual({ jobId: 'job-old' });
    expect(jobs.create).not.toHaveBeenCalled();
  });
  it('publishes only a fully read-back-verified export and retains every original', async () => {
    const { service, storage, objects, tx } = setup(work);
    expect(await service.assemble({ id: 'job-a', payload }, 'worker-a')).toMatchObject({ fileId: 'export-a', frameCount: 4 });
    expect(storage.composeObject).toHaveBeenCalledTimes(1);
    expect(tx.robotFile.create).toHaveBeenCalledWith({ data: expect.objectContaining({ recognitionType: 'garage-continuous-video', fileSize: BigInt(output.length), folderPath: '前相机/连续录像' }) });
    expect([...objects.keys()].filter(key => key.startsWith('uploads/'))).toEqual([]);
    expect(objects.get('raw/a')!.toString()).toBe('source-a'); expect(objects.get('raw/b')!.toString()).toBe('source-b');
  });
  it('rechecks independent session proof under the publication lock without changing source metadata', async () => {
    const { service, prisma, tx, groups } = setup(work);
    const sessionId = 'c'.repeat(32);
    const sources = [first, second].map(row => ({ ...row, metadata: { ...row.metadata as object, closeReason: 'rotation', captureSessionId: sessionId } }));
    const metadata = { ...dataset.metadata, archiveRecordingSessions: { [sessionId]: {
      robotId: first.robotId, startedAt: new Date(99000).toISOString(), endedAt: new Date(103000).toISOString(),
      reason: 'desktop_stopped', captureIncomplete: true,
    } } };
    const closed = groupArchiveRecordings(dataset.id, '前相机', sources, metadata.archiveRecordingSessions)[0];
    groups.mockRestore();
    prisma.dataset.findFirst.mockResolvedValue({ ...dataset, metadata } as never);
    tx.dataset.findUnique.mockResolvedValue({ ...dataset, metadata } as never);
    prisma.robotFile.findMany.mockResolvedValue(sources as never); tx.robotFile.findMany.mockResolvedValue(sources as never);
    await expect(service.assemble({ id: 'job-a', payload: { ...payload, recordingId: closed.id } }, 'worker-a')).resolves.toMatchObject({ fileId: 'export-a' });
    expect(tx.robotFile.create).toHaveBeenCalledTimes(1);
    expect(sources.every(row => row.metadata.closeReason === 'rotation')).toBe(true);
  });
  it('aborts before running a helper when cancelled or not the owning worker', async () => {
    const { service, prisma, storage } = setup(work);
    prisma.job.findUnique.mockResolvedValue({ lockedBy: 'worker-a', status: JobStatus.RUNNING, cancelRequested: true });
    await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).rejects.toBeInstanceOf(ArchiveAssemblyCancelledError);
    expect(storage.composeObject).not.toHaveBeenCalled();
  });
  it('defers resource contention instead of exhausting retry attempts', async () => {
    const { service, jobs } = setup(work); jobs.acquireLease.mockResolvedValue(false);
    await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).rejects.toBeInstanceOf(ArchiveAssemblyBusyError);
  });
  it('fails source SHA mismatch before publication and preserves source objects', async () => {
    const { service, objects, tx, storage } = setup(work); objects.set('raw/a', Buffer.from('corrupt!'));
    await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).rejects.toThrow('SHA-256');
    expect(tx.robotFile.create).not.toHaveBeenCalled(); expect(storage.composeObject).not.toHaveBeenCalled(); expect(objects.has('raw/a')).toBe(true);
  });
  it('does not publish when a source record changes during assembly', async () => {
    const { service, tx, objects } = setup(work); tx.robotFile.findMany.mockResolvedValue([{ ...first, checksum: 'f'.repeat(64) }, second]);
    await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).rejects.toThrow('原始录像已变化');
    expect(tx.robotFile.create).not.toHaveBeenCalled(); expect([...objects.keys()]).toEqual(['raw/a', 'raw/b']);
  });
  it('does not publish an obsolete group when an earlier or middle source arrives during assembly', async () => {
    const { service, tx } = setup(work);
    tx.robotFile.findMany.mockResolvedValue([source('late', 99000, 99960), first, second]);
    await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).rejects.toThrow('原始录像已变化');
    expect(tx.robotFile.create).not.toHaveBeenCalled();
  });
  it('stops a running helper and normally cancels an obsolete group after a late source arrives', async () => {
    jest.useFakeTimers({ doNotFake: ['nextTick', 'queueMicrotask', 'setImmediate'] });
    const { service, jobs, objects, storage, tx, groups } = setup(work);
    const lifecycle = { isCancelRequested: jest.fn(async () => false), beginStep: jest.fn(async () => true),
      completeStep: jest.fn(), heartbeat: jest.fn(async () => ({ count: 1 })),
      markCancelled: jest.fn(async () => undefined), finish: jest.fn(), fail: jest.fn() };
    Object.assign(jobs, lifecycle);
    const child = Object.assign(new EventEmitter(), { stderr: new PassThrough(),
      kill: jest.fn<boolean, [string]>(() => { setImmediate(() => child.emit('close', null, 'SIGTERM')); return true; }) });
    let spawned!: () => void;
    const started = new Promise<void>(resolve => { spawned = resolve; });
    const spawn = jest.spyOn(childProcess, 'spawn').mockImplementation(() => { spawned(); return child as never; });
    const helper = (ArchiveRecordingsService.prototype as unknown as {
      runHelper: FakeHelperService['runHelper'];
    }).runHelper;
    jest.spyOn(service, 'runHelper').mockImplementation((...args) => helper.call(service, ...args));
    const executor = new JobExecutorService(jobs as unknown as JobsService,
      {} as never, {} as never, {} as never, {} as never, service);
    const job = { id: 'job-a', type: JobType.ARCHIVE_VIDEO_ASSEMBLE, payload } as unknown as Parameters<JobExecutorService['execute']>[0];
    const execute = executor.execute(job, 'worker-a');
    try {
      await started;
      expect(groups).toHaveBeenCalledTimes(1);
      groups.mockResolvedValue(groupArchiveRecordings(dataset.id, '前相机', [source('late', 99000, 99960), first, second]));
      await jest.advanceTimersByTimeAsync(4_999);
      expect(child.kill).not.toHaveBeenCalled(); expect(groups).toHaveBeenCalledTimes(1);
      await jest.advanceTimersByTimeAsync(1);
      await execute;
      expect(child.kill).toHaveBeenCalledWith('SIGTERM'); expect(spawn).toHaveBeenCalledTimes(1);
      expect(lifecycle.markCancelled).toHaveBeenCalledWith('job-a', 'worker-a');
      expect(lifecycle.fail).not.toHaveBeenCalled(); expect(lifecycle.finish).not.toHaveBeenCalled();
      expect(jobs.releaseLease).toHaveBeenCalledWith('archive-video-assembly', 'worker-a:job-a');
      expect(storage.composeObject).not.toHaveBeenCalled(); expect(tx.robotFile.create).not.toHaveBeenCalled();
      expect([...objects.keys()]).toEqual(['raw/a', 'raw/b']);
      expect(objects.get('raw/a')!.toString()).toBe('source-a');
      expect(objects.get('raw/b')!.toString()).toBe('source-b');
      expect(await readdir(work)).toEqual([]);
      expect(jest.getTimerCount()).toBe(0);
    } finally { jest.useRealTimers(); jest.restoreAllMocks(); }
  });
  it('shares in-flight group checks and throttles inventories across streaming and helper checks', async () => {
    const { service, groups } = setup(work);
    let now = Date.now();
    const clock = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const helper = service.runHelper.bind(service);
    service.runHelper = async (input, path, resultPath, check) => {
      await Promise.all([check(), check(), check()]);
      expect(groups).toHaveBeenCalledTimes(1);
      now += 5_000;
      let complete!: (value: typeof group[]) => void;
      groups.mockImplementationOnce(() => new Promise<typeof group[]>(resolve => { complete = resolve; }));
      const checking = Promise.all([check(), check(), check()]);
      await Promise.resolve(); await Promise.resolve();
      expect(groups).toHaveBeenCalledTimes(2);
      now += 5_000;
      const laterCheck = check();
      await Promise.resolve(); await Promise.resolve();
      expect(groups).toHaveBeenCalledTimes(2);
      complete([group]);
      await checking; await laterCheck;
      now += 4_999;
      await Promise.all([check(), check(), check()]);
      expect(groups).toHaveBeenCalledTimes(2);
      now += 1;
      await check();
      expect(groups).toHaveBeenCalledTimes(3);
      await helper(input, path, resultPath, check);
    };
    try {
      await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).resolves.toMatchObject({ fileId: 'export-a' });
      expect(groups).toHaveBeenCalledTimes(3);
    } finally { clock.mockRestore(); }
  });
  it('keeps the same closed fingerprint valid when independent proof metadata is updated', async () => {
    const { service, groups, prisma, tx } = setup(work);
    let now = Date.now();
    const clock = jest.spyOn(Date, 'now').mockImplementation(() => now);
    const helper = service.runHelper.bind(service);
    service.runHelper = async (...args) => {
      now += 5_000;
      prisma.dataset.findFirst.mockResolvedValue({ ...dataset,
        metadata: { ...dataset.metadata, note: 'unrelated dataset update' } } as never);
      groups.mockResolvedValue([{ ...group, captureIncomplete: true, closureSessionId: 'c'.repeat(32) }]);
      await helper(...args);
    };
    try {
      await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).resolves.toMatchObject({ fileId: 'export-a' });
      expect(groups).toHaveBeenCalledTimes(2); expect(tx.robotFile.create).toHaveBeenCalledTimes(1);
    } finally { clock.mockRestore(); }
  });
  it.each(['open', 'invalid'])('cancels a fingerprint that becomes %s while retaining the originals', async state => {
    const { service, groups, objects, jobs, tx } = setup(work);
    let now = Date.now();
    const clock = jest.spyOn(Date, 'now').mockImplementation(() => now);
    service.runHelper = async (_input, _path, _resultPath, check) => {
      now += 5_000;
      groups.mockResolvedValue([{ ...group, closed: state !== 'open', error: state === 'invalid' ? 'source metadata invalid' : null }]);
      await check();
      throw new Error('Obsolete helper continued after its check');
    };
    try {
      await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).rejects.toBeInstanceOf(ArchiveAssemblyCancelledError);
      expect([...objects.keys()]).toEqual(['raw/a', 'raw/b']);
      expect(jobs.releaseLease).toHaveBeenCalledWith('archive-video-assembly', 'worker-a:job-a');
      expect(tx.robotFile.create).not.toHaveBeenCalled();
    } finally { clock.mockRestore(); }
  });
  it('checks cancellation under the publication job lock before creating an export record', async () => {
    const { service, tx } = setup(work);
    tx.job.findUnique.mockResolvedValue({ lockedBy: 'worker-a', status: JobStatus.RUNNING, cancelRequested: true });
    await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).rejects.toBeInstanceOf(ArchiveAssemblyCancelledError);
    expect(tx.robotFile.create).not.toHaveBeenCalled();
  });
  it('retains a failing historical path in the retry queue after cursor advancement', async () => {
    const { service, prisma, groups, jobs } = setup(work);
    prisma.dataset.findMany.mockResolvedValue([dataset] as never);
    groups.mockRejectedValueOnce(new Error('temporary storage failure')).mockResolvedValue([group]);
    await expect(service.scanClosedRecordings('worker-a')).rejects.toThrow('temporary storage failure');
    const state = service as unknown as { nextScanAt: number; pendingSensors: Map<string, {datasetId: string; path: string; retryAt: number}> };
    expect(state.pendingSensors.size).toBe(1);
    state.nextScanAt = 0; for (const item of state.pendingSensors.values()) item.retryAt = 0;
    prisma.job.findUnique.mockResolvedValue(null as never);
    await service.scanClosedRecordings('worker-a');
    expect(state.pendingSensors.size).toBe(0); expect(jobs.create).toHaveBeenCalledTimes(1);
  });
  it('discovers a new source even when twenty historical sensors are waiting to retry', async () => {
    const { service, prisma, jobs } = setup(work);
    const state = service as unknown as { pendingSensors: Map<string, {datasetId: string; path: string; retryAt: number}> };
    for (let index = 0; index < 20; index++) state.pendingSensors.set(`old-${index}`, { datasetId: 'old', path: `old-${index}`, retryAt: Date.now() + 60_000 });
    prisma.robotFile.findMany.mockResolvedValue([first] as never); prisma.job.findUnique.mockResolvedValue(null as never);
    await service.scanClosedRecordings('worker-a');
    expect(jobs.create).toHaveBeenCalledTimes(1); expect(state.pendingSensors.size).toBe(20);
  });
  it('discovers a late session end proof after file scanning and historical backfill have completed', async () => {
    const { service, prisma, groups, jobs } = setup(work);
    (service as unknown as { backfillDone: boolean }).backfillDone = true;
    prisma.robotFile.findMany.mockResolvedValue([]);
    prisma.dataset.findMany.mockResolvedValue([{ ...dataset, updatedAt: new Date(), metadata: {
      ...dataset.metadata, archiveRecordingSessions: { session: { reason: 'desktop_stopped' } },
    } }] as never);
    prisma.job.findUnique.mockResolvedValue(null as never);
    await service.scanClosedRecordings('worker-a');
    expect(groups).toHaveBeenCalledWith(dataset.id, '前相机');
    expect(jobs.create).toHaveBeenCalledTimes(1);
  });
  it('emits valid Prisma UUID predicates on the very first scan before either cursor exists', async () => {
    const { service, prisma } = setup(work);
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    const initialSourceQuery = jest.fn(async (query: {where: {OR: Array<{id?: {gt: string}}>}}) => {
      for (const branch of query.where.OR) if (branch.id) expect(branch.id.gt).toMatch(uuid);
      return [];
    });
    prisma.robotFile.findMany.mockImplementation(initialSourceQuery as never);
    await service.scanClosedRecordings('worker-a');
    expect(initialSourceQuery).toHaveBeenCalledTimes(1);
    expect(prisma.dataset.findMany).toHaveBeenCalledWith({ where: { deletingAt: null, metadata: { path: ['type'], equals: 'garage-native-archive' } }, orderBy: { id: 'asc' }, take: 2 });
    expect(initialSourceQuery.mock.calls[0][0].where.OR).toEqual([
      { createdAt: expect.objectContaining({ gt: expect.any(Date) }) },
      { createdAt: expect.any(Date), id: { gt: '00000000-0000-0000-0000-000000000000' } },
    ]);
  });
  it('shows a deleted export as failed and only regenerates after an explicit request', async () => {
    const { service, prisma, tx, jobs } = setup(work);
    const finished = { id: 'job-old', status: JobStatus.SUCCEEDED, resourceId: group.id };
    prisma.job.findMany.mockResolvedValue([finished] as never); prisma.job.findUnique.mockResolvedValue(finished as never);
    const job = { findUniqueOrThrow: jest.fn(async () => finished), update: jest.fn(async () => ({ ...finished, status: JobStatus.QUEUED })) };
    Object.assign(tx, { job, jobStep: { updateMany: jest.fn() } });
    expect((await service.list(dataset.id, '前相机', actor)).recordings[0]).toMatchObject({ state: 'failed', error: '完整录像已删除，可重新生成' });
    prisma.dataset.findMany.mockResolvedValue([dataset] as never);
    await service.scanClosedRecordings('worker-a');
    expect(job.update).not.toHaveBeenCalled(); expect(jobs.create).not.toHaveBeenCalled();
    expect(await service.requestAssembly(dataset.id, group.id, { ...actor, role: UserRole.OPERATOR })).toEqual({ jobId: 'job-old' });
    expect(job.update).toHaveBeenCalledWith({ where: { id: 'job-old' }, data: expect.objectContaining({ status: JobStatus.QUEUED, attempts: 0 }) });
  });
  it('cancels a storage stream that has stopped producing chunks', async () => {
    const { service, prisma, storage, tx } = setup(work);
    const idle = new PassThrough();
    storage.getObject.mockImplementationOnce(async () => {
      setTimeout(() => prisma.job.findUnique.mockResolvedValue({ lockedBy: 'worker-a', status: JobStatus.RUNNING, cancelRequested: true }), 20);
      return idle;
    });
    await expect(service.assemble({ id: 'job-a', payload }, 'worker-a')).rejects.toBeInstanceOf(ArchiveAssemblyCancelledError);
    expect(idle.destroyed).toBe(true); expect(tx.robotFile.create).not.toHaveBeenCalled();
  });
});
