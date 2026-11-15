import { BadRequestException, ConflictException, ForbiddenException, ServiceUnavailableException, Inject, Injectable, NotFoundException } from '@nestjs/common';
import { JobType, Prisma, ReconciliationIssueStatus, ReconciliationIssueType, ReconciliationStatus, UploadStatus, UserRole } from '@prisma/client';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { datasetFolder } from './dataset-path';
import { OBJECT_STORAGE, ObjectStorage, StoredObject } from './object-storage';

@Injectable()
export class StorageOperationsService {
  constructor(private readonly prisma: PrismaService, private readonly jobs: JobsService, @Inject(OBJECT_STORAGE) private readonly storage: ObjectStorage) {}

  async executeDatasetDelete(datasetId: string, objectKeys: string[]) {
    const dataset = await this.prisma.dataset.findUnique({ where: { id: datasetId } });
    if (!dataset) return { datasetId, deleted: true };
    if (!dataset.deletingAt) throw new ConflictException('数据集尚未确认删除');
    try {
      for (let offset = 0; offset < objectKeys.length; offset += 1000) await this.storage.deleteObjects(objectKeys.slice(offset, offset + 1000));
    } catch { throw new ServiceUnavailableException('存储清理暂时失败，后台任务将重试'); }
    // Keep the name reserved until all old objects (including the marker) are gone.
    await this.prisma.dataset.delete({ where: { id: datasetId } });
    return { datasetId, deleted: true, objectCount: objectKeys.length };
  }

  async pageObjects(prefix: string | undefined, cursor: string | undefined, limit: number) {
    const result = await this.storage.listObjects({ prefix, cursor, limit });
    const keys = result.items.map((item) => item.objectKey);
    const [files, versions, recycled] = await Promise.all([
      this.prisma.robotFile.findMany({ where: { objectKey: { in: keys } }, select: { id: true, objectKey: true, originalName: true } }),
      this.prisma.datasetVersion.findMany({ where: { manifestKey: { in: keys } }, select: { id: true, manifestKey: true, version: true, dataset: { select: { id: true, name: true } } } }),
      this.prisma.recycledFile.findMany({ where: { objectKey: { in: keys } }, select: { id: true, objectKey: true, originalName: true } }),
    ]);
    const recycleMap = new Map(recycled.map(item => [item.objectKey, item]));
    const fileMap = new Map(files.map((item) => [item.objectKey, item]));
    const manifestMap = new Map(versions.filter((item) => item.manifestKey).map((item) => [item.manifestKey as string, item]));
    return { ...result, items: result.items.map((item) => ({ ...item, mapping: fileMap.has(item.objectKey) ? { type: 'RobotFile', ...fileMap.get(item.objectKey) } : manifestMap.has(item.objectKey) ? { type: 'Manifest', ...manifestMap.get(item.objectKey) } : recycleMap.has(item.objectKey) ? { type: 'RecycledFile', ...recycleMap.get(item.objectKey) } : null })) };
  }

  async enqueueScan(userId: string) {
    const run = await this.prisma.reconciliationRun.create({ data: { requestedById: userId } });
    const job = await this.jobs.create(JobType.STORAGE_SCAN, { payload: { runId: run.id }, resourceType: 'ReconciliationRun', resourceId: run.id, requestedById: userId });
    await this.prisma.reconciliationRun.update({ where: { id: run.id }, data: { jobId: job.id } });
    return { run: { ...run, jobId: job.id }, job };
  }

  async enqueueOrphanDelete(userId: string, role: UserRole, issueIds: string[], confirmation: string) {
    if (role !== UserRole.ADMIN && role !== UserRole.SUPER_ADMIN && role !== UserRole.SYSTEM_ADMIN) throw new ForbiddenException('Only Super Admin can delete orphan objects');
    if (confirmation !== 'DELETE_ORPHAN_OBJECTS') throw new BadRequestException('Explicit orphan deletion confirmation is required');
    const issues = await this.prisma.reconciliationIssue.findMany({ where: { id: { in: issueIds }, type: ReconciliationIssueType.ORPHAN_OBJECT, status: ReconciliationIssueStatus.OPEN } });
    if (issues.length !== new Set(issueIds).size) throw new BadRequestException('Every issue must be an open orphan object issue');
    return this.jobs.create(JobType.STORAGE_ORPHAN_DELETE, { payload: { issueIds }, resourceType: 'ReconciliationIssue', requestedById: userId, maxAttempts: 3 });
  }

  pageRuns(page: number, pageSize: number) { return this.prisma.$transaction(async (tx) => ({ items: await tx.reconciliationRun.findMany({ orderBy: { createdAt: 'desc' }, skip: (page - 1) * pageSize, take: pageSize, include: { requestedBy: { select: { id: true, displayName: true } } } }), total: await tx.reconciliationRun.count(), page, pageSize })); }
  async getRun(id: string) { const run = await this.prisma.reconciliationRun.findUnique({ where: { id }, include: { issues: { orderBy: [{ status: 'asc' }, { createdAt: 'desc' }] } } }); if (!run) throw new NotFoundException('Reconciliation run not found'); return this.serializeRun(run); }
  async ignoreIssue(id: string, note?: string) { try { return await this.prisma.reconciliationIssue.update({ where: { id }, data: { status: ReconciliationIssueStatus.IGNORED, resolutionNote: note, resolvedAt: new Date() } }); } catch { throw new NotFoundException('Reconciliation issue not found'); } }

  async executeScan(runId: string) {
    const run = await this.prisma.reconciliationRun.update({ where: { id: runId }, data: { status: ReconciliationStatus.RUNNING, startedAt: new Date(), errorMessage: null } });
    try {
      const [files, versions, activeUploads, datasets, folders, recycled] = await Promise.all([
        this.prisma.robotFile.findMany({ select: { id: true, objectKey: true, fileSize: true } }),
        this.prisma.datasetVersion.findMany({ where: { manifestKey: { not: null } }, select: { id: true, manifestKey: true } }),
        this.prisma.uploadSession.findMany({ where: this.activeUploadWhere(), select: { tempPrefix: true, finalObjectKey: true } }),
        this.prisma.dataset.findMany({ select: { id: true, name: true } }),
        this.prisma.datasetFolder.findMany({ select: { objectKey: true } }),
        this.prisma.recycledFile.findMany({ select: { objectKey: true, fileSize: true } }),
      ]);
      const expected = new Map<string, { robotFileId?: string; size?: bigint }>();
      for (const file of recycled) expected.set(file.objectKey, { size: file.fileSize });
      for (const file of files) expected.set(file.objectKey, { robotFileId: file.id, size: file.fileSize });
      for (const version of versions) if (version.manifestKey) expected.set(version.manifestKey, {});
      const objects = await this.listAllObjects();
      // Folder markers are application-owned metadata, not orphan file objects.
      for (const dataset of datasets) {
        try { const marker = `${datasetFolder(dataset.name)}.dataset`; if (objects.some(item => item.objectKey === marker)) expected.set(marker, {}); } catch { /* Legacy names may predate folder validation. */ }
      }
      for (const folder of folders) if (objects.some(item => item.objectKey === folder.objectKey)) expected.set(folder.objectKey, {});
      const objectMap = new Map(objects.map((item) => [item.objectKey, item]));
      const issues: Prisma.ReconciliationIssueCreateManyInput[] = [];
      for (const [objectKey, metadata] of expected) {
        const actual = objectMap.get(objectKey);
        if (!actual) issues.push({ runId, type: ReconciliationIssueType.MISSING_OBJECT, objectKey, robotFileId: metadata.robotFileId, expectedSize: metadata.size });
        else if (metadata.size !== undefined && BigInt(actual.size) !== metadata.size) issues.push({ runId, type: ReconciliationIssueType.SIZE_MISMATCH, objectKey, robotFileId: metadata.robotFileId, expectedSize: metadata.size, actualSize: BigInt(actual.size) });
      }
      const tempPrefixes = activeUploads.map((item) => item.tempPrefix);
      const finalUploadKeys = new Set(activeUploads.map(item => item.finalObjectKey).filter(Boolean));
      for (const item of objects) if (!expected.has(item.objectKey) && !finalUploadKeys.has(item.objectKey) && !tempPrefixes.some((prefix) => item.objectKey.startsWith(prefix))) issues.push({ runId, type: ReconciliationIssueType.ORPHAN_OBJECT, objectKey: item.objectKey, actualSize: BigInt(item.size) });
      await this.prisma.$transaction(async (tx) => {
        await tx.reconciliationIssue.deleteMany({ where: { runId } });
        if (issues.length) await tx.reconciliationIssue.createMany({ data: issues });
        await tx.reconciliationRun.update({ where: { id: runId }, data: { status: ReconciliationStatus.COMPLETED, scannedObjects: objects.length, indexedObjects: expected.size, issueCount: issues.length, completedAt: new Date() } });
      });
      if (issues.length && run.requestedById) await this.prisma.notification.create({ data: { userId: run.requestedById, type: 'RECONCILIATION_ISSUES', title: 'Storage reconciliation found issues', message: `${issues.length} storage issue(s) require review.`, resourceUrl: `/storage?run=${runId}` } });
      return { runId, scannedObjects: objects.length, indexedObjects: expected.size, issueCount: issues.length };
    } catch (error) {
      await this.prisma.reconciliationRun.update({ where: { id: runId }, data: { status: ReconciliationStatus.FAILED, errorMessage: error instanceof Error ? error.message : 'Storage scan failed', completedAt: new Date() } });
      throw error;
    }
  }

  async executeOrphanDelete(issueIds: string[]) {
    const issues = await this.prisma.reconciliationIssue.findMany({ where: { id: { in: issueIds }, type: ReconciliationIssueType.ORPHAN_OBJECT, status: ReconciliationIssueStatus.OPEN } });
    if (issues.length !== new Set(issueIds).size) throw new BadRequestException('Orphan issues changed before deletion');
    await this.prisma.$transaction(async tx => {
      for (const key of issues.map(issue => issue.objectKey).sort()) await tx.$queryRaw`SELECT pg_advisory_xact_lock(hashtextextended(${key}, 0))::text`;
      const keys = issues.map(issue => issue.objectKey);
      // Finalization creates RobotFile and completes UploadSession together.
      // Lock the session before rechecking file indexes, otherwise cleanup can
      // observe no RobotFile before commit and no active session after commit.
      // Include every ancestor to protect a part before its UploadPart exists.
      const prefixes = [...new Set(keys.flatMap(key => [...key.matchAll(/\//g)].map(match => key.slice(0, match.index! + 1))))];
      const sessions = await tx.$queryRaw<Array<{ id: string; status: UploadStatus; expiresAt: Date }>>(Prisma.sql`
        SELECT id, status, "expiresAt" FROM "UploadSession"
        WHERE "finalObjectKey" IN (${Prisma.join(keys)})
          ${prefixes.length ? Prisma.sql`OR "tempPrefix" IN (${Prisma.join(prefixes)})` : Prisma.empty}
        ORDER BY id FOR UPDATE
      `);
      if (sessions.some(session => session.status === UploadStatus.FINALIZING
        || ([UploadStatus.CREATED, UploadStatus.UPLOADING, UploadStatus.FAILED] as UploadStatus[]).includes(session.status)
          && session.expiresAt > new Date())) throw new ConflictException('对象仍属于未完成的上传，请等待完成或先取消上传后重新扫描');
      const protectedCounts = await Promise.all([
        tx.robotFile.count({ where: { objectKey: { in: keys } } }),
        tx.recycledFile.count({ where: { objectKey: { in: keys } } }),
        tx.datasetVersion.count({ where: { manifestKey: { in: keys } } }),
        tx.datasetFolder.count({ where: { objectKey: { in: keys } } }),
      ]);
      if (protectedCounts.some(Boolean)) throw new ConflictException('对象已被文件、回收站或版本使用，请重新扫描存储');
      await this.storage.deleteObjects(keys);
    }, { timeout: 30000 });
    await this.prisma.reconciliationIssue.updateMany({ where: { id: { in: issueIds } }, data: { status: ReconciliationIssueStatus.RESOLVED, resolutionNote: 'Confirmed orphan object deletion', resolvedAt: new Date() } });
    return { deleted: issues.length, objectKeys: issues.map((issue) => issue.objectKey) };
  }

  private activeUploadWhere(): Prisma.UploadSessionWhereInput {
    return { OR: [{ status: UploadStatus.FINALIZING }, {
      status: { in: [UploadStatus.CREATED, UploadStatus.UPLOADING, UploadStatus.FAILED] }, expiresAt: { gt: new Date() },
    }] };
  }

  private async listAllObjects() { const items: StoredObject[] = []; let cursor: string | undefined; do { const page = await this.storage.listObjects({ cursor, limit: 1000 }); items.push(...page.items); cursor = page.nextCursor; } while (cursor); return items; }
  private serializeRun<T extends { issues: Array<{ expectedSize: bigint | null; actualSize: bigint | null }> }>(run: T) { return { ...run, issues: run.issues.map((issue) => ({ ...issue, expectedSize: issue.expectedSize?.toString() ?? null, actualSize: issue.actualSize?.toString() ?? null })) }; }
}
