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
Object.defineProperty(exports, "__esModule", { value: true });
exports.TagsService = void 0;
const permissions_1 = require("../auth/permissions");
const warehouse_query_service_1 = require("../warehouse/warehouse-query.service");
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const prisma_service_1 = require("../prisma/prisma.service");
let TagsService = class TagsService {
    prisma;
    constructor(prisma) {
        this.prisma = prisma;
    }
    list(user) {
        return this.prisma.tag.findMany({
            where: this.tagScope(user),
            include: { _count: { select: { robotFiles: true, datasets: true } } },
            orderBy: { name: 'asc' },
        });
    }
    async create(name, color, user) {
        this.assertColor(color);
        try {
            return await this.prisma.tag.create({ data: { name: name.trim(), color, tenantId: user.tenantId } });
        }
        catch (error) {
            this.throwConflict(error);
        }
    }
    async update(id, input, user) {
        await this.checkTags([id], user);
        if (input.color)
            this.assertColor(input.color);
        try {
            return await this.prisma.tag.update({ where: { id }, data: { name: input.name?.trim(), color: input.color } });
        }
        catch (error) {
            if (error instanceof client_1.Prisma.PrismaClientKnownRequestError && error.code === 'P2025')
                throw new common_1.NotFoundException('Tag not found');
            this.throwConflict(error);
        }
    }
    async remove(id, user) {
        await this.checkTags([id], user);
        try {
            await this.prisma.tag.delete({ where: { id } });
        }
        catch (error) {
            if (error instanceof client_1.Prisma.PrismaClientKnownRequestError && error.code === 'P2025')
                throw new common_1.NotFoundException('Tag not found');
            throw error;
        }
    }
    async attachFile(fileId, tagId, user) { await this.checkFiles([fileId], user); await this.checkTags([tagId], user); return this.prisma.robotFileTag.upsert({ where: { robotFileId_tagId: { robotFileId: fileId, tagId } }, update: {}, create: { robotFileId: fileId, tagId } }); }
    async detachFile(fileId, tagId, user) { await this.checkFiles([fileId], user); await this.checkTags([tagId], user); return this.prisma.robotFileTag.deleteMany({ where: { robotFileId: fileId, tagId } }); }
    async attachDataset(datasetId, tagId, user) { await this.checkDataset(datasetId, user); await this.checkTags([tagId], user); return this.prisma.datasetTag.upsert({ where: { datasetId_tagId: { datasetId, tagId } }, update: {}, create: { datasetId, tagId } }); }
    async detachDataset(datasetId, tagId, user) { await this.checkDataset(datasetId, user); await this.checkTags([tagId], user); return this.prisma.datasetTag.deleteMany({ where: { datasetId, tagId } }); }
    async bulkAttachFiles(fileIds, tagIds, user) {
        await this.checkFiles(fileIds, user);
        await this.checkTags(tagIds, user);
        if (!fileIds.length || !tagIds.length)
            throw new common_1.BadRequestException('fileIds and tagIds are required');
        const [files, tags] = await Promise.all([this.prisma.robotFile.count({ where: { id: { in: fileIds } } }), this.prisma.tag.count({ where: { id: { in: tagIds } } })]);
        if (files !== new Set(fileIds).size || tags !== new Set(tagIds).size)
            throw new common_1.NotFoundException('One or more files or tags were not found');
        const pairs = fileIds.flatMap((robotFileId) => tagIds.map((tagId) => ({ robotFileId, tagId })));
        await this.prisma.robotFileTag.createMany({ data: pairs, skipDuplicates: true });
        return { attached: pairs.length };
    }
    tagScope(user) { return (0, permissions_1.isGlobalAdmin)(user.role) ? {} : { tenantId: user.tenantId ?? '00000000-0000-0000-0000-000000000000' }; }
    async checkTags(ids, user) { if (await this.prisma.tag.count({ where: { id: { in: ids }, ...this.tagScope(user) } }) !== new Set(ids).size)
        throw new common_1.NotFoundException('标签不存在或无权访问'); }
    async checkFiles(ids, user) { if (await this.prisma.robotFile.count({ where: { id: { in: ids }, robot: (0, warehouse_query_service_1.warehouseScope)(user) } }) !== new Set(ids).size)
        throw new common_1.NotFoundException('文件不存在或无权访问'); }
    async checkDataset(id, user) { if (!await this.prisma.dataset.findFirst({ where: { id, ...(0, warehouse_query_service_1.warehouseScope)(user) } }))
        throw new common_1.NotFoundException('数据集不存在或无权访问'); }
    assertColor(color) { if (!/^#[0-9a-f]{6}$/i.test(color))
        throw new common_1.BadRequestException('color must be a six-digit hex value'); }
    throwConflict(error) { if (error instanceof client_1.Prisma.PrismaClientKnownRequestError && error.code === 'P2002')
        throw new common_1.ConflictException('Tag name already exists'); throw error; }
};
exports.TagsService = TagsService;
exports.TagsService = TagsService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService])
], TagsService);
//# sourceMappingURL=tags.service.js.map