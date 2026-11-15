import { BadRequestException, ConflictException, ForbiddenException, Injectable, NotFoundException } from '@nestjs/common';
import { JobStatus, JobStepStatus, JobType, Prisma, UserRole } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';
import { AuthUser } from '../auth/auth.types';

const STEPS: Record<JobType, string[]> = {
  DATASET_DELETE: ['Delete Dataset Objects'],
  DATASET_PUBLISH: ['Validation', 'Workspace Snapshot', 'DVC Add', 'DVC Push', 'Git Commit/Tag', 'Recovery Check', 'Persist Version'],
  RECOVERY_VERIFY: ['Prepare Temporary Workspace', 'Checkout Git Tag', 'DVC Pull', 'Verify Manifest and Files', 'Cleanup'],
  STORAGE_SCAN: ['Load Database Index', 'List ObjectStorage', 'Compare Objects', 'Persist Issues'],
  STORAGE_ORPHAN_DELETE: ['Validate Issues', 'Delete Objects', 'Resolve Issues'],
  UPLOAD_FINALIZE: ['Validate Parts', 'Compose Object', 'Verify Format and Checksum', 'Create RobotFile', 'Cleanup Parts'],
  RETENTION_CLEANUP: [],
  ARCHIVE_VIDEO_ASSEMBLE: ['Assemble Continuous Recording'],
};

@Injectable()
export class JobsService {
  constructor(private readonly prisma: PrismaService) {}
  create(type: JobType, input: { payload: Prisma.InputJsonValue; resourceType?: string; resourceId?: string; deduplicationKey?: string; requestedById?: string; tenantId?: string | null; projectId?: string | null; maxAttempts?: number }, client: Pick<Prisma.TransactionClient, 'job'> = this.prisma) { return client.job.create({ data: { type, payload: input.payload, resourceType: input.resourceType, resourceId: input.resourceId, deduplicationKey: input.deduplicationKey, requestedById: input.requestedById, tenantId: input.tenantId ?? undefined, projectId: input.projectId ?? undefined, maxAttempts: input.maxAttempts ?? 3, steps: { create: STEPS[type].map((name, stepOrder) => ({ name, stepOrder })) } }, include: { steps: { orderBy: { stepOrder: 'asc' } }, requestedBy: { select: { id: true, displayName: true, email: true } } } }); }
  async page(query: { page: number; pageSize: number; status?: JobStatus; type?: JobType; search?: string; requestedById?: string }, actor: UserRole | AuthUser) { const where: Prisma.JobWhereInput = { status: query.status, type: query.type, ...this.scope(actor), ...(query.search && { OR: [{ resourceType: { contains: query.search, mode: 'insensitive' } }, { resourceId: { contains: query.search, mode: 'insensitive' } }, { errorMessage: { contains: query.search, mode: 'insensitive' } }] }) }; const [items, total] = await this.prisma.$transaction([this.prisma.job.findMany({ where, include: { requestedBy: { select: { id: true, displayName: true, email: true } }, steps: { orderBy: { stepOrder: 'asc' } } }, orderBy: { createdAt: 'desc' }, skip: (query.page - 1) * query.pageSize, take: query.pageSize }), this.prisma.job.count({ where })]); return { items, total, page: query.page, pageSize: query.pageSize }; }
  async get(id: string) { const job = await this.prisma.job.findUnique({ where: { id }, include: { requestedBy: { select: { id: true, displayName: true, email: true } }, steps: { orderBy: { stepOrder: 'asc' } } } }); if (!job) throw new NotFoundException('Job not found'); return job; }
  async getScoped(id: string, user: AuthUser) { const job=await this.prisma.job.findFirst({ where:{id,...this.scope(user)}, include:{requestedBy:{select:{id:true,displayName:true,email:true}},steps:{orderBy:{stepOrder:'asc'}}} });if(!job)throw new NotFoundException('Job not found');return job; }
  async retry(id: string, user?: AuthUser) { const job = user?await this.getScoped(id,user):await this.get(id); this.assertLifecyclePermission(job.type, user); if (job.status !== JobStatus.FAILED) throw new ConflictException('Only FAILED jobs can be retried'); await this.prisma.$transaction([this.prisma.jobStep.updateMany({ where: { jobId: id, status: JobStepStatus.FAILED }, data: { status: JobStepStatus.PENDING, errorMessage: null, startedAt: null, finishedAt: null } }), this.prisma.job.update({ where: { id }, data: { status: JobStatus.QUEUED, errorCode: null, errorMessage: null, availableAt: new Date(), lockedAt: null, lockedBy: null, cancelRequested: false, finishedAt: null } })]); return user?this.getScoped(id,user):this.get(id); }
  async cancel(id: string, user?: AuthUser) { const job = user?await this.getScoped(id,user):await this.get(id); this.assertLifecyclePermission(job.type, user); if (job.type === JobType.DATASET_DELETE) throw new ConflictException('数据集删除已确认，清理任务不能取消；失败时可重试'); if (job.status !== JobStatus.QUEUED && job.status !== JobStatus.RUNNING) throw new ConflictException('Job cannot be cancelled'); if (job.status === JobStatus.QUEUED) await this.prisma.job.update({ where: { id }, data: { status: JobStatus.CANCELLED, cancelRequested: true, finishedAt: new Date() } }); else await this.prisma.job.update({ where: { id }, data: { cancelRequested: true } }); return user?this.getScoped(id,user):this.get(id); }
  private assertLifecyclePermission(type: JobType, user?: AuthUser) { if (user?.role === 'OPERATOR' && (type === 'DATASET_PUBLISH' || type === 'RECOVERY_VERIFY')) throw new ForbiddenException('版本发布与恢复任务需要项目管理员权限'); }
  async claim(workerId: string) {
    await this.recoverStale();
    const rows = await this.prisma.$queryRaw<Array<{ id: string }>>(Prisma.sql`
      WITH candidate AS (
        SELECT id FROM "Job" WHERE status = 'QUEUED'::"JobStatus" AND "availableAt" <= NOW()
        ORDER BY "createdAt" ASC FOR UPDATE SKIP LOCKED LIMIT 1
      )
      UPDATE "Job" SET status = 'RUNNING'::"JobStatus", "lockedBy" = ${workerId}, "lockedAt" = NOW(), "heartbeatAt" = NOW(), "startedAt" = COALESCE("startedAt", NOW()), attempts = attempts + 1, "updatedAt" = NOW()
      WHERE id IN (SELECT id FROM candidate) RETURNING id
    `);
    return rows[0] ? this.get(rows[0].id) : null;
  }
  heartbeat(id: string, workerId: string) { return this.prisma.job.updateMany({ where: { id, lockedBy: workerId, status: JobStatus.RUNNING }, data: { heartbeatAt: new Date() } }); }
  async defer(id: string, workerId: string) { await this.prisma.job.updateMany({ where: { id, lockedBy: workerId, status: JobStatus.RUNNING }, data: { status: JobStatus.QUEUED, availableAt: new Date(Date.now() + 30_000), attempts: { decrement: 1 }, lockedAt: null, lockedBy: null, heartbeatAt: null } }); }
  async beginStep(jobId: string, name: string, workerId?: string) { return this.withOwner(jobId, workerId, async client => { const step = await client.jobStep.findUnique({ where: { jobId_name: { jobId, name } } }); if (!step) throw new BadRequestException(`Unknown job step ${name}`); if (step.status === JobStepStatus.SUCCEEDED) return false; await client.jobStep.update({ where: { id: step.id }, data: { status: JobStepStatus.RUNNING, startedAt: new Date(), errorMessage: null } }); return true; }); }
  async completeStep(jobId: string, name: string, output?: Prisma.InputJsonValue, workerId?: string) { return this.withOwner(jobId, workerId, async client => { const steps = await client.jobStep.findMany({ where: { jobId }, orderBy: { stepOrder: 'asc' } }); const step = steps.find((item) => item.name === name); if (!step) throw new BadRequestException(`Unknown job step ${name}`); await client.jobStep.update({ where: { id: step.id }, data: { status: JobStepStatus.SUCCEEDED, progress: 100, output, finishedAt: new Date() } }); const succeeded = steps.filter((item) => item.status === JobStepStatus.SUCCEEDED || item.id === step.id).length; await client.job.update({ where: { id: jobId }, data: { progress: Math.round(succeeded / steps.length * 100) } }); }); }
  async finish(id: string, result?: Prisma.InputJsonValue, workerId?: string) { return this.withOwner(id, workerId, async client => { const job = await client.job.update({ where: { id }, data: { status: JobStatus.SUCCEEDED, progress: 100, result, finishedAt: new Date(), lockedBy: null, lockedAt: null, heartbeatAt: null }, include: { requestedBy: true } }); if (job.requestedById) await client.notification.create({ data: { userId: job.requestedById, jobId: id, type: 'JOB_SUCCEEDED', title: `${job.type.replaceAll('_', ' ')} completed`, message: 'The operation completed successfully.', resourceUrl: `/jobs/${id}` } }); return job; }, true); }
  async fail(id: string, error: unknown, stepName?: string, infrastructure = false, workerId?: string) { return this.withOwner(id, workerId, async client => { const job = await client.job.findUniqueOrThrow({ where: { id } }); const message = error instanceof Error ? error.message : 'Job failed'; if (stepName) await client.jobStep.updateMany({ where: { jobId: id, name: stepName }, data: { status: JobStepStatus.FAILED, errorMessage: message, finishedAt: new Date() } }); const retry = infrastructure && job.attempts < job.maxAttempts; const delays = [5_000, 30_000, 120_000]; await client.job.update({ where: { id }, data: retry ? { status: JobStatus.QUEUED, errorCode: 'INFRASTRUCTURE_ERROR', errorMessage: message, availableAt: new Date(Date.now() + delays[Math.min(job.attempts - 1, delays.length - 1)]), lockedBy: null, lockedAt: null, heartbeatAt: null } : { status: JobStatus.FAILED, errorCode: infrastructure ? 'INFRASTRUCTURE_ERROR' : 'BUSINESS_ERROR', errorMessage: message, finishedAt: new Date(), lockedBy: null, lockedAt: null, heartbeatAt: null } }); if (!retry && job.requestedById) await client.notification.create({ data: { userId: job.requestedById, jobId: id, type: 'JOB_FAILED', title: `${job.type.replaceAll('_', ' ')} failed`, message, resourceUrl: `/jobs/${id}` } }); }); }
  async isCancelRequested(id: string) { return (await this.prisma.job.findUnique({ where: { id }, select: { cancelRequested: true } }))?.cancelRequested ?? true; }
  async markCancelled(id: string, workerId?: string) { return this.withOwner(id, workerId, async client => { await client.job.update({ where: { id }, data: { status: JobStatus.CANCELLED, finishedAt: new Date(), lockedBy: null, lockedAt: null, heartbeatAt: null } }); }); }
  async acquireLease(key: string, owner: string, ttlMs = 70_000) {
    const expiresAt = new Date(Date.now() + ttlMs);
    const rows = await this.prisma.$queryRaw<Array<{ key: string }>>(Prisma.sql`
      INSERT INTO "JobLease" (key, owner, "expiresAt", "heartbeatAt", "updatedAt")
      VALUES (${key}, ${owner}, ${expiresAt}, NOW(), NOW())
      ON CONFLICT (key) DO UPDATE SET owner = EXCLUDED.owner, "expiresAt" = EXCLUDED."expiresAt", "heartbeatAt" = NOW(), "updatedAt" = NOW()
      WHERE "JobLease"."expiresAt" < NOW() OR "JobLease".owner = ${owner}
      RETURNING key
    `);
    return rows.length === 1;
  }
  async heartbeatLease(key: string, owner: string, ttlMs = 70_000) { return this.prisma.jobLease.updateMany({ where: { key, owner, expiresAt: { gt: new Date() } }, data: { heartbeatAt: new Date(), expiresAt: new Date(Date.now() + ttlMs) } }); }
  async releaseLease(key: string, owner: string) { await this.prisma.jobLease.deleteMany({ where: { key, owner } }); }
  private async withOwner<T>(id: string, workerId: string | undefined, action: (client: Prisma.TransactionClient) => Promise<T>, rejectCancellation = false): Promise<T> {
    if (!workerId) return action(this.prisma);
    return this.prisma.$transaction(async tx => {
      const rows = await tx.$queryRaw<Array<{ lockedBy: string | null; status: JobStatus; cancelRequested: boolean }>>(Prisma.sql`SELECT "lockedBy", status, "cancelRequested" FROM "Job" WHERE id = ${id}::uuid FOR UPDATE`);
      if (rows[0]?.lockedBy !== workerId || rows[0]?.status !== JobStatus.RUNNING) throw new JobOwnershipLostError();
      if (rejectCancellation && rows[0]?.cancelRequested) throw new JobCancellationRequestedError();
      return action(tx);
    });
  }
  private async recoverStale() { await this.prisma.job.updateMany({ where: { status: JobStatus.RUNNING, heartbeatAt: { lt: new Date(Date.now() - 60_000) } }, data: { status: JobStatus.QUEUED, lockedBy: null, lockedAt: null, heartbeatAt: null, availableAt: new Date() } }); }
  private scope(actor: UserRole | AuthUser): Prisma.JobWhereInput { if(typeof actor==='string'||actor.role===UserRole.ADMIN||(actor.role === UserRole.SUPER_ADMIN || actor.role === UserRole.SYSTEM_ADMIN))return{};if(actor.role===UserRole.VIEWER)return{requestedById:actor.id};return{tenantId:actor.tenantId??'__NO_TENANT__',...(actor.role===UserRole.TENANT_ADMIN?{}:{projectId:{in:actor.projectIds}})}; }
}

export class JobOwnershipLostError extends Error {}

export class JobCancellationRequestedError extends Error {}
