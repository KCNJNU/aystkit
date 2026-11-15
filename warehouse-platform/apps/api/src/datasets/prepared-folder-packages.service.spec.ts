import { BadRequestException, ConflictException, ForbiddenException } from '@nestjs/common';
import { PreparedFolderPackage, Prisma, UserRole } from '@prisma/client';
import { AuthUser } from '../auth/auth.types';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { ArchiveRecordingsService } from './archive-recordings.service';
import { PreparedFolderPackagesService } from './prepared-folder-packages.service';

const actor: AuthUser = { id: 'user', email: 'operator@example.invalid', displayName: 'Operator', role: UserRole.OPERATOR,
  tenantId: 'tenant', projectIds: ['project'], mustChangePassword: false };
const now = new Date('2026-11-03T05:00:00Z');
const dataset = { id: 'dataset', name: '地库归档', tenantId: 'tenant', projectId: 'project', deletingAt: null,
  metadata: { type: 'garage-native-archive' } };
const snapshot = (path: string, taskId = (path === 'camera' ? 'a' : 'b').repeat(64)) => ({ schemaVersion: 1 as const,
  datasetId: dataset.id, datasetName: dataset.name, path, at: now.toISOString(), taskId,
  objectKey: `private/.folder-downloads/${path}/${taskId}/folder.tar.zst`, fileName: `${path}_20261103.tar.zst`,
  files: [{ id: `file-${path}`, hash: 'c'.repeat(64) }], folders: [path] });
const saved = (path: string) => snapshot(path) as unknown as Prisma.JsonValue;
const row = (path: string, state = 'QUEUED'): PreparedFolderPackage => ({ id: `prepared-${path}`, datasetId: dataset.id,
  path, state, requestedById: actor.id, taskId: snapshot(path).taskId, snapshot: saved(path),
  objectKey: snapshot(path).objectKey, fileName: snapshot(path).fileName, readyAt: state === 'READY' ? now : null,
  errorMessage: null, runOwner: state === 'PROCESSING' ? 'old-owner' : null, createdAt: now, updatedAt: now });
type Query = { where?: Record<string, unknown>; select?: unknown; orderBy?: unknown };
type Change = Query & { data: Partial<PreparedFolderPackage> };
type Control = { check: (force?: boolean) => Promise<void>; signal: AbortSignal };

function setup(initial: PreparedFolderPackage[] = []) {
  const rows = new Map(initial.map(item => [item.id, { ...item }]));
  const matches = (item: PreparedFolderPackage, query: Query) => Object.entries(query.where ?? {}).every(([key, value]) => {
    if (key === 'datasetId_path') {
      const scope = value as { datasetId: string; path: string };
      return item.datasetId === scope.datasetId && item.path === scope.path;
    }
    if (key === 'path' && value && typeof value === 'object') {
      const filter = value as { in?: string[]; notIn?: string[] };
      return (!filter.in || filter.in.includes(item.path)) && (!filter.notIn || !filter.notIn.includes(item.path));
    }
    return item[key as keyof PreparedFolderPackage] === value;
  });
  const preparedFolderPackage = {
    findUnique: jest.fn(async (query: Query) => [...rows.values()].find(item => matches(item, query)) ?? null),
    findMany: jest.fn(async (query: Query) => [...rows.values()].filter(item => matches(item, query))),
    findFirst: jest.fn(async (query: Query) => [...rows.values()].find(item => matches(item, query)) ?? null),
    deleteMany: jest.fn(async (query: Query) => {
      let count = 0;
      for (const item of rows.values()) if (matches(item, query)) { rows.delete(item.id); count++; }
      return { count };
    }),
    updateMany: jest.fn(async (query: Change) => {
      let count = 0;
      for (const item of rows.values()) if (matches(item, query)) { Object.assign(item, query.data); count++; }
      return { count };
    }),
    upsert: jest.fn(async (query: Query & { create: Partial<PreparedFolderPackage>; update: Partial<PreparedFolderPackage> }) => {
      const previous = [...rows.values()].find(item => matches(item, query));
      const result = previous ? Object.assign(previous, query.update)
        : { ...row(String(query.create.path)), ...query.create } as PreparedFolderPackage;
      rows.set(result.id, result);
      return { ...result };
    }),
  };
  const account = { ...actor, isActive: true, deletedAt: null, projectAccess: [{ projectId: 'project' }] };
  const prisma = { preparedFolderPackage, dataset: { findFirst: jest.fn().mockResolvedValue(dataset) },
    datasetFolder: { findMany: jest.fn().mockResolvedValue([{ path: 'camera' }, { path: 'lidar' }]),
      findUnique: jest.fn().mockResolvedValue({ parentPath: '' }) },
    user: { findFirst: jest.fn().mockResolvedValue(account) }, jobLease: { updateMany: jest.fn().mockResolvedValue({ count: 1 }) },
    $queryRaw: jest.fn().mockResolvedValue([{ id: dataset.id }]), $executeRaw: jest.fn().mockResolvedValue(1), $transaction: jest.fn() };
  prisma.$transaction.mockImplementation(async (action: (tx: typeof prisma) => Promise<unknown>) => {
    const before = [...rows].map(([id, item]) => [id, { ...item }] as const);
    try { return await action(prisma); }
    catch (error) { rows.clear(); for (const [id, item] of before) rows.set(id, item); throw error; }
  });
  const recordings = {
    capturePreparedFolderSnapshot: jest.fn<Promise<ReturnType<typeof snapshot>>, [string, string, AuthUser]>(async (_id, path) => snapshot(path)),
    validatePreparedFolderPackage: jest.fn<Promise<void>, [string, string, AuthUser, Prisma.JsonValue]>().mockResolvedValue(undefined),
    runPreparedFolderPackage: jest.fn<Promise<void>, [string, string, AuthUser, Prisma.JsonValue, Control]>().mockResolvedValue(undefined),
  };
  const jobs = { acquireLease: jest.fn().mockResolvedValue(true), releaseLease: jest.fn().mockResolvedValue(undefined) };
  const service = new PreparedFolderPackagesService(prisma as unknown as PrismaService,
    recordings as unknown as ArchiveRecordingsService, jobs as unknown as JobsService);
  const run = () => (service as unknown as { runNext: () => Promise<void> }).runNext();
  return { rows, prisma, recordings, jobs, service, run };
}

function holdRun(fixture: ReturnType<typeof setup>) {
  let entered!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  fixture.recordings.runPreparedFolderPackage.mockImplementation(async (_id, _path, _user, _saved, control) => {
    entered();
    await new Promise<void>((_resolve, reject) => {
      if (control.signal.aborted) reject(control.signal.reason);
      else control.signal.addEventListener('abort', () => reject(control.signal.reason), { once: true });
    });
  });
  return started;
}

describe('durable device folder preparations', () => {
  beforeEach(() => { jest.useFakeTimers({ now }); });
  afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });

  it('atomically queues every root device and returns public task identities without download URLs or internal keys', async () => {
    const f = setup();
    const result = await f.service.request(dataset.id, actor);
    expect(result).toMatchObject({ total: 2, ready: 0, active: 2, items: [
      { path: 'camera', state: 'QUEUED', taskId: 'a'.repeat(64) }, { path: 'lidar', state: 'QUEUED' },
    ] });
    expect(f.prisma.$queryRaw).toHaveBeenCalledTimes(1);
    expect(f.prisma.datasetFolder.findMany).toHaveBeenCalledWith(expect.objectContaining({
      where: { datasetId: dataset.id, parentPath: '' }, take: 65,
    }));
    expect(JSON.stringify(result)).not.toMatch(/objectKey|runOwner|requestedById|snapshot|private\/|"url"/);
    expect(f.recordings.runPreparedFolderPackage).not.toHaveBeenCalled();
  });

  it('rolls back an incomplete batch instead of accepting only the first devices', async () => {
    const f = setup();
    f.recordings.capturePreparedFolderSnapshot.mockImplementation(async (_id, path) => {
      if (path === 'lidar') throw new ConflictException('源文件已变化');
      return snapshot(path);
    });
    await expect(f.service.request(dataset.id, actor)).rejects.toThrow('源文件已变化');
    expect(f.rows.size).toBe(0);
  });

  it('keeps active snapshots immutable and reuses a validated READY cache with the same task identity', async () => {
    const f = setup([row('camera', 'PROCESSING'), row('lidar', 'READY')]);
    const result = await f.service.request(dataset.id, actor);
    expect(result).toMatchObject({ ready: 1, active: 1 });
    expect(f.recordings.capturePreparedFolderSnapshot).toHaveBeenCalledTimes(1);
    expect(f.recordings.capturePreparedFolderSnapshot).toHaveBeenCalledWith(dataset.id, 'lidar', actor);
    expect(f.prisma.preparedFolderPackage.upsert).not.toHaveBeenCalled();
    expect(f.rows.get('prepared-camera')?.runOwner).toBe('old-owner');
  });

  it('requeues an explicitly requested newer snapshot while GET keeps an earlier valid snapshot ready', async () => {
    const f = setup([row('camera', 'READY')]);
    f.prisma.datasetFolder.findMany.mockResolvedValue([{ path: 'camera' }]);
    f.recordings.capturePreparedFolderSnapshot.mockResolvedValue(snapshot('camera', 'd'.repeat(64)));
    expect(await f.service.list(dataset.id, actor)).toMatchObject({ ready: 1 });
    expect(f.recordings.capturePreparedFolderSnapshot).not.toHaveBeenCalled();
    expect(await f.service.request(dataset.id, actor)).toMatchObject({ ready: 0, active: 1,
      items: [{ state: 'QUEUED', taskId: 'd'.repeat(64) }] });
  });

  it('returns a failed READY validation safely without mutating durable state', async () => {
    const f = setup([row('camera', 'READY')]);
    f.recordings.validatePreparedFolderPackage.mockRejectedValue(new Error('storage credentials private/key'));
    const result = await f.service.list(dataset.id, actor);
    expect(result).toMatchObject({ ready: 0, items: [{ state: 'FAILED', readyAt: null,
      errorMessage: '设备压缩包准备失败，请稍后重试' }] });
    expect(f.rows.get('prepared-camera')?.state).toBe('READY');
    expect(f.prisma.preparedFolderPackage.updateMany).not.toHaveBeenCalled();
  });

  it('hides removed or moved device roots immediately and retires only their preparation metadata on the next request', async () => {
    const f = setup([row('camera', 'READY'), row('retired-device', 'PROCESSING')]);
    f.prisma.datasetFolder.findMany.mockResolvedValue([{ path: 'camera' }]);
    expect(await f.service.list(dataset.id, actor)).toMatchObject({ total: 1, ready: 1, active: 0,
      items: [{ path: 'camera' }] });
    expect(f.rows.has('prepared-retired-device')).toBe(true);
    expect(await f.service.request(dataset.id, actor)).toMatchObject({ total: 1, ready: 1, active: 0 });
    expect(f.rows.has('prepared-retired-device')).toBe(false);
    expect(f.prisma.preparedFolderPackage.deleteMany).toHaveBeenCalledWith({
      where: { datasetId: dataset.id, path: { notIn: ['camera'] } },
    });
    expect(f.recordings.runPreparedFolderPackage).not.toHaveBeenCalled();
  });

  it('an empty current root inventory reports an empty batch and retires old device metadata', async () => {
    const f = setup([row('camera', 'READY')]);
    f.prisma.datasetFolder.findMany.mockResolvedValue([]);
    expect(await f.service.list(dataset.id, actor)).toMatchObject({ items: [], total: 0, ready: 0, active: 0 });
    expect(await f.service.request(dataset.id, actor)).toMatchObject({ items: [], total: 0, ready: 0, active: 0 });
    expect(f.rows.size).toBe(0);
  });

  it('rejects non-readers, inaccessible/non-archive datasets, and oversized root inventories before accepting work', async () => {
    const f = setup();
    await expect(f.service.request(dataset.id, { ...actor, role: UserRole.BASIC })).rejects.toBeInstanceOf(ForbiddenException);
    f.prisma.dataset.findFirst.mockResolvedValueOnce(null);
    await expect(f.service.list(dataset.id, actor)).rejects.toThrow('无访问权限');
    f.prisma.dataset.findFirst.mockResolvedValueOnce({ ...dataset, metadata: {} });
    await expect(f.service.request(dataset.id, actor)).rejects.toBeInstanceOf(BadRequestException);
    f.prisma.datasetFolder.findMany.mockResolvedValue(Array.from({ length: 65 }, (_, index) => ({ path: `device-${index}` })));
    await expect(f.service.request(dataset.id, actor)).rejects.toThrow('超过64个');
    expect(f.rows.size).toBe(0);
  });

  it('starts only when explicitly requested in the API and never runs from the worker context', async () => {
    const previous = process.env.RUNTIME_SERVICE_NAME;
    const f = setup();
    try {
      process.env.RUNTIME_SERVICE_NAME = 'worker';
      f.service.start();
      expect(f.jobs.acquireLease).not.toHaveBeenCalled();
      process.env.RUNTIME_SERVICE_NAME = 'api';
      f.service.start(); f.service.start();
      await Promise.resolve();
      expect(f.jobs.acquireLease).toHaveBeenCalledTimes(1);
      await f.service.onModuleDestroy();
      expect(jest.getTimerCount()).toBe(0);
    } finally {
      if (previous === undefined) delete process.env.RUNTIME_SERVICE_NAME; else process.env.RUNTIME_SERVICE_NAME = previous;
    }
  });

  it('does not claim or recover durable rows until it owns the global lease', async () => {
    const f = setup([row('camera', 'PROCESSING')]);
    f.jobs.acquireLease.mockResolvedValue(false);
    await f.run();
    expect(f.prisma.preparedFolderPackage.findMany).not.toHaveBeenCalled();
    expect(f.recordings.runPreparedFolderPackage).not.toHaveBeenCalled();
    expect(f.rows.get('prepared-camera')?.runOwner).toBe('old-owner');
    expect(jest.getTimerCount()).toBe(0);
  });

  it('recovers a previous owner after restart and publishes only after current-user authorization', async () => {
    const f = setup([row('camera', 'PROCESSING')]);
    await f.run();
    const owner = f.jobs.acquireLease.mock.calls[0][1];
    expect(owner).toMatch(/^api:/);
    expect(f.prisma.preparedFolderPackage.updateMany).toHaveBeenCalledWith({
      where: { id: 'prepared-camera', state: 'PROCESSING', runOwner: 'old-owner' },
      data: { state: 'QUEUED', runOwner: null, readyAt: null, errorMessage: null },
    });
    expect(f.prisma.user.findFirst).toHaveBeenCalledWith({ where: { id: actor.id, isActive: true, deletedAt: null },
      include: { projectAccess: { select: { projectId: true } } } });
    expect(f.rows.get('prepared-camera')).toMatchObject({ state: 'READY', runOwner: null });
    expect(f.jobs.releaseLease).toHaveBeenCalledWith('prepared-folder-packages:global', owner);
    expect(jest.getTimerCount()).toBe(0);
  });

  it('throttles chunk checks, permits a forced publication check, and keeps concurrent checks single-flight', async () => {
    const f = setup([row('camera')]);
    f.recordings.runPreparedFolderPackage.mockImplementation(async (_id, _path, _user, _saved, control) => {
      await Promise.all(Array.from({ length: 100 }, () => control.check()));
      expect(f.prisma.user.findFirst).toHaveBeenCalledTimes(1);
      let release!: (value: unknown) => void;
      f.prisma.user.findFirst.mockReturnValueOnce(new Promise(resolve => { release = resolve; }));
      const forced = control.check(true);
      await Promise.resolve(); await Promise.resolve();
      const waiting = Promise.all(Array.from({ length: 100 }, () => control.check()));
      expect(f.prisma.user.findFirst).toHaveBeenCalledTimes(2);
      release({ ...actor, projectAccess: [{ projectId: 'project' }] });
      await forced; await waiting;
    });
    await f.run();
    expect(f.prisma.user.findFirst).toHaveBeenCalledTimes(3);
    expect(f.rows.get('prepared-camera')?.state).toBe('READY');
  });

  it('aborts compression on permission revocation and marks the durable row failed rather than resuming forever', async () => {
    const f = setup([row('camera')]);
    const entered = holdRun(f), running = f.run();
    await entered;
    f.prisma.user.findFirst.mockResolvedValue(null);
    await jest.advanceTimersByTimeAsync(5_000);
    await running;
    const control = f.recordings.runPreparedFolderPackage.mock.calls[0][4];
    expect(control.signal.aborted).toBe(true);
    expect(control.signal.reason).toBeInstanceOf(ForbiddenException);
    expect(f.rows.get('prepared-camera')).toMatchObject({ state: 'FAILED', runOwner: null,
      errorMessage: '预打包申请账号已停用或删除' });
    expect(jest.getTimerCount()).toBe(0);
  });

  it('stops active compression when a device root is deleted or moved without waiting for another POST', async () => {
    const f = setup([row('camera')]);
    const entered = holdRun(f), running = f.run();
    await entered;
    f.prisma.datasetFolder.findUnique.mockResolvedValue({ parentPath: 'moved-parent' });
    await jest.advanceTimersByTimeAsync(5_000);
    await running;
    expect(f.recordings.runPreparedFolderPackage.mock.calls[0][4].signal.aborted).toBe(true);
    expect(f.rows.get('prepared-camera')).toMatchObject({ state: 'FAILED', runOwner: null,
      errorMessage: '设备文件夹已删除或移走' });
    expect(jest.getTimerCount()).toBe(0);
  });

  it('stops the old executor on lease loss without overwriting its replacement owner', async () => {
    const f = setup([row('camera')]);
    const entered = holdRun(f), running = f.run();
    await entered;
    Object.assign(f.rows.get('prepared-camera')!, { state: 'PROCESSING', runOwner: 'replacement-api' });
    f.prisma.$executeRaw.mockResolvedValue(0);
    await jest.advanceTimersByTimeAsync(15_000);
    await running;
    expect(f.rows.get('prepared-camera')).toMatchObject({ state: 'PROCESSING', runOwner: 'replacement-api' });
    expect(f.prisma.preparedFolderPackage.updateMany.mock.calls.some(([query]) => query.data.state === 'FAILED')).toBe(false);
    expect(jest.getTimerCount()).toBe(0);
  });

  it('treats a rejected heartbeat as resumable lease loss even while the durable row still belongs to this attempt', async () => {
    const f = setup([row('camera')]);
    const entered = holdRun(f), running = f.run();
    await entered;
    f.prisma.$executeRaw.mockResolvedValue(0);
    await jest.advanceTimersByTimeAsync(15_000);
    await running;
    expect(f.prisma.$executeRaw).toHaveBeenCalledTimes(1);
    expect(f.rows.get('prepared-camera')).toMatchObject({ state: 'PROCESSING', runOwner: expect.stringMatching(/^api:/) });
    expect(jest.getTimerCount()).toBe(0);
  });

  it('ignores a successful but late renewal after expiry instead of rearming timers or reviving work', async () => {
    const f = setup([row('camera')]);
    let renew!: (count: number) => void;
    f.prisma.$executeRaw.mockReturnValue(new Promise(resolve => { renew = resolve; }));
    const entered = holdRun(f), running = f.run();
    await entered;
    await jest.advanceTimersByTimeAsync(70_000);
    await running;
    renew(1);
    await Promise.resolve(); await Promise.resolve();
    expect(f.rows.get('prepared-camera')?.state).toBe('PROCESSING');
    expect(f.recordings.runPreparedFolderPackage).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  });

  it('expires locally when renewal and a forced database check hang, retaining PROCESSING for restart', async () => {
    const f = setup([row('camera')]);
    let entered!: () => void;
    const started = new Promise<void>(resolve => { entered = resolve; });
    f.recordings.runPreparedFolderPackage.mockImplementation(async (_id, _path, _user, _saved, control) => {
      f.prisma.preparedFolderPackage.findUnique.mockReturnValue(new Promise(() => undefined));
      entered();
      await control.check(true);
    });
    f.prisma.$executeRaw.mockReturnValue(new Promise(() => undefined));
    const running = f.run();
    await started;
    await jest.advanceTimersByTimeAsync(70_000);
    await running;
    expect(f.prisma.$executeRaw).toHaveBeenCalledTimes(1);
    const renewal = f.prisma.$executeRaw.mock.calls[0][0] as Prisma.Sql;
    expect(renewal.text).toContain('"expiresAt" > NOW()');
    expect(renewal.values).toContain('prepared-folder-packages:global');
    expect(f.rows.get('prepared-camera')?.state).toBe('PROCESSING');
    expect(jest.getTimerCount()).toBe(0);
  });

  it('aborts shutdown work without permanently failing it and clears runner timers', async () => {
    const f = setup([row('camera')]);
    const entered = holdRun(f), running = f.run();
    await entered;
    await f.service.onModuleDestroy();
    await running;
    expect(f.rows.get('prepared-camera')?.state).toBe('PROCESSING');
    expect(f.jobs.releaseLease).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  });
});
