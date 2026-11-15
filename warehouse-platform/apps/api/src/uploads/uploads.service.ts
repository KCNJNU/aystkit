import { BadRequestException, ConflictException, ForbiddenException, Inject, Injectable, NotFoundException } from '@nestjs/common';
import { JobStatus, JobType, Prisma, UploadStatus, UserRole } from '@prisma/client';
import { createHash, randomUUID } from 'node:crypto';
import { extname } from 'node:path';
import { parse as parseYaml } from 'yaml';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { SettingsService } from '../settings/settings.service';
import { OBJECT_STORAGE, ObjectStorage } from '../storage/object-storage';
import { ensureDatasetFolders } from '../datasets/dataset-folders';
import { datasetFolder, uploadDestination, validateFolderPath } from '../storage/dataset-path';
import { AuthUser } from '../auth/auth.types';
import { isUUID } from 'class-validator';
import { assertProcessedPackageName, isProcessedFolder, validateProcessedPackage } from './processed-package';

export const UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024;
export const MAX_UPLOAD_PARTS = 10_000;
const STRUCTURED_VALIDATION_BYTES = { json: 50 * 1024 * 1024, yaml: 10 * 1024 * 1024 } as const;
const EXTENSION_KIND = new Map([
  ['.json', 'json'], ['.jpg', 'image'], ['.jpeg', 'image'], ['.png', 'image'], ['.pcd', 'pcd'],
  ['.pgm', 'map'], ['.yaml', 'yaml'], ['.yml', 'yaml'], ['.log', 'log'], ['.txt', 'log'],
]);
const LIMIT_KEYS = { json: 'upload.jsonMaxBytes', image: 'upload.imageMaxBytes', pcd: 'upload.pcdMaxBytes', map: 'upload.mapMaxBytes', yaml: 'upload.yamlMaxBytes', log: 'upload.logMaxBytes' } as const;

type CreateUploadInput = { folderPath?: string; datasetId?: string; robotId: string; originalName: string; mimeType: string; fileSize: number; checksum: string; recognitionType: string; schemaVersion: string; capturedAt: string; metadata?: Record<string, unknown>; lastModified?: number };

@Injectable()
export class UploadsService {
  async defaults(user: AuthUser, datasetId?: string) {
    if (datasetId) {
      const dataset = await this.targetDataset(datasetId, user);
      const robotId = this.datasetRobotId(dataset.metadata);
      if (robotId) {
        if (!dataset.tenantId || !dataset.projectId) throw new ConflictException('数据集所属机器人缺少有效的数据项目');
        const global = user.role === 'ADMIN' || user.role === 'SUPER_ADMIN' || user.role === 'SYSTEM_ADMIN';
        const robotScope: Prisma.RobotWhereInput = global ? {} : { tenantId: user.tenantId ?? '00000000-0000-0000-0000-000000000000', ...(user.role === 'TENANT_ADMIN' ? {} : { projectId: { in: user.projectIds } }) };
        const source = await this.prisma.robot.findFirst({ where: { id: robotId, lifecycleStatus: 'ACTIVE', ...robotScope }, select: { id: true, tenantId: true, projectId: true } });
        if (!source) throw new NotFoundException('数据集所属机器人不存在、已停用或无访问权限');
        if (source.tenantId !== dataset.tenantId || source.projectId !== dataset.projectId)
          throw new ConflictException('数据集所属机器人与数据集不在同一个项目');
        const project = await this.prisma.project.findFirst({ where: { id: dataset.projectId, tenantId: dataset.tenantId, status: 'ACTIVE', tenant: { status: 'ACTIVE' } }, select: { id: true } });
        if (!project) throw new ConflictException('数据集所属项目已停用或不存在');
        return { robotId: source.id, recognitionType: 'unclassified', schemaVersion: '1.0', capturedAtStrategy: 'LAST_MODIFIED' as const };
      }
      const source = await this.prisma.robot.upsert({ where: { externalId: `warehouse-dataset:${dataset.id}` }, update: {}, create: { externalId: `warehouse-dataset:${dataset.id}`, name: `数据集 ${dataset.name}`, tenantId: dataset.tenantId, projectId: dataset.projectId, metadata: { type: 'warehouse-upload-source' } }, select: { id: true } });
      return { robotId: source.id, recognitionType: 'unclassified', schemaVersion: '1.0', capturedAtStrategy: 'LAST_MODIFIED' as const };
    }
    const global = user.role === 'ADMIN' || (user.role === 'SUPER_ADMIN' || user.role === 'SYSTEM_ADMIN');
    if (!global && !user.tenantId) throw new ForbiddenException('账号尚未分配数据空间');
    const scope = global ? {} : { tenantId: user.tenantId!, ...(user.role === 'TENANT_ADMIN' ? {} : { projectId: { in: user.projectIds } }) };
    let source = await this.prisma.robot.findFirst({ where: scope, select: { id: true }, orderBy: { createdAt: 'desc' } });
    if (!source) {
      let projectId: string | null = null;
      if (!global) {
        const project = await this.prisma.project.findFirst({ where: { tenantId: user.tenantId!, ...(user.role === 'TENANT_ADMIN' ? {} : { id: { in: user.projectIds } }) }, select: { id: true }, orderBy: { createdAt: 'asc' } });
        if (!project) throw new ForbiddenException('请管理员先分配一个可访问的数据项目');
        projectId = project.id;
      }
      const externalId = `warehouse-upload:${global ? 'global' : user.tenantId}:${projectId ?? 'shared'}`;
      source = await this.prisma.robot.upsert({ where: { externalId }, update: {}, create: { externalId, name: '内部文件上传', tenantId: global ? null : user.tenantId, projectId, metadata: { type: 'warehouse-upload-source' } }, select: { id: true } });
    }
    return { robotId: source.id, recognitionType: 'unclassified', schemaVersion: '1.0', capturedAtStrategy: 'LAST_MODIFIED' as const };
  }
  constructor(private readonly prisma: PrismaService, private readonly jobs: JobsService, private readonly settings: SettingsService, @Inject(OBJECT_STORAGE) private readonly storage: ObjectStorage) {}

  async create(owner: string | AuthUser, input: CreateUploadInput) {
    if (!Number.isSafeInteger(input.fileSize) || input.fileSize < 1) throw new BadRequestException('fileSize must be a positive safe integer');
    const totalParts = Math.ceil(input.fileSize / UPLOAD_CHUNK_SIZE);
    if (totalParts > MAX_UPLOAD_PARTS) throw new BadRequestException('Upload transport supports at most 10000 parts of 8 MiB; split this file before uploading');
    const dataset = input.datasetId ? await this.targetDataset(input.datasetId, owner) : undefined;
    const datasetRobotId = dataset ? this.datasetRobotId(dataset.metadata) : undefined;
    if (datasetRobotId && datasetRobotId !== input.robotId) throw new ForbiddenException('上传来源与数据集所属机器人不一致');
    const destination = dataset ? uploadDestination(input.folderPath ?? '', input.originalName) : { folderPath: '', name: input.originalName };
    if (dataset) {
      assertProcessedPackageName(destination.folderPath, destination.name);
    }
    const ownerId=typeof owner==='string'?owner:owner.id;
    const extension = extname(input.originalName).toLowerCase();
    const kind = EXTENSION_KIND.get(extension) ?? 'binary';
    if (!/^[a-f0-9]{64}$/i.test(input.checksum)) throw new BadRequestException('checksum must be SHA-256');
    const limitKey = LIMIT_KEYS[kind as keyof typeof LIMIT_KEYS];
    const maxFileSize = limitKey ? BigInt(await this.settings.get(limitKey)) : 0n;
    if (maxFileSize > 0n && BigInt(input.fileSize) > maxFileSize) throw new ConflictException(`File exceeds the configured ${kind} size limit`);
    const capturedAt = new Date(input.capturedAt);
    if (Number.isNaN(capturedAt.valueOf())) throw new BadRequestException('capturedAt must be a valid date');
    const robotScope: Prisma.RobotWhereInput=typeof owner==='string'||owner.role===UserRole.ADMIN||(owner.role === UserRole.SUPER_ADMIN || owner.role === UserRole.SYSTEM_ADMIN)?{}:{tenantId:owner.tenantId??'__NO_TENANT__',...(owner.role===UserRole.TENANT_ADMIN?{}:{projectId:{in:owner.projectIds}})};
    const [robot, duplicate] = await Promise.all([this.prisma.robot.findFirst({ where: { id: input.robotId, ...robotScope }, select: { id: true, tenantId: true, projectId: true } }), this.prisma.robotFile.findFirst({ where: { checksum: input.checksum.toLowerCase(), datasetId: input.datasetId ?? null, folderPath: destination.folderPath }, select: { id: true } })]);
    if (!robot) throw new NotFoundException('Robot not found');
    if (dataset && (robot.tenantId !== dataset.tenantId || robot.projectId !== dataset.projectId)) throw new ForbiddenException('上传来源与目标数据集不在同一个项目');
    if (duplicate) throw new ConflictException({ message: 'A file with this checksum already exists', robotFileId: duplicate.id });
    const id = randomUUID();
    const data = { id, ownerId, datasetId: dataset?.id, folderPath: destination.folderPath, robotId: input.robotId, originalName: destination.name, fileType: kind, mimeType: input.mimeType, fileSize: BigInt(input.fileSize), checksum: input.checksum.toLowerCase(), recognitionType: input.recognitionType, schemaVersion: input.schemaVersion, capturedAt, metadata: input.metadata as Prisma.InputJsonObject | undefined, chunkSize: UPLOAD_CHUNK_SIZE, totalParts, maxFileSize, tempPrefix: `uploads/${id}/parts/`, expiresAt: new Date(Date.now() + 24 * 60 * 60 * 1000) };
    if (!dataset) return this.serialize(await this.prisma.uploadSession.create({ data, include: { parts: true } }));
    return this.prisma.$transaction(async tx => {
      await tx.$queryRaw`SELECT id FROM "Dataset" WHERE id = ${dataset.id}::uuid FOR NO KEY UPDATE`;
      const current = await tx.dataset.findUniqueOrThrow({ where: { id: dataset.id } });
      if (current.deletingAt) throw new ConflictException('数据集正在删除');
      const selectedPath = validateFolderPath(input.folderPath ?? '');
      if (selectedPath && !await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: dataset.id, path: selectedPath } } })) throw new NotFoundException('目标文件夹不存在');
      const filePath = [destination.folderPath, destination.name].filter(Boolean).join('/');
      if (await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: dataset.id, path: filePath } } })) throw new ConflictException('当前目录已存在同名文件夹');
      let finalObjectKey = `${datasetFolder(dataset.name)}${filePath}`;
      if (await tx.robotFile.findUnique({ where: { objectKey: finalObjectKey } }) || await tx.uploadSession.findUnique({ where: { finalObjectKey } }) || await this.storage.exists(finalObjectKey)) {
        const stem = extension ? destination.name.slice(0, -extension.length) : destination.name;
        finalObjectKey = `${datasetFolder(dataset.name)}${destination.folderPath ? destination.folderPath + '/' : ''}${stem}__${id}${extension}`;
      }
      if (Buffer.byteLength(finalObjectKey) > 1024) throw new BadRequestException('文件路径过长');
      await ensureDatasetFolders(tx, this.storage, dataset, destination.folderPath);
      return this.serialize(await tx.uploadSession.create({ data: { ...data, finalObjectKey }, include: { parts: true } }));
    }, { timeout: 30000 });
  }

  async get(id: string, userId: string, role: UserRole) { const session = await this.find(id); this.assertOwner(session.ownerId, userId, role); return this.serialize(session); }

  async putPart(id: string, partNumber: number, contentRange: string | undefined, partChecksum: string | undefined, content: Buffer, userId: string, role: UserRole) {
    const session = await this.find(id); this.assertOwner(session.ownerId, userId, role); this.assertWritable(session.status, session.expiresAt);
    if (!Number.isInteger(partNumber) || partNumber < 1 || partNumber > session.totalParts) throw new BadRequestException('partNumber is outside the upload range');
    if (!partChecksum || !/^[a-f0-9]{64}$/i.test(partChecksum)) throw new BadRequestException('X-Part-SHA256 is required');
    const expectedStart = (partNumber - 1) * session.chunkSize;
    const expectedEnd = Math.min(Number(session.fileSize), expectedStart + session.chunkSize) - 1;
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(contentRange ?? '');
    if (!match || Number(match[1]) !== expectedStart || Number(match[2]) !== expectedEnd || BigInt(match[3]) !== session.fileSize || content.length !== expectedEnd - expectedStart + 1) throw new BadRequestException('Content-Range does not match this part');
    const actual = createHash('sha256').update(content).digest('hex');
    if (actual !== partChecksum.toLowerCase()) throw new ConflictException('Part checksum mismatch');
    const objectKey = `${session.tempPrefix}${String(partNumber).padStart(6, '0')}`;
    const existing = session.parts.find((part) => part.partNumber === partNumber);
    if (existing?.checksum === actual && existing.size === content.length && await this.storage.exists(existing.objectKey)) return existing;
    await this.storage.putObject({ objectKey, content, contentType: 'application/octet-stream' });
    const part = await this.prisma.uploadPart.upsert({ where: { uploadSessionId_partNumber: { uploadSessionId: id, partNumber } }, update: { objectKey, size: content.length, checksum: actual }, create: { uploadSessionId: id, partNumber, objectKey, size: content.length, checksum: actual } });
    const updated = await this.prisma.uploadSession.updateMany({ where: { id,
      status: { in: [UploadStatus.CREATED, UploadStatus.UPLOADING, UploadStatus.FAILED] }, expiresAt: { gt: new Date() },
    }, data: { status: UploadStatus.UPLOADING } });
    if (updated.count !== 1) throw new ConflictException('Upload state changed while this part was being written');
    return part;
  }

  async complete(id: string, userId: string, role: UserRole) {
    const session = await this.find(id); this.assertOwner(session.ownerId, userId, role);
    if (session.status === UploadStatus.COMPLETED && session.robotFileId) return { completed: true, robotFileId: session.robotFileId };
    const existing = await this.prisma.job.findFirst({ where: { type: JobType.UPLOAD_FINALIZE, resourceId: id, status: { in: [JobStatus.QUEUED, JobStatus.RUNNING] } }, include: { steps: true } });
    if (existing) return existing;
    this.assertWritable(session.status, session.expiresAt);
    if (session.parts.length !== session.totalParts) throw new ConflictException('All parts must be uploaded before completion');
    const robot=await this.prisma.robot.findUniqueOrThrow({where:{id:session.robotId},select:{tenantId:true,projectId:true}});
    // Commit FINALIZING and its job together. A concurrent abort must win before
    // this transition or be rejected without deleting the worker's input parts.
    return this.prisma.$transaction(async tx => {
      const updated = await tx.uploadSession.updateMany({ where: { id,
        status: { in: [UploadStatus.CREATED, UploadStatus.UPLOADING, UploadStatus.FAILED] }, expiresAt: { gt: new Date() },
      }, data: { status: UploadStatus.FINALIZING } });
      if (updated.count !== 1) throw new ConflictException('Upload state changed before completion; refresh this upload');
      return this.jobs.create(JobType.UPLOAD_FINALIZE, { payload: { uploadSessionId: id }, resourceType: 'UploadSession', resourceId: id, requestedById: userId, tenantId: robot.tenantId, projectId: robot.projectId, maxAttempts: 3 }, tx);
    });
  }

  async abort(id: string, userId: string, role: UserRole) {
    const session = await this.find(id); this.assertOwner(session.ownerId, userId, role);
    if (session.status === UploadStatus.COMPLETED) throw new ConflictException('Completed uploads cannot be aborted');
    if (session.status === UploadStatus.FINALIZING) throw new ConflictException('Upload session is finalizing; wait for the job to finish before aborting');
    const updated = await this.prisma.uploadSession.updateMany({ where: { id,
      status: { in: [UploadStatus.CREATED, UploadStatus.UPLOADING, UploadStatus.FAILED, UploadStatus.EXPIRED, UploadStatus.ABORTED] },
    }, data: { status: UploadStatus.ABORTED, errorMessage: null } });
    if (updated.count !== 1) throw new ConflictException('Upload state changed; finalizing or completed uploads cannot be aborted');
    await this.storage.deleteObjects(session.parts.map((part) => part.objectKey));
  }

  async finalize(id: string) {
    const session = await this.find(id);
    if (session.status === UploadStatus.COMPLETED && session.robotFileId) return { robotFileId: session.robotFileId, uploadSessionId: id };
    if (session.status === UploadStatus.ABORTED || session.status === UploadStatus.EXPIRED || session.status === UploadStatus.COMPLETED)
      throw new ConflictException(`Upload session is ${session.status.toLowerCase()}`);
    if (session.parts.length !== session.totalParts) throw new ConflictException('Upload is missing one or more parts');
    const ordered = [...session.parts].sort((a, b) => a.partNumber - b.partNumber);
    if (ordered.reduce((sum, item) => sum + BigInt(item.size), 0n) !== session.fileSize) throw new ConflictException('Uploaded parts do not match declared file size');
    const robot = await this.prisma.robot.findUniqueOrThrow({ where: { id: session.robotId }, select: { externalId: true } });
    const extension = extname(session.originalName).toLowerCase();
    const date = session.capturedAt;
    const finalObjectKey = session.finalObjectKey ?? `raw/${robot.externalId ?? session.robotId}/${date.getUTCFullYear()}/${String(date.getUTCMonth() + 1).padStart(2, '0')}/${String(date.getUTCDate()).padStart(2, '0')}/${randomUUID()}${extension}`;
    const claimed = await this.prisma.uploadSession.updateMany({ where: { id,
      status: { in: [UploadStatus.CREATED, UploadStatus.UPLOADING, UploadStatus.FAILED, UploadStatus.FINALIZING] },
    }, data: { finalObjectKey, status: UploadStatus.FINALIZING } });
    if (claimed.count !== 1) throw new ConflictException('Upload state changed before finalization');
    await this.storage.composeObject(finalObjectKey, ordered.map((part) => part.objectKey), session.mimeType);
    try {
      const inspection = await this.inspectObject(finalObjectKey, session.fileType);
      if (inspection.size !== session.fileSize) throw new ConflictException('Composed object size mismatch');
      if (inspection.checksum !== session.checksum) throw new ConflictException('Complete file checksum mismatch');
      if (session.datasetId && isProcessedFolder(session.folderPath ?? '')) {
        assertProcessedPackageName(session.folderPath ?? '', session.originalName);
        await validateProcessedPackage(this.storage, finalObjectKey, Number(session.fileSize));
      }
      const duplicate = await this.prisma.robotFile.findFirst({ where: { checksum: session.checksum, datasetId: session.datasetId ?? null, folderPath: session.folderPath ?? '' }, select: { id: true } });
      if (duplicate) {
        const acceptedId = await this.prisma.$transaction(async tx => {
          await this.lockFinalObject(tx, finalObjectKey);
          const current = await this.validateFinalization(tx, id, finalObjectKey);
          if (current.status === UploadStatus.COMPLETED && current.robotFileId) return current.robotFileId;
          if (session.datasetId) await this.attachToDraft(tx, session.datasetId, duplicate.id);
          await tx.uploadSession.update({ where: { id }, data: { status: UploadStatus.COMPLETED, robotFileId: duplicate.id, completedAt: new Date(), errorMessage: null } });
          return duplicate.id;
        });
        await this.removeUnindexedFinalObject(finalObjectKey).catch(() => undefined);
        await this.cleanupParts(session.parts.map((part) => part.objectKey));
        return { robotFileId: acceptedId, uploadSessionId: id, duplicate: true };
      }
      const file = await this.prisma.$transaction(async (tx) => {
        await this.lockFinalObject(tx, finalObjectKey);
        const current = await this.validateFinalization(tx, id, finalObjectKey);
        if (current.status === UploadStatus.COMPLETED && current.robotFileId) return { id: current.robotFileId };
        const created = await tx.robotFile.create({ data: { folderPath: session.folderPath ?? '', datasetId: session.datasetId, robotId: session.robotId, objectKey: finalObjectKey, originalName: session.originalName, fileType: session.fileType, mimeType: session.mimeType, fileSize: session.fileSize, checksum: session.checksum, recognitionType: session.recognitionType, schemaVersion: session.schemaVersion, capturedAt: session.capturedAt, metadata: session.metadata === null ? undefined : session.metadata as Prisma.InputJsonValue } });
        if (session.datasetId) await this.attachToDraft(tx, session.datasetId, created.id);
        await tx.uploadSession.update({ where: { id }, data: { status: UploadStatus.COMPLETED, robotFileId: created.id, completedAt: new Date(), errorMessage: null } });
        return created;
      });
      await this.cleanupParts(session.parts.map((part) => part.objectKey));
      return { robotFileId: file.id, uploadSessionId: id, objectKey: finalObjectKey };
    } catch (error) {
      await this.removeUnindexedFinalObject(finalObjectKey).catch(() => undefined);
      await this.prisma.uploadSession.updateMany({ where: { id, status: { in: [UploadStatus.FINALIZING, UploadStatus.FAILED] } }, data: { status: UploadStatus.FAILED, errorMessage: error instanceof Error ? error.message : 'Upload finalization failed' } }).catch(() => undefined);
      throw error;
    }
  }

  private async lockFinalObject(tx: Prisma.TransactionClient, objectKey: string) {
    await tx.$queryRaw`SELECT pg_advisory_xact_lock(hashtextextended(${objectKey}, 0))::text`;
  }

  private async validateFinalization(tx: Prisma.TransactionClient, id: string, objectKey: string) {
    // Match orphan cleanup's order: object advisory lock, upload row, indexes.
    await tx.$queryRaw`SELECT id FROM "UploadSession" WHERE id = ${id}::uuid FOR UPDATE`;
    const current = await tx.uploadSession.findUnique({ where: { id } });
    if (!current) throw new NotFoundException('Upload session not found');
    if (current.status === UploadStatus.COMPLETED && current.robotFileId) return current;
    if (current.status !== UploadStatus.FINALIZING || current.finalObjectKey !== objectKey)
      throw new ConflictException('Upload state changed before file registration');
    if (!await this.storage.exists(objectKey)) throw new ConflictException('Composed object disappeared before file registration');
    return current;
  }

  private async removeUnindexedFinalObject(objectKey: string) {
    // A stale execution may fail after another one has already registered the
    // same object. Never compensate by deleting that execution's accepted data.
    await this.prisma.$transaction(async tx => {
      await this.lockFinalObject(tx, objectKey);
      const [files, recycled] = await Promise.all([
        tx.robotFile.count({ where: { objectKey } }), tx.recycledFile.count({ where: { objectKey } }),
      ]);
      if (!files && !recycled) await this.storage.deleteObject(objectKey);
    });
  }

  private datasetRobotId(metadata: unknown): string | undefined {
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)
        || !Object.prototype.hasOwnProperty.call(metadata, 'robotId')) return undefined;
    const robotId = (metadata as Record<string, unknown>).robotId;
    if (typeof robotId !== 'string' || !isUUID(robotId)) throw new ConflictException('数据集所属机器人无效，请联系管理员核对');
    return robotId;
  }

  private async targetDataset(id: string, owner: string | AuthUser) {
    const global = typeof owner === 'string' || owner.role === UserRole.ADMIN || (owner.role === UserRole.SUPER_ADMIN || owner.role === UserRole.SYSTEM_ADMIN);
    const scope = global || typeof owner === 'string' ? {} : { tenantId: owner.tenantId ?? '00000000-0000-0000-0000-000000000000', ...(owner.role === UserRole.TENANT_ADMIN ? {} : { projectId: { in: owner.projectIds } }) };
    const dataset = await this.prisma.dataset.findFirst({ where: { id, deletingAt: null, ...scope } });
    if (!dataset) throw new NotFoundException('数据集不存在或无访问权限');
    datasetFolder(dataset.name);
    return dataset;
  }

  private async attachToDraft(tx: Prisma.TransactionClient, datasetId: string, robotFileId: string) {
    // Serialize draft creation without conflicting with the key-share locks taken
    // by concurrent RobotFile foreign-key inserts against this dataset.
    await tx.$queryRaw`SELECT id FROM "Dataset" WHERE id = ${datasetId}::uuid FOR NO KEY UPDATE`;
    const latest = await tx.datasetVersion.findFirst({ where: { datasetId }, orderBy: { version: 'desc' } });
    let draft = latest?.status === 'DRAFT' ? latest : null;
    if (draft) {
      const writable = await tx.datasetVersion.updateMany({ where: { id: draft.id, status: 'DRAFT' }, data: { updatedAt: new Date() } });
      if (!writable.count) draft = null;
    }
    if (!draft) draft = await tx.datasetVersion.create({ data: { datasetId, version: (latest?.version ?? 0) + 1 } });
    await tx.datasetVersionFile.createMany({ data: [{ datasetVersionId: draft.id, robotFileId }], skipDuplicates: true });
    await tx.dataset.update({ where: { id: datasetId }, data: { updatedAt: new Date() } });
  }

  private async inspectObject(objectKey: string, kind: string) {
    const stream = await this.storage.getObject(objectKey);
    const hash = createHash('sha256');
    let size = 0n, prefixBytes = 0;
    const prefix: Buffer[] = [], document: Buffer[] = [];
    const documentLimit = STRUCTURED_VALIDATION_BYTES[kind as keyof typeof STRUCTURED_VALIDATION_BYTES];
    for await (const raw of stream) {
      const chunk = Buffer.isBuffer(raw) ? raw : Buffer.from(raw);
      size += BigInt(chunk.length);
      // Syntax parsers require a complete structured document. Keep their
      // existing inspection budgets distinct from optional upload policies.
      // Videos and other binary objects never accumulate a complete body.
      if (documentLimit && size > BigInt(documentLimit)) throw new ConflictException(
        `${kind.toUpperCase()} structured-format validation exceeds its ${documentLimit / 1024 / 1024} MiB parser memory budget`);
      hash.update(chunk);
      if (prefixBytes < 64 * 1024) {
        const slice = chunk.subarray(0, 64 * 1024 - prefixBytes);
        prefix.push(slice); prefixBytes += slice.length;
      }
      if (documentLimit) document.push(chunk);
    }
    this.validateFormat(kind, Buffer.concat(prefix), documentLimit ? Buffer.concat(document) : undefined);
    return { checksum: hash.digest('hex'), size };
  }

  private validateFormat(kind: string, header: Buffer, full?: Buffer) {
    if (!header.length) throw new ConflictException('File must not be empty');
    if (kind === 'json') { try { JSON.parse((full ?? header).toString('utf8')); } catch { throw new ConflictException('JSON is invalid'); } }
    if (kind === 'yaml') { try { const parsed: unknown = parseYaml((full ?? header).toString('utf8')); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error(); } catch { throw new ConflictException('YAML must contain a mapping'); } }
    if (kind === 'image' && !(header.subarray(0, 3).equals(Buffer.from([0xff, 0xd8, 0xff])) || header.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])))) throw new ConflictException('Image magic bytes are invalid');
    if (kind === 'pcd' && !/(^|\n)(VERSION|FIELDS|DATA)\s+/m.test(header.toString('ascii'))) throw new ConflictException('PCD header is invalid');
    if (kind === 'map' && !/^P[25]\s/.test(header.toString('ascii'))) throw new ConflictException('PGM header must be P2 or P5');
  }

  private async cleanupParts(keys: string[]) { await this.storage.deleteObjects(keys).catch(() => undefined); }
  private async find(id: string) { const session = await this.prisma.uploadSession.findUnique({ where: { id }, include: { parts: true } }); if (!session) throw new NotFoundException('Upload session not found'); return session; }
  private assertOwner(ownerId: string, userId: string, role: UserRole) { if (ownerId !== userId && role !== UserRole.ADMIN && role !== UserRole.SUPER_ADMIN && role !== UserRole.SYSTEM_ADMIN) throw new ForbiddenException('Upload session belongs to another user'); }
  private assertWritable(status: UploadStatus, expiresAt: Date) { if (expiresAt <= new Date()) throw new ConflictException('Upload session has expired'); if (status !== UploadStatus.CREATED && status !== UploadStatus.UPLOADING && status !== UploadStatus.FAILED) throw new ConflictException(`Upload session is ${status.toLowerCase()}`); }
  private serialize<T extends { fileSize: bigint; maxFileSize: bigint }>(value: T) { return { ...value, fileSize: value.fileSize.toString(), maxFileSize: value.maxFileSize.toString() }; }
}
