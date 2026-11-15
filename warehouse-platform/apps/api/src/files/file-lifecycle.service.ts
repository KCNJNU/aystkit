import { BadRequestException, ConflictException, Inject, Injectable, NotFoundException } from '@nestjs/common';
import { Prisma, RobotFile } from '@prisma/client';
import { extname } from 'node:path';
import { AuthUser } from '../auth/auth.types';
import { PrismaService } from '../prisma/prisma.service';
import { OBJECT_STORAGE, ObjectStorage } from '../storage/object-storage';
import { datasetFolder, validateEntryName, validateFolderPath } from '../storage/dataset-path';
import { FilesService } from './files.service';
import { RecycleQueryDto, RelocateFileDto } from './dto/file-lifecycle.dto';

@Injectable()
export class FileLifecycleService {
  constructor(private readonly prisma: PrismaService, @Inject(OBJECT_STORAGE) private readonly storage: ObjectStorage, private readonly files: FilesService) {}

  private scope(user: AuthUser) {
    return ['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN'].includes(user.role) ? {} : { tenantId: user.tenantId ?? '00000000-0000-0000-0000-000000000000', ...(user.role === 'TENANT_ADMIN' ? {} : { projectId: { in: user.projectIds } }) };
  }
  private async file(id: string, user: AuthUser, tx: Prisma.TransactionClient = this.prisma) {
    const file = await tx.robotFile.findFirst({ where: { id, robot: this.scope(user) }, include: { robot: { select: { tenantId: true, projectId: true } }, tags: { select: { tagId: true } } } });
    if (!file) throw new NotFoundException('文件不存在或无操作权限');
    return file;
  }
  private async lockDataset(tx: Prisma.TransactionClient, datasetId: string | null, user: AuthUser) {
    if (!datasetId) return null;
    await tx.$queryRaw`SELECT id FROM "Dataset" WHERE id = ${datasetId}::uuid FOR NO KEY UPDATE`;
    const dataset = await tx.dataset.findFirst({ where: { id: datasetId, deletingAt: null, ...this.scope(user) } });
    if (!dataset) throw new ConflictException('数据集正在删除或无操作权限');
    return dataset;
  }

  async impact(id: string, user: AuthUser) {
    const file = await this.file(id, user);
    const refs = await this.files.references(id, user);
    const dataset = file.datasetId ? await this.prisma.dataset.findFirst({ where: { id: file.datasetId, deletingAt: null, ...this.scope(user) } }) : null;
    const blocked = file.datasetId && !dataset;
    const reasons = [...(refs.total ? [`仍有 ${refs.total} 处版本引用，请先解除可修改的草稿引用；历史快照引用不可解除`] : []), ...(blocked ? ['所属数据集正在删除或无操作权限'] : [])];
    return { ...refs, objectKey: file.objectKey, folderPath: file.folderPath, datasetId: file.datasetId, datasetName: dataset?.name ?? null, fileSize: file.fileSize.toString(), canRecycle: reasons.length === 0, blockers: reasons, canRelocate: Boolean(dataset) && refs.restrictedCount === 0 && refs.references.every(ref => ref.canDetach), recycleReviewDays: 30 };
  }

  async recycle(id: string, user: AuthUser) {
    const source = await this.file(id, user);
    try {
      await this.prisma.$transaction(async tx => {
        await this.lockDataset(tx, source.datasetId, user);
        await tx.$queryRaw`SELECT id FROM "RobotFile" WHERE id = ${id}::uuid FOR UPDATE`;
        const { robot, tags, ...file } = await this.file(id, user, tx);
        if (await tx.datasetVersionFile.count({ where: { robotFileId: id } })) throw new ConflictException('文件仍被数据集版本引用，请先查看删除影响并解除草稿引用');
        await tx.$queryRaw`SELECT pg_advisory_xact_lock(hashtextextended(${file.objectKey}, 0))::text`;
        const snapshot = JSON.parse(JSON.stringify({ file, tagIds: tags.map(tag => tag.tagId) }, (_, value) => typeof value === 'bigint' ? value.toString() : value)) as Prisma.InputJsonValue;
        await tx.recycledFile.create({ data: { id, datasetId: file.datasetId, tenantId: robot.tenantId, projectId: robot.projectId, originalName: file.originalName, objectKey: file.objectKey, fileSize: file.fileSize, folderPath: file.folderPath, snapshot, deletedById: user.id, reviewAt: new Date(Date.now() + 30 * 86400000) } });
        // FK RESTRICT is the final guard against a concurrent version attachment.
        await tx.robotFile.delete({ where: { id } });
      });
    } catch (error) {
      if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2003') throw new ConflictException('文件引用已变化，请刷新删除影响后重试');
      throw error;
    }
  }

  async recycleList(query: RecycleQueryDto, user: AuthUser) {
    const where = { ...this.scope(user), ...(query.search?.trim() ? { originalName: { contains: query.search.trim(), mode: 'insensitive' as const } } : {}) };
    const [items, total] = await this.prisma.$transaction([
      this.prisma.recycledFile.findMany({ where, select: { id: true, datasetId: true, originalName: true, objectKey: true, fileSize: true, folderPath: true, deletedAt: true, reviewAt: true, purgingAt: true }, orderBy: { deletedAt: 'desc' }, skip: (query.page - 1) * query.pageSize, take: query.pageSize }),
      this.prisma.recycledFile.count({ where }),
    ]);
    return { items: items.map(file => ({ ...file, fileSize: file.fileSize.toString() })), total, page: query.page, pageSize: query.pageSize };
  }

  async restore(id: string, user: AuthUser) {
    const source = await this.prisma.recycledFile.findFirst({ where: { id, ...this.scope(user) } });
    if (!source) throw new NotFoundException('回收文件不存在或无操作权限');
    try {
      return await this.prisma.$transaction(async tx => {
        await this.lockDataset(tx, source.datasetId, user);
        await tx.$queryRaw`SELECT id FROM "RecycledFile" WHERE id = ${id}::uuid FOR UPDATE`;
        const entry = await tx.recycledFile.findFirst({ where: { id, ...this.scope(user) } });
        if (!entry || entry.purgingAt) throw new ConflictException('文件已被处理或正在彻底清理，请刷新回收站');
        const { file, tagIds } = entry.snapshot as unknown as { file: RobotFile; tagIds: string[] };
        if (!await tx.robot.findFirst({ where: { id: file.robotId, ...this.scope(user) } })) throw new ConflictException('原数据来源已删除或无权限，无法恢复');
        if (file.datasetId && file.folderPath && !await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: file.datasetId, path: file.folderPath } } })) throw new ConflictException('原文件夹不存在，无法恢复');
        if (await tx.robotFile.findFirst({ where: { OR: [{ objectKey: entry.objectKey }, { datasetId: file.datasetId, folderPath: file.folderPath, originalName: file.originalName }, { datasetId: file.datasetId, folderPath: file.folderPath, checksum: file.checksum }] } })) throw new ConflictException('原位置存在同名或相同内容文件，请先移动现有文件再恢复');
        if (file.datasetId && await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: file.datasetId, path: [file.folderPath, file.originalName].filter(Boolean).join('/') } } })) throw new ConflictException('原位置存在同名文件夹，请先处理后恢复');
        if (await tx.uploadSession.findFirst({ where: { datasetId: file.datasetId, folderPath: file.folderPath, originalName: file.originalName, status: { in: ['CREATED', 'UPLOADING', 'FINALIZING', 'FAILED'] }, expiresAt: { gt: new Date() } } })) throw new ConflictException('原位置有同名文件正在上传，请完成或取消上传后重试');
        await tx.$queryRaw`SELECT pg_advisory_xact_lock(hashtextextended(${entry.objectKey}, 0))::text`;
        if (!await this.storage.exists(entry.objectKey)) throw new ConflictException('原始存储对象缺失，无法恢复');
        const tags = await tx.tag.findMany({ where: { id: { in: tagIds } }, select: { id: true } });
        await tx.robotFile.create({ data: { ...file, fileSize: BigInt(file.fileSize), capturedAt: new Date(file.capturedAt), createdAt: new Date(file.createdAt), updatedAt: new Date(), metadata: file.metadata === null ? Prisma.JsonNull : file.metadata as Prisma.InputJsonValue, tags: { create: tags.map(tag => ({ tagId: tag.id })) } } });
        await tx.recycledFile.delete({ where: { id } });
        return { id, restored: true };
      }, { timeout: 30000 });
    } catch (error) {
      if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2002') throw new ConflictException('恢复位置已被占用，请刷新后处理冲突');
      throw error;
    }
  }

  async purge(id: string, user: AuthUser) {
    const entry = await this.prisma.$transaction(async tx => {
      await tx.$queryRaw`SELECT id FROM "RecycledFile" WHERE id = ${id}::uuid FOR UPDATE`;
      const item = await tx.recycledFile.findFirst({ where: { id, ...this.scope(user) } });
      if (!item) throw new NotFoundException('回收文件不存在或无操作权限');
      // Persist intent before storage deletion so a failed cleanup can be retried,
      // but never restored into a live row whose object may already be gone.
      return tx.recycledFile.update({ where: { id }, data: { purgingAt: item.purgingAt ?? new Date() } });
    });
    await this.storage.deleteObject(entry.objectKey);
    await this.prisma.recycledFile.deleteMany({ where: { id, purgingAt: { not: null } } });
    return { id, purged: true };
  }

  async relocate(id: string, input: RelocateFileDto, user: AuthUser) {
    if (input.confirmed !== true) throw new BadRequestException('请先确认移动或重命名的影响');
    validateEntryName(input.originalName); validateFolderPath(input.folderPath);
    const source = await this.file(id, user);
    if (!source.datasetId) throw new BadRequestException('此文件未归属数据集，暂不支持整理路径');
    if (extname(source.originalName).toLowerCase() !== extname(input.originalName).toLowerCase()) throw new BadRequestException('重命名须保留原扩展名，避免文件类型与内容不一致');
    let copiedKey: string | undefined;
    let oldKey: string | undefined;
    let result;
    try {
      result = await this.prisma.$transaction(async tx => {
        const dataset = await this.lockDataset(tx, source.datasetId, user);
        await tx.$queryRaw`SELECT id FROM "RobotFile" WHERE id = ${id}::uuid FOR UPDATE`;
        const file = await this.file(id, user, tx);
        if (!dataset || file.datasetId !== dataset.id) throw new ConflictException('文件位置已变化，请刷新');
        if (file.originalName === input.originalName && file.folderPath === input.folderPath) return { id, objectKey: file.objectKey };
        if (input.folderPath && !await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: dataset.id, path: input.folderPath } } })) throw new NotFoundException('目标文件夹不存在');
        await tx.$queryRaw`SELECT v.id FROM "DatasetVersion" v JOIN "DatasetVersionFile" f ON f."datasetVersionId" = v.id WHERE f."robotFileId" = ${id}::uuid ORDER BY v.id FOR UPDATE OF v`;
        const versions = await tx.datasetVersion.findMany({ where: { files: { some: { robotFileId: id } } }, include: { dataset: true } });
        const visible = await tx.datasetVersion.count({ where: { files: { some: { robotFileId: id } }, dataset: { deletingAt: null, ...this.scope(user) } } });
        if (visible !== versions.length || versions.some(version => version.status !== 'DRAFT')) throw new ConflictException('文件被受保护或无权限的数据集版本引用，无法移动或重命名');
        const relative = [input.folderPath, input.originalName].filter(Boolean).join('/');
        const destination = `${datasetFolder(dataset.name)}${relative}`;
        if (Buffer.byteLength(destination) > 1024) throw new BadRequestException('目标路径过长');
        if (await tx.datasetFolder.findUnique({ where: { datasetId_path: { datasetId: dataset.id, path: relative } } }) || await tx.robotFile.findFirst({ where: { id: { not: id }, datasetId: dataset.id, folderPath: input.folderPath, OR: [{ originalName: input.originalName }, { checksum: file.checksum }] } }) || await tx.uploadSession.findFirst({ where: { OR: [{ finalObjectKey: destination, status: { in: ['CREATED', 'UPLOADING', 'FINALIZING', 'FAILED'] }, expiresAt: { gt: new Date() } }, { datasetId: dataset.id, folderPath: input.folderPath, originalName: input.originalName, status: { in: ['CREATED', 'UPLOADING', 'FINALIZING', 'FAILED'] }, expiresAt: { gt: new Date() } }] } }) || await tx.recycledFile.findUnique({ where: { objectKey: destination } }) || await this.storage.exists(destination)) throw new ConflictException('目标名称、路径或内容已被占用，请更换名称或位置');
        for (const key of [file.objectKey, destination].sort()) await tx.$queryRaw`SELECT pg_advisory_xact_lock(hashtextextended(${key}, 0))::text`;
        await this.storage.composeObject(destination, [file.objectKey], file.mimeType);
        copiedKey = destination;
        const copied = await this.storage.statObject(destination);
        if (BigInt(copied.size) !== file.fileSize) throw new ConflictException('复制后的文件大小校验失败，原文件已保留');
        await tx.robotFile.update({ where: { id }, data: { originalName: input.originalName, folderPath: input.folderPath, objectKey: destination } });
        for (const version of versions) await tx.datasetVersion.update({ where: { id: version.id }, data: { updatedAt: new Date(Math.max(Date.now(), version.updatedAt.getTime() + 1)) } });
        oldKey = file.objectKey;
        return { id, objectKey: destination };
      }, { timeout: 60000 });
    } catch (error) {
      if (copiedKey) await this.storage.deleteObject(copiedKey).catch(() => undefined);
      throw error;
    }
    let cleanupPending = false;
    if (oldKey) await this.storage.deleteObject(oldKey).catch(() => { cleanupPending = true; });
    return { ...result, cleanupPending };
  }
}
