import { BadRequestException, ConflictException, Inject, Injectable, NotFoundException, Optional } from '@nestjs/common';
import { Prisma } from '@prisma/client';
import { createHash, randomUUID } from 'node:crypto';
import { extname } from 'node:path';
import { PrismaService } from '../prisma/prisma.service';
import { OBJECT_STORAGE, ObjectStorage } from '../storage/object-storage';
import { ListFilesQueryDto } from './dto/list-files-query.dto';
import { UploadedFile } from './uploaded-file';
import { SettingsService } from '../settings/settings.service';
import { AuthUser } from '../auth/auth.types';

const MAX_FILE_SIZE = 10 * 1024 * 1024;
const EXTENSION_KIND = new Map([
  ['.json', 'json'], ['.jpg', 'image'], ['.jpeg', 'image'], ['.png', 'image'], ['.pcd', 'pcd'],
  ['.pgm', 'map'], ['.yaml', 'yaml'], ['.yml', 'yaml'], ['.log', 'log'], ['.txt', 'log'],
]);

@Injectable()
export class FilesService {
  constructor(private readonly prisma: PrismaService, @Inject(OBJECT_STORAGE) private readonly storage: ObjectStorage, @Optional() private readonly settings?: SettingsService) {}

  async create(input: { robotId: string; recognitionType: string; capturedAt: string; schemaVersion: string; metadata?: string; file: UploadedFile }, scope?: AuthUser) {
    const { file } = input;
    if (!file || file.size === 0 || file.size > MAX_FILE_SIZE) throw new ConflictException('File size must be between 1 byte and 10 MiB');
    const extension = extname(file.originalname).toLowerCase();
    const fileType = EXTENSION_KIND.get(extension) ?? 'binary';
    const robot = await this.prisma.robot.findFirst({ where: { id: input.robotId, ...this.robotScope(scope) }, select: { id: true, externalId: true } });
    if (!robot) throw new NotFoundException('Robot not found');
    const checksum = createHash('sha256').update(file.buffer).digest('hex');
    if (await this.prisma.robotFile.findFirst({ where: { checksum, datasetId: null }, select: { id: true } })) throw new ConflictException('A file with this checksum already exists');
    const capturedAt = new Date(input.capturedAt);
    if (Number.isNaN(capturedAt.valueOf())) throw new ConflictException('capturedAt must be a valid date');
    let metadata: Prisma.InputJsonValue | undefined;
    if (input.metadata) {
      try {
        const parsed: unknown = JSON.parse(input.metadata);
        if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('not an object');
        metadata = parsed as Prisma.InputJsonObject;
      } catch { throw new BadRequestException('metadata must be a valid JSON object'); }
    }
    const robotCode = robot.externalId ?? robot.id;
    const uuid = randomUUID();
    const objectKey = `raw/${robotCode}/${capturedAt.getUTCFullYear()}/${String(capturedAt.getUTCMonth() + 1).padStart(2, '0')}/${String(capturedAt.getUTCDate()).padStart(2, '0')}/${uuid}${extension}`;
    const mimeType = file.mimetype || 'application/octet-stream';
    await this.storage.putObject({ objectKey, content: file.buffer, contentType: mimeType });
    try {
      return this.serialize(await this.prisma.robotFile.create({ data: { robotId: robot.id, objectKey, originalName: file.originalname, fileType, mimeType, fileSize: BigInt(file.size), checksum, recognitionType: input.recognitionType, schemaVersion: input.schemaVersion, capturedAt, metadata } }));
    } catch (error: unknown) {
      await this.storage.deleteObject(objectKey).catch(() => undefined);
      throw error;
    }
  }

  async list(query: ListFilesQueryDto, scope?: AuthUser) {
    const search = query.search?.trim();
    const where: Prisma.RobotFileWhereInput = {
      datasetId: query.datasetId,
      folderPath: query.folderPath,
      robotId: query.robotId,
      robot: this.robotScope(scope),
      fileType: query.fileType,
      recognitionType: query.recognitionType,
      schemaVersion: query.schemaVersion,
      checksum: query.checksum,
      ...(query.tagId && { tags: { some: { tagId: query.tagId } } }),
      ...(search && { OR: [{ originalName: { contains: search, mode: 'insensitive' } }, { objectKey: { contains: search, mode: 'insensitive' } }, { checksum: { contains: search, mode: 'insensitive' } }] }),
      capturedAt: { gte: query.startTime ? new Date(query.startTime) : undefined, lte: query.endTime ? new Date(query.endTime) : undefined },
      createdAt: { gte: query.uploadedStartTime ? new Date(query.uploadedStartTime) : undefined, lte: query.uploadedEndTime ? new Date(query.uploadedEndTime) : undefined },
      fileSize: { gte: query.minSize === undefined ? undefined : BigInt(query.minSize), lte: query.maxSize === undefined ? undefined : BigInt(query.maxSize) },
    };
    const [items, total] = await this.prisma.$transaction([this.prisma.robotFile.findMany({ where, select: { id: true, datasetId: true, folderPath: true, robotId: true, objectKey: true, originalName: true, fileType: true, mimeType: true, fileSize: true, checksum: true, recognitionType: true, schemaVersion: true, capturedAt: true, createdAt: true, updatedAt: true, robot: { select: { id: true, name: true, externalId: true } }, tags: { include: { tag: true } } }, orderBy: { [query.sortBy]: query.sortOrder }, skip: (query.page - 1) * query.pageSize, take: query.pageSize }), this.prisma.robotFile.count({ where })]);
    return { items: items.map((item) => this.serialize(item)), total, page: query.page, pageSize: query.pageSize };
  }

  async get(id: string, scope?: AuthUser) {
    const item = await this.prisma.robotFile.findFirst({ where: { id, robot: this.robotScope(scope) }, include: { robot: { select: { id: true, name: true, externalId: true } }, tags: { include: { tag: true } }, datasetFiles: { where: { datasetVersion: { dataset: this.datasetScope(scope) } }, include: { datasetVersion: { include: { dataset: { select: { id: true, name: true } } } } }, orderBy: { createdAt: 'desc' } } } });
    if (!item) throw new NotFoundException('Robot file not found');
    return this.serialize(item);
  }
  async getDownloadUrl(id: string, scope?: AuthUser) { const item = await this.find(id, scope); if (!(await this.storage.exists(item.objectKey))) throw new NotFoundException('Stored object not found'); const ttl = this.settings ? await this.settings.get('download.presignedTtlSeconds') : 900; return { url: await this.storage.getPresignedDownloadUrl(item.objectKey, ttl) }; }
  async update(id: string, input: { recognitionType?: string; metadata?: Record<string, unknown> }, scope: AuthUser) {
    const item = await this.find(id, scope);
    return this.prisma.$transaction(async tx => {
      // Share the dataset lock with freeze/upload so immutable versions stay stable.
      if (item.datasetId) await tx.$queryRaw`SELECT id FROM "Dataset" WHERE id = ${item.datasetId}::uuid FOR NO KEY UPDATE`;
      const immutable = await tx.datasetVersionFile.count({ where: { robotFileId: id, datasetVersion: { status: { not: 'DRAFT' } } } });
      if (immutable) throw new ConflictException('文件已被冻结或发布版本引用，请上传新文件后创建新版本');
      return this.serialize(await tx.robotFile.update({ where: { id }, data: { recognitionType: input.recognitionType, metadata: input.metadata as Prisma.InputJsonObject | undefined } }));
    });
  }
  async references(id: string, scope: AuthUser) {
    const file = await this.find(id, scope);
    const [memberships, total] = await this.prisma.$transaction([
      this.prisma.datasetVersionFile.findMany({ where: { robotFileId: id, datasetVersion: { dataset: this.datasetScope(scope) } }, include: { datasetVersion: { include: { dataset: { select: { id: true, name: true } } } } }, orderBy: { createdAt: 'desc' } }),
      this.prisma.datasetVersionFile.count({ where: { robotFileId: id } }),
    ]);
    const canWrite = ['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR'].includes(scope.role);
    return { fileId: id, originalName: file.originalName, total, restrictedCount: total - memberships.length, references: memberships.map(({ createdAt, datasetVersion: version }) => ({
      versionId: version.id, datasetId: version.dataset.id, datasetName: version.dataset.name, version: version.version, status: version.status, referencedAt: createdAt,
      canDetach: canWrite && version.status === 'DRAFT',
      reason: version.status !== 'DRAFT' ? '历史快照受保护，无法解除引用' : !canWrite ? '当前账号没有文件修改权限' : null,
    })) };
  }

  async detachReferences(id: string, input: { versionIds: string[]; confirmed: boolean }, scope: AuthUser) {
    if (input.confirmed !== true || !input.versionIds.length || input.versionIds.length > 100 || new Set(input.versionIds).size !== input.versionIds.length) throw new BadRequestException('请选择版本并确认解除引用');
    await this.find(id, scope);
    const versionIds = [...input.versionIds].sort();
    return this.prisma.$transaction(async tx => {
      // Lock before reading status: a simultaneous freeze must either finish first,
      // or fail its updatedAt check after this membership change commits.
      await tx.$queryRaw(Prisma.sql`SELECT id FROM "DatasetVersion" WHERE id IN (${Prisma.join(versionIds.map(versionId => Prisma.sql`${versionId}::uuid`))}) ORDER BY id FOR UPDATE`);
      const versions = await tx.datasetVersion.findMany({ where: { id: { in: versionIds }, dataset: this.datasetScope(scope), files: { some: { robotFileId: id } } } });
      if (versions.length !== versionIds.length) throw new ConflictException('引用已变化或无操作权限，请刷新引用列表后重试');
      if (versions.some(version => version.status !== 'DRAFT')) throw new ConflictException('冻结、已发布或归档版本不能解除引用，请保留历史快照');
      const removed = await tx.datasetVersionFile.deleteMany({ where: { robotFileId: id, datasetVersionId: { in: versionIds } } });
      if (removed.count !== versionIds.length) throw new ConflictException('引用已变化，请刷新引用列表后重试');
      for (const version of versions) {
        const remaining = await tx.datasetVersionFile.findMany({ where: { datasetVersionId: version.id }, select: { robotFile: { select: { fileSize: true } } } });
        await tx.datasetVersion.update({ where: { id: version.id }, data: { fileCount: remaining.length, totalSize: remaining.reduce((sum, member) => sum + member.robotFile.fileSize, 0n), updatedAt: new Date(Math.max(Date.now(), version.updatedAt.getTime() + 1)) } });
      }
      return { detachedCount: removed.count, remainingCount: await tx.datasetVersionFile.count({ where: { robotFileId: id } }) };
    });
  }

  private datasetScope(scope?: AuthUser): Prisma.DatasetWhereInput { if (!scope || ['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN'].includes(scope.role)) return { deletingAt: null }; return { deletingAt: null, tenantId: scope.tenantId ?? '__NO_TENANT__', ...(scope.role === 'TENANT_ADMIN' ? {} : { projectId: { in: scope.projectIds } }) }; }
  private async find(id: string, scope?: AuthUser) { const item = await this.prisma.robotFile.findFirst({ where: { id, robot: this.robotScope(scope) } }); if (!item) throw new NotFoundException('Robot file not found'); return item; }
  private robotScope(scope?: AuthUser): Prisma.RobotWhereInput { if (!scope || scope.role === 'ADMIN' || (scope.role === 'SUPER_ADMIN' || scope.role === 'SYSTEM_ADMIN')) return {}; return { tenantId: scope.tenantId ?? '__NO_TENANT__', ...(scope.role === 'TENANT_ADMIN' ? {} : { projectId: { in: scope.projectIds } }) }; }
  private serialize<T extends { fileSize: bigint }>(item: T): Omit<T, 'fileSize'> & { fileSize: string } { return { ...item, fileSize: item.fileSize.toString() }; }
}
