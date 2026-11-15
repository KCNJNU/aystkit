import { ConfigService } from '@nestjs/config';
import { BadRequestException, ConflictException, Injectable, NotFoundException, UnauthorizedException } from '@nestjs/common';
import { AuthUser } from '../auth/auth.types';
import { isGlobalAdmin } from '../auth/permissions';
import { PrismaService } from '../prisma/prisma.service';

const datasetSelect = { id: true, name: true, description: true, updatedAt: true, createdAt: true } as const;
const versionSelect = { id: true, datasetId: true, version: true, status: true, fileCount: true, totalSize: true, updatedAt: true } as const;
const fileSelect = { id: true, originalName: true, fileType: true, mimeType: true, fileSize: true, recognitionType: true, schemaVersion: true, capturedAt: true, createdAt: true } as const;
export type Source = { type: 'dataset' | 'file' | 'version' | 'summary' | 'system'; id: string; name: string; version?: number };
export type QueryResult = { data: unknown; sources: Source[] };
export const safeJson = (value: unknown): string => JSON.stringify(value, (_, item) => typeof item === 'bigint' ? item.toString() : item);

export function warehouseScope(user: AuthUser) {
  if (!user?.id) throw new UnauthorizedException();
  if (isGlobalAdmin(user.role)) return {};
  return {
    tenantId: user.tenantId ?? '00000000-0000-0000-0000-000000000000',
    ...(user.role === 'TENANT_ADMIN' ? {} : { projectId: { in: user.projectIds ?? [] } }),
  };
}

@Injectable()
export class WarehouseQueryService {
  constructor(private readonly prisma: PrismaService, private readonly config: ConfigService = new ConfigService()) {}

  projects(user: AuthUser) {
    warehouseScope(user);
    return this.prisma.project.findMany({
      where: {
        status: 'ACTIVE',
        ...(isGlobalAdmin(user.role) ? {} : {
          tenantId: user.tenantId ?? '00000000-0000-0000-0000-000000000000',
          ...(user.role === 'TENANT_ADMIN' ? {} : { id: { in: user.projectIds ?? [] } }),
        }),
      },
      select: { id: true, name: true, tenantId: true, tenant: { select: { name: true } } },
      take: 200,
      orderBy: { name: 'asc' },
    });
  }

  async registerRobot(input: { id: string; name: string; externalId: string; projectId: string }, user: AuthUser) {
    warehouseScope(user);
    if (!isGlobalAdmin(user.role)) throw new UnauthorizedException();
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(input.externalId) || !input.name.trim())
      throw new BadRequestException('机器人编号或名称无效');
    const project = await this.prisma.project.findFirst({ where: { id: input.projectId, status: 'ACTIVE' }, select: { id: true, tenantId: true } });
    if (!project) throw new NotFoundException('项目不存在');
    const existing = await this.prisma.robot.findFirst({ where: { OR: [{ id: input.id }, { externalId: input.externalId }] },
      select: { id: true, externalId: true, projectId: true, name: true, onlineStatus: true, lifecycleStatus: true, metadata: true } });
    if (existing) {
      const metadata = existing.metadata as { type?: string } | null;
      const legacy = (this.config.get<string>('WAREHOUSE_ROBOT_IDS') ?? '').split(',').map(id => id.trim());
      if (existing.id === input.id && existing.externalId === input.externalId && existing.projectId === project.id
          && existing.lifecycleStatus === 'ACTIVE' && (metadata?.type === 'fleet-robot' || legacy.includes(existing.id)))
        return { id: existing.id, name: existing.name, externalId: existing.externalId, onlineStatus: existing.onlineStatus };
      throw new ConflictException('机器人身份已绑定到其他记录');
    }
    return this.prisma.robot.create({ data: { id: input.id, name: input.name.trim(), externalId: input.externalId, projectId: project.id, tenantId: project.tenantId, metadata: { type: 'fleet-robot', observerId: input.externalId } },
      select: { id: true, name: true, externalId: true, onlineStatus: true } });
  }

  async robots(user: AuthUser) {
    const ids = (this.config.get<string>('WAREHOUSE_ROBOT_IDS') ?? '').split(',').map(id => id.trim()).filter(Boolean);
    const robots = await this.prisma.robot.findMany({
      where: { ...warehouseScope(user), lifecycleStatus: 'ACTIVE', OR: [
        { metadata: { path: ['type'], equals: 'fleet-robot' } },
        ...(ids.length ? [{ id: { in: ids } }] : []),
      ] },
      select: { id: true, name: true, externalId: true, projectId: true, onlineStatus: true, metadata: true },
      take: 100, orderBy: { name: 'asc' },
    });
    return robots.map(({ metadata, ...robot }) => {
      const profile = metadata as { type?: string; observerId?: unknown } | null;
      const observerId = profile?.type === 'fleet-robot' && typeof profile.observerId === 'string'
        && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(profile.observerId) ? profile.observerId : undefined;
      return { ...robot, ...(observerId ? { observerId } : {}) };
    });
  }

  sources(user: AuthUser) {
    return this.prisma.robot.findMany({
      where: warehouseScope(user),
      select: { id: true, name: true, externalId: true, createdAt: true, updatedAt: true },
      take: 200,
      orderBy: { createdAt: 'desc' },
    });
  }

  async datasets(user: AuthUser, keyword = '', limit = 10): Promise<QueryResult> {
    const items = await this.prisma.dataset.findMany({ where: { deletingAt: null, ...warehouseScope(user), ...(keyword ? { OR: [{ name: { contains: keyword, mode: 'insensitive' } }, { description: { contains: keyword, mode: 'insensitive' } }] } : {}) }, select: { ...datasetSelect, versions: { select: versionSelect, orderBy: { version: 'desc' }, take: 1 } }, take: Math.min(limit, 20), orderBy: { updatedAt: 'desc' } });
    return { data: items.map(item => ({ ...item, name: item.name.slice(0, 300), description: item.description?.slice(0, 1000) })), sources: items.map(item => ({ type: 'dataset', id: item.id, name: item.name.slice(0, 300), version: item.versions[0]?.version })) };
  }
  async dataset(user: AuthUser, id: string): Promise<QueryResult> {
    const item = await this.prisma.dataset.findFirst({ where: { id, deletingAt: null, ...warehouseScope(user) }, select: datasetSelect });
    if (!item) throw new NotFoundException('未找到可访问的数据集');
    return { data: { ...item, name: item.name.slice(0, 300), description: item.description?.slice(0, 1000) }, sources: [{ type: 'dataset', id: item.id, name: item.name.slice(0, 300) }] };
  }
  async versions(user: AuthUser, id: string, limit = 10): Promise<QueryResult> {
    await this.dataset(user, id);
    const items = await this.prisma.datasetVersion.findMany({ where: { datasetId: id, dataset: { deletingAt: null, ...warehouseScope(user) } }, select: versionSelect, take: Math.min(limit, 20), orderBy: { version: 'desc' } });
    return { data: items, sources: items.map(item => ({ type: 'version', id: item.id, name: `版本 ${item.version}`, version: item.version })) };
  }
  async files(user: AuthUser, keyword = '', limit = 10): Promise<QueryResult> {
    const items = await this.prisma.robotFile.findMany({ where: { robot: warehouseScope(user), ...(keyword ? { originalName: { contains: keyword, mode: 'insensitive' } } : {}) }, select: fileSelect, take: Math.min(limit, 20), orderBy: { createdAt: 'desc' } });
    return { data: items.map(item => ({ ...item, originalName: item.originalName.slice(0, 300) })), sources: items.map(item => ({ type: 'file', id: item.id, name: item.originalName.slice(0, 300) })) };
  }
  async file(user: AuthUser, id: string): Promise<QueryResult> {
    const item = await this.prisma.robotFile.findFirst({ where: { id, robot: warehouseScope(user) }, select: fileSelect });
    if (!item) throw new NotFoundException('未找到可访问的文件');
    return { data: { ...item, originalName: item.originalName.slice(0, 300) }, sources: [{ type: 'file', id: item.id, name: item.originalName.slice(0, 300) }] };
  }
  async statistics(user: AuthUser): Promise<QueryResult> {
    const scope = warehouseScope(user);
    const [dataset_count, version_count, file_count, size] = await Promise.all([
      this.prisma.dataset.count({ where: { deletingAt: null, ...scope } }), this.prisma.datasetVersion.count({ where: { dataset: { deletingAt: null, ...scope } } }),
      this.prisma.robotFile.count({ where: { robot: scope } }), this.prisma.robotFile.aggregate({ where: { robot: scope }, _sum: { fileSize: true } }),
    ]);
    return { data: { dataset_count, version_count, file_count, managed_bytes: (size._sum.fileSize ?? 0n).toString(), scope: '当前用户可访问的数据' }, sources: [{ type: 'summary', id: 'warehouse', name: '当前用户可访问的数仓统计' }] };
  }

  async overview(user: AuthUser) {
    const scope = warehouseScope(user);
    const fileScope = { robot: scope };
    const datasetScope = { deletingAt: null, ...scope };
    const [dataset_count, version_count, file_count, size, datasets, uploads] = await Promise.all([
      this.prisma.dataset.count({ where: datasetScope }),
      this.prisma.datasetVersion.count({ where: { dataset: datasetScope } }),
      this.prisma.robotFile.count({ where: fileScope }),
      this.prisma.robotFile.aggregate({ where: fileScope, _sum: { fileSize: true } }),
      this.prisma.dataset.findMany({
        where: datasetScope,
        select: { id: true, name: true, description: true, updatedAt: true, createdAt: true, versions: { select: { id: true, datasetId: true, version: true, status: true, fileCount: true, totalSize: true, updatedAt: true }, orderBy: { version: 'desc' }, take: 1 } },
        take: 10,
        orderBy: { updatedAt: 'desc' },
      }),
      this.prisma.robotFile.findMany({
        where: fileScope,
        select: { id: true, originalName: true, fileType: true, mimeType: true, fileSize: true, recognitionType: true, schemaVersion: true, capturedAt: true, createdAt: true },
        take: 10,
        orderBy: { createdAt: 'desc' },
      }),
    ]);
    return {
      summary: { dataset_count, version_count, file_count, managed_bytes: (size._sum.fileSize ?? 0n).toString(), scope: '当前用户可访问的数据' },
      datasets,
      uploads,
    };
  }
}
