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
exports.ArchiveRecordingsService = exports.ArchiveAssemblyBusyError = exports.ArchiveAssemblyCancelledError = void 0;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const client_1 = require("@prisma/client");
const node_child_process_1 = require("node:child_process");
const node_crypto_1 = require("node:crypto");
const node_fs_1 = require("node:fs");
const promises_1 = require("node:fs/promises");
const node_path_1 = require("node:path");
const node_stream_1 = require("node:stream");
const promises_2 = require("node:stream/promises");
const jobs_service_1 = require("../jobs/jobs.service");
const prisma_service_1 = require("../prisma/prisma.service");
const dataset_path_1 = require("../storage/dataset-path");
const object_storage_1 = require("../storage/object-storage");
const object_stream_1 = require("../storage/object-stream");
const warehouse_query_service_1 = require("../warehouse/warehouse-query.service");
const archive_recording_groups_1 = require("./archive-recording-groups");
const dataset_folders_1 = require("./dataset-folders");
class ArchiveAssemblyCancelledError extends Error {
}
exports.ArchiveAssemblyCancelledError = ArchiveAssemblyCancelledError;
class ArchiveAssemblyBusyError extends Error {
}
exports.ArchiveAssemblyBusyError = ArchiveAssemblyBusyError;
const MAX_SENSOR_PARTS = 5_000;
const ASSEMBLY_GROUP_CHECK_MS = 5_000;
const STAGE_BYTES = 8 * 1024 * 1024;
const PACKAGE_TIMEOUT_MS = 120_000;
const MAX_FOLDER_ENTRIES = 10_000;
const MAX_FOLDER_PACKAGE_TASKS = 32;
const FOLDER_PACKAGE_TASK_TTL_MS = 15 * 60_000;
let ArchiveRecordingsService = class ArchiveRecordingsService {
    prisma;
    jobs;
    storage;
    config;
    packaging = false;
    folderPackageTasks = new Map();
    nextScanAt = 0;
    backfillCursor;
    backfillDone = false;
    liveCursor = { createdAt: new Date(Date.now() - 300_000), id: '00000000-0000-0000-0000-000000000000' };
    pendingSensors = new Map();
    closureCursor = { updatedAt: new Date(Date.now() - 300_000), id: '00000000-0000-0000-0000-000000000000' };
    constructor(prisma, jobs, storage, config) {
        this.prisma = prisma;
        this.jobs = jobs;
        this.storage = storage;
        this.config = config;
    }
    async endSession(datasetId, sessionId, body, user) {
        const writers = [client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR];
        if (!writers.includes(user.role))
            throw new common_1.ForbiddenException('当前账号无权提交采集结束证明');
        const start = Date.parse(body.startedAt), end = Date.parse(body.endedAt);
        const segmentIds = Array.isArray(body.segmentIds) ? [...body.segmentIds].sort() : undefined;
        const recovered = body.reason === 'recovered_interruption';
        if (!/^(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$/.test(sessionId)
            || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(body.robotId)
            || !Number.isFinite(start) || !Number.isFinite(end) || end < start || end - start > 24 * 3600_000
            || end > Date.now() + 300_000 || (!recovered && body.reason !== 'desktop_stopped') || typeof body.captureIncomplete !== 'boolean'
            || (recovered ? body.captureIncomplete !== true || !segmentIds || typeof body.recoveryEvidenceSha256 !== 'string'
                || !/^[a-f0-9]{64}$/.test(body.recoveryEvidenceSha256) : body.recoveryEvidenceSha256 !== undefined)
            || (body.segmentIds !== undefined && !Array.isArray(body.segmentIds))
            || (segmentIds && (segmentIds.length === 0 || segmentIds.length > 5000 || new Set(segmentIds).size !== segmentIds.length
                || segmentIds.some(id => typeof id !== 'string' || !/^[a-f0-9]{32}$/.test(id)))))
            throw new common_1.BadRequestException('采集结束证明参数无效');
        const proof = { robotId: body.robotId, startedAt: new Date(start).toISOString(), endedAt: new Date(end).toISOString(),
            reason: body.reason, captureIncomplete: body.captureIncomplete, ...(segmentIds ? { segmentIds } : {}),
            ...(recovered ? { recoveryEvidenceSha256: body.recoveryEvidenceSha256 } : {}) };
        return this.prisma.$transaction(async (tx) => {
            await tx.$queryRaw(client_1.Prisma.sql `SELECT id FROM "Dataset" WHERE id = ${datasetId}::uuid FOR UPDATE`);
            const dataset = await tx.dataset.findFirst({ where: { id: datasetId, deletingAt: null, ...(0, warehouse_query_service_1.warehouseScope)(user) } });
            if (!dataset)
                throw new common_1.NotFoundException('数据集不存在');
            const metadata = (0, archive_recording_groups_1.jsonRecord)(dataset.metadata);
            if (metadata.type !== 'garage-native-archive' || metadata.robotId !== body.robotId)
                throw new common_1.ConflictException('采集机器人与数据集不匹配');
            if (typeof metadata.day === 'string') {
                const day = Date.parse(metadata.day + 'T00:00:00+08:00');
                if (!Number.isFinite(day) || end < day || start >= day + 24 * 3600_000)
                    throw new common_1.ConflictException('采集结束证明不在数据集日期范围内');
            }
            const sessions = (0, archive_recording_groups_1.jsonRecord)(metadata.archiveRecordingSessions);
            if (Object.hasOwn(sessions, sessionId)) {
                const ordered = (value) => JSON.stringify(Object.entries((0, archive_recording_groups_1.jsonRecord)(value)).sort(([a], [b]) => a.localeCompare(b)));
                if (ordered(sessions[sessionId]) !== ordered(proof))
                    throw new common_1.ConflictException('采集会话已有不同结束证明');
                return { sessionId, closed: true, captureIncomplete: proof.captureIncomplete, idempotent: true };
            }
            if (Object.keys(sessions).length >= 256)
                throw new common_1.ConflictException('采集结束证明数量超限');
            if (segmentIds) {
                const rows = await tx.robotFile.findMany({ where: { datasetId, robotId: body.robotId,
                        recognitionType: { startsWith: 'garage-native-' }, originalName: { endsWith: '.mkv', mode: 'insensitive' },
                        capturedAt: { gte: new Date(start), lte: new Date(end) } }, take: MAX_SENSOR_PARTS + 1 });
                if (rows.length > MAX_SENSOR_PARTS)
                    throw new common_1.ConflictException('采集片段过多，请分批核对');
                const found = new Set();
                for (const row of rows) {
                    const meta = (0, archive_recording_groups_1.jsonRecord)(row.metadata), id = meta.segmentId;
                    if (typeof id !== 'string' || !segmentIds.includes(id))
                        continue;
                    const begin = Date.parse(String(meta.startedAt)), finish = Date.parse(String(meta.endAt));
                    const legacyToken = meta.captureSessionId === undefined || meta.captureSessionId === null || meta.captureSessionId === '';
                    if (row.robotId !== body.robotId || !(0, archive_recording_groups_1.isOriginalArchiveVideo)(row) || !Number.isFinite(begin) || !Number.isFinite(finish)
                        || finish < begin || begin < start || finish > end || (!legacyToken && meta.captureSessionId !== sessionId))
                        throw new common_1.ConflictException('采集片段与结束证明的机器人、会话或时间范围不匹配');
                    found.add(id);
                }
                if (found.size !== segmentIds.length)
                    throw new common_1.ConflictException('结束证明包含缺失或不属于本次采集的片段');
            }
            await tx.dataset.update({ where: { id: datasetId }, data: { metadata: (0, archive_recording_groups_1.inputMetadata)({ ...metadata,
                        archiveRecordingSessions: { ...sessions, [sessionId]: proof } }), updatedAt: new Date() } });
            return { sessionId, closed: true, captureIncomplete: proof.captureIncomplete, idempotent: false };
        });
    }
    async list(datasetId, path, user) {
        const dataset = await this.dataset(datasetId, user);
        const normalized = this.sensorPath(path);
        if (!normalized || (0, archive_recording_groups_1.jsonRecord)(dataset.metadata).type !== 'garage-native-archive')
            return { datasetId, path: normalized, recordings: [] };
        const groups = await this.groups(datasetId, normalized);
        const ids = groups.map(group => group.id);
        const [exports, jobs] = await Promise.all([
            this.prisma.robotFile.findMany({ where: { datasetId, recognitionType: archive_recording_groups_1.ARCHIVE_ASSEMBLY_TYPE, folderPath: `${normalized}/连续录像` } }),
            this.prisma.job.findMany({ where: { type: client_1.JobType.ARCHIVE_VIDEO_ASSEMBLE, resourceType: 'ARCHIVE_RECORDING', resourceId: { in: ids } }, orderBy: { createdAt: 'desc' } }),
        ]);
        return { datasetId, path: normalized, recordings: groups.map(group => {
                const file = group.closed && !group.error ? exports.find(item => (0, archive_recording_groups_1.jsonRecord)(item.metadata).recordingId === group.id) ?? (group.partCount === 1 ? group.sources[0] : null) : null;
                const downloadableVideos = !file && !group.error ? group.sources.filter(source => source.datasetId === datasetId
                    && source.robotId === group.robotId && source.fileSize > 0n && (0, archive_recording_groups_1.isOriginalArchiveVideo)(source))
                    .slice(0, MAX_SENSOR_PARTS).map(source => {
                    const metadata = (0, archive_recording_groups_1.jsonRecord)(source.metadata);
                    return { id: source.id, originalName: source.originalName, fileSize: source.fileSize.toString(),
                        startedAt: String(metadata.startedAt), endAt: String(metadata.endAt) };
                }) : [];
                const job = jobs.find(item => item.resourceId === group.id);
                const state = group.error ? 'failed' : !group.closed ? 'waiting_end' : file ? 'ready' : job?.status === client_1.JobStatus.RUNNING ? 'processing' : job?.status === client_1.JobStatus.FAILED || job?.status === client_1.JobStatus.CANCELLED || job?.status === client_1.JobStatus.SUCCEEDED ? 'failed' : 'pending';
                const { sources: _sources, error, ...publicGroup } = group;
                return { ...publicGroup, state, file: file ? (0, archive_recording_groups_1.inputMetadata)(file) : null, downloadableVideos, jobId: job?.id ?? null,
                    error: error ?? (state === 'failed' ? job?.status === client_1.JobStatus.SUCCEEDED ? '完整录像已删除，可重新生成' : job?.status === client_1.JobStatus.CANCELLED ? '完整录像生成已取消，可重新生成' : job?.errorMessage ?? '完整录像生成失败' : null) };
            }) };
    }
    async listPackages(datasetId, path, user) {
        const dataset = await this.dataset(datasetId, user);
        const normalized = this.sensorPath(path);
        if (!normalized || (0, archive_recording_groups_1.jsonRecord)(dataset.metadata).type !== 'garage-native-archive')
            return { datasetId, path: normalized, packages: [] };
        const groups = await this.compactPackageGroups(datasetId, normalized);
        return { datasetId, path: normalized, packages: groups.map(group => ({
                id: group.id, sensor: normalized, startedAt: group.startedAt, endAt: group.endAt,
                bytes: group.sources.reduce((sum, source) => sum + source.fileSize, 0n).toString(),
                fileCount: group.sources.length, state: 'ready',
            })) };
    }
    async folderPackage(datasetId, path, user, options = {}) {
        if (options.taskId && (!options.background || options.retry || !/^[a-f0-9]{64}$/.test(options.taskId)))
            throw new common_1.BadRequestException('压缩包任务参数无效');
        const normalized = (0, dataset_path_1.validateFolderPath)(path);
        const snapshot = await this.folderPackageSnapshot(datasetId, normalized, user);
        const now = Date.now();
        for (const [key, task] of this.folderPackageTasks) {
            if (task.state !== 'processing' && now - task.updatedAt > FOLDER_PACKAGE_TASK_TTL_MS)
                this.folderPackageTasks.delete(key);
        }
        if (options.taskId) {
            const task = [...this.folderPackageTasks.values()].find(item => item.id === options.taskId
                && item.datasetId === datasetId && item.path === normalized);
            if (!task)
                throw new common_1.ConflictException({ code: 'FOLDER_PACKAGE_EXPIRED', message: '压缩包任务已过期，请重新点击下载' });
            return this.folderPackageTaskResult(task, snapshot, user);
        }
        const folderHash = (0, node_crypto_1.createHash)('sha256').update(normalized).digest('hex');
        const digest = (0, node_crypto_1.createHash)('sha256').update(JSON.stringify({ datasetId, path: normalized,
            files: snapshot.files.map(file => this.folderPackageFileIdentity(file)), folders: snapshot.folders.map(folder => folder.path) })).digest('hex');
        const objectKey = `${(0, dataset_path_1.datasetFolder)(snapshot.dataset.name)}.folder-downloads/${folderHash}/${digest}.tar.zst`;
        const rootName = (0, dataset_path_1.validateEntryName)(normalized.split('/').at(-1) || snapshot.dataset.name);
        const members = snapshot.files.map(file => {
            (0, dataset_path_1.validateFolderPath)(file.folderPath);
            const relative = normalized ? file.folderPath.slice(normalized.length).replace(/^\//, '') : file.folderPath;
            const leaf = (0, dataset_path_1.validateEntryName)(file.originalName);
            return { file, relativePath: [rootName, relative, leaf].filter(Boolean).join('/') };
        });
        const directories = new Set([rootName, ...snapshot.folders.map(folder => {
                (0, dataset_path_1.validateFolderPath)(folder.path);
                const relative = normalized ? folder.path.slice(normalized.length).replace(/^\//, '') : folder.path;
                return [rootName, relative].filter(Boolean).join('/');
            })]);
        const names = new Set();
        for (const member of members) {
            if (names.has(member.relativePath))
                throw new common_1.ConflictException('文件夹中存在同名文件，请重命名后下载压缩包');
            names.add(member.relativePath);
            const parts = member.relativePath.split('/');
            for (let index = 1; index < parts.length; index++)
                directories.add(parts.slice(0, index).join('/'));
            if (member.file.fileSize < 0n || member.file.fileSize > BigInt(Number.MAX_SAFE_INTEGER)
                || !/^[a-f0-9]{64}$/i.test(member.file.checksum))
                throw new common_1.ConflictException('文件缺少有效大小或校验值，暂时无法打包');
        }
        if ([...directories].some(directory => names.has(directory)))
            throw new common_1.ConflictException('文件夹中存在文件和目录同名，请重命名后下载压缩包');
        const generate = async () => {
            let publishAttempted = false;
            try {
                if (!await this.storage.exists(objectKey))
                    await this.withPackageSlot(async () => {
                        const cachePrefix = objectKey.slice(0, objectKey.lastIndexOf('/') + 1);
                        await this.cleanupFolderPackageCache(cachePrefix, objectKey);
                        const sourceBytes = snapshot.files.reduce((sum, file) => sum + file.fileSize, 0n);
                        const scratch = await this.packageScratch(sourceBytes, 3n);
                        try {
                            const input = (0, node_path_1.join)(scratch, 'input');
                            const tree = (0, node_path_1.join)(input, 'files');
                            await (0, promises_1.mkdir)(tree, { recursive: true, mode: 0o700 });
                            for (const directory of directories)
                                await (0, promises_1.mkdir)((0, node_path_1.join)(tree, directory), { recursive: true, mode: 0o700 });
                            for (const { file, relativePath } of members) {
                                const target = await (0, promises_1.open)((0, node_path_1.join)(tree, relativePath), 'wx', 0o600);
                                try {
                                    const result = await (0, object_stream_1.inspectStoredObject)(this.storage, file.objectKey, async (chunk) => { await target.writeFile(chunk); }, { maxBytes: Number(file.fileSize) });
                                    if (result.size !== file.fileSize || result.checksum !== file.checksum.toLowerCase())
                                        throw new common_1.ConflictException('文件内容与入仓校验值不符，压缩包未生成');
                                }
                                finally {
                                    await target.close();
                                }
                            }
                            await (0, promises_1.writeFile)((0, node_path_1.join)(input, 'manifest.json'), JSON.stringify((0, archive_recording_groups_1.inputMetadata)({ schemaVersion: 1,
                                type: 'dataset-folder-download', datasetId, datasetName: snapshot.dataset.name, path: normalized,
                                snapshotAt: snapshot.at, fileCount: members.length, totalBytes: sourceBytes.toString(),
                                folders: [...directories], files: members.map(({ file, relativePath }) => ({
                                    id: file.id, path: `files/${relativePath}`, originalName: file.originalName, folderPath: file.folderPath,
                                    bytes: file.fileSize.toString(), sha256: file.checksum, robotId: file.robotId,
                                    capturedAt: file.capturedAt, recognitionType: file.recognitionType, schemaVersion: file.schemaVersion,
                                    mimeType: file.mimeType, fileType: file.fileType, metadata: file.metadata,
                                })) }), null, 2) + '\n', { mode: 0o600 });
                            const output = (0, node_path_1.join)(scratch, 'folder.tar.zst');
                            await this.createTarZstd(input, output);
                            await this.verifyFolderPackageSnapshot(datasetId, normalized, user, snapshot);
                            publishAttempted = true;
                            await this.publishPackage(objectKey, output);
                            await this.cleanupFolderPackageCache(cachePrefix, objectKey);
                        }
                        finally {
                            await (0, promises_1.rm)(scratch, { recursive: true, force: true });
                        }
                    });
                await this.verifyFolderPackageSnapshot(datasetId, normalized, user, snapshot);
                return { url: await this.storage.getPresignedDownloadUrl(objectKey),
                    fileName: `${rootName}_${this.packageTime(snapshot.at)}.tar.zst` };
            }
            catch (error) {
                if (publishAttempted)
                    await this.storage.deleteObject(objectKey).catch(() => undefined);
                throw error;
            }
        };
        if (!options.background)
            return generate();
        const previous = this.folderPackageTasks.get(objectKey);
        if (previous?.state === 'processing')
            return this.folderPackageTaskResult(previous, snapshot, user);
        if (previous?.state === 'failed' && !options.retry)
            throw previous.error;
        const cached = await this.storage.exists(objectKey);
        const current = this.folderPackageTasks.get(objectKey);
        if (current?.state === 'processing')
            return this.folderPackageTaskResult(current, snapshot, user);
        if (current?.state === 'failed' && !options.retry)
            throw current.error;
        if (!current && this.folderPackageTasks.size >= MAX_FOLDER_PACKAGE_TASKS) {
            const terminal = [...this.folderPackageTasks].filter(([, task]) => task.state !== 'processing')
                .sort(([, left], [, right]) => left.updatedAt - right.updatedAt)[0];
            if (terminal)
                this.folderPackageTasks.delete(terminal[0]);
            else
                throw new common_1.ServiceUnavailableException('压缩包任务已满，请稍后重试');
        }
        const task = { state: cached ? 'ready' : 'processing', updatedAt: now,
            id: (0, node_crypto_1.createHash)('sha256').update(objectKey).digest('hex'), datasetId, datasetName: snapshot.dataset.name, path: normalized, objectKey,
            fileName: `${rootName}_${this.packageTime(snapshot.at)}.tar.zst`,
            files: new Map(snapshot.files.map(file => [file.id, this.folderPackageFileHash(file)])),
            folders: new Set(snapshot.folders.map(folder => folder.path)) };
        this.folderPackageTasks.set(objectKey, task);
        if (cached)
            return this.folderPackageTaskResult(task, snapshot, user);
        void generate().then(() => {
            task.state = 'ready';
            task.updatedAt = Date.now();
        }, error => {
            task.state = 'failed';
            task.updatedAt = Date.now();
            task.error = error instanceof common_1.HttpException ? error : new common_1.ServiceUnavailableException({
                code: 'FOLDER_PACKAGE_FAILED', message: '文件夹压缩包生成失败，原始文件已保留，请重试',
            });
        });
        return { state: 'processing', retryAfterMs: 2000, taskId: task.id };
    }
    folderPackageFileHash(file) {
        return (0, node_crypto_1.createHash)('sha256').update(this.folderPackageFileIdentity(file)).digest('hex');
    }
    async folderPackageTaskResult(task, snapshot, user) {
        const current = task.state === 'ready' ? await this.folderPackageSnapshot(task.datasetId, task.path, user) : snapshot;
        const files = new Map(current.files.map(file => [file.id, this.folderPackageFileHash(file)]));
        const folders = new Set(current.folders.map(folder => folder.path));
        if (current.dataset.name !== task.datasetName || [...task.files].some(([id, hash]) => files.get(id) !== hash)
            || [...task.folders].some(path => !folders.has(path)))
            throw new common_1.ConflictException('文件夹内容已被删除、移动或修改，请刷新后重试');
        if (task.state === 'failed')
            throw task.error;
        if (task.state === 'processing')
            return { state: 'processing', retryAfterMs: 2000, taskId: task.id };
        if (!await this.storage.exists(task.objectKey))
            throw new common_1.ConflictException({
                code: 'FOLDER_PACKAGE_EXPIRED', message: '压缩包缓存已过期，请重新点击下载',
            });
        return { state: 'ready', taskId: task.id,
            url: await this.storage.getPresignedDownloadUrl(task.objectKey), fileName: task.fileName };
    }
    async cleanupFolderPackageCache(prefix, keepKey) {
        const entries = [];
        const cursors = new Set();
        let cursor;
        do {
            const page = await this.storage.listObjects({ prefix, cursor, limit: 1000 });
            for (const entry of page.items) {
                if (!entry.objectKey.startsWith(prefix) || !/^[a-f0-9]{64}\.tar\.zst$/.test(entry.objectKey.slice(prefix.length)))
                    throw new common_1.ConflictException('下载缓存目录内容无效');
                entries.push(entry);
            }
            cursor = page.nextCursor;
            if (entries.length > MAX_FOLDER_ENTRIES || cursor && cursors.has(cursor))
                throw new common_1.ServiceUnavailableException('下载缓存清理尚未完成，请稍后重试');
            if (cursor)
                cursors.add(cursor);
        } while (cursor);
        const recent = entries.filter(entry => entry.objectKey !== keepKey
            && (entry.lastModified?.getTime() ?? 0) >= Date.now() - 3600_000)
            .sort((a, b) => (b.lastModified?.getTime() ?? 0) - (a.lastModified?.getTime() ?? 0))[0]?.objectKey;
        const obsolete = entries.filter(entry => entry.objectKey !== keepKey && entry.objectKey !== recent).map(entry => entry.objectKey);
        if (obsolete.length)
            await this.storage.deleteObjects(obsolete);
    }
    async folderPackageSnapshot(datasetId, path, user) {
        const dataset = await this.dataset(datasetId, user);
        if (path && !await this.prisma.datasetFolder.findUnique({ where: { datasetId_path: { datasetId, path } } }))
            throw new common_1.NotFoundException('文件夹不存在');
        const descendantPrefix = path.replace(/[\\%_]/g, '\\$&') + '/';
        const [files, folders] = await Promise.all([
            this.prisma.robotFile.findMany({ where: { datasetId,
                    ...(path ? { OR: [{ folderPath: path }, { folderPath: { startsWith: descendantPrefix } }] } : {}) },
                orderBy: { id: 'asc' }, take: MAX_FOLDER_ENTRIES + 1 }),
            this.prisma.datasetFolder.findMany({ where: { datasetId,
                    ...(path ? { OR: [{ path }, { path: { startsWith: descendantPrefix } }] } : {}) },
                select: { path: true }, orderBy: { path: 'asc' }, take: MAX_FOLDER_ENTRIES + 1 }),
        ]);
        if (files.length > MAX_FOLDER_ENTRIES || folders.length > MAX_FOLDER_ENTRIES)
            throw new common_1.BadRequestException('文件夹条目超过单次打包上限，请进入子文件夹分别下载');
        if (files.some(file => file.datasetId !== datasetId || path && file.folderPath !== path && !file.folderPath.startsWith(path + '/'))
            || folders.some(folder => path && folder.path !== path && !folder.path.startsWith(path + '/')))
            throw new common_1.ConflictException('文件夹内容范围无效');
        return { dataset, files, folders, at: new Date().toISOString() };
    }
    async verifyFolderPackageSnapshot(datasetId, path, user, previous) {
        const current = await this.folderPackageSnapshot(datasetId, path, user);
        const files = new Map(current.files.map(file => [file.id, this.folderPackageFileIdentity(file)]));
        const folders = new Set(current.folders.map(folder => folder.path));
        if (current.dataset.name !== previous.dataset.name
            || previous.files.some(file => files.get(file.id) !== this.folderPackageFileIdentity(file))
            || previous.folders.some(folder => !folders.has(folder.path)))
            throw new common_1.ConflictException('文件夹内容已被删除、移动或修改，请刷新后重试');
    }
    folderPackageFileIdentity(file) {
        return JSON.stringify({ source: (0, archive_recording_groups_1.sourceFingerprint)(file), originalName: file.originalName,
            capturedAt: file.capturedAt, mimeType: file.mimeType, fileType: file.fileType,
            recognitionType: file.recognitionType, schemaVersion: file.schemaVersion });
    }
    async package(datasetId, recordingId, user) {
        if (!/^[a-f0-9]{32,64}$/.test(recordingId))
            throw new common_1.BadRequestException('归档片段标识无效');
        const dataset = await this.dataset(datasetId, user);
        if ((0, archive_recording_groups_1.jsonRecord)(dataset.metadata).type !== 'garage-native-archive')
            throw new common_1.NotFoundException('地库录像不存在');
        for (const path of await this.sensorPaths(datasetId)) {
            const group = (await this.groups(datasetId, path)).find(item => item.id === recordingId);
            if (!group)
                continue;
            if (!group.closed || group.error)
                throw new common_1.ConflictException('采集尚未封装完成，暂时无法下载压缩包');
            const exportFile = await this.prisma.robotFile.findFirst({ where: { datasetId, recognitionType: archive_recording_groups_1.ARCHIVE_ASSEMBLY_TYPE, folderPath: `${path}/连续录像`, metadata: { path: ['recordingId'], equals: group.id } } });
            const source = exportFile ?? (group.partCount === 1 ? group.sources[0] : null);
            if (!source)
                throw new common_1.ConflictException('请先生成完整录像，再下载压缩包');
            const packageKey = `${(0, dataset_path_1.datasetFolder)(dataset.name)}${path}/连续录像/${group.id}/${group.id}.tar.zst`;
            if (!await this.storage.exists(packageKey))
                await this.withPackageSlot(async () => {
                    const scratch = await this.packageScratch(source.fileSize);
                    try {
                        const input = (0, node_path_1.join)(scratch, 'input');
                        await (0, promises_1.mkdir)(input, { mode: 0o700 });
                        const videoPath = (0, node_path_1.join)(input, source.originalName.replace(/[^a-zA-Z0-9._-]/g, '_') || 'recording.mkv');
                        const stream = await this.storage.getObject(source.objectKey);
                        await (0, promises_2.pipeline)(stream, (0, node_fs_1.createWriteStream)(videoPath, { mode: 0o600, flags: 'wx' }));
                        await (0, promises_1.writeFile)((0, node_path_1.join)(input, 'manifest.json'), JSON.stringify({ schemaVersion: 1, datasetId, sensor: path, recordingId: group.id, startedAt: group.startedAt, endAt: group.endAt, source: { originalName: source.originalName, checksum: source.checksum, bytes: source.fileSize.toString() } }, null, 2) + '\n', { mode: 0o600 });
                        const packagePath = (0, node_path_1.join)(scratch, `${group.id}.tar.zst`);
                        await this.createTarZstd(input, packagePath);
                        await this.publishPackage(packageKey, packagePath);
                    }
                    finally {
                        await (0, promises_1.rm)(scratch, { recursive: true, force: true });
                    }
                });
            return { url: await this.storage.getPresignedDownloadUrl(packageKey), fileName: `${path}_${this.packageTime(group.startedAt)}_${this.packageTime(group.endAt)}.tar.zst` };
        }
        for (const path of await this.sensorPaths(datasetId)) {
            const group = (await this.compactPackageGroups(datasetId, path)).find(item => item.id === recordingId);
            if (!group)
                continue;
            const packageKey = `${(0, dataset_path_1.datasetFolder)(dataset.name)}${path}/连续数据包/${group.id}.tar.zst`;
            if (!await this.storage.exists(packageKey))
                await this.withPackageSlot(() => this.createCompactPackage(packageKey, datasetId, path, group));
            return { url: await this.storage.getPresignedDownloadUrl(packageKey), fileName: `${path}_${this.packageTime(group.startedAt)}_${this.packageTime(group.endAt)}_数据包.tar.zst` };
        }
        throw new common_1.NotFoundException('录像片段已变化，请刷新后重试');
    }
    async compactPackageGroups(datasetId, path) {
        const rows = await this.prisma.robotFile.findMany({
            where: { datasetId, folderPath: { startsWith: `${path}/` }, recognitionType: { startsWith: 'garage-native-' } },
            orderBy: [{ capturedAt: 'asc' }, { id: 'asc' }], take: MAX_SENSOR_PARTS * 4,
        });
        const groups = new Map();
        for (const row of rows) {
            const metadata = (0, archive_recording_groups_1.jsonRecord)(row.metadata);
            const segmentId = typeof metadata.segmentId === 'string' ? metadata.segmentId : undefined;
            const relative = typeof metadata.relativePath === 'string' ? metadata.relativePath : row.originalName;
            const compact = /(?:\.mkv|\.zst|compressed-bag\/metadata\.yaml)$/i.test(relative);
            if (!segmentId || !/^[a-f0-9]{32,64}$/.test(segmentId) || !compact
                || typeof metadata.startedAt !== 'string' || typeof metadata.endAt !== 'string')
                continue;
            const group = groups.get(segmentId) ?? { id: segmentId, startedAt: metadata.startedAt, endAt: metadata.endAt, sources: [] };
            group.sources.push(row);
            groups.set(segmentId, group);
        }
        return [...groups.values()].filter(group => group.sources.length > 0);
    }
    async createCompactPackage(packageKey, datasetId, sensor, group) {
        const scratch = await this.packageScratch(group.sources.reduce((sum, source) => sum + source.fileSize, 0n));
        try {
            const input = (0, node_path_1.join)(scratch, 'input');
            await (0, promises_1.mkdir)(input, { mode: 0o700 });
            for (const source of group.sources) {
                const relative = (0, archive_recording_groups_1.jsonRecord)(source.metadata).relativePath;
                const name = typeof relative === 'string' && relative.length ? relative : source.originalName;
                const safe = name.split('/').filter(part => part && part !== '.' && part !== '..').join('/') || 'archive.bin';
                const destination = (0, node_path_1.join)(input, safe);
                await (0, promises_1.mkdir)((0, node_path_1.join)(destination, '..'), { recursive: true, mode: 0o700 });
                await (0, promises_2.pipeline)(await this.storage.getObject(source.objectKey), (0, node_fs_1.createWriteStream)(destination, { mode: 0o600, flags: 'wx' }));
            }
            await (0, promises_1.writeFile)((0, node_path_1.join)(input, 'manifest.json'), JSON.stringify({ schemaVersion: 1, datasetId, sensor, recordingId: group.id, startedAt: group.startedAt, endAt: group.endAt, files: group.sources.map(source => ({ name: source.originalName, checksum: source.checksum, bytes: source.fileSize.toString() })) }, null, 2) + '\n', { mode: 0o600 });
            const output = (0, node_path_1.join)(scratch, `${group.id}.tar.zst`);
            await this.createTarZstd(input, output);
            await this.publishPackage(packageKey, output);
        }
        finally {
            await (0, promises_1.rm)(scratch, { recursive: true, force: true });
        }
    }
    async withPackageSlot(work) {
        if (this.packaging)
            throw new common_1.ServiceUnavailableException('已有压缩包正在生成，请稍后重试');
        this.packaging = true;
        try {
            await work();
        }
        finally {
            this.packaging = false;
        }
    }
    async packageScratch(sourceBytes, copies = 2n) {
        const base = (0, node_path_1.resolve)(this.config.get('ARCHIVE_VIDEO_WORK_DIR') ?? './data/archive-video-assembly');
        await (0, promises_1.mkdir)(base, { recursive: true, mode: 0o700 });
        const disk = await (0, promises_1.statfs)(base, { bigint: true });
        if (disk.bavail * disk.bsize < sourceBytes * copies + 64n * 1024n * 1024n)
            throw new common_1.ServiceUnavailableException('压缩包临时磁盘空间不足，原始文件已保留');
        return (0, promises_1.mkdtemp)((0, node_path_1.join)(base, 'package-'));
    }
    async publishPackage(objectKey, path) {
        const info = await (0, promises_1.stat)(path);
        if (!info.isFile() || !Number.isSafeInteger(info.size) || info.size <= 0)
            throw new common_1.ConflictException('压缩包文件无效');
        const content = (0, node_fs_1.createReadStream)(path);
        try {
            await this.storage.putObject({ objectKey, content, contentLength: info.size, contentType: 'application/zstd' });
        }
        finally {
            content.destroy();
        }
    }
    packageTime(value) {
        return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value)).replaceAll('-', '') + '_' + new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value)).replaceAll(':', '');
    }
    async createTarZstd(directory, output) {
        await new Promise((resolvePromise, reject) => {
            const child = (0, node_child_process_1.spawn)('tar', ['-C', directory, '-cf', '-', '.'], { stdio: ['ignore', 'pipe', 'pipe'] });
            const compressor = (0, node_child_process_1.spawn)('zstd', ['-T1', '-q', '-o', output], { stdio: ['pipe', 'ignore', 'pipe'] });
            let stderr = '', failure, closed = 0, pipeDone = false, settled = false;
            let timer, killTimer;
            const done = () => {
                if (settled || closed !== 2 || !pipeDone)
                    return;
                settled = true;
                clearTimeout(timer);
                clearTimeout(killTimer);
                if (failure)
                    reject(failure);
                else
                    resolvePromise();
            };
            const fail = (error) => {
                if (!failure) {
                    failure = error;
                    killTimer = setTimeout(() => { child.kill('SIGKILL'); compressor.kill('SIGKILL'); }, 1000);
                    killTimer.unref();
                    child.kill('SIGTERM');
                    compressor.kill('SIGTERM');
                }
                done();
            };
            for (const [command, process] of [['tar', child], ['zstd', compressor]]) {
                process.stderr.on('data', (chunk) => { stderr = (stderr + chunk.toString('utf8')).slice(-2000); });
                process.once('error', (error) => fail(new common_1.ServiceUnavailableException({
                    code: error.code === 'ENOENT' ? 'FOLDER_PACKAGE_COMMAND_MISSING' : 'FOLDER_PACKAGE_COMMAND_FAILED',
                    message: error.code === 'ENOENT' ? `服务器缺少 ${command} 打包工具，请联系管理员` : '压缩包命令启动失败，请稍后重试',
                }, { cause: error })));
                process.once('close', code => {
                    closed += 1;
                    if (code !== 0)
                        fail(new common_1.ServiceUnavailableException({ code: 'FOLDER_PACKAGE_COMPRESSION_FAILED',
                            message: '压缩包生成失败，原始文件已保留，请重试',
                        }, { cause: new Error(`${command}: ${stderr || `exit=${code}`}`) }));
                    done();
                });
            }
            timer = setTimeout(() => fail(new common_1.ServiceUnavailableException('压缩包生成超时，原始文件已保留，请稍后重试')), PACKAGE_TIMEOUT_MS);
            timer.unref();
            void (0, promises_2.pipeline)(child.stdout, compressor.stdin).then(() => { pipeDone = true; done(); }, error => { pipeDone = true; fail(error); });
        });
    }
    async requestAssembly(datasetId, recordingId, user) {
        if (!/^[a-f0-9]{64}$/.test(recordingId))
            throw new common_1.BadRequestException('录像标识无效');
        const dataset = await this.dataset(datasetId, user);
        if ((0, archive_recording_groups_1.jsonRecord)(dataset.metadata).type !== 'garage-native-archive')
            throw new common_1.NotFoundException('地库录像不存在');
        const sensors = await this.sensorPaths(datasetId);
        for (const path of sensors) {
            const group = (await this.groups(datasetId, path)).find(item => item.id === recordingId);
            if (group)
                return { jobId: (await this.enqueue(dataset, group, user))?.id ?? null };
        }
        throw new common_1.NotFoundException('录像片段已变化，请刷新后重试');
    }
    async dataset(id, user) {
        const value = await this.prisma.dataset.findFirst({ where: { id, deletingAt: null, ...(user ? (0, warehouse_query_service_1.warehouseScope)(user) : {}) } });
        if (!value)
            throw new common_1.NotFoundException('数据集不存在');
        return value;
    }
    sensorPath(path) {
        (0, dataset_path_1.validateFolderPath)(path);
        if (path.includes('/'))
            throw new common_1.BadRequestException('请在传感器目录查看完整录像');
        return path;
    }
    async sensorPaths(datasetId) {
        const folders = await this.prisma.datasetFolder.findMany({ where: { datasetId, parentPath: '' }, select: { path: true }, take: 100 });
        return folders.map(folder => folder.path);
    }
    async groups(datasetId, path) {
        const sources = await this.prisma.robotFile.findMany({ where: { datasetId, folderPath: { startsWith: `${path}/` }, recognitionType: { startsWith: 'garage-native-' }, originalName: { endsWith: '.mkv', mode: 'insensitive' } }, take: MAX_SENSOR_PARTS + 1 });
        if (sources.length > MAX_SENSOR_PARTS)
            throw new common_1.ConflictException('该传感器单日片段过多，请联系管理员分批处理');
        const dataset = await this.prisma.dataset.findFirst({ where: { id: datasetId, deletingAt: null }, select: { metadata: true } });
        return (0, archive_recording_groups_1.groupArchiveRecordings)(datasetId, path, sources, (0, archive_recording_groups_1.jsonRecord)(dataset?.metadata).archiveRecordingSessions);
    }
    async enqueue(dataset, group, user) {
        if (!group.closed)
            throw new common_1.ConflictException('采集尚未结束，请等待关闭后生成完整录像');
        if (group.error)
            throw new common_1.ConflictException(group.error);
        if (group.partCount === 1)
            return null;
        const key = `archive-video:${dataset.id}:${group.id}`;
        let existing = await this.prisma.job.findUnique({ where: { deduplicationKey: key } });
        if (existing) {
            if (user && existing.status === client_1.JobStatus.FAILED)
                return this.jobs.retry(existing.id, user);
            if (user && (existing.status === client_1.JobStatus.SUCCEEDED || existing.status === client_1.JobStatus.CANCELLED))
                return this.resetMissingExport(existing.id, dataset.id, group.id);
            return existing;
        }
        try {
            return await this.jobs.create(client_1.JobType.ARCHIVE_VIDEO_ASSEMBLE, { payload: { datasetId: dataset.id, path: group.path, recordingId: group.id },
                resourceType: 'ARCHIVE_RECORDING', resourceId: group.id, deduplicationKey: key,
                requestedById: user?.id, tenantId: dataset.tenantId, projectId: dataset.projectId, maxAttempts: 3 });
        }
        catch (error) {
            if (error instanceof client_1.Prisma.PrismaClientKnownRequestError && error.code === 'P2002') {
                existing = await this.prisma.job.findUnique({ where: { deduplicationKey: key } });
                if (existing)
                    return existing;
            }
            throw error;
        }
    }
    async resetMissingExport(jobId, datasetId, recordingId) {
        return this.prisma.$transaction(async (tx) => {
            await tx.$queryRaw(client_1.Prisma.sql `SELECT id FROM "Job" WHERE id = ${jobId}::uuid FOR UPDATE`);
            const current = await tx.job.findUniqueOrThrow({ where: { id: jobId } });
            if (current.status !== client_1.JobStatus.SUCCEEDED && current.status !== client_1.JobStatus.CANCELLED)
                return current;
            const file = await tx.robotFile.findFirst({ where: { datasetId, recognitionType: archive_recording_groups_1.ARCHIVE_ASSEMBLY_TYPE, metadata: { path: ['recordingId'], equals: recordingId } } });
            if (file)
                return current;
            await tx.jobStep.updateMany({ where: { jobId }, data: { status: 'PENDING', progress: 0, errorMessage: null, startedAt: null, finishedAt: null } });
            return tx.job.update({ where: { id: jobId }, data: { status: client_1.JobStatus.QUEUED, progress: 0, attempts: 0, cancelRequested: false, result: client_1.Prisma.DbNull,
                    startedAt: null, finishedAt: null, lockedAt: null, lockedBy: null, heartbeatAt: null, errorCode: null, errorMessage: null, availableAt: new Date() } });
        });
    }
    async scanClosedRecordings(owner) {
        if (Date.now() < this.nextScanAt)
            return;
        this.nextScanAt = Date.now() + 5_000;
        if (!await this.jobs.acquireLease('archive-recording-scan', owner, 30_000))
            return;
        try {
            const dirty = this.pendingSensors;
            const add = (datasetId, path) => {
                const key = `${datasetId}:${path}`;
                if (!dirty.has(key))
                    dirty.set(key, { datasetId, path, retryAt: 0 });
            };
            if (!this.backfillDone && dirty.size < 200) {
                const datasets = await this.prisma.dataset.findMany({ where: { deletingAt: null, metadata: { path: ['type'], equals: 'garage-native-archive' }, ...(this.backfillCursor ? { id: { gt: this.backfillCursor } } : {}) }, orderBy: { id: 'asc' }, take: 2 });
                for (const dataset of datasets) {
                    for (const path of await this.sensorPaths(dataset.id))
                        add(dataset.id, path);
                    this.backfillCursor = dataset.id;
                }
                if (datasets.length < 2)
                    this.backfillDone = true;
            }
            const rows = await this.prisma.robotFile.findMany({ where: { recognitionType: { startsWith: 'garage-native-' }, datasetId: { not: null }, OR: [{ createdAt: { gt: this.liveCursor.createdAt } }, { createdAt: this.liveCursor.createdAt, id: { gt: this.liveCursor.id } }] }, orderBy: [{ createdAt: 'asc' }, { id: 'asc' }], take: 50 });
            for (const row of rows) {
                this.liveCursor = { createdAt: row.createdAt, id: row.id };
                if (!(0, archive_recording_groups_1.isOriginalArchiveVideo)(row) || !row.datasetId)
                    continue;
                const path = row.folderPath.split('/')[0];
                if (!path)
                    continue;
                add(row.datasetId, path);
            }
            const closedDatasets = await this.prisma.dataset.findMany({ where: { deletingAt: null,
                    metadata: { path: ['type'], equals: 'garage-native-archive' }, OR: [{ updatedAt: { gt: this.closureCursor.updatedAt } },
                        { updatedAt: this.closureCursor.updatedAt, id: { gt: this.closureCursor.id } }] },
                orderBy: [{ updatedAt: 'asc' }, { id: 'asc' }], take: 2 });
            for (const dataset of closedDatasets) {
                if (Object.keys((0, archive_recording_groups_1.jsonRecord)((0, archive_recording_groups_1.jsonRecord)(dataset.metadata).archiveRecordingSessions)).length)
                    for (const path of await this.sensorPaths(dataset.id))
                        add(dataset.id, path);
                this.closureCursor = { updatedAt: dataset.updatedAt ?? new Date(), id: dataset.id };
            }
            let deferred;
            for (const [key, value] of [...dirty].filter(([, item]) => item.retryAt <= Date.now()).slice(0, 10)) {
                try {
                    const dataset = await this.prisma.dataset.findFirst({ where: { id: value.datasetId, deletingAt: null } });
                    if (dataset && (0, archive_recording_groups_1.jsonRecord)(dataset.metadata).type === 'garage-native-archive') {
                        for (const group of await this.groups(value.datasetId, value.path))
                            if (group.closed && !group.error && group.partCount > 1)
                                await this.enqueue(dataset, group);
                    }
                    dirty.delete(key);
                }
                catch (error) {
                    dirty.delete(key);
                    dirty.set(key, { ...value, retryAt: Date.now() + 30_000 });
                    deferred = error;
                }
            }
            if (deferred)
                throw deferred;
        }
        finally {
            await this.jobs.releaseLease('archive-recording-scan', owner);
        }
    }
    async assemble(job, workerId) {
        const payload = (0, archive_recording_groups_1.jsonRecord)(job.payload);
        if (typeof payload.datasetId !== 'string' || typeof payload.path !== 'string' || typeof payload.recordingId !== 'string')
            throw new common_1.ConflictException('录像任务参数无效');
        const path = this.sensorPath(payload.path), dataset = await this.dataset(payload.datasetId);
        const group = (await this.groups(dataset.id, path)).find(item => item.id === payload.recordingId);
        if (!group || !group.closed || group.error)
            throw new common_1.ConflictException('原始录像已变化或尚未封装完成，请刷新后重试');
        let groupCheckedAt = Date.now();
        let groupCheck;
        let obsoleteGroup;
        const existing = await this.prisma.robotFile.findFirst({ where: { datasetId: dataset.id, recognitionType: archive_recording_groups_1.ARCHIVE_ASSEMBLY_TYPE, metadata: { path: ['recordingId'], equals: group.id } } });
        if (existing)
            return { fileId: existing.id, recordingId: group.id };
        if (group.partCount === 1)
            return { fileId: group.sources[0].id, recordingId: group.id };
        const lease = 'archive-video-assembly', owner = `${workerId}:${job.id}`;
        if (!await this.jobs.acquireLease(lease, owner))
            throw new ArchiveAssemblyBusyError('录像合并资源繁忙，稍后自动重试');
        let leaseLost = false;
        const timer = setInterval(() => void this.jobs.heartbeatLease(lease, owner).then(result => { if (result.count !== 1)
            leaseLost = true; }).catch(() => { leaseLost = true; }), 10_000);
        const check = async () => {
            if (obsoleteGroup)
                throw obsoleteGroup;
            if (leaseLost)
                throw new common_1.ServiceUnavailableException('录像处理租约已失效');
            const ownership = await this.prisma.job.findUnique({ where: { id: job.id }, select: { lockedBy: true, status: true, cancelRequested: true } });
            if (!ownership || ownership.cancelRequested)
                throw new ArchiveAssemblyCancelledError();
            if (ownership.lockedBy !== workerId || ownership.status !== client_1.JobStatus.RUNNING)
                throw new common_1.ServiceUnavailableException('录像任务所有权已失效');
            if (obsoleteGroup)
                throw obsoleteGroup;
            if (!groupCheck && Date.now() - groupCheckedAt >= ASSEMBLY_GROUP_CHECK_MS) {
                groupCheck = this.groups(dataset.id, path).then(groups => {
                    const current = groups.find(item => item.id === group.id);
                    if (!current || !current.closed || current.error) {
                        obsoleteGroup = new ArchiveAssemblyCancelledError('原始录像分组已变化，已取消过时的生成任务；原始片段保留');
                        throw obsoleteGroup;
                    }
                    groupCheckedAt = Date.now();
                }).finally(() => { groupCheck = undefined; });
            }
            if (groupCheck)
                await groupCheck;
        };
        const attemptId = (0, node_crypto_1.randomUUID)();
        let scratch, finalKey, published = false;
        const staged = [];
        try {
            await check();
            const base = (0, node_path_1.resolve)(this.config.get('ARCHIVE_VIDEO_WORK_DIR') ?? './data/archive-video-assembly');
            await (0, promises_1.mkdir)(base, { recursive: true, mode: 0o700 });
            const disk = await (0, promises_1.statfs)(base, { bigint: true });
            const required = BigInt(group.sourceBytes) * 5n + 256n * 1024n * 1024n;
            if (disk.bavail * disk.bsize < required)
                throw new common_1.ServiceUnavailableException('录像处理临时磁盘空间不足，原片段已保留');
            scratch = await (0, promises_1.mkdtemp)((0, node_path_1.join)(base, 'assembly-'));
            const sources = [];
            const identities = new Map();
            for (const source of group.sources) {
                await check();
                const local = (0, node_path_1.join)(scratch, `${source.id}.mkv`);
                identities.set(source.id, await this.downloadSource(source, local, check));
                const metadata = (0, archive_recording_groups_1.jsonRecord)(source.metadata);
                sources.push({ id: source.id, path: local, checksum: source.checksum, size: source.fileSize.toString(), startedAt: metadata.startedAt, endAt: metadata.endAt, frameCount: metadata.receivedFrames,
                    metadata: { videoAttachments: metadata.videoAttachments, videoTimecodeScaleNs: metadata.videoTimecodeScaleNs } });
            }
            const input = (0, node_path_1.join)(scratch, 'input.json'), output = (0, node_path_1.join)(scratch, 'video.mkv'), resultPath = (0, node_path_1.join)(scratch, 'result.json');
            await (0, promises_1.writeFile)(input, JSON.stringify({ schemaVersion: 1, recordingId: group.id, robotId: group.robotId, sensor: group.sensor, startedAt: group.startedAt, endAt: group.endAt, sources }), { mode: 0o600 });
            await this.runHelper(input, output, resultPath, check);
            const result = this.validateResult(JSON.parse(await (0, promises_1.readFile)(resultPath, 'utf8')), group);
            const outputInfo = await this.hashFile(output, check);
            if (outputInfo.size.toString() !== result.size || outputInfo.sha256 !== result.sha256)
                throw new common_1.ConflictException('完整录像输出校验失败');
            await check();
            finalKey = `${(0, dataset_path_1.datasetFolder)(dataset.name)}${path}/连续录像/${group.id}/${attemptId}/video.mkv`;
            const descriptor = await (0, promises_1.open)(output, 'r');
            try {
                let part = 0;
                for (;;) {
                    await check();
                    const buffer = Buffer.allocUnsafe(STAGE_BYTES), { bytesRead } = await descriptor.read(buffer, 0, buffer.length, null);
                    if (!bytesRead)
                        break;
                    const key = `uploads/archive-assembly/${job.id}/${attemptId}/${++part}`;
                    staged.push(key);
                    await this.storage.putObject({ objectKey: key, content: buffer.subarray(0, bytesRead), contentType: 'application/octet-stream' });
                }
            }
            finally {
                await descriptor.close();
            }
            if (!staged.length || staged.length > 10_000)
                throw new common_1.ConflictException('录像输出为空或超出对象存储分片数限制');
            await check();
            await this.storage.composeObject(finalKey, staged, 'video/x-matroska');
            const remote = await this.hashStream(await this.storage.getObject(finalKey), check);
            if (remote.size.toString() !== result.size || remote.sha256 !== result.sha256)
                throw new common_1.ConflictException('完整录像存储回读校验失败');
            await check();
            for (const source of group.sources) {
                const current = await this.storage.statObject(source.objectKey);
                const identity = identities.get(source.id);
                if (BigInt(current.size) !== source.fileSize || (identity.etag && current.etag !== identity.etag)
                    || (identity.lastModified && current.lastModified?.getTime() !== identity.lastModified.getTime()))
                    throw new common_1.ConflictException('原始录像已删除或变化，录像不会发布');
            }
            const file = await this.prisma.$transaction(async (tx) => {
                await tx.$queryRaw(client_1.Prisma.sql `SELECT id FROM "Dataset" WHERE id = ${dataset.id}::uuid FOR UPDATE`);
                await tx.$queryRaw(client_1.Prisma.sql `SELECT id FROM "Job" WHERE id = ${job.id}::uuid FOR UPDATE`);
                const jobState = await tx.job.findUnique({ where: { id: job.id }, select: { lockedBy: true, status: true, cancelRequested: true } });
                if (!jobState || jobState.cancelRequested)
                    throw new ArchiveAssemblyCancelledError();
                if (jobState.lockedBy !== workerId || jobState.status !== client_1.JobStatus.RUNNING)
                    throw new common_1.ServiceUnavailableException('录像任务所有权已失效');
                const currentDataset = await tx.dataset.findUnique({ where: { id: dataset.id } });
                if (!currentDataset || currentDataset.deletingAt)
                    throw new common_1.ConflictException('数据集已删除，录像不会发布');
                const fresh = await tx.robotFile.findMany({ where: { datasetId: dataset.id, folderPath: { startsWith: `${path}/` }, recognitionType: { startsWith: 'garage-native-' }, originalName: { endsWith: '.mkv', mode: 'insensitive' } }, take: MAX_SENSOR_PARTS + 1 });
                if (fresh.length > MAX_SENSOR_PARTS || !(0, archive_recording_groups_1.groupArchiveRecordings)(dataset.id, path, fresh, (0, archive_recording_groups_1.jsonRecord)(currentDataset.metadata).archiveRecordingSessions).some(item => item.id === group.id && item.closed && !item.error))
                    throw new common_1.ConflictException('原始录像已变化，录像不会发布');
                await check();
                await (0, dataset_folders_1.ensureDatasetFolders)(tx, this.storage, currentDataset, `${path}/连续录像`);
                const duplicate = await tx.robotFile.findUnique({ where: { objectKey: finalKey } });
                if (duplicate) {
                    if (duplicate.checksum !== result.sha256)
                        throw new common_1.ConflictException('完整录像对象冲突');
                    return duplicate;
                }
                return tx.robotFile.create({ data: { datasetId: dataset.id, robotId: group.robotId, folderPath: `${path}/连续录像`, objectKey: finalKey,
                        originalName: this.videoName(group), fileType: 'mkv', mimeType: 'video/x-matroska', fileSize: BigInt(result.size), checksum: result.sha256,
                        recognitionType: archive_recording_groups_1.ARCHIVE_ASSEMBLY_TYPE, schemaVersion: '1.0', capturedAt: new Date(group.startedAt),
                        metadata: (0, archive_recording_groups_1.inputMetadata)({ archiveType: 'garage-native-archive', archiveRole: archive_recording_groups_1.ARCHIVE_ASSEMBLY_ROLE, recordingId: group.id, sensor: group.sensor,
                            startedAt: group.startedAt, endAt: group.endAt, sourceIds: group.sources.map(source => source.id), sourceChecksums: group.sources.map(source => source.checksum),
                            partCount: group.partCount, sourceBytes: group.sourceBytes, ...result }) } });
            }, { timeout: 60_000 });
            published = true;
            return { fileId: file.id, recordingId: group.id, frameCount: result.frameCount, bytes: result.size };
        }
        finally {
            try {
                if (staged.length)
                    await this.storage.deleteObjects(staged).catch(() => undefined);
                if (finalKey && !published) {
                    const attached = await this.prisma.robotFile.findUnique({ where: { objectKey: finalKey }, select: { id: true } }).catch(() => ({ id: 'unknown' }));
                    if (!attached)
                        await this.storage.deleteObject(finalKey).catch(() => undefined);
                }
                if (scratch)
                    await (0, promises_1.rm)(scratch, { recursive: true, force: true });
            }
            finally {
                clearInterval(timer);
                await this.jobs.releaseLease(lease, owner).catch(() => undefined);
            }
        }
    }
    videoName(group) {
        const time = (value) => new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value)).replaceAll(':', '-');
        return `${group.sensor}_${time(group.startedAt)}_${time(group.endAt)}_完整录像.mkv`;
    }
    validateResult(value, group) {
        const result = (0, archive_recording_groups_1.jsonRecord)(value);
        if (result.schemaVersion !== 1 || result.losslessRemux !== true || result.packetSha256Validated !== true || result.timecodeScaleNs !== 1000
            || result.sourceCount !== group.partCount || result.frameCount !== group.frameCount || !/^[a-f0-9]{64}$/.test(String(result.sha256))
            || !/^[a-f0-9]{64}$/.test(String(result.indexSha256)) || !/^[1-9][0-9]*$/.test(String(result.size))
            || typeof result.durationSeconds !== 'number' || !Number.isFinite(result.durationSeconds) || result.durationSeconds <= 0
            || typeof result.codec !== 'string' || !['hevc', 'mjpeg', 'ffv1'].includes(result.codec)
            || !Number.isSafeInteger(result.width) || Number(result.width) <= 0 || !Number.isSafeInteger(result.height) || Number(result.height) <= 0
            || !Array.isArray(result.attachments) || result.attachments.filter(item => ['index.jsonl', 'frames.index.jsonl'].includes(String((0, archive_recording_groups_1.jsonRecord)(item).name)) && (0, archive_recording_groups_1.jsonRecord)(item).sha256 === result.indexSha256).length !== 1
            || !Array.isArray(result.gaps))
            throw new common_1.ConflictException('录像封装程序未通过完整性验证');
        return result;
    }
    async downloadSource(source, destination, check) {
        const object = await this.storage.statObject(source.objectKey);
        if (BigInt(object.size) !== source.fileSize)
            throw new common_1.ConflictException('原始录像大小已变化');
        const stream = await this.storage.getObject(source.objectKey);
        const guard = this.guardStream(stream, check);
        const hash = (0, node_crypto_1.createHash)('sha256');
        let size = 0n, lastCheck = 0;
        const verify = new node_stream_1.Transform({ transform: (chunk, _encoding, callback) => {
                guard.touch();
                hash.update(chunk);
                size += BigInt(chunk.length);
                if (size > source.fileSize)
                    return callback(new common_1.ConflictException('原始录像大小校验失败'));
                if (Date.now() - lastCheck > 1_000) {
                    lastCheck = Date.now();
                    void check().then(() => callback(null, chunk), callback);
                }
                else
                    callback(null, chunk);
            } });
        try {
            await (0, promises_2.pipeline)(stream, verify, (0, node_fs_1.createWriteStream)(destination, { mode: 0o600, flags: 'wx' }));
        }
        finally {
            guard.stop();
        }
        if (size !== source.fileSize || hash.digest('hex') !== source.checksum)
            throw new common_1.ConflictException('原始录像 SHA-256 校验失败');
        return object;
    }
    async hashStream(stream, check) {
        const guard = this.guardStream(stream, check);
        const hash = (0, node_crypto_1.createHash)('sha256');
        let size = 0n, lastCheck = 0;
        try {
            for await (const item of stream) {
                guard.touch();
                hash.update(item);
                size += BigInt(item.length);
                if (Date.now() - lastCheck > 1_000) {
                    lastCheck = Date.now();
                    await check();
                }
            }
        }
        catch (error) {
            stream.destroy?.();
            throw error;
        }
        finally {
            guard.stop();
        }
        return { sha256: hash.digest('hex'), size };
    }
    hashFile(path, check) { return this.hashStream((0, node_fs_1.createReadStream)(path), check); }
    guardStream(stream, check) {
        let lastData = Date.now(), checking = false, stopped = false;
        const timer = setInterval(() => {
            if (checking || stopped)
                return;
            checking = true;
            void (async () => {
                await check();
                if (Date.now() - lastData > 60_000)
                    throw new common_1.ServiceUnavailableException('对象存储传输超时，原始录像已保留');
            })().catch(error => { if (!stopped)
                stream.destroy(error instanceof Error ? error : new Error('Stream aborted')); }).finally(() => { checking = false; });
        }, 1_000);
        return { touch: () => { lastData = Date.now(); }, stop: () => { stopped = true; clearInterval(timer); } };
    }
    async runHelper(input, output, result, check) {
        const helper = (0, node_path_1.resolve)(__dirname, '../../scripts/assemble-archive-video.py');
        await (0, promises_1.stat)(helper);
        const timeoutSeconds = Math.max(60, Math.min(86_400, Number(this.config.get('ARCHIVE_VIDEO_TIMEOUT_SECONDS') ?? 1800)));
        await new Promise((done, reject) => {
            const child = (0, node_child_process_1.spawn)('python3', [helper, '--input', input, '--output', output, '--result', result, '--timeout-seconds', String(timeoutSeconds)], { stdio: ['ignore', 'ignore', 'pipe'] });
            let failure, stderr = '', killing, checking = false;
            const stop = (error) => { if (failure)
                return; failure = error; child.kill('SIGTERM'); killing = setTimeout(() => child.kill('SIGKILL'), 5_000); };
            child.stderr.on('data', (chunk) => { if (stderr.length < 4000)
                stderr += chunk.toString('utf8').slice(0, 4000 - stderr.length); });
            const timer = setInterval(() => { if (checking)
                return; checking = true; void check().catch(stop).finally(() => { checking = false; }); }, 1_000);
            const deadline = setTimeout(() => stop(new common_1.ConflictException('录像封装超时，原始文件已保留')), (timeoutSeconds + 15) * 1_000);
            const cleanup = () => { clearInterval(timer); clearTimeout(deadline); if (killing)
                clearTimeout(killing); };
            child.once('error', error => { cleanup(); reject(error); });
            child.once('close', code => { cleanup(); if (failure)
                reject(failure);
            else if (code !== 0)
                reject(new common_1.ConflictException(`录像封装失败：${stderr.trim().slice(-2000) || `exit ${code}`}`));
            else
                done(); });
        });
    }
};
exports.ArchiveRecordingsService = ArchiveRecordingsService;
exports.ArchiveRecordingsService = ArchiveRecordingsService = __decorate([
    (0, common_1.Injectable)(),
    __param(2, (0, common_1.Inject)(object_storage_1.OBJECT_STORAGE)),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService, jobs_service_1.JobsService, Object, config_1.ConfigService])
], ArchiveRecordingsService);
