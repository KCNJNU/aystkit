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
exports.UploadsService = exports.MAX_UPLOAD_PARTS = exports.UPLOAD_CHUNK_SIZE = void 0;
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const node_crypto_1 = require("node:crypto");
const node_path_1 = require("node:path");
const yaml_1 = require("yaml");
const jobs_service_1 = require("../jobs/jobs.service");
const prisma_service_1 = require("../prisma/prisma.service");
const settings_service_1 = require("../settings/settings.service");
const object_storage_1 = require("../storage/object-storage");
const dataset_folders_1 = require("../datasets/dataset-folders");
const dataset_path_1 = require("../storage/dataset-path");
const class_validator_1 = require("class-validator");
const processed_package_1 = require("./processed-package");
exports.UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024;
exports.MAX_UPLOAD_PARTS = 10_000;
const STRUCTURED_VALIDATION_BYTES = { json: 50 * 1024 * 1024, yaml: 10 * 1024 * 1024 };
const EXTENSION_KIND = new Map([
    ['.json', 'json'], ['.jpg', 'image'], ['.jpeg', 'image'], ['.png', 'image'], ['.pcd', 'pcd'],
    ['.pgm', 'map'], ['.yaml', 'yaml'], ['.yml', 'yaml'], ['.log', 'log'], ['.txt', 'log'],
]);
const LIMIT_KEYS = { json: 'upload.jsonMaxBytes', image: 'upload.imageMaxBytes', pcd: 'upload.pcdMaxBytes', map: 'upload.mapMaxBytes', yaml: 'upload.yamlMaxBytes', log: 'upload.logMaxBytes' };
let UploadsService = class UploadsService {
    prisma;
    jobs;
    settings;
    storage;
    async defaults(user, datasetId) {
        if (datasetId) {
            const dataset = await this.targetDataset(datasetId, user);
            const robotId = this.datasetRobotId(dataset.metadata);
            if (robotId) {
                if (!dataset.tenantId || !dataset.projectId)
                    throw new common_1.ConflictException('数据集所属机器人缺少有效的数据项目');
                const global = user.role === 'ADMIN' || user.role === 'SUPER_ADMIN' || user.role === 'SYSTEM_ADMIN';
                const robotScope = global ? {} : { tenantId: user.tenantId ?? '00000000-0000-0000-0000-000000000000', ...(user.role === 'TENANT_ADMIN' ? {} : { projectId: { in: user.projectIds } }) };
                const source = await this.prisma.robot.findFirst({ where: { id: robotId, lifecycleStatus: 'ACTIVE', ...robotScope }, select: { id: true, tenantId: true, projectId: true } });
                if (!source)
                    throw new common_1.NotFoundException('数据集所属机器人不存在、已停用或无访问权限');
                if (source.tenantId !== dataset.tenantId || source.projectId !== dataset.projectId)
                    throw new common_1.ConflictException('数据集所属机器人与数据集不在同一个项目');
                const project = await this.prisma.project.findFirst({ where: { id: dataset.projectId, tenantId: dataset.tenantId, status: 'ACTIVE', tenant: { status: 'ACTIVE' } }, select: { id: true } });
                if (!project)
                    throw new common_1.ConflictException('数据集所属项目已停用或不存在');
                return { robotId: source.id, recognitionType: 'unclassified', schemaVersion: '1.0', capturedAtStrategy: 'LAST_MODIFIED' };
            }
            const source = await this.prisma.robot.upsert({ where: { externalId: `warehouse-dataset:${dataset.id}` }, update: {}, create: { externalId: `warehouse-dataset:${dataset.id}`, name: `数据集 ${dataset.name}`, tenantId: dataset.tenantId, projectId: dataset.projectId, metadata: { type: 'warehouse-upload-source' } }, select: { id: true } });
            return { robotId: source.id, recognitionType: 'unclassified', schemaVersion: '1.0', capturedAtStrategy: 'LAST_MODIFIED' };
        }
        const global = user.role === 'ADMIN' || (user.role === 'SUPER_ADMIN' || user.role === 'SYSTEM_ADMIN');
        if (!global && !user.tenantId)
            throw new common_1.ForbiddenException('账号尚未分配数据空间');
        const scope = global ? {} : { tenantId: user.tenantId, ...(user.role === 'TENANT_ADMIN' ? {} : { projectId: { in: user.projectIds } }) };
        let source = await this.prisma.robot.findFirst({ where: scope, select: { id: true }, orderBy: { createdAt: 'desc' } });
        if (!source) {
            let projectId = null;
            if (!global) {
                const project = await this.prisma.project.findFirst({ where: { tenantId: user.tenantId, ...(user.role === 'TENANT_ADMIN' ? {} : { id: { in: user.projectIds } }) }, select: { id: true }, orderBy: { createdAt: 'asc' } });
                if (!project)
                    throw new common_1.ForbiddenException('请管理员先分配一个可访问的数据项目');
                projectId = project.id;
            }
            const externalId = `warehouse-upload:${global ? 'global' : user.tenantId}:${projectId ?? 'shared'}`;
            source = await this.prisma.robot.upsert({ where: { externalId }, update: {}, create: { externalId, name: '内部文件上传', tenantId: global ? null : user.tenantId, projectId, metadata: { type: 'warehouse-upload-source' } }, select: { id: true } });
        }
        return { robotId: source.id, recognitionType: 'unclassified', schemaVersion: '1.0', capturedAtStrategy: 'LAST_MODIFIED' };
    }
    constructor(prisma, jobs, settings, storage) {
        this.prisma = prisma;
        this.jobs = jobs;
        this.settings = settings;
        this.storage = storage;
    }
    async create(owner, input) {
        if (!Number.isSafeInteger(input.fileSize) || input.fileSize < 1)
            throw new common_1.BadRequestException('fileSize must be a positive safe integer');
        const totalParts = Math.ceil(input.fileSize / exports.UPLOAD_CHUNK_SIZE);
        if (totalParts > exports.MAX_UPLOAD_PARTS)
            throw new common_1.BadRequestException('Upload transport supports at most 10000 parts of 8 MiB; split this file before uploading');
        const dataset = input.datasetId ? await this.targetDataset(input.datasetId, owner) : undefined;
        const datasetRobotId = dataset ? this.datasetRobotId(dataset.metadata) : undefined;
        if (datasetRobotId && datasetRobotId !== input.robotId)
            throw new common_1.ForbiddenException('上传来源与数据集所属机器人不一致');
        const destination = dataset ? (0, dataset_path_1.uploadDestination)(input.folderPath ?? '', input.originalName) : { folderPath: '', name: input.originalName };
        if (dataset) {
            (0, processed_package_1.assertProcessedPackageName)(destination.folderPath, destination.name);
        }
        const ownerId = typeof owner === 'string' ? owner : owner.id;
        const extension = (0, node_path_1.extname)(input.originalName).toLowerCase();
        const kind = EXTENSION_KIND.get(extension) ?? 'binary';
        if (!/^[a-f0-9]{64}$/i.test(input.checksum))
            throw new common_1.BadRequestException('checksum must be SHA-256');
        const limitKey = LIMIT_KEYS[kind];
        const maxFileSize = limitKey ? BigInt(await this.settings.get(limitKey)) : 0n;
        if (maxFileSize > 0n && BigInt(input.fileSize) > maxFileSize)
            throw new common_1.ConflictException(`File exceeds the configured ${kind} size limit`);
        const capturedAt = new Date(input.capturedAt);
        if (Number.isNaN(capturedAt.valueOf()))
            throw new common_1.BadRequestException('capturedAt must be a valid date');
        const robotScope = typeof owner === 'string' || owner.role === client_1.UserRole.ADMIN || (owner.role === client_1.UserRole.SUPER_ADMIN || owner.role === client_1.UserRole.SYSTEM_ADMIN) ? {} : { tenantId: owner.tenantId ?? '__NO_TENANT__', ...(owner.role === client_1.UserRole.TENANT_ADMIN ? {} : { projectId: { in: owner.projectIds } }) };
        const [robot, duplicate] = await Promise.all([this.prisma.robot.findFirst({ where: { id: input.robotId, ...robotScope }, select: { id: true, tenantId: true, projectId: true } }), this.prisma.robotFile.findFirst({ where: { checksum: input.checksum.toLowerCase(), datasetId: input.datasetId ?? null, folderPath: destination.folderPath }, select: { id: true } })]);
        if (!robot)
            throw new common_1.NotFoundException('Robot not found');
        if (dataset && (robot.tenantId !== dataset.tenantId || robot.projectId !== dataset.projectId))
            throw new common_1.ForbiddenException('上传来源与目标数据集不在同一个项目');
        if (duplicate)
            throw new common_1.ConflictException({ message: 'A file with this checksum already exists', robotFileId: duplicate.id });
        const id = (0, node_crypto_1.randomUUID)();
        const data = { id, ownerId, datasetId: dataset?.id, folderPath: destination.folderPath, robotId: input.robotId, originalName: destination.name, fileType: kind, mimeType: input.mimeType, fileSize: BigInt(input.fileSize), checksum: input.checksum.toLowerCase(), recognitionType: input.recognitionType, schemaVersion: input.schemaVersion, capturedAt, metadata: input.metadata, chunkSize: exports.UPLOAD_CHUNK_SIZE, totalParts, maxFileSize, tempPrefix: `uploads/${id}/parts/`, expiresAt: new Date(Date.now() + 24 * 60 * 60 * 1000) };
        if (!dataset)
            return this.serialize(await this.prisma.uploadSession.create({ data, include: { parts: true } }));
        return this.prisma.$transaction(async (tx) => {
            await tx.$queryRaw `SELECT id FROM "Dataset" WHERE id = ${dataset.id}::uuid FOR NO KEY UPDATE`;
            const current = await tx.dataset.findUniqueOrThrow({ where: { id: dataset.id } });
            if (current.deletingAt)
                throw new common_1.ConflictException('数据集正在删除');
            const selectedPath = (0, dataset_path_1.validateFolderPath)(input.folderPath ?? '');
            if (selectedPath && !await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: dataset.id, path: selectedPath } } }))
                throw new common_1.NotFoundException('目标文件夹不存在');
            const filePath = [destination.folderPath, destination.name].filter(Boolean).join('/');
            if (await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: dataset.id, path: filePath } } }))
                throw new common_1.ConflictException('当前目录已存在同名文件夹');
            let finalObjectKey = `${(0, dataset_path_1.datasetFolder)(dataset.name)}${filePath}`;
            if (await tx.robotFile.findUnique({ where: { objectKey: finalObjectKey } }) || await tx.uploadSession.findUnique({ where: { finalObjectKey } }) || await this.storage.exists(finalObjectKey)) {
                const stem = extension ? destination.name.slice(0, -extension.length) : destination.name;
                finalObjectKey = `${(0, dataset_path_1.datasetFolder)(dataset.name)}${destination.folderPath ? destination.folderPath + '/' : ''}${stem}__${id}${extension}`;
            }
            if (Buffer.byteLength(finalObjectKey) > 1024)
                throw new common_1.BadRequestException('文件路径过长');
            await (0, dataset_folders_1.ensureDatasetFolders)(tx, this.storage, dataset, destination.folderPath);
            return this.serialize(await tx.uploadSession.create({ data: { ...data, finalObjectKey }, include: { parts: true } }));
        }, { timeout: 30000 });
    }
    async get(id, userId, role) { const session = await this.find(id); this.assertOwner(session.ownerId, userId, role); return this.serialize(session); }
    async putPart(id, partNumber, contentRange, partChecksum, content, userId, role) {
        const session = await this.find(id);
        this.assertOwner(session.ownerId, userId, role);
        this.assertWritable(session.status, session.expiresAt);
        if (!Number.isInteger(partNumber) || partNumber < 1 || partNumber > session.totalParts)
            throw new common_1.BadRequestException('partNumber is outside the upload range');
        if (!partChecksum || !/^[a-f0-9]{64}$/i.test(partChecksum))
            throw new common_1.BadRequestException('X-Part-SHA256 is required');
        const expectedStart = (partNumber - 1) * session.chunkSize;
        const expectedEnd = Math.min(Number(session.fileSize), expectedStart + session.chunkSize) - 1;
        const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(contentRange ?? '');
        if (!match || Number(match[1]) !== expectedStart || Number(match[2]) !== expectedEnd || BigInt(match[3]) !== session.fileSize || content.length !== expectedEnd - expectedStart + 1)
            throw new common_1.BadRequestException('Content-Range does not match this part');
        const actual = (0, node_crypto_1.createHash)('sha256').update(content).digest('hex');
        if (actual !== partChecksum.toLowerCase())
            throw new common_1.ConflictException('Part checksum mismatch');
        const objectKey = `${session.tempPrefix}${String(partNumber).padStart(6, '0')}`;
        const existing = session.parts.find((part) => part.partNumber === partNumber);
        if (existing?.checksum === actual && existing.size === content.length && await this.storage.exists(existing.objectKey))
            return existing;
        await this.storage.putObject({ objectKey, content, contentType: 'application/octet-stream' });
        const part = await this.prisma.uploadPart.upsert({ where: { uploadSessionId_partNumber: { uploadSessionId: id, partNumber } }, update: { objectKey, size: content.length, checksum: actual }, create: { uploadSessionId: id, partNumber, objectKey, size: content.length, checksum: actual } });
        const updated = await this.prisma.uploadSession.updateMany({ where: { id,
                status: { in: [client_1.UploadStatus.CREATED, client_1.UploadStatus.UPLOADING, client_1.UploadStatus.FAILED] }, expiresAt: { gt: new Date() },
            }, data: { status: client_1.UploadStatus.UPLOADING } });
        if (updated.count !== 1)
            throw new common_1.ConflictException('Upload state changed while this part was being written');
        return part;
    }
    async complete(id, userId, role) {
        const session = await this.find(id);
        this.assertOwner(session.ownerId, userId, role);
        if (session.status === client_1.UploadStatus.COMPLETED && session.robotFileId)
            return { completed: true, robotFileId: session.robotFileId };
        const existing = await this.prisma.job.findFirst({ where: { type: client_1.JobType.UPLOAD_FINALIZE, resourceId: id, status: { in: [client_1.JobStatus.QUEUED, client_1.JobStatus.RUNNING] } }, include: { steps: true } });
        if (existing)
            return existing;
        this.assertWritable(session.status, session.expiresAt);
        if (session.parts.length !== session.totalParts)
            throw new common_1.ConflictException('All parts must be uploaded before completion');
        const robot = await this.prisma.robot.findUniqueOrThrow({ where: { id: session.robotId }, select: { tenantId: true, projectId: true } });
        return this.prisma.$transaction(async (tx) => {
            const updated = await tx.uploadSession.updateMany({ where: { id,
                    status: { in: [client_1.UploadStatus.CREATED, client_1.UploadStatus.UPLOADING, client_1.UploadStatus.FAILED] }, expiresAt: { gt: new Date() },
                }, data: { status: client_1.UploadStatus.FINALIZING } });
            if (updated.count !== 1)
                throw new common_1.ConflictException('Upload state changed before completion; refresh this upload');
            return this.jobs.create(client_1.JobType.UPLOAD_FINALIZE, { payload: { uploadSessionId: id }, resourceType: 'UploadSession', resourceId: id, requestedById: userId, tenantId: robot.tenantId, projectId: robot.projectId, maxAttempts: 3 }, tx);
        });
    }
    async abort(id, userId, role) {
        const session = await this.find(id);
        this.assertOwner(session.ownerId, userId, role);
        if (session.status === client_1.UploadStatus.COMPLETED)
            throw new common_1.ConflictException('Completed uploads cannot be aborted');
        if (session.status === client_1.UploadStatus.FINALIZING)
            throw new common_1.ConflictException('Upload session is finalizing; wait for the job to finish before aborting');
        const updated = await this.prisma.uploadSession.updateMany({ where: { id,
                status: { in: [client_1.UploadStatus.CREATED, client_1.UploadStatus.UPLOADING, client_1.UploadStatus.FAILED, client_1.UploadStatus.EXPIRED, client_1.UploadStatus.ABORTED] },
            }, data: { status: client_1.UploadStatus.ABORTED, errorMessage: null } });
        if (updated.count !== 1)
            throw new common_1.ConflictException('Upload state changed; finalizing or completed uploads cannot be aborted');
        await this.storage.deleteObjects(session.parts.map((part) => part.objectKey));
    }
    async finalize(id) {
        const session = await this.find(id);
        if (session.status === client_1.UploadStatus.COMPLETED && session.robotFileId)
            return { robotFileId: session.robotFileId, uploadSessionId: id };
        if (session.status === client_1.UploadStatus.ABORTED || session.status === client_1.UploadStatus.EXPIRED || session.status === client_1.UploadStatus.COMPLETED)
            throw new common_1.ConflictException(`Upload session is ${session.status.toLowerCase()}`);
        if (session.parts.length !== session.totalParts)
            throw new common_1.ConflictException('Upload is missing one or more parts');
        const ordered = [...session.parts].sort((a, b) => a.partNumber - b.partNumber);
        if (ordered.reduce((sum, item) => sum + BigInt(item.size), 0n) !== session.fileSize)
            throw new common_1.ConflictException('Uploaded parts do not match declared file size');
        const robot = await this.prisma.robot.findUniqueOrThrow({ where: { id: session.robotId }, select: { externalId: true } });
        const extension = (0, node_path_1.extname)(session.originalName).toLowerCase();
        const date = session.capturedAt;
        const finalObjectKey = session.finalObjectKey ?? `raw/${robot.externalId ?? session.robotId}/${date.getUTCFullYear()}/${String(date.getUTCMonth() + 1).padStart(2, '0')}/${String(date.getUTCDate()).padStart(2, '0')}/${(0, node_crypto_1.randomUUID)()}${extension}`;
        const claimed = await this.prisma.uploadSession.updateMany({ where: { id,
                status: { in: [client_1.UploadStatus.CREATED, client_1.UploadStatus.UPLOADING, client_1.UploadStatus.FAILED, client_1.UploadStatus.FINALIZING] },
            }, data: { finalObjectKey, status: client_1.UploadStatus.FINALIZING } });
        if (claimed.count !== 1)
            throw new common_1.ConflictException('Upload state changed before finalization');
        await this.storage.composeObject(finalObjectKey, ordered.map((part) => part.objectKey), session.mimeType);
        try {
            const inspection = await this.inspectObject(finalObjectKey, session.fileType);
            if (inspection.size !== session.fileSize)
                throw new common_1.ConflictException('Composed object size mismatch');
            if (inspection.checksum !== session.checksum)
                throw new common_1.ConflictException('Complete file checksum mismatch');
            if (session.datasetId && (0, processed_package_1.isProcessedFolder)(session.folderPath ?? '')) {
                (0, processed_package_1.assertProcessedPackageName)(session.folderPath ?? '', session.originalName);
                await (0, processed_package_1.validateProcessedPackage)(this.storage, finalObjectKey, Number(session.fileSize));
            }
            const duplicate = await this.prisma.robotFile.findFirst({ where: { checksum: session.checksum, datasetId: session.datasetId ?? null, folderPath: session.folderPath ?? '' }, select: { id: true } });
            if (duplicate) {
                const acceptedId = await this.prisma.$transaction(async (tx) => {
                    await this.lockFinalObject(tx, finalObjectKey);
                    const current = await this.validateFinalization(tx, id, finalObjectKey);
                    if (current.status === client_1.UploadStatus.COMPLETED && current.robotFileId)
                        return current.robotFileId;
                    if (session.datasetId)
                        await this.attachToDraft(tx, session.datasetId, duplicate.id);
                    await tx.uploadSession.update({ where: { id }, data: { status: client_1.UploadStatus.COMPLETED, robotFileId: duplicate.id, completedAt: new Date(), errorMessage: null } });
                    return duplicate.id;
                });
                await this.removeUnindexedFinalObject(finalObjectKey).catch(() => undefined);
                await this.cleanupParts(session.parts.map((part) => part.objectKey));
                return { robotFileId: acceptedId, uploadSessionId: id, duplicate: true };
            }
            const file = await this.prisma.$transaction(async (tx) => {
                await this.lockFinalObject(tx, finalObjectKey);
                const current = await this.validateFinalization(tx, id, finalObjectKey);
                if (current.status === client_1.UploadStatus.COMPLETED && current.robotFileId)
                    return { id: current.robotFileId };
                const created = await tx.robotFile.create({ data: { folderPath: session.folderPath ?? '', datasetId: session.datasetId, robotId: session.robotId, objectKey: finalObjectKey, originalName: session.originalName, fileType: session.fileType, mimeType: session.mimeType, fileSize: session.fileSize, checksum: session.checksum, recognitionType: session.recognitionType, schemaVersion: session.schemaVersion, capturedAt: session.capturedAt, metadata: session.metadata === null ? undefined : session.metadata } });
                if (session.datasetId)
                    await this.attachToDraft(tx, session.datasetId, created.id);
                await tx.uploadSession.update({ where: { id }, data: { status: client_1.UploadStatus.COMPLETED, robotFileId: created.id, completedAt: new Date(), errorMessage: null } });
                return created;
            });
            await this.cleanupParts(session.parts.map((part) => part.objectKey));
            return { robotFileId: file.id, uploadSessionId: id, objectKey: finalObjectKey };
        }
        catch (error) {
            await this.removeUnindexedFinalObject(finalObjectKey).catch(() => undefined);
            await this.prisma.uploadSession.updateMany({ where: { id, status: { in: [client_1.UploadStatus.FINALIZING, client_1.UploadStatus.FAILED] } }, data: { status: client_1.UploadStatus.FAILED, errorMessage: error instanceof Error ? error.message : 'Upload finalization failed' } }).catch(() => undefined);
            throw error;
        }
    }
    async lockFinalObject(tx, objectKey) {
        await tx.$queryRaw `SELECT pg_advisory_xact_lock(hashtextextended(${objectKey}, 0))::text`;
    }
    async validateFinalization(tx, id, objectKey) {
        await tx.$queryRaw `SELECT id FROM "UploadSession" WHERE id = ${id}::uuid FOR UPDATE`;
        const current = await tx.uploadSession.findUnique({ where: { id } });
        if (!current)
            throw new common_1.NotFoundException('Upload session not found');
        if (current.status === client_1.UploadStatus.COMPLETED && current.robotFileId)
            return current;
        if (current.status !== client_1.UploadStatus.FINALIZING || current.finalObjectKey !== objectKey)
            throw new common_1.ConflictException('Upload state changed before file registration');
        if (!await this.storage.exists(objectKey))
            throw new common_1.ConflictException('Composed object disappeared before file registration');
        return current;
    }
    async removeUnindexedFinalObject(objectKey) {
        await this.prisma.$transaction(async (tx) => {
            await this.lockFinalObject(tx, objectKey);
            const [files, recycled] = await Promise.all([
                tx.robotFile.count({ where: { objectKey } }), tx.recycledFile.count({ where: { objectKey } }),
            ]);
            if (!files && !recycled)
                await this.storage.deleteObject(objectKey);
        });
    }
    datasetRobotId(metadata) {
        if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)
            || !Object.prototype.hasOwnProperty.call(metadata, 'robotId'))
            return undefined;
        const robotId = metadata.robotId;
        if (typeof robotId !== 'string' || !(0, class_validator_1.isUUID)(robotId))
            throw new common_1.ConflictException('数据集所属机器人无效，请联系管理员核对');
        return robotId;
    }
    async targetDataset(id, owner) {
        const global = typeof owner === 'string' || owner.role === client_1.UserRole.ADMIN || (owner.role === client_1.UserRole.SUPER_ADMIN || owner.role === client_1.UserRole.SYSTEM_ADMIN);
        const scope = global || typeof owner === 'string' ? {} : { tenantId: owner.tenantId ?? '00000000-0000-0000-0000-000000000000', ...(owner.role === client_1.UserRole.TENANT_ADMIN ? {} : { projectId: { in: owner.projectIds } }) };
        const dataset = await this.prisma.dataset.findFirst({ where: { id, deletingAt: null, ...scope } });
        if (!dataset)
            throw new common_1.NotFoundException('数据集不存在或无访问权限');
        (0, dataset_path_1.datasetFolder)(dataset.name);
        return dataset;
    }
    async attachToDraft(tx, datasetId, robotFileId) {
        await tx.$queryRaw `SELECT id FROM "Dataset" WHERE id = ${datasetId}::uuid FOR NO KEY UPDATE`;
        const latest = await tx.datasetVersion.findFirst({ where: { datasetId }, orderBy: { version: 'desc' } });
        let draft = latest?.status === 'DRAFT' ? latest : null;
        if (draft) {
            const writable = await tx.datasetVersion.updateMany({ where: { id: draft.id, status: 'DRAFT' }, data: { updatedAt: new Date() } });
            if (!writable.count)
                draft = null;
        }
        if (!draft)
            draft = await tx.datasetVersion.create({ data: { datasetId, version: (latest?.version ?? 0) + 1 } });
        await tx.datasetVersionFile.createMany({ data: [{ datasetVersionId: draft.id, robotFileId }], skipDuplicates: true });
        await tx.dataset.update({ where: { id: datasetId }, data: { updatedAt: new Date() } });
    }
    async inspectObject(objectKey, kind) {
        const stream = await this.storage.getObject(objectKey);
        const hash = (0, node_crypto_1.createHash)('sha256');
        let size = 0n, prefixBytes = 0;
        const prefix = [], document = [];
        const documentLimit = STRUCTURED_VALIDATION_BYTES[kind];
        for await (const raw of stream) {
            const chunk = Buffer.isBuffer(raw) ? raw : Buffer.from(raw);
            size += BigInt(chunk.length);
            if (documentLimit && size > BigInt(documentLimit))
                throw new common_1.ConflictException(`${kind.toUpperCase()} structured-format validation exceeds its ${documentLimit / 1024 / 1024} MiB parser memory budget`);
            hash.update(chunk);
            if (prefixBytes < 64 * 1024) {
                const slice = chunk.subarray(0, 64 * 1024 - prefixBytes);
                prefix.push(slice);
                prefixBytes += slice.length;
            }
            if (documentLimit)
                document.push(chunk);
        }
        this.validateFormat(kind, Buffer.concat(prefix), documentLimit ? Buffer.concat(document) : undefined);
        return { checksum: hash.digest('hex'), size };
    }
    validateFormat(kind, header, full) {
        if (!header.length)
            throw new common_1.ConflictException('File must not be empty');
        if (kind === 'json') {
            try {
                JSON.parse((full ?? header).toString('utf8'));
            }
            catch {
                throw new common_1.ConflictException('JSON is invalid');
            }
        }
        if (kind === 'yaml') {
            try {
                const parsed = (0, yaml_1.parse)((full ?? header).toString('utf8'));
                if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object')
                    throw new Error();
            }
            catch {
                throw new common_1.ConflictException('YAML must contain a mapping');
            }
        }
        if (kind === 'image' && !(header.subarray(0, 3).equals(Buffer.from([0xff, 0xd8, 0xff])) || header.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))))
            throw new common_1.ConflictException('Image magic bytes are invalid');
        if (kind === 'pcd' && !/(^|\n)(VERSION|FIELDS|DATA)\s+/m.test(header.toString('ascii')))
            throw new common_1.ConflictException('PCD header is invalid');
        if (kind === 'map' && !/^P[25]\s/.test(header.toString('ascii')))
            throw new common_1.ConflictException('PGM header must be P2 or P5');
    }
    async cleanupParts(keys) { await this.storage.deleteObjects(keys).catch(() => undefined); }
    async find(id) { const session = await this.prisma.uploadSession.findUnique({ where: { id }, include: { parts: true } }); if (!session)
        throw new common_1.NotFoundException('Upload session not found'); return session; }
    assertOwner(ownerId, userId, role) { if (ownerId !== userId && role !== client_1.UserRole.ADMIN && role !== client_1.UserRole.SUPER_ADMIN && role !== client_1.UserRole.SYSTEM_ADMIN)
        throw new common_1.ForbiddenException('Upload session belongs to another user'); }
    assertWritable(status, expiresAt) { if (expiresAt <= new Date())
        throw new common_1.ConflictException('Upload session has expired'); if (status !== client_1.UploadStatus.CREATED && status !== client_1.UploadStatus.UPLOADING && status !== client_1.UploadStatus.FAILED)
        throw new common_1.ConflictException(`Upload session is ${status.toLowerCase()}`); }
    serialize(value) { return { ...value, fileSize: value.fileSize.toString(), maxFileSize: value.maxFileSize.toString() }; }
};
exports.UploadsService = UploadsService;
exports.UploadsService = UploadsService = __decorate([
    (0, common_1.Injectable)(),
    __param(3, (0, common_1.Inject)(object_storage_1.OBJECT_STORAGE)),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService, jobs_service_1.JobsService, settings_service_1.SettingsService, Object])
], UploadsService);
//# sourceMappingURL=uploads.service.js.map