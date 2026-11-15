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
exports.DatasetsService = void 0;
const dataset_robot_scope_1 = require("./dataset-robot-scope");
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const node_crypto_1 = require("node:crypto");
const prisma_service_1 = require("../prisma/prisma.service");
const object_storage_1 = require("../storage/object-storage");
const object_stream_1 = require("../storage/object-stream");
const dataset_folders_1 = require("./dataset-folders");
const archive_directory_view_1 = require("./archive-directory-view");
const dataset_path_1 = require("../storage/dataset-path");
let DatasetsService = class DatasetsService {
    prisma;
    storage;
    constructor(prisma, storage) {
        this.prisma = prisma;
        this.storage = storage;
    }
    async create(input, scope) {
        const folder = (0, dataset_path_1.datasetFolder)(input.name);
        try {
            let projectId = input.projectId;
            let metadata = input.metadata;
            let robot = null;
            if (input.robotId) {
                if (metadata && Object.prototype.hasOwnProperty.call(metadata, 'robotId') && metadata.robotId !== input.robotId)
                    throw new common_1.BadRequestException('数据集所属机器人与元数据不一致');
                const robotScope = !scope || scope.role === 'ADMIN' || scope.role === 'SUPER_ADMIN' || scope.role === 'SYSTEM_ADMIN'
                    ? {} : { tenantId: scope.tenantId ?? '00000000-0000-0000-0000-000000000000', ...(scope.role === 'TENANT_ADMIN' ? {} : { projectId: { in: scope.projectIds } }) };
                robot = await this.prisma.robot.findFirst({ where: { id: input.robotId, lifecycleStatus: 'ACTIVE', ...robotScope }, select: { id: true, tenantId: true, projectId: true } });
                if (!robot)
                    throw new common_1.NotFoundException('机器人不存在、已停用或无访问权限');
                if (!robot.projectId || !robot.tenantId)
                    throw new common_1.BadRequestException('机器人尚未分配数据项目');
                if (projectId && projectId !== robot.projectId)
                    throw new common_1.BadRequestException('机器人与数据集必须属于同一个项目');
                projectId = robot.projectId;
                metadata = { ...metadata, robotId: robot.id };
            }
            let assignment = this.assignment(scope, projectId);
            if (projectId) {
                const project = await this.prisma.project.findFirst({ where: { id: projectId, status: 'ACTIVE', ...(robot ? { tenant: { status: 'ACTIVE' } } : {}), ...(scope && scope.role !== 'ADMIN' && scope.role !== 'SUPER_ADMIN' && scope.role !== 'SYSTEM_ADMIN' ? { tenantId: scope.tenantId ?? '00000000-0000-0000-0000-000000000000', ...(scope.role === 'TENANT_ADMIN' ? {} : { id: { in: scope.projectIds, equals: projectId } }) } : {}) }, select: { id: true, tenantId: true } });
                if (!project)
                    throw new common_1.NotFoundException('数据项目不存在或无访问权限');
                if (robot && project.tenantId !== robot.tenantId)
                    throw new common_1.BadRequestException('机器人与数据项目的数据空间不一致');
                assignment = { tenantId: project.tenantId, projectId: project.id };
            }
            return await this.prisma.$transaction(async (tx) => {
                const dataset = await tx.dataset.create({ data: { name: input.name, description: input.description, metadata: metadata, ...assignment } });
                await this.storage.putObject({ objectKey: `${folder}.dataset`, content: Buffer.from(JSON.stringify({ datasetId: dataset.id })), contentType: 'application/json' });
                return dataset;
            }, { timeout: 15000 });
        }
        catch (error) {
            this.throwUniqueConflict(error, 'A dataset with this name already exists');
        }
    }
    async delete(id, confirmationName, scope) {
        return this.prisma.$transaction(async (tx) => {
            const dataset = await tx.dataset.findFirst({ where: { id, ...this.datasetScope(scope) } });
            if (!dataset)
                throw new common_1.NotFoundException('数据集不存在或无访问权限');
            if (confirmationName !== dataset.name)
                throw new common_1.BadRequestException('输入的数据集名称不匹配');
            await tx.$queryRaw `SELECT id FROM "Dataset" WHERE id = ${id}::uuid FOR UPDATE`;
            const current = await tx.dataset.findUniqueOrThrow({ where: { id } });
            if (current.deletingAt)
                throw new common_1.ConflictException('数据集正在删除，请在后台任务查看进度');
            if (await tx.recycledFile.count({ where: { datasetId: id } }))
                throw new common_1.ConflictException('数据集仍有回收站文件，请先恢复或由管理员彻底清理后再删除数据集');
            const uploads = await tx.uploadSession.findMany({ where: { datasetId: id }, include: { parts: true } });
            const now = Date.now();
            const blockingUploads = uploads.filter(upload => upload.status === 'FINALIZING'
                || (['CREATED', 'UPLOADING', 'FAILED'].includes(upload.status) && upload.expiresAt.getTime() > now));
            if (blockingUploads.length)
                throw new common_1.ConflictException({
                    code: 'DATASET_UPLOADS_PENDING',
                    message: '数据集仍有未完成的上传，请先完成或取消上传任务',
                    blockingUploads: blockingUploads.map(upload => ({
                        id: upload.id, originalName: upload.originalName, status: upload.status,
                        partCount: upload.parts.length, createdAt: upload.createdAt,
                        updatedAt: upload.updatedAt, expiresAt: upload.expiresAt,
                        canAbortEmpty: upload.status === 'CREATED' && upload.parts.length === 0
                            && upload.updatedAt.getTime() <= now - 15 * 60 * 1000
                            && (upload.ownerId === scope.id || scope.role === 'ADMIN' || scope.role === 'SUPER_ADMIN' || scope.role === 'SYSTEM_ADMIN'),
                    })),
                });
            const versions = await tx.datasetVersion.findMany({ where: { datasetId: id }, select: { id: true, manifestKey: true } });
            const jobs = await tx.job.count({ where: { status: { in: ['QUEUED', 'RUNNING'] }, OR: [{ resourceType: 'DatasetVersion', resourceId: { in: versions.map(version => version.id) } }, { resourceType: 'UploadSession', resourceId: { in: uploads.map(upload => upload.id) } }] } });
            if (jobs)
                throw new common_1.ConflictException('数据集仍有运行中的后台任务，请等待完成后再删除');
            const files = await tx.robotFile.findMany({ where: { datasetId: id }, select: { id: true, objectKey: true } });
            const shared = await tx.datasetVersionFile.count({ where: { robotFileId: { in: files.map(file => file.id) }, datasetVersion: { datasetId: { not: id } } } });
            if (shared)
                throw new common_1.ConflictException('该数据集的文件仍被其他数据集版本引用，请先解除引用后再删除');
            const folders = await tx.datasetFolder.findMany({ where: { datasetId: id }, select: { objectKey: true } });
            const objectKeys = new Set([...files, ...folders].map(file => file.objectKey));
            for (const version of versions)
                if (version.manifestKey)
                    objectKeys.add(version.manifestKey);
            for (const upload of uploads) {
                if (upload.finalObjectKey)
                    objectKeys.add(upload.finalObjectKey);
                for (const part of upload.parts)
                    objectKeys.add(part.objectKey);
            }
            let folder;
            try {
                folder = (0, dataset_path_1.datasetFolder)(dataset.name);
            }
            catch { }
            if (folder) {
                objectKeys.add(`${folder}.dataset`);
                for (const key of await this.folderDownloadCleanupKeys(folder))
                    objectKeys.add(key);
            }
            await tx.dataset.update({ where: { id }, data: { deletingAt: new Date() } });
            await tx.preparedFolderPackage?.deleteMany({ where: { datasetId: id } });
            await tx.datasetVersion.deleteMany({ where: { datasetId: id } });
            await tx.uploadSession.deleteMany({ where: { datasetId: id } });
            await tx.robotFile.deleteMany({ where: { datasetId: id } });
            return tx.job.create({ data: { type: client_1.JobType.DATASET_DELETE, resourceType: 'Dataset', resourceId: id, requestedById: scope.id, tenantId: dataset.tenantId, projectId: dataset.projectId, payload: { datasetId: id, datasetName: dataset.name, objectKeys: [...objectKeys] }, steps: { create: [{ name: 'Delete Dataset Objects', stepOrder: 0 }] } } });
        }, { timeout: 30000 });
    }
    async folderDownloadCleanupKeys(folder) {
        const prefix = `${folder}.folder-downloads/`;
        const keys = new Set(), cursors = new Set();
        const deadline = Date.now() + 10_000;
        let cursor;
        do {
            const remaining = deadline - Date.now();
            if (remaining <= 0)
                throw new common_1.ServiceUnavailableException('下载缓存清单读取超时，请稍后重试删除');
            let timer;
            let page;
            try {
                page = await Promise.race([
                    this.storage.listObjects({ prefix, cursor, limit: 1000 }),
                    new Promise((_, reject) => {
                        timer = setTimeout(() => reject(new common_1.ServiceUnavailableException('下载缓存清单读取超时，请稍后重试删除')), remaining);
                    }),
                ]);
            }
            finally {
                if (timer)
                    clearTimeout(timer);
            }
            for (const item of page.items) {
                if (typeof item.objectKey !== 'string' || !item.objectKey.startsWith(prefix))
                    throw new common_1.ServiceUnavailableException('下载缓存清单范围无效，删除未执行');
                if (/^[a-f0-9]{64}\/[a-f0-9]{64}\.tar\.zst$/.test(item.objectKey.slice(prefix.length)))
                    keys.add(item.objectKey);
            }
            cursor = page.nextCursor;
            if (cursor !== undefined) {
                if (!cursor || typeof cursor !== 'string' || cursors.has(cursor))
                    throw new common_1.ServiceUnavailableException('下载缓存清单分页无效，删除未执行');
                cursors.add(cursor);
            }
        } while (cursor !== undefined);
        return [...keys];
    }
    async folders(id, scope) {
        if (!await this.prisma.dataset.findFirst({ where: { id, ...this.datasetScope(scope) }, select: { id: true } }))
            throw new common_1.NotFoundException('数据集不存在或无访问权限');
        return this.prisma.datasetFolder.findMany({ where: { datasetId: id }, orderBy: { path: 'asc' } });
    }
    async contents(id, query, scope) {
        const path = (0, dataset_path_1.validateFolderPath)(query.path);
        const dataset = await this.prisma.dataset.findFirst({ where: { id, ...this.datasetScope(scope) }, select: { id: true, name: true, metadata: true } });
        if (!dataset)
            throw new common_1.NotFoundException('数据集不存在或无访问权限');
        if (path && !await this.prisma.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: id, path } } }))
            throw new common_1.NotFoundException('文件夹不存在');
        const segmentPaths = (0, archive_directory_view_1.isArchiveTimeSlot)(dataset.metadata, path)
            ? (0, archive_directory_view_1.archiveSegmentPaths)(path, await this.prisma.datasetFolder.findMany({ where: { datasetId: id, parentPath: path }, orderBy: { name: 'asc' } }))
            : [];
        const where = { datasetId: id,
            folderPath: segmentPaths.length ? { in: [path, ...segmentPaths] } : path };
        const folderWhere = segmentPaths.length
            ? { datasetId: id, OR: [
                    { parentPath: path, path: { notIn: segmentPaths } },
                    { parentPath: { in: segmentPaths } },
                ] }
            : { datasetId: id, parentPath: path };
        const [folders, files, total] = await this.prisma.$transaction([
            this.prisma.datasetFolder.findMany({ where: folderWhere, orderBy: [{ name: 'asc' }, { path: 'asc' }] }),
            this.prisma.robotFile.findMany({ where, orderBy: [{ originalName: 'asc' }, { id: 'asc' }], skip: (query.page - 1) * query.pageSize, take: query.pageSize }),
            this.prisma.robotFile.count({ where }),
        ]);
        const datedFolders = await this.captureDatedFolders(id, dataset.metadata, folders, path === '');
        return { dataset, path, folders: datedFolders, files: files.map(file => this.serializeFile(file)), total, page: query.page, pageSize: query.pageSize,
            ...(segmentPaths.length ? { view: { kind: 'archive-time-slot', collapsedSegments: segmentPaths.length } } : {}) };
    }
    async captureDatedFolders(datasetId, metadata, folders, scanSources) {
        if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)
            || metadata.type !== 'garage-native-archive' || !folders.length)
            return folders;
        const paths = new Set(folders.map(folder => folder.path));
        const ranges = new Map();
        const fallback = (0, archive_directory_view_1.archiveDatasetDateRange)(metadata);
        if (!scanSources)
            return folders.map(folder => fallback ? { ...folder, captureDateRange: fallback } : folder);
        let after;
        while (true) {
            const files = await this.prisma.robotFile.findMany({ where: { datasetId,
                    ...(after ? { id: { gt: after } } : {}), OR: [...paths].map(path => ({ OR: [
                            { folderPath: path }, { folderPath: { startsWith: path.replace(/[\\%_]/g, '\\$&') + '/' } },
                        ] })) }, select: { id: true, folderPath: true, metadata: true, capturedAt: true },
                orderBy: { id: 'asc' }, take: 1000 });
            for (const file of files) {
                const range = (0, archive_directory_view_1.archiveCaptureDateRange)(file.metadata, file.capturedAt);
                if (!range || typeof file.folderPath !== 'string')
                    continue;
                let ancestor = file.folderPath;
                while (ancestor) {
                    if (paths.has(ancestor))
                        ranges.set(ancestor, (0, archive_directory_view_1.mergeCaptureDateRange)(ranges.get(ancestor), range));
                    ancestor = ancestor.includes('/') ? ancestor.slice(0, ancestor.lastIndexOf('/')) : '';
                }
            }
            if (files.length < 1000)
                break;
            const last = files.at(-1).id;
            if (last === after)
                throw new common_1.ServiceUnavailableException('采集日期查询未完成，请刷新后重试');
            after = last;
        }
        return folders.map(folder => {
            const captureDateRange = ranges.get(folder.path) ?? fallback;
            return captureDateRange ? { ...folder, captureDateRange } : folder;
        });
    }
    async createFolder(id, parentPath, name, scope) {
        (0, dataset_path_1.validateEntryName)(name);
        (0, dataset_path_1.validateFolderPath)(parentPath);
        const path = (0, dataset_path_1.validateFolderPath)([parentPath, name].filter(Boolean).join('/'));
        return this.prisma.$transaction(async (tx) => {
            const dataset = await tx.dataset.findFirst({ where: { id, ...this.datasetScope(scope) } });
            if (!dataset)
                throw new common_1.NotFoundException('数据集不存在或无访问权限');
            await tx.$queryRaw `SELECT id FROM "Dataset" WHERE id = ${id}::uuid FOR NO KEY UPDATE`;
            if (parentPath && !await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: id, path: parentPath } } }))
                throw new common_1.NotFoundException('上级文件夹不存在');
            if (await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: id, path } } }))
                throw new common_1.ConflictException('当前目录下已存在同名文件夹');
            await (0, dataset_folders_1.ensureDatasetFolders)(tx, this.storage, dataset, path);
            return tx.datasetFolder.findUniqueOrThrow({ where: { datasetId_path: { datasetId: id, path } } });
        }, { timeout: 30000 });
    }
    async get(id, scope) {
        const dataset = await this.prisma.dataset.findFirst({ where: { id, ...this.datasetScope(scope) }, include: { versions: { orderBy: { version: 'desc' }, include: { files: { select: { robotFile: { select: { fileSize: true } } } } } } } });
        if (!dataset)
            throw new common_1.NotFoundException('Dataset not found');
        return { ...dataset, versions: dataset.versions.map((version) => this.serializeVersionWithLiveStats(version)) };
    }
    async list(scope) {
        const datasets = await this.prisma.dataset.findMany({ where: this.datasetScope(scope), include: { versions: { orderBy: { version: 'desc' }, include: { files: { select: { robotFile: { select: { fileSize: true } } } } } } }, orderBy: { createdAt: 'desc' } });
        return datasets.map((dataset) => ({ ...dataset, versions: dataset.versions.map((version) => this.serializeVersionWithLiveStats(version)) }));
    }
    async page(query, scope) {
        const search = query.search?.trim();
        const where = {
            ...this.datasetScope(scope),
            AND: [(0, dataset_robot_scope_1.datasetRobotScope)(query.robotId)],
            ...(search && { OR: [{ name: { contains: search, mode: 'insensitive' } }, { description: { contains: search, mode: 'insensitive' } }] }),
            ...(query.status && { versions: { some: { status: query.status } } }),
            ...(query.tagId && { tags: { some: { tagId: query.tagId } } }),
        };
        const [items, total] = await this.prisma.$transaction([
            this.prisma.dataset.findMany({ where, orderBy: { [query.sortBy]: query.sortOrder }, skip: (query.page - 1) * query.pageSize, take: query.pageSize, include: { versions: { orderBy: { version: 'desc' }, include: { files: { select: { robotFile: { select: { fileSize: true } } } } } } } }),
            this.prisma.dataset.count({ where }),
        ]);
        return {
            items: items.map((dataset) => {
                const versions = dataset.versions.map((version) => this.serializeVersionWithLiveStats(version));
                return { ...dataset, versions, latestVersion: versions[0] ?? null, versionCount: versions.length };
            }),
            total, page: query.page, pageSize: query.pageSize,
        };
    }
    async pageVersions(query, scope) {
        const search = query.search?.trim();
        const where = {
            datasetId: query.datasetId,
            status: query.status,
            dataset: { AND: [this.datasetScope(scope), (0, dataset_robot_scope_1.datasetRobotScope)(query.robotId)] },
            ...(search && { OR: [{ gitTag: { contains: search, mode: 'insensitive' } }, { gitCommit: { contains: search, mode: 'insensitive' } }, { dataset: { AND: [this.datasetScope(scope), (0, dataset_robot_scope_1.datasetRobotScope)(query.robotId)], name: { contains: search, mode: 'insensitive' } } }] }),
        };
        const [items, total] = await this.prisma.$transaction([
            this.prisma.datasetVersion.findMany({ where, orderBy: { [query.sortBy]: query.sortOrder }, skip: (query.page - 1) * query.pageSize, take: query.pageSize, include: { dataset: { select: { id: true, name: true } }, files: { select: { robotFile: { select: { fileSize: true } } } } } }),
            this.prisma.datasetVersion.count({ where }),
        ]);
        return { items: items.map((version) => this.serializeVersionWithLiveStats(version)), total, page: query.page, pageSize: query.pageSize };
    }
    async createVersion(datasetId, fileIds, sourceVersionId, scope) {
        const dataset = scope ? await this.prisma.dataset.findFirst({ where: { id: datasetId, ...this.datasetScope(scope) }, select: { id: true } }) : await this.prisma.dataset.findUnique({ where: { id: datasetId }, select: { id: true } });
        if (!dataset)
            throw new common_1.NotFoundException('Dataset not found');
        if (sourceVersionId && fileIds.length)
            throw new common_1.BadRequestException('sourceVersionId and fileIds are mutually exclusive');
        let selectedFileIds = fileIds;
        if (sourceVersionId) {
            const source = await this.prisma.datasetVersion.findUnique({ where: { id: sourceVersionId }, select: { datasetId: true, files: { select: { robotFileId: true } } } });
            if (!source)
                throw new common_1.NotFoundException('Source dataset version not found');
            if (source.datasetId !== datasetId)
                throw new common_1.ConflictException('Source version must belong to the same dataset');
            selectedFileIds = source.files.map((file) => file.robotFileId);
        }
        await this.ensureFilesExist(selectedFileIds, scope);
        const latest = await this.prisma.datasetVersion.aggregate({ where: { datasetId }, _max: { version: true } });
        try {
            const version = await this.prisma.datasetVersion.create({ data: { datasetId, version: (latest._max.version ?? 0) + 1, files: { create: selectedFileIds.map((robotFileId) => ({ robotFileId })) } }, include: { files: { include: { robotFile: true } } } });
            return this.serializeDetailedVersion(version);
        }
        catch (error) {
            this.throwUniqueConflict(error, 'A version was created concurrently; retry the request');
        }
    }
    async getVersion(id, scope) { return this.serializeDetailedVersion(await this.findVersion(id, scope)); }
    async addFiles(versionId, fileIds, scope) {
        const version = await this.findVersion(versionId, scope);
        this.assertDraft(version.status);
        await this.ensureFilesExist(fileIds, scope);
        const existing = new Set(version.files.map((file) => file.robotFileId));
        const additions = fileIds.filter((fileId) => !existing.has(fileId));
        if (additions.length)
            await this.prisma.datasetVersionFile.createMany({ data: additions.map((robotFileId) => ({ datasetVersionId: versionId, robotFileId })), skipDuplicates: true });
        return this.getVersion(versionId, scope);
    }
    async replaceFiles(versionId, fileIds, scope) {
        const version = await this.findVersion(versionId, scope);
        this.assertDraft(version.status);
        await this.ensureFilesExist(fileIds, scope);
        await this.prisma.$transaction(async (transaction) => {
            await transaction.datasetVersionFile.deleteMany({ where: { datasetVersionId: versionId } });
            if (fileIds.length)
                await transaction.datasetVersionFile.createMany({ data: fileIds.map((robotFileId) => ({ datasetVersionId: versionId, robotFileId })) });
        });
        return this.getVersion(versionId, scope);
    }
    async listVersionFiles(versionId, query, candidates = false, scope) {
        const version = await this.prisma.datasetVersion.findFirst({ where: { id: versionId, dataset: this.datasetScope(scope) }, select: { id: true } });
        if (!version)
            throw new common_1.NotFoundException('Dataset version not found');
        const search = query.search?.trim();
        const where = {
            ...(candidates ? { datasetFiles: { none: { datasetVersionId: versionId } } } : { datasetFiles: { some: { datasetVersionId: versionId } } }),
            robot: this.robotScope(scope),
            robotId: query.robotId,
            recognitionType: query.recognitionType,
            schemaVersion: query.schemaVersion,
            capturedAt: { gte: query.startTime ? new Date(query.startTime) : undefined, lte: query.endTime ? new Date(query.endTime) : undefined },
            ...(search && { OR: [{ originalName: { contains: search, mode: 'insensitive' } }, { objectKey: { contains: search, mode: 'insensitive' } }, { checksum: { contains: search, mode: 'insensitive' } }] }),
        };
        const [items, total] = await this.prisma.$transaction([
            this.prisma.robotFile.findMany({ where, orderBy: { [query.sortBy]: query.sortOrder }, skip: (query.page - 1) * query.pageSize, take: query.pageSize, include: { robot: { select: { id: true, name: true, externalId: true } } } }),
            this.prisma.robotFile.count({ where }),
        ]);
        return { items: items.map((file) => this.serializeFile(file)), total, page: query.page, pageSize: query.pageSize };
    }
    async removeFile(versionId, fileId, scope) {
        const version = await this.findVersion(versionId, scope);
        this.assertDraft(version.status);
        const result = await this.prisma.datasetVersionFile.deleteMany({ where: { datasetVersionId: versionId, robotFileId: fileId } });
        if (!result.count)
            throw new common_1.NotFoundException('Robot file is not in this dataset version');
    }
    async freeze(id, scope) {
        const version = await this.findVersion(id, scope);
        this.assertDraft(version.status);
        const validation = await this.validateVersion(id, scope);
        if (!validation.valid)
            throw new common_1.ConflictException({ message: 'Dataset version validation failed', validation });
        const manifest = this.createManifest(version);
        const content = Buffer.from(JSON.stringify(manifest, null, 2) + '\n', 'utf8');
        const manifestChecksum = (0, node_crypto_1.createHash)('sha256').update(content).digest('hex');
        const manifestKey = `manifests/${version.datasetId}/v${version.version}/manifest.json`;
        await this.storage.putObject({ objectKey: manifestKey, content, contentType: 'application/json' });
        try {
            const frozen = await this.prisma.datasetVersion.update({ where: { id, status: client_1.DatasetVersionStatus.DRAFT, updatedAt: version.updatedAt }, data: { status: client_1.DatasetVersionStatus.FROZEN, frozenAt: new Date(), manifestKey, manifestChecksum, fileCount: version.files.length, totalSize: version.files.reduce((sum, item) => sum + item.robotFile.fileSize, BigInt(0)) }, include: { files: { include: { robotFile: true } } } });
            return this.serializeVersion(frozen);
        }
        catch (error) {
            await this.storage.deleteObject(manifestKey).catch(() => undefined);
            if (error instanceof client_1.Prisma.PrismaClientKnownRequestError && error.code === 'P2025')
                throw new common_1.ConflictException('数据集文件已变化，请刷新后重新冻结版本');
            throw error;
        }
    }
    async validateVersion(id, scope) {
        const version = await this.findVersion(id, scope);
        const issues = [];
        if (!version.files.length)
            issues.push({ code: 'EMPTY_VERSION', severity: 'error', message: 'Dataset version must contain at least one RobotFile' });
        let checkedObjects = 0;
        await this.mapWithConcurrency(version.files, 3, async ({ robotFile }) => {
            if (!/^[a-f0-9]{64}$/i.test(robotFile.checksum))
                issues.push({ code: 'INVALID_CHECKSUM', severity: 'error', fileId: robotFile.id, objectKey: robotFile.objectKey, message: 'RobotFile checksum is not a SHA-256 value' });
            if (robotFile.fileSize <= 0n)
                issues.push({ code: 'INVALID_SIZE', severity: 'error', fileId: robotFile.id, objectKey: robotFile.objectKey, message: 'RobotFile size must be greater than zero' });
            const operationState = { phase: 'check' };
            try {
                if (!await this.withTimeout(this.storage.exists(robotFile.objectKey), robotFile.objectKey)) {
                    issues.push({ code: 'OBJECT_MISSING', severity: 'error', fileId: robotFile.id, objectKey: robotFile.objectKey, message: 'ObjectStorage object is not reachable' });
                    return;
                }
                operationState.phase = 'read';
                const inspection = await (0, object_stream_1.inspectStoredObject)(this.storage, robotFile.objectKey);
                checkedObjects += 1;
                if (inspection.size !== robotFile.fileSize)
                    issues.push({ code: 'SIZE_MISMATCH', severity: 'error', fileId: robotFile.id, objectKey: robotFile.objectKey, message: `Stored object size ${inspection.size.toString()} does not match metadata ${robotFile.fileSize.toString()}` });
                if (inspection.checksum !== robotFile.checksum)
                    issues.push({ code: 'CHECKSUM_MISMATCH', severity: 'error', fileId: robotFile.id, objectKey: robotFile.objectKey, message: 'Stored object checksum does not match RobotFile metadata' });
            }
            catch (error) {
                issues.push({ code: operationState.phase === 'read' ? 'OBJECT_READ_FAILED' : 'OBJECT_CHECK_FAILED', severity: 'error', fileId: robotFile.id, objectKey: robotFile.objectKey, message: error instanceof Error ? error.message : `ObjectStorage object could not be ${operationState.phase === 'read' ? 'read' : 'checked'}` });
            }
        });
        if (version.status !== client_1.DatasetVersionStatus.DRAFT)
            await this.validateManifest(version, issues);
        const errors = issues.filter((issue) => issue.severity === 'error').length;
        const warnings = issues.filter((issue) => issue.severity === 'warning').length;
        return {
            versionId: version.id, status: version.status, valid: errors === 0, validatedAt: new Date().toISOString(),
            summary: { files: version.files.length, totalSize: version.files.reduce((sum, item) => sum + item.robotFile.fileSize, 0n).toString(), checkedObjects, errors, warnings },
            issues,
        };
    }
    async compareVersions(datasetId, baseVersionId, targetVersionId, scope) {
        const dataset = scope ? await this.prisma.dataset.findFirst({ where: { id: datasetId, ...this.datasetScope(scope) }, select: { id: true, name: true } }) : await this.prisma.dataset.findUnique({ where: { id: datasetId }, select: { id: true, name: true } });
        if (!dataset)
            throw new common_1.NotFoundException('Dataset not found');
        const versions = await this.prisma.datasetVersion.findMany({ where: { id: { in: [baseVersionId, targetVersionId] } }, include: { files: { include: { robotFile: { include: { robot: { select: { id: true, name: true, externalId: true } } } } } } } });
        const base = versions.find((version) => version.id === baseVersionId);
        const target = versions.find((version) => version.id === targetVersionId);
        if (!base || !target)
            throw new common_1.NotFoundException('One or more dataset versions were not found');
        if (base.datasetId !== datasetId || target.datasetId !== datasetId)
            throw new common_1.ConflictException('Both versions must belong to the requested dataset');
        const baseMap = new Map(base.files.map((membership) => [membership.robotFileId, membership.robotFile]));
        const targetMap = new Map(target.files.map((membership) => [membership.robotFileId, membership.robotFile]));
        const added = [...targetMap].filter(([id]) => !baseMap.has(id)).map(([, file]) => this.serializeCompareFile(file));
        const removed = [...baseMap].filter(([id]) => !targetMap.has(id)).map(([, file]) => this.serializeCompareFile(file));
        const unchanged = [...targetMap].filter(([id]) => baseMap.has(id)).map(([, file]) => this.serializeCompareFile(file));
        const baseSize = base.files.reduce((sum, item) => sum + item.robotFile.fileSize, 0n);
        const targetSize = target.files.reduce((sum, item) => sum + item.robotFile.fileSize, 0n);
        const sort = (items) => items.sort((a, b) => a.originalName.localeCompare(b.originalName));
        return {
            dataset,
            base: { id: base.id, version: base.version, status: base.status, fileCount: base.files.length, totalSize: baseSize.toString() },
            target: { id: target.id, version: target.version, status: target.status, fileCount: target.files.length, totalSize: targetSize.toString() },
            summary: { added: added.length, removed: removed.length, unchanged: unchanged.length, totalSizeDelta: (targetSize - baseSize).toString() },
            files: { added: sort(added), removed: sort(removed), unchanged: sort(unchanged) },
        };
    }
    async findVersion(id, scope) {
        const include = { dataset: { select: { id: true, tenantId: true, projectId: true } }, files: { include: { robotFile: { include: { robot: { select: { id: true, name: true, externalId: true } } } } }, orderBy: { robotFileId: 'asc' } } };
        const version = scope ? await this.prisma.datasetVersion.findFirst({ where: { id, dataset: this.datasetScope(scope) }, include }) : await this.prisma.datasetVersion.findUnique({ where: { id }, include });
        if (!version)
            throw new common_1.NotFoundException('Dataset version not found');
        return { ...version, dataset: version.dataset ?? { id: version.datasetId, tenantId: null, projectId: null } };
    }
    async ensureFilesExist(fileIds, scope) {
        if (!fileIds.length)
            return;
        const count = await this.prisma.robotFile.count({ where: { id: { in: fileIds }, robot: this.robotScope(scope) } });
        if (count !== fileIds.length)
            throw new common_1.NotFoundException('One or more robot files were not found');
    }
    datasetScope(scope) { if (!scope || scope.role === 'ADMIN' || (scope.role === 'SUPER_ADMIN' || scope.role === 'SYSTEM_ADMIN'))
        return { deletingAt: null }; return { deletingAt: null, tenantId: scope.tenantId ?? '__NO_TENANT__', ...(scope.role === 'TENANT_ADMIN' ? {} : { projectId: { in: scope.projectIds } }) }; }
    robotScope(scope) { if (!scope || scope.role === 'ADMIN' || (scope.role === 'SUPER_ADMIN' || scope.role === 'SYSTEM_ADMIN'))
        return {}; return { tenantId: scope.tenantId ?? '__NO_TENANT__', ...(scope.role === 'TENANT_ADMIN' ? {} : { projectId: { in: scope.projectIds } }) }; }
    assignment(scope, projectId) { if (!scope || scope.role === 'ADMIN' || (scope.role === 'SUPER_ADMIN' || scope.role === 'SYSTEM_ADMIN'))
        return {}; if (!scope.tenantId)
        throw new common_1.BadRequestException('Authenticated user has no tenant'); if (!projectId && scope.projectIds.length !== 1)
        throw new common_1.BadRequestException('请选择数据集所属项目'); return { tenantId: scope.tenantId, projectId: projectId ?? scope.projectIds[0] }; }
    async validateManifest(version, issues) {
        if (!version.manifestKey || !version.manifestChecksum) {
            issues.push({ code: 'MANIFEST_MISSING', severity: 'error', message: 'Frozen or published version has no manifest reference' });
            return;
        }
        let manifestExists = false;
        try {
            manifestExists = await this.withTimeout(this.storage.exists(version.manifestKey), version.manifestKey);
        }
        catch (error) {
            issues.push({ code: 'MANIFEST_CHECK_FAILED', severity: 'error', objectKey: version.manifestKey, message: error instanceof Error ? error.message : 'Manifest could not be checked' });
            return;
        }
        if (!manifestExists) {
            issues.push({ code: 'MANIFEST_OBJECT_MISSING', severity: 'error', objectKey: version.manifestKey, message: 'Manifest object is not reachable' });
            return;
        }
        try {
            const content = await this.readObjectWithTimeout(version.manifestKey);
            if ((0, node_crypto_1.createHash)('sha256').update(content).digest('hex') !== version.manifestChecksum)
                issues.push({ code: 'MANIFEST_CHECKSUM_MISMATCH', severity: 'error', objectKey: version.manifestKey, message: 'Manifest checksum does not match the frozen version' });
            const manifest = JSON.parse(content.toString('utf8'));
            const actualIds = new Set((manifest.files ?? []).map((file) => file.fileId));
            const expectedIds = new Set(version.files.map((file) => file.robotFileId));
            if (actualIds.size !== expectedIds.size || [...expectedIds].some((id) => !actualIds.has(id)))
                issues.push({ code: 'MANIFEST_MEMBERSHIP_MISMATCH', severity: 'error', objectKey: version.manifestKey, message: 'Manifest membership does not match the database version' });
            const liveSize = version.files.reduce((sum, item) => sum + item.robotFile.fileSize, 0n);
            if (version.fileCount !== version.files.length || version.totalSize !== liveSize)
                issues.push({ code: 'VERSION_AGGREGATE_MISMATCH', severity: 'error', message: 'Persisted version aggregates do not match its membership' });
        }
        catch (error) {
            issues.push({ code: 'MANIFEST_READ_FAILED', severity: 'error', objectKey: version.manifestKey, message: error instanceof Error ? error.message : 'Manifest could not be read' });
        }
    }
    async readObjectWithTimeout(objectKey) {
        return this.withTimeout(this.readObject(objectKey), objectKey);
    }
    async readObject(objectKey) {
        return (0, object_stream_1.readManifestObject)(this.storage, objectKey);
    }
    async withTimeout(operation, objectKey) {
        let timer;
        try {
            return await Promise.race([operation, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`ObjectStorage check timed out for ${objectKey}`)), 15_000); })]);
        }
        finally {
            if (timer)
                clearTimeout(timer);
        }
    }
    async mapWithConcurrency(items, concurrency, action) {
        let cursor = 0;
        const worker = async () => { while (cursor < items.length) {
            const item = items[cursor++];
            await action(item);
        } };
        await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
    }
    assertDraft(status) { if (status !== client_1.DatasetVersionStatus.DRAFT)
        throw new common_1.ConflictException('Frozen dataset versions cannot be modified; create a new version instead'); }
    createManifest(version) {
        return { datasetId: version.datasetId, version: version.version, createdAt: version.createdAt.toISOString(), files: version.files.map(({ robotFile }) => ({ fileId: robotFile.id, robotId: robotFile.robotId, objectKey: robotFile.objectKey, checksum: robotFile.checksum, fileSize: robotFile.fileSize.toString(), capturedAt: robotFile.capturedAt.toISOString() })) };
    }
    serializeVersion(value) {
        const serializedFiles = value.files?.map((membership) => membership.robotFile ? { ...membership, robotFile: this.serializeFile(membership.robotFile) } : membership);
        return { ...value, totalSize: value.totalSize.toString(), ...(serializedFiles && { files: serializedFiles }) };
    }
    serializeVersionWithLiveStats(value) {
        const { files, ...version } = value;
        const fileCount = files.length;
        const totalSize = files.reduce((sum, item) => sum + item.robotFile.fileSize, 0n).toString();
        return { ...version, fileCount, totalSize };
    }
    serializeDetailedVersion(value) {
        const serialized = this.serializeVersion(value);
        if (value.status !== client_1.DatasetVersionStatus.DRAFT)
            return serialized;
        return { ...serialized, fileCount: value.files.length, totalSize: value.files.reduce((sum, item) => sum + item.robotFile.fileSize, 0n).toString() };
    }
    serializeFile(value) { return { ...value, fileSize: value.fileSize.toString() }; }
    serializeCompareFile(file) { return { id: file.id, originalName: file.originalName, checksum: file.checksum, recognitionType: file.recognitionType, schemaVersion: file.schemaVersion, capturedAt: file.capturedAt.toISOString(), fileSize: file.fileSize.toString(), robot: file.robot }; }
    throwUniqueConflict(error, message) { if (error instanceof client_1.Prisma.PrismaClientKnownRequestError && error.code === 'P2002')
        throw new common_1.ConflictException(message); throw error; }
};
exports.DatasetsService = DatasetsService;
exports.DatasetsService = DatasetsService = __decorate([
    (0, common_1.Injectable)(),
    __param(1, (0, common_1.Inject)(object_storage_1.OBJECT_STORAGE)),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService, Object])
], DatasetsService);
//# sourceMappingURL=datasets.service.js.map