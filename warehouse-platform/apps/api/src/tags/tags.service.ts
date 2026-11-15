import { AuthUser } from '../auth/auth.types';
import { isGlobalAdmin } from '../auth/permissions';
import { warehouseScope } from '../warehouse/warehouse-query.service';
import { BadRequestException, ConflictException, Injectable, NotFoundException } from '@nestjs/common';
import { Prisma } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';

@Injectable()
export class TagsService {
  constructor(private readonly prisma: PrismaService) {}

  list(user: AuthUser) {
    return this.prisma.tag.findMany({
      where: this.tagScope(user),
      include: { _count: { select: { robotFiles: true, datasets: true } } },
      orderBy: { name: 'asc' },
    });
  }

  async create(name: string, color: string, user: AuthUser) {
    this.assertColor(color);
    try { return await this.prisma.tag.create({ data: { name: name.trim(), color, tenantId: user.tenantId } }); }
    catch (error) { this.throwConflict(error); }
  }

  async update(id: string, input: { name?: string; color?: string }, user: AuthUser) {
    await this.checkTags([id], user);
    if (input.color) this.assertColor(input.color);
    try { return await this.prisma.tag.update({ where: { id }, data: { name: input.name?.trim(), color: input.color } }); }
    catch (error) {
      if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2025') throw new NotFoundException('Tag not found');
      this.throwConflict(error);
    }
  }

  async remove(id: string, user: AuthUser) {
    await this.checkTags([id], user);
    try { await this.prisma.tag.delete({ where: { id } }); }
    catch (error) { if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2025') throw new NotFoundException('Tag not found'); throw error; }
  }

  async attachFile(fileId: string, tagId: string, user: AuthUser) { await this.checkFiles([fileId], user); await this.checkTags([tagId], user); return this.prisma.robotFileTag.upsert({ where: { robotFileId_tagId: { robotFileId: fileId, tagId } }, update: {}, create: { robotFileId: fileId, tagId } }); }
  async detachFile(fileId: string, tagId: string, user: AuthUser) { await this.checkFiles([fileId], user); await this.checkTags([tagId], user); return this.prisma.robotFileTag.deleteMany({ where: { robotFileId: fileId, tagId } }); }
  async attachDataset(datasetId: string, tagId: string, user: AuthUser) { await this.checkDataset(datasetId, user); await this.checkTags([tagId], user); return this.prisma.datasetTag.upsert({ where: { datasetId_tagId: { datasetId, tagId } }, update: {}, create: { datasetId, tagId } }); }
  async detachDataset(datasetId: string, tagId: string, user: AuthUser) { await this.checkDataset(datasetId, user); await this.checkTags([tagId], user); return this.prisma.datasetTag.deleteMany({ where: { datasetId, tagId } }); }
  async bulkAttachFiles(fileIds: string[], tagIds: string[], user: AuthUser) {
    await this.checkFiles(fileIds, user); await this.checkTags(tagIds, user);
    if (!fileIds.length || !tagIds.length) throw new BadRequestException('fileIds and tagIds are required');
    const [files, tags] = await Promise.all([this.prisma.robotFile.count({ where: { id: { in: fileIds } } }), this.prisma.tag.count({ where: { id: { in: tagIds } } })]);
    if (files !== new Set(fileIds).size || tags !== new Set(tagIds).size) throw new NotFoundException('One or more files or tags were not found');
    const pairs = fileIds.flatMap((robotFileId) => tagIds.map((tagId) => ({ robotFileId, tagId })));
    await this.prisma.robotFileTag.createMany({ data: pairs, skipDuplicates: true });
    return { attached: pairs.length };
  }

  private tagScope(user: AuthUser) { return isGlobalAdmin(user.role) ? {} : { tenantId: user.tenantId ?? '00000000-0000-0000-0000-000000000000' }; }
  private async checkTags(ids: string[], user: AuthUser) { if (await this.prisma.tag.count({ where: { id: { in: ids }, ...this.tagScope(user) } }) !== new Set(ids).size) throw new NotFoundException('标签不存在或无权访问'); }
  private async checkFiles(ids: string[], user: AuthUser) { if (await this.prisma.robotFile.count({ where: { id: { in: ids }, robot: warehouseScope(user) } }) !== new Set(ids).size) throw new NotFoundException('文件不存在或无权访问'); }
  private async checkDataset(id: string, user: AuthUser) { if (!await this.prisma.dataset.findFirst({ where: { id, ...warehouseScope(user) } })) throw new NotFoundException('数据集不存在或无权访问'); }
  private assertColor(color: string) { if (!/^#[0-9a-f]{6}$/i.test(color)) throw new BadRequestException('color must be a six-digit hex value'); }
  private throwConflict(error: unknown): never { if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2002') throw new ConflictException('Tag name already exists'); throw error; }
}
