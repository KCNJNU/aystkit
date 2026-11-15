import { BadRequestException, ForbiddenException, HttpException, Injectable, Logger, NotFoundException, OnModuleDestroy } from '@nestjs/common';
import { Prisma } from '@prisma/client';
import { randomUUID } from 'node:crypto';
import { AuthUser } from '../auth/auth.types';
import { canReadData } from '../auth/permissions';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { warehouseScope } from '../warehouse/warehouse-query.service';
import { ArchiveRecordingsService } from './archive-recordings.service';

const MAX_DEVICE_FOLDERS = 64;
const LEASE_KEY = 'prepared-folder-packages:global';
const LEASE_TTL_MS = 70_000;
const HEARTBEAT_MS = 15_000;
const REAUTHORIZE_MS = 5_000;
const SCAN_MS = 5_000;
type PreparedRow = Prisma.PreparedFolderPackageGetPayload<Record<string, never>>;
type PreparedState = 'QUEUED' | 'PROCESSING' | 'READY' | 'FAILED';
type PreparedItem = {
  id: string; path: string; taskId: string; state: PreparedState; fileName: string;
  readyAt: Date | null; errorMessage: string | null; createdAt: Date; updatedAt: Date;
};

class PreparationInterrupted extends Error {}

@Injectable()
export class PreparedFolderPackagesService implements OnModuleDestroy {
  private readonly logger = new Logger(PreparedFolderPackagesService.name);
  private started = false;
  private stopping = false;
  private scanTimer?: ReturnType<typeof setTimeout>;
  private active?: Promise<void>;
  private controller?: AbortController;

  constructor(private readonly prisma: PrismaService, private readonly recordings: ArchiveRecordingsService,
    private readonly jobs: JobsService) {}

  /** One transaction accepts the complete device batch; leaving the page cannot truncate it. */
  async request(datasetId: string, user: AuthUser) {
    this.assertReader(user);
    const items = await this.prisma.$transaction(async tx => {
      await tx.$queryRaw(Prisma.sql`SELECT id FROM "Dataset" WHERE id = ${datasetId}::uuid FOR UPDATE`);
      await this.dataset(datasetId, user, tx);
      const folders = await tx.datasetFolder.findMany({ where: { datasetId, parentPath: '' },
        orderBy: { path: 'asc' }, take: MAX_DEVICE_FOLDERS + 1, select: { path: true } });
      if (folders.length > MAX_DEVICE_FOLDERS) throw new BadRequestException('设备文件夹超过64个，请分批准备');
      // Retired or moved device roots stop contributing to the batch. Removing
      // their durable row also fences an active runner at its next check.
      await tx.preparedFolderPackage.deleteMany({ where: { datasetId, path: { notIn: folders.map(folder => folder.path) } } });
      const result: PreparedItem[] = [];
      for (const folder of folders) {
        const where = { datasetId_path: { datasetId, path: folder.path } };
        const previous = await tx.preparedFolderPackage.findUnique({ where });
        if (previous && ['QUEUED', 'PROCESSING'].includes(previous.state)) {
          result.push(this.item(previous));
          continue;
        }
        const snapshot = await this.recordings.capturePreparedFolderSnapshot(datasetId, folder.path, user);
        if (previous?.state === 'READY' && previous.taskId === snapshot.taskId) {
          try {
            await this.recordings.validatePreparedFolderPackage(datasetId, folder.path, user, previous.snapshot);
            result.push(this.item(previous));
            continue;
          } catch { /* An explicit preparation request can repair a missing or invalid cache. */ }
        }
        const data = { requestedById: user.id, taskId: snapshot.taskId,
          snapshot: snapshot as unknown as Prisma.InputJsonValue, objectKey: snapshot.objectKey,
          fileName: snapshot.fileName, state: 'QUEUED', readyAt: null, errorMessage: null, runOwner: null };
        const row = await tx.preparedFolderPackage.upsert({ where,
          create: { datasetId, path: folder.path, ...data }, update: data });
        result.push(this.item(row));
      }
      return result;
    }, { timeout: 60_000 });
    this.wake();
    return this.summary(items);
  }

  async list(datasetId: string, user: AuthUser) {
    this.assertReader(user);
    await this.dataset(datasetId, user);
    const folders = await this.prisma.datasetFolder.findMany({ where: { datasetId, parentPath: '' }, select: { path: true } });
    const rows = await this.prisma.preparedFolderPackage.findMany({
      where: { datasetId, path: { in: folders.map(folder => folder.path) } }, orderBy: { path: 'asc' } });
    const items: PreparedItem[] = [];
    for (const row of rows) {
      const item = this.item(row);
      if (row.state === 'READY') {
        try { await this.recordings.validatePreparedFolderPackage(datasetId, row.path, user, row.snapshot); }
        catch (error) { item.state = 'FAILED'; item.readyAt = null; item.errorMessage = this.safeError(error); }
      }
      items.push(item);
    }
    return this.summary(items);
  }

  /** Called explicitly by the HTTP bootstrap. The shared worker module never starts this runner. */
  start() {
    if (process.env.RUNTIME_SERVICE_NAME !== 'api' || this.started) return;
    this.started = true;
    this.stopping = false;
    this.wake();
  }

  async onModuleDestroy() {
    this.stopping = true;
    clearTimeout(this.scanTimer);
    this.controller?.abort(new PreparationInterrupted('API正在停止，预打包将在重启后继续'));
    await this.active;
  }

  private wake() {
    if (!this.started || this.stopping || this.active) return;
    clearTimeout(this.scanTimer);
    const running = this.runNext().catch(error => {
      this.logger.warn(`Prepared folder scan deferred: ${this.safeError(error)}`);
    });
    this.active = running;
    void running.finally(() => {
      if (this.active === running) this.active = undefined;
      if (!this.stopping) {
        this.scanTimer = setTimeout(() => this.wake(), SCAN_MS);
        this.scanTimer.unref();
      }
    });
  }

  private async runNext() {
    if (this.stopping) return;
    const owner = `api:${process.pid}:${randomUUID()}`;
    let validUntil = Date.now() + LEASE_TTL_MS;
    let acquired = false;
    const controller = new AbortController();
    this.controller = controller;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    let renewing = false;
    const interrupt = (message: string) => controller.abort(new PreparationInterrupted(message));
    const assertLease = () => {
      if (this.stopping && !controller.signal.aborted) interrupt('API正在停止');
      if (Date.now() >= validUntil && !controller.signal.aborted) interrupt('预打包租约已过期');
      if (controller.signal.aborted) throw controller.signal.reason;
    };
    const armDeadline = () => {
      clearTimeout(deadline);
      deadline = setTimeout(() => interrupt('预打包租约续期超时'), Math.max(0, validUntil - Date.now()));
      deadline.unref();
    };
    armDeadline();
    const heartbeat = setInterval(() => {
      if (!acquired || renewing || controller.signal.aborted) return;
      renewing = true;
      const renewalStarted = Date.now();
      void this.prisma.$executeRaw(Prisma.sql`UPDATE "JobLease"
        SET "heartbeatAt" = NOW(), "expiresAt" = ${new Date(renewalStarted + LEASE_TTL_MS)}, "updatedAt" = NOW()
        WHERE key = ${LEASE_KEY} AND owner = ${owner} AND "expiresAt" > NOW()`)
        .then(count => {
          // A delayed response must never revive this attempt after its local deadline.
          if (controller.signal.aborted) return;
          if (count !== 1 || Date.now() >= validUntil) { interrupt('预打包租约已失效'); return; }
          validUntil = renewalStarted + LEASE_TTL_MS;
          armDeadline();
        }).catch(() => interrupt('预打包租约续期失败')).finally(() => { renewing = false; });
    }, HEARTBEAT_MS);
    heartbeat.unref();
    let row: PreparedRow | undefined;
    let checkTimer: ReturnType<typeof setInterval> | undefined;
    try {
      assertLease();
      acquired = await this.abortable(this.jobs.acquireLease(LEASE_KEY, owner, LEASE_TTL_MS), controller.signal);
      if (!acquired) return;
      assertLease();
      // Holding the global lease proves every previous PROCESSING owner is stale.
      const abandoned = await this.abortable(this.prisma.preparedFolderPackage.findMany({
        where: { state: 'PROCESSING' }, select: { id: true, runOwner: true } }), controller.signal);
      for (const previous of abandoned) {
        assertLease();
        // The observed owner also fences a delayed recovery query from a newer run.
        await this.abortable(this.prisma.preparedFolderPackage.updateMany({
          where: { id: previous.id, state: 'PROCESSING', runOwner: previous.runOwner },
          data: { state: 'QUEUED', runOwner: null, readyAt: null, errorMessage: null } }), controller.signal);
      }
      assertLease();
      const candidate = await this.abortable(this.prisma.preparedFolderPackage.findFirst({ where: { state: 'QUEUED' },
        orderBy: [{ createdAt: 'asc' }, { id: 'asc' }] }), controller.signal);
      if (!candidate) return;
      const claimed = await this.abortable(this.prisma.preparedFolderPackage.updateMany({ where: { id: candidate.id, state: 'QUEUED' },
        data: { state: 'PROCESSING', runOwner: owner, readyAt: null, errorMessage: null } }), controller.signal);
      if (claimed.count !== 1) return;
      row = candidate;
      let user: AuthUser | undefined;
      let checkedAt = -Infinity;
      let checking: Promise<void> | undefined;
      const inspect = async () => {
        assertLease();
        const current = await this.prisma.preparedFolderPackage.findUnique({ where: { id: candidate.id },
          select: { state: true, runOwner: true, requestedById: true } });
        assertLease();
        if (!current || current.state !== 'PROCESSING' || current.runOwner !== owner) {
          interrupt('预打包任务所有权已改变');
          assertLease();
        }
        if (current!.requestedById !== candidate.requestedById)
          throw new ForbiddenException('预打包申请账号已改变，请重新准备');
        user = await this.currentUser(candidate.requestedById);
        await this.dataset(candidate.datasetId, user);
        const folder = await this.prisma.datasetFolder.findUnique({
          where: { datasetId_path: { datasetId: candidate.datasetId, path: candidate.path } }, select: { parentPath: true } });
        if (!folder || folder.parentPath !== '') throw new NotFoundException('设备文件夹已删除或移走');
        assertLease();
        checkedAt = Date.now();
      };
      const check = async (force = false) => {
        assertLease();
        while (checking) {
          await checking;
          if (!force) return;
        }
        if (!force && Date.now() - checkedAt < REAUTHORIZE_MS) return;
        const pending = this.abortable(inspect(), controller.signal).catch(error => {
          // Archive cancels I/O and suppresses stale cleanup when this signal is aborted.
          controller.abort(error);
          throw error;
        }).finally(() => { if (checking === pending) checking = undefined; });
        checking = pending;
        await pending;
      };
      checkTimer = setInterval(() => { void check().catch(() => undefined); }, REAUTHORIZE_MS);
      checkTimer.unref();
      await check(true);
      await this.recordings.runPreparedFolderPackage(candidate.datasetId, candidate.path, user!, candidate.snapshot,
        { check, signal: controller.signal });
      await check(true);
      const finished = await this.abortable(this.prisma.preparedFolderPackage.updateMany({ where: { id: candidate.id, state: 'PROCESSING', runOwner: owner },
        data: { state: 'READY', readyAt: new Date(), errorMessage: null, runOwner: null } }), controller.signal);
      if (finished.count !== 1) { interrupt('预打包任务所有权已改变'); assertLease(); }
    } catch (error) {
      // Interrupted attempts remain durable and recover after the next API acquires the lease.
      const interrupted = this.stopping || controller.signal.reason instanceof PreparationInterrupted;
      if (row && !interrupted) {
        await this.abortable(this.prisma.preparedFolderPackage.updateMany({ where: { id: row.id, state: 'PROCESSING', runOwner: owner },
          data: { state: 'FAILED', readyAt: null, errorMessage: this.safeError(error), runOwner: null } }), AbortSignal.timeout(5_000));
      } else if (!row && !controller.signal.aborted) throw error;
    } finally {
      // Pending renewals cannot re-arm timers or revive a completed attempt.
      controller.abort(new PreparationInterrupted('本次预打包执行已结束'));
      clearInterval(heartbeat);
      clearInterval(checkTimer);
      clearTimeout(deadline);
      if (this.controller === controller) this.controller = undefined;
      if (acquired) await this.abortable(this.jobs.releaseLease(LEASE_KEY, owner), AbortSignal.timeout(5_000)).catch(() => undefined);
    }
  }

  private assertReader(user: AuthUser) {
    if (!user?.id || !canReadData(user.role)) throw new ForbiddenException('当前账号无权准备或查看数据压缩包');
  }

  private async currentUser(id: string | null): Promise<AuthUser> {
    if (!id) throw new ForbiddenException('预打包申请账号已不存在');
    const user = await this.prisma.user.findFirst({ where: { id, isActive: true, deletedAt: null },
      include: { projectAccess: { select: { projectId: true } } } });
    if (!user) throw new ForbiddenException('预打包申请账号已停用或删除');
    const actor: AuthUser = { id: user.id, email: user.email, displayName: user.displayName, role: user.role,
      tenantId: user.tenantId, projectIds: user.projectAccess.map(access => access.projectId), mustChangePassword: user.mustChangePassword };
    this.assertReader(actor);
    return actor;
  }

  private async dataset(id: string, user: AuthUser, client: Pick<PrismaService, 'dataset'> = this.prisma) {
    const dataset = await client.dataset.findFirst({ where: { id, deletingAt: null, ...warehouseScope(user) } });
    if (!dataset) throw new NotFoundException('数据集不存在或当前账号已无访问权限');
    const metadata = dataset.metadata;
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata) || metadata.type !== 'garage-native-archive')
      throw new BadRequestException('此功能仅适用于设备采集归档数据集');
    return dataset;
  }

  private item(row: PreparedRow): PreparedItem {
    const state: PreparedState = ['QUEUED', 'PROCESSING', 'READY', 'FAILED'].includes(row.state)
      ? row.state as PreparedState : 'FAILED';
    return { id: row.id, path: row.path, taskId: row.taskId, state, fileName: row.fileName, readyAt: row.readyAt,
      errorMessage: row.errorMessage, createdAt: row.createdAt, updatedAt: row.updatedAt };
  }

  private summary(items: PreparedItem[]) {
    return { items, total: items.length, ready: items.filter(item => item.state === 'READY').length,
      active: items.filter(item => item.state === 'QUEUED' || item.state === 'PROCESSING').length };
  }

  private safeError(error: unknown): string {
    if (error instanceof HttpException) {
      const response = error.getResponse();
      const message = typeof response === 'string' ? response
        : response && typeof response === 'object' && 'message' in response ? response.message : undefined;
      // eslint-disable-next-line no-control-regex -- Strip control characters before returning an error message to the client.
      if (typeof message === 'string') return message.replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, 300);
    }
    return '设备压缩包准备失败，请稍后重试';
  }

  private abortable<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
    if (signal.aborted) { void operation.catch(() => undefined); return Promise.reject(signal.reason); }
    return new Promise<T>((resolve, reject) => {
      const abort = () => { signal.removeEventListener('abort', abort); reject(signal.reason); };
      signal.addEventListener('abort', abort, { once: true });
      operation.then(value => {
        signal.removeEventListener('abort', abort);
        if (signal.aborted) reject(signal.reason); else resolve(value);
      }, error => { signal.removeEventListener('abort', abort); reject(error); });
    });
  }
}
