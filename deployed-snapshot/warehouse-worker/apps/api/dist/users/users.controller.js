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
exports.UsersController = void 0;
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const argon2 = require("argon2");
const class_transformer_1 = require("class-transformer");
const class_validator_1 = require("class-validator");
const auth_decorators_1 = require("../auth/auth.decorators");
const permissions_1 = require("../auth/permissions");
const prisma_service_1 = require("../prisma/prisma.service");
class UserPageQuery {
    page = 1;
    pageSize = 20;
    search;
    role;
}
__decorate([
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    __metadata("design:type", Object)
], UserPageQuery.prototype, "page", void 0);
__decorate([
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    (0, class_validator_1.Max)(100),
    __metadata("design:type", Object)
], UserPageQuery.prototype, "pageSize", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], UserPageQuery.prototype, "search", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsEnum)(client_1.UserRole),
    __metadata("design:type", String)
], UserPageQuery.prototype, "role", void 0);
class CreateUserDto {
    email;
    displayName;
    role;
    tenantId;
    projectIds;
    temporaryPassword;
}
__decorate([
    (0, class_validator_1.IsEmail)(),
    __metadata("design:type", String)
], CreateUserDto.prototype, "email", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.MinLength)(2),
    __metadata("design:type", String)
], CreateUserDto.prototype, "displayName", void 0);
__decorate([
    (0, class_validator_1.IsEnum)(client_1.UserRole),
    __metadata("design:type", String)
], CreateUserDto.prototype, "role", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], CreateUserDto.prototype, "tenantId", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsArray)(),
    (0, class_validator_1.ArrayMaxSize)(100),
    (0, class_validator_1.ArrayUnique)(),
    (0, class_validator_1.IsUUID)('all', { each: true }),
    __metadata("design:type", Array)
], CreateUserDto.prototype, "projectIds", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.MinLength)(12),
    __metadata("design:type", String)
], CreateUserDto.prototype, "temporaryPassword", void 0);
class UpdateUserDto {
    email;
    displayName;
    isActive;
    role;
    tenantId;
    projectIds;
}
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsEmail)(),
    __metadata("design:type", String)
], UpdateUserDto.prototype, "email", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.MinLength)(2),
    __metadata("design:type", String)
], UpdateUserDto.prototype, "displayName", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsBoolean)(),
    __metadata("design:type", Boolean)
], UpdateUserDto.prototype, "isActive", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsEnum)(client_1.UserRole),
    __metadata("design:type", String)
], UpdateUserDto.prototype, "role", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], UpdateUserDto.prototype, "tenantId", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsArray)(),
    (0, class_validator_1.ArrayMaxSize)(100),
    (0, class_validator_1.ArrayUnique)(),
    (0, class_validator_1.IsUUID)('all', { each: true }),
    __metadata("design:type", Array)
], UpdateUserDto.prototype, "projectIds", void 0);
class ResetPasswordDto {
    temporaryPassword;
}
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.MinLength)(12),
    __metadata("design:type", String)
], ResetPasswordDto.prototype, "temporaryPassword", void 0);
let UsersController = class UsersController {
    prisma;
    constructor(prisma) {
        this.prisma = prisma;
    }
    async scopeOptions(actor) {
        const global = (0, permissions_1.isGlobalAdmin)(actor.role);
        const tenants = await this.prisma.tenant.findMany({ where: { status: 'ACTIVE', ...(global ? {} : { id: actor.tenantId ?? '00000000-0000-0000-0000-000000000000' }) }, select: { id: true, name: true, code: true }, orderBy: { name: 'asc' }, take: 100 });
        const projects = await this.prisma.project.findMany({ where: { tenantId: { in: tenants.map(item => item.id) }, status: 'ACTIVE' }, select: { id: true, name: true, tenantId: true }, orderBy: { name: 'asc' }, take: 100 });
        return { tenants, projects, assignableRoles: (0, permissions_1.assignableRoles)(actor.role) };
    }
    async page(query, actor) {
        const where = { deletedAt: null, role: query.role, ...this.scope(actor), ...(query.search && { OR: [{ email: { contains: query.search, mode: 'insensitive' } }, { displayName: { contains: query.search, mode: 'insensitive' } }] }) };
        const [items, total] = await this.prisma.$transaction([this.prisma.user.findMany({ where, select: publicUser, orderBy: { createdAt: 'desc' }, skip: (query.page - 1) * query.pageSize, take: query.pageSize }), this.prisma.user.count({ where })]);
        return { items, total, page: query.page, pageSize: query.pageSize };
    }
    async create(body, actor) {
        this.assertAssignable(actor, body.role);
        if (await this.prisma.user.findUnique({ where: { email: body.email.toLowerCase() } }))
            throw new common_1.BadRequestException('邮箱已存在或已被历史账号使用');
        const tenantId = await this.assignmentTenant(actor, body.role, body.tenantId);
        const projectIds = (0, permissions_1.isGlobalAdmin)(body.role) || body.role === 'TENANT_ADMIN' ? [] : body.projectIds ?? [];
        await this.validateProjects(tenantId, projectIds);
        if (body.role !== 'BASIC' && !(0, permissions_1.isGlobalAdmin)(body.role) && body.role !== 'TENANT_ADMIN' && !projectIds.length)
            throw new common_1.BadRequestException('请选择至少一个数据项目');
        return this.prisma.user.create({ data: { email: body.email.toLowerCase(), displayName: body.displayName, role: body.role, tenantId, projectAccess: { create: projectIds.map(projectId => ({ projectId })) }, passwordHash: await argon2.hash(body.temporaryPassword, { type: argon2.argon2id }), mustChangePassword: true }, select: publicUser });
    }
    async update(id, body, actor) {
        const current = await this.visible(id, actor);
        this.assertManageable(actor, current.role, id);
        if (body.role && body.role !== current.role)
            this.assertAssignable(actor, body.role);
        if (id === actor.id && (body.isActive === false || (body.role && body.role !== current.role) || body.tenantId !== undefined || body.projectIds !== undefined))
            throw new common_1.ForbiddenException('不能修改自己的角色、数据范围或停用自己');
        const role = body.role ?? current.role;
        const tenantId = await this.assignmentTenant(actor, role, body.tenantId ?? current.tenantId ?? undefined);
        const projectIds = (0, permissions_1.isGlobalAdmin)(role) || role === 'TENANT_ADMIN' ? [] : body.projectIds ?? current.projectAccess.map(item => item.projectId);
        await this.validateProjects(tenantId, projectIds);
        if (role !== 'BASIC' && !(0, permissions_1.isGlobalAdmin)(role) && role !== 'TENANT_ADMIN' && !projectIds.length)
            throw new common_1.BadRequestException('请选择至少一个数据项目');
        return this.prisma.$transaction(async (tx) => {
            await tx.$queryRaw `SELECT pg_advisory_xact_lock(238190923)::text AS locked`;
            const target = await tx.user.findUniqueOrThrow({ where: { id } });
            if (target.deletedAt)
                throw new common_1.NotFoundException('用户已删除');
            this.assertManageable(actor, target.role, id);
            if ((0, permissions_1.isSuperAdmin)(target.role) && (body.isActive === false || !(0, permissions_1.isSuperAdmin)(role)))
                await this.assertOtherSuper(tx, id);
            const user = await tx.user.update({ where: { id }, data: { email: body.email?.toLowerCase(), displayName: body.displayName, isActive: body.isActive, role, tenantId, projectAccess: { deleteMany: {}, create: projectIds.map(projectId => ({ projectId })) } }, select: publicUser });
            if (body.isActive === false || body.role !== undefined || body.projectIds !== undefined || body.tenantId !== undefined)
                await tx.session.deleteMany({ where: { userId: id } });
            return user;
        });
    }
    async remove(id, actor) {
        const current = await this.visible(id, actor);
        this.assertManageable(actor, current.role, id);
        if (id === actor.id)
            throw new common_1.ForbiddenException('不能删除当前登录账号');
        await this.prisma.$transaction(async (tx) => {
            await tx.$queryRaw `SELECT pg_advisory_xact_lock(238190923)::text AS locked`;
            const target = await tx.user.findUniqueOrThrow({ where: { id } });
            this.assertManageable(actor, target.role, id);
            if ((0, permissions_1.isSuperAdmin)(target.role))
                await this.assertOtherSuper(tx, id);
            await tx.session.deleteMany({ where: { userId: id } });
            await tx.user.update({ where: { id }, data: { deletedAt: new Date(), isActive: false } });
        });
    }
    async reset(id, body, actor) {
        const current = await this.visible(id, actor);
        this.assertManageable(actor, current.role, id);
        await this.prisma.$transaction([this.prisma.user.update({ where: { id }, data: { passwordHash: await argon2.hash(body.temporaryPassword, { type: argon2.argon2id }), mustChangePassword: true } }), this.prisma.session.deleteMany({ where: { userId: id } })]);
        return { success: true };
    }
    async revokeSessions(id, actor) { const current = await this.visible(id, actor); this.assertManageable(actor, current.role, id); await this.prisma.session.deleteMany({ where: { userId: id } }); }
    scope(actor) { return (0, permissions_1.isGlobalAdmin)(actor.role) ? {} : { tenantId: actor.tenantId ?? '__NO_TENANT__' }; }
    async visible(id, actor) { const user = await this.prisma.user.findFirst({ where: { id, deletedAt: null, ...this.scope(actor) }, select: { id: true, tenantId: true, role: true, projectAccess: { select: { projectId: true } } } }); if (!user)
        throw new common_1.NotFoundException('用户不存在'); return user; }
    async validateProjects(tenantId, projectIds) { if (!projectIds.length)
        return; if (!tenantId || await this.prisma.project.count({ where: { id: { in: projectIds }, tenantId, status: 'ACTIVE' } }) !== projectIds.length)
        throw new common_1.ForbiddenException('项目必须属于用户的数据空间'); }
    assertAssignable(actor, role) { if (!(0, permissions_1.assignableRoles)(actor.role).includes(role))
        throw new common_1.ForbiddenException('不能授予同级或更高级角色'); }
    assertManageable(actor, role, id) { if (actor.id !== id && !(0, permissions_1.isSuperAdmin)(actor.role) && !(0, permissions_1.assignableRoles)(actor.role).includes(role))
        throw new common_1.ForbiddenException('不能管理同级或更高级账号'); }
    async assertOtherSuper(tx, id) { if (!await tx.user.count({ where: { id: { not: id }, role: { in: ['ADMIN', 'SUPER_ADMIN'] }, isActive: true, deletedAt: null } }))
        throw new common_1.ForbiddenException('必须保留至少一个启用的超级管理员'); }
    async assignmentTenant(actor, role, requested) {
        if ((0, permissions_1.isGlobalAdmin)(role))
            return null;
        if (!(0, permissions_1.isGlobalAdmin)(actor.role)) {
            if (!actor.tenantId)
                throw new common_1.BadRequestException('当前账号未分配数据空间');
            if (requested && requested !== actor.tenantId)
                throw new common_1.ForbiddenException('不能分配其他数据空间');
            return actor.tenantId;
        }
        if (!requested)
            throw new common_1.BadRequestException('请选择用户所属数据空间');
        const tenant = await this.prisma.tenant.findFirst({ where: { id: requested, status: 'ACTIVE' }, select: { id: true } });
        if (!tenant)
            throw new common_1.BadRequestException('数据空间不存在或已停用');
        return tenant.id;
    }
};
exports.UsersController = UsersController;
__decorate([
    (0, common_1.Get)('scope-options'),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], UsersController.prototype, "scopeOptions", null);
__decorate([
    (0, common_1.Get)('page'),
    __param(0, (0, common_1.Query)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [UserPageQuery, Object]),
    __metadata("design:returntype", Promise)
], UsersController.prototype, "page", null);
__decorate([
    (0, common_1.Post)(),
    __param(0, (0, common_1.Body)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [CreateUserDto, Object]),
    __metadata("design:returntype", Promise)
], UsersController.prototype, "create", null);
__decorate([
    (0, common_1.Patch)(':id'),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, UpdateUserDto, Object]),
    __metadata("design:returntype", Promise)
], UsersController.prototype, "update", null);
__decorate([
    (0, common_1.Delete)(':id'),
    (0, common_1.HttpCode)(204),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", Promise)
], UsersController.prototype, "remove", null);
__decorate([
    (0, common_1.Post)(':id/reset-password'),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, ResetPasswordDto, Object]),
    __metadata("design:returntype", Promise)
], UsersController.prototype, "reset", null);
__decorate([
    (0, common_1.Delete)(':id/sessions'),
    (0, common_1.HttpCode)(204),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", Promise)
], UsersController.prototype, "revokeSessions", null);
exports.UsersController = UsersController = __decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN),
    (0, common_1.Controller)('v1/users'),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService])
], UsersController);
const publicUser = { id: true, email: true, displayName: true, role: true, tenantId: true, projectAccess: { select: { projectId: true } }, isActive: true, mustChangePassword: true, lastLoginAt: true, createdAt: true, updatedAt: true };
//# sourceMappingURL=users.controller.js.map