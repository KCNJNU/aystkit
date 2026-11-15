import { BadRequestException, Body, Controller, Delete, ForbiddenException, Get, HttpCode, NotFoundException, Param, Patch, Post, Query } from '@nestjs/common';
import { UserRole } from '@prisma/client';
import * as argon2 from 'argon2';
import { Type } from 'class-transformer';
import { ArrayMaxSize, ArrayUnique, IsArray, IsBoolean, IsEmail, IsEnum, IsInt, IsOptional, IsString, IsUUID, Max, Min, MinLength } from 'class-validator';
import { CurrentUser, Roles } from '../auth/auth.decorators';
import { AuthUser } from '../auth/auth.types';
import { assignableRoles, isGlobalAdmin, isSuperAdmin } from '../auth/permissions';
import { PrismaService } from '../prisma/prisma.service';

class UserPageQuery { @Type(() => Number) @IsInt() @Min(1) page = 1; @Type(() => Number) @IsInt() @Min(1) @Max(100) pageSize = 20; @IsOptional() @IsString() search?: string; @IsOptional() @IsEnum(UserRole) role?: UserRole; }
class CreateUserDto { @IsEmail() email!: string; @IsString() @MinLength(2) displayName!: string; @IsEnum(UserRole) role!: UserRole; @IsOptional() @IsUUID() tenantId?: string; @IsOptional() @IsArray() @ArrayMaxSize(100) @ArrayUnique() @IsUUID('all', { each: true }) projectIds?: string[]; @IsString() @MinLength(12) temporaryPassword!: string; }
class UpdateUserDto { @IsOptional() @IsEmail() email?: string; @IsOptional() @IsString() @MinLength(2) displayName?: string; @IsOptional() @IsBoolean() isActive?: boolean; @IsOptional() @IsEnum(UserRole) role?: UserRole; @IsOptional() @IsUUID() tenantId?: string; @IsOptional() @IsArray() @ArrayMaxSize(100) @ArrayUnique() @IsUUID('all', { each: true }) projectIds?: string[]; }
class ResetPasswordDto { @IsString() @MinLength(12) temporaryPassword!: string; }

@Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN)
@Controller('v1/users')
export class UsersController {
  constructor(private readonly prisma: PrismaService) {}
  @Get('scope-options') async scopeOptions(@CurrentUser() actor: AuthUser) {
    const global = isGlobalAdmin(actor.role);
    const tenants = await this.prisma.tenant.findMany({ where: { status: 'ACTIVE', ...(global ? {} : { id: actor.tenantId ?? '00000000-0000-0000-0000-000000000000' }) }, select: { id: true, name: true, code: true }, orderBy: { name: 'asc' }, take: 100 });
    const projects = await this.prisma.project.findMany({ where: { tenantId: { in: tenants.map(item => item.id) }, status: 'ACTIVE' }, select: { id: true, name: true, tenantId: true }, orderBy: { name: 'asc' }, take: 100 });
    return { tenants, projects, assignableRoles: assignableRoles(actor.role) };
  }
  @Get('page') async page(@Query() query: UserPageQuery, @CurrentUser() actor: AuthUser) {
    const where = { deletedAt: null, role: query.role, ...this.scope(actor), ...(query.search && { OR: [{ email: { contains: query.search, mode: 'insensitive' as const } }, { displayName: { contains: query.search, mode: 'insensitive' as const } }] }) };
    const [items, total] = await this.prisma.$transaction([this.prisma.user.findMany({ where, select: publicUser, orderBy: { createdAt: 'desc' }, skip: (query.page - 1) * query.pageSize, take: query.pageSize }), this.prisma.user.count({ where })]);
    return { items, total, page: query.page, pageSize: query.pageSize };
  }
  @Post() async create(@Body() body: CreateUserDto, @CurrentUser() actor: AuthUser) {
    this.assertAssignable(actor, body.role);
    if (await this.prisma.user.findUnique({ where: { email: body.email.toLowerCase() } })) throw new BadRequestException('邮箱已存在或已被历史账号使用');
    const tenantId = await this.assignmentTenant(actor, body.role, body.tenantId);
    const projectIds = isGlobalAdmin(body.role) || body.role === 'TENANT_ADMIN' ? [] : body.projectIds ?? [];
    await this.validateProjects(tenantId, projectIds);
    if (body.role !== 'BASIC' && !isGlobalAdmin(body.role) && body.role !== 'TENANT_ADMIN' && !projectIds.length) throw new BadRequestException('请选择至少一个数据项目');
    return this.prisma.user.create({ data: { email: body.email.toLowerCase(), displayName: body.displayName, role: body.role, tenantId, projectAccess: { create: projectIds.map(projectId => ({ projectId })) }, passwordHash: await argon2.hash(body.temporaryPassword, { type: argon2.argon2id }), mustChangePassword: true }, select: publicUser });
  }
  @Patch(':id') async update(@Param('id') id: string, @Body() body: UpdateUserDto, @CurrentUser() actor: AuthUser) {
    const current = await this.visible(id, actor); this.assertManageable(actor, current.role, id);
    if (body.role && body.role !== current.role) this.assertAssignable(actor, body.role);
    if (id === actor.id && (body.isActive === false || (body.role && body.role !== current.role) || body.tenantId !== undefined || body.projectIds !== undefined)) throw new ForbiddenException('不能修改自己的角色、数据范围或停用自己');
    const role = body.role ?? current.role;
    const tenantId = await this.assignmentTenant(actor, role, body.tenantId ?? current.tenantId ?? undefined);
    const projectIds = isGlobalAdmin(role) || role === 'TENANT_ADMIN' ? [] : body.projectIds ?? current.projectAccess.map(item => item.projectId);
    await this.validateProjects(tenantId, projectIds);
    if (role !== 'BASIC' && !isGlobalAdmin(role) && role !== 'TENANT_ADMIN' && !projectIds.length) throw new BadRequestException('请选择至少一个数据项目');
    return this.prisma.$transaction(async tx => {
      // Serialize global-admin changes so two requests cannot remove the last administrator.
      await tx.$queryRaw`SELECT pg_advisory_xact_lock(238190923)::text AS locked`;
      const target = await tx.user.findUniqueOrThrow({ where: { id } });
      if (target.deletedAt) throw new NotFoundException('用户已删除');
      this.assertManageable(actor, target.role, id);
      if (isSuperAdmin(target.role) && (body.isActive === false || !isSuperAdmin(role))) await this.assertOtherSuper(tx, id);
      const user = await tx.user.update({ where: { id }, data: { email: body.email?.toLowerCase(), displayName: body.displayName, isActive: body.isActive, role, tenantId, projectAccess: { deleteMany: {}, create: projectIds.map(projectId => ({ projectId })) } }, select: publicUser });
      if (body.isActive === false || body.role !== undefined || body.projectIds !== undefined || body.tenantId !== undefined) await tx.session.deleteMany({ where: { userId: id } });
      return user;
    });
  }
  @Delete(':id') @HttpCode(204) async remove(@Param('id') id: string, @CurrentUser() actor: AuthUser) {
    const current = await this.visible(id, actor); this.assertManageable(actor, current.role, id);
    if (id === actor.id) throw new ForbiddenException('不能删除当前登录账号');
    await this.prisma.$transaction(async tx => {
      await tx.$queryRaw`SELECT pg_advisory_xact_lock(238190923)::text AS locked`;
      const target = await tx.user.findUniqueOrThrow({ where: { id } });
      this.assertManageable(actor, target.role, id);
      if (isSuperAdmin(target.role)) await this.assertOtherSuper(tx, id);
      await tx.session.deleteMany({ where: { userId: id } });
      await tx.user.update({ where: { id }, data: { deletedAt: new Date(), isActive: false } });
    });
  }
  @Post(':id/reset-password') async reset(@Param('id') id: string, @Body() body: ResetPasswordDto, @CurrentUser() actor: AuthUser) {
    const current = await this.visible(id, actor); this.assertManageable(actor, current.role, id);
    await this.prisma.$transaction([this.prisma.user.update({ where: { id }, data: { passwordHash: await argon2.hash(body.temporaryPassword, { type: argon2.argon2id }), mustChangePassword: true } }), this.prisma.session.deleteMany({ where: { userId: id } })]); return { success: true };
  }
  @Delete(':id/sessions') @HttpCode(204) async revokeSessions(@Param('id') id: string, @CurrentUser() actor: AuthUser) { const current = await this.visible(id, actor); this.assertManageable(actor, current.role, id); await this.prisma.session.deleteMany({ where: { userId: id } }); }
  private scope(actor: AuthUser) { return isGlobalAdmin(actor.role) ? {} : { tenantId: actor.tenantId ?? '__NO_TENANT__' }; }
  private async visible(id: string, actor: AuthUser) { const user = await this.prisma.user.findFirst({ where: { id, deletedAt: null, ...this.scope(actor) }, select: { id: true, tenantId: true, role: true, projectAccess: { select: { projectId: true } } } }); if (!user) throw new NotFoundException('用户不存在'); return user; }
  private async validateProjects(tenantId: string | null, projectIds: string[]) { if (!projectIds.length) return; if (!tenantId || await this.prisma.project.count({ where: { id: { in: projectIds }, tenantId, status: 'ACTIVE' } }) !== projectIds.length) throw new ForbiddenException('项目必须属于用户的数据空间'); }
  private assertAssignable(actor: AuthUser, role: UserRole) { if (!assignableRoles(actor.role).includes(role)) throw new ForbiddenException('不能授予同级或更高级角色'); }
  private assertManageable(actor: AuthUser, role: UserRole, id: string) { if (actor.id !== id && !isSuperAdmin(actor.role) && !assignableRoles(actor.role).includes(role)) throw new ForbiddenException('不能管理同级或更高级账号'); }
  private async assertOtherSuper(tx: Pick<PrismaService, 'user'>, id: string) { if (!await tx.user.count({ where: { id: { not: id }, role: { in: ['ADMIN', 'SUPER_ADMIN'] }, isActive: true, deletedAt: null } })) throw new ForbiddenException('必须保留至少一个启用的超级管理员'); }
  private async assignmentTenant(actor: AuthUser, role: UserRole, requested?: string) {
    if (isGlobalAdmin(role)) return null;
    if (!isGlobalAdmin(actor.role)) { if (!actor.tenantId) throw new BadRequestException('当前账号未分配数据空间'); if (requested && requested !== actor.tenantId) throw new ForbiddenException('不能分配其他数据空间'); return actor.tenantId; }
    if (!requested) throw new BadRequestException('请选择用户所属数据空间');
    const tenant = await this.prisma.tenant.findFirst({ where: { id: requested, status: 'ACTIVE' }, select: { id: true } });
    if (!tenant) throw new BadRequestException('数据空间不存在或已停用'); return tenant.id;
  }
}
const publicUser = { id: true, email: true, displayName: true, role: true, tenantId: true, projectAccess: { select: { projectId: true } }, isActive: true, mustChangePassword: true, lastLoginAt: true, createdAt: true, updatedAt: true } as const;
