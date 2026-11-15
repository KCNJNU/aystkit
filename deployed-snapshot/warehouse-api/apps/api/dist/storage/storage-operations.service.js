"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
var __metadata = (this && this.__metadata) || function (k, v) {
    if (typeof Reflect === "object" && typeof Reflect.metadata === "function") return Reflect.metadata(k, v);
};
var __param = (this && this.__param) || function (paramIndex, decorator) {
    return function (target, key) { decorator(target, key, paramIndex); }
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.StorageOperationsService = void 0;
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const jobs_service_1 = require("../jobs/jobs.service");
const prisma_service_1 = require("../prisma/prisma.service");
const dataset_path_1 = require("./dataset-path");
const object_storage_1 = require("./object-storage");
let StorageOperationsService = class StorageOperationsService {
    prisma;
    jobs;
    storage;
    constructor(prisma, jobs, storage) {
        this.prisma = prisma;
        this.jobs = jobs;
        this.storage = storage;
    }
    async executeDatasetDelete(datasetId, objectKeys) {
        const dataset = await this.prisma.dataset.findUnique({ where: { id: datasetId } });
        if (!dataset)
            return { datasetId, deleted: true };
        if (!dataset.deletingAt)
            throw new common_1.ConflictException('数据集尚未确认删除');
        try {
            for (let offset = 0; offset < objectKeys.length; offset += 1000)
                await this.storage.deleteObjects(objectKeys.slice(offset, offset + 1000));
        }
        catch {
            throw new common_1.ServiceUnavailableException('存储清理暂时失败，后台任务将重试');
        }
        await this.prisma.dataset.delete({ where: { id: datasetId } });
        return { datasetId, deleted: true, objectCount: objectKeys.length };
    }
    async pageObjects(prefix, cursor, limit) {
        const result = await this.storage.listObjects({ prefix, cursor, limit });
        const keys = result.items.map((item) => item.objectKey);
        const [files, versions, recycled] = await Promise.all([
            this.prisma.robotFile.findMany({ where: { objectKey: { in: keys } }, select: { id: true, objectKey: true, originalName: true } }),
            this.prisma.datasetVersion.findMany({ where: { manifestKey: { in: keys } }, select: { id: true, manifestKey: true, version: true, dataset: { select: { id: true, name: true } } } }),
            this.prisma.recycledFile.findMany({ where: { objectKey: { in: keys } }, select: { id: true, objectKey: true, originalName: true } }),
        ]);
        const recycleMap = new Map(recycled.map(item => [item.objectKey, item]));
        const fileMap = new Map(files.map((item) => [item.objectKey, item]));
        const manifestMap = new Map(versions.filter((item) => item.manifestKey).map((item) => [item.manifestKey, item]));
        return { ...result, items: result.items.map((item) => ({ ...item, mapping: fileMap.has(item.objectKey) ? { type: 'RobotFile', ...fileMap.get(item.objectKey) } : manifestMap.has(item.objectKey) ? { type: 'Manifest', ...manifestMap.get(item.objectKey) } : recycleMap.has(item.objectKey) ? { type: 'RecycledFile', ...recycleMap.get(item.objectKey) } : null })) };
    }
    async enqueueScan(userId) {
        const run = await this.prisma.reconciliationRun.create({ data: { requestedById: userId } });
        const job = await this.jobs.create(client_1.JobType.STORAGE_SCAN, { payload: { runId: run.id }, resourceType: 'ReconciliationRun', resourceId: run.id, requestedById: userId });
        await this.prisma.reconciliationRun.update({ where: { id: run.id }, data: { jobId: job.id } });
        return { run: { ...run, jobId: job.id }, job };
    }
    async enqueueOrphanDelete(userId, role, issueIds, confirmation) {
        if (role !== client_1.UserRole.ADMIN && role !== client_1.UserRole.SUPER_ADMIN && role !== client_1.UserRole.SYSTEM_ADMIN)
            throw new common_1.ForbiddenException('Only Super Admin can delete orphan objects');
        if (confirmation !== 'DELETE_ORPHAN_OBJECTS')
            throw new common_1.BadRequestException('Explicit orphan deletion confirmation is required');
        const issues = await this.prisma.reconciliationIssue.findMany({ where: { id: { in: issueIds }, type: client_1.ReconciliationIssueType.ORPHAN_OBJECT, status: client_1.ReconciliationIssueStatus.OPEN } });
        if (issues.length !== new Set(issueIds).size)
            throw new common_1.BadRequestException('Every issue must be an open orphan object issue');
        return this.jobs.create(client_1.JobType.STORAGE_ORPHAN_DELETE, { payload: { issueIds }, resourceType: 'ReconciliationIssue', requestedById: userId, maxAttempts: 3 });
    }
    pageRuns(page, pageSize) { return this.prisma.$transaction(async (tx) => ({ items: await tx.reconciliationRun.findMany({ orderBy: { createdAt: 'desc' }, skip: (page - 1) * pageSize, take: pageSize, include: { requestedBy: { select: { id: true, displayName: true } } } }), total: await tx.reconciliationRun.count(), page, pageSize })); }
    async getRun(id) { const run = await this.prisma.reconciliationRun.findUnique({ where: { id }, include: { issues: { orderBy: [{ status: 'asc' }, { createdAt: 'desc' }] } } }); if (!run)
        throw new common_1.NotFoundException('Reconciliation run not found'); return this.serializeRun(run); }
    async ignoreIssue(id, note) { try {
        return await this.prisma.reconciliationIssue.update({ where: { id }, data: { status: client_1.ReconciliationIssueStatus.IGNORED, resolutionNote: note, resolvedAt: new Date() } });
    }
    catch {
        throw new common_1.NotFoundException('Reconciliation issue not found');
    } }
    async executeScan(runId) {
        const run = await this.prisma.reconciliationRun.update({ where: { id: runId }, data: { status: client_1.ReconciliationStatus.RUNNING, startedAt: new Date(), errorMessage: null } });
        try {
            const [files, versions, activeUploads, datasets, folders, recycled] = await Promise.all([
                this.prisma.robotFile.findMany({ select: { id: true, objectKey: true, fileSize: true } }),
                this.prisma.datasetVersion.findMany({ where: { manifestKey: { not: null } }, select: { id: true, manifestKey: true } }),
                this.prisma.uploadSession.findMany({ where: this.activeUploadWhere(), select: { tempPrefix: true, finalObjectKey: true } }),
                this.prisma.dataset.findMany({ select: { id: true, name: true } }),
                this.prisma.datasetFolder.findMany({ select: { objectKey: true } }),
                this.prisma.recycledFile.findMany({ select: { objectKey: true, fileSize: true } }),
            ]);
            const expected = new Map();
            for (const file of recycled)
                expected.set(file.objectKey, { size: file.fileSize });
            for (const file of files)
                expected.set(file.objectKey, { robotFileId: file.id, size: file.fileSize });
            for (const version of versions)
                if (version.manifestKey)
                    expected.set(version.manifestKey, {});
            const objects = await this.listAllObjects();
            for (const dataset of datasets) {
                try {
                    const marker = `${(0, dataset_path_1.datasetFolder)(dataset.name)}.dataset`;
                    if (objects.some(item => item.objectKey === marker))
                        expected.set(marker, {});
                }
                catch { }
            }
            for (const folder of folders)
                if (objects.some(item => item.objectKey === folder.objectKey))
                    expected.set(folder.objectKey, {});
            const objectMap = new Map(objects.map((item) => [item.objectKey, item]));
            const issues = [];
            for (const [objectKey, metadata] of expected) {
                const actual = objectMap.get(objectKey);
                if (!actual)
                    issues.push({ runId, type: client_1.ReconciliationIssueType.MISSING_OBJECT, objectKey, robotFileId: metadata.robotFileId, expectedSize: metadata.size });
                else if (metadata.size !== undefined && BigInt(actual.size) !== metadata.size)
                    issues.push({ runId, type: client_1.ReconciliationIssueType.SIZE_MISMATCH, objectKey, robotFileId: metadata.robotFileId, expectedSize: metadata.size, actualSize: BigInt(actual.size) });
            }
            const tempPrefixes = activeUploads.map((item) => item.tempPrefix);
            const finalUploadKeys = new Set(activeUploads.map(item => item.finalObjectKey).filter(Boolean));
            for (const item of objects)
                if (!expected.has(item.objectKey) && !finalUploadKeys.has(item.objectKey) && !tempPrefixes.some((prefix) => item.objectKey.startsWith(prefix)))
                    issues.push({ runId, type: client_1.ReconciliationIssueType.ORPHAN_OBJECT, objectKey: item.objectKey, actualSize: BigInt(item.size) });
            await this.prisma.$transaction(async (tx) => {
                await tx.reconciliationIssue.deleteMany({ where: { runId } });
                if (issues.length)
                    await tx.reconciliationIssue.createMany({ data: issues });
                await tx.reconciliationRun.update({ where: { id: runId }, data: { status: client_1.ReconciliationStatus.COMPLETED, scannedObjects: objects.length, indexedObjects: expected.size, issueCount: issues.length, completedAt: new Date() } });
            });
            if (issues.length && run.requestedById)
                await this.prisma.notification.create({ data: { userId: run.requestedById, type: 'RECONCILIATION_ISSUES', title: 'Storage reconciliation found issues', message: `${issues.length} storage issue(s) require review.`, resourceUrl: `/storage?run=${runId}` } });
            return { runId, scannedObjects: objects.length, indexedObjects: expected.size, issueCount: issues.length };
        }
        catch (error) {
            await this.prisma.reconciliationRun.update({ where: { id: runId }, data: { status: client_1.ReconciliationStatus.FAILED, errorMessage: error instanceof Error ? error.message : 'Storage scan failed', completedAt: new Date() } });
            throw error;
        }
    }
    async executeOrphanDelete(issueIds) {
        const issues = await this.prisma.reconciliationIssue.findMany({ where: { id: { in: issueIds }, type: client_1.ReconciliationIssueType.ORPHAN_OBJECT, status: client_1.ReconciliationIssueStatus.OPEN } });
        if (issues.length !== new Set(issueIds).size)
            throw new common_1.BadRequestException('Orphan issues changed before deletion');
        await this.prisma.$transaction(async (tx) => {
            for (const key of issues.map(issue => issue.objectKey).sort())
                await tx.$queryRaw `SELECT pg_advisory_xact_lock(hashtextextended(${key}, 0))::text`;
            const keys = issues.map(issue => issue.objectKey);
            const prefixes = [...new Set(keys.flatMap(key => [...key.matchAll(/\//g)].map(match => key.slice(0, match.index + 1))))];
            const sessions = await tx.$queryRaw(client_1.Prisma.sql `
        SELECT id, status, "expiresAt" FROM "UploadSession"
        WHERE "finalObjectKey" IN (${client_1.Prisma.join(keys)})
          ${prefixes.length ? client_1.Prisma.sql `OR "tempPrefix" IN (${client_1.Prisma.join(prefixes)})` : client_1.Prisma.empty}
        ORDER BY id FOR UPDATE
      `);
            if (sessions.some(session => session.status === client_1.UploadStatus.FINALIZING
                || [client_1.UploadStatus.CREATED, client_1.UploadStatus.UPLOADING, client_1.UploadStatus.FAILED].includes(session.status)
                    && session.expiresAt > new Date()))
                throw new common_1.ConflictException('对象仍属于未完成的上传，请等待完成或先取消上传后重新扫描');
            const protectedCounts = await Promise.all([
                tx.robotFile.count({ where: { objectKey: { in: keys } } }),
                tx.recycledFile.count({ where: { objectKey: { in: keys } } }),
                tx.datasetVersion.count({ where: { manifestKey: { in: keys } } }),
                tx.datasetFolder.count({ where: { objectKey: { in: keys } } }),
            ]);
            if (protectedCounts.some(Boolean))
                throw new common_1.ConflictException('对象已被文件、回收站或版本使用，请重新扫描存储');
            await this.storage.deleteObjects(keys);
        }, { timeout: 30000 });
        await this.prisma.reconciliationIssue.updateMany({ where: { id: { in: issueIds } }, data: { status: client_1.ReconciliationIssueStatus.RESOLVED, resolutionNote: 'Confirmed orphan object deletion', resolvedAt: new Date() } });
        return { deleted: issues.length, objectKeys: issues.map((issue) => issue.objectKey) };
    }
    activeUploadWhere() {
        return { OR: [{ status: client_1.UploadStatus.FINALIZING }, {
                    status: { in: [client_1.UploadStatus.CREATED, client_1.UploadStatus.UPLOADING, client_1.UploadStatus.FAILED] }, expiresAt: { gt: new Date() },
                }] };
    }
    async listAllObjects() { const items = []; let cursor; do {
        const page = await this.storage.listObjects({ cursor, limit: 1000 });
        items.push(...page.items);
        cursor = page.nextCursor;
    } while (cursor); return items; }
    serializeRun(run) { return { ...run, issues: run.issues.map((issue) => ({ ...issue, expectedSize: issue.expectedSize?.toString() ?? null, actualSize: issue.actualSize?.toString() ?? null })) }; }
};
exports.StorageOperationsService = StorageOperationsService;
exports.StorageOperationsService = StorageOperationsService = __decorate([
    (0, common_1.Injectable)(),
    __param(2, (0, common_1.Inject)(object_storage_1.OBJECT_STORAGE)),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService, jobs_service_1.JobsService, Object])
], StorageOperationsService);
//# sourceMappingURL=storage-operations.service.js.map