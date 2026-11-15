import type { UserRole } from '../services/api';

export const roleNames: Record<UserRole, string> = {
  ADMIN: '超级管理员', SUPER_ADMIN: '超级管理员', SYSTEM_ADMIN: '系统管理员',
  TENANT_ADMIN: '数据空间管理员', PROJECT_ADMIN: '项目管理员', OPERATOR: '文件操作员',
  VIEWER: '文件浏览员', BASIC: '普通用户', MAINTENANCE_ENGINEER: '维护工程师（兼容角色）',
};
export const roleDescriptions: Record<UserRole, string> = {
  ADMIN: '全部权限，可管理所有角色与全平台数据', SUPER_ADMIN: '全部权限，可管理所有角色与全平台数据',
  SYSTEM_ADMIN: '一级及以下权限：管理下级用户、存储维护、系统设置；不能管理同级和超级管理员',
  TENANT_ADMIN: '本数据空间内的用户管理，以及二级、三级和基础权限；无全局存储维护与系统设置权限',
  PROJECT_ADMIN: '授权项目内的二级、三级和基础权限，另可冻结、发布与恢复数据版本', OPERATOR: '授权项目内的文件增删改、浏览下载，以及基础权限',
  VIEWER: '授权项目内的文件浏览、下载，以及基础权限', BASIC: 'AI 对话、系统状态、个人账号与通知；不能查询文件和数据集',
  MAINTENANCE_ENGINEER: '兼容已有账号，数仓内按三级及基础权限处理',
};
export const readRoles: UserRole[] = ['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR', 'VIEWER', 'MAINTENANCE_ENGINEER'];
export const writeRoles: UserRole[] = ['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR'];
export const globalRoles: UserRole[] = ['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN'];
export function assignableRoles(role?: UserRole): UserRole[] {
  const scoped: UserRole[] = ['PROJECT_ADMIN', 'OPERATOR', 'VIEWER', 'BASIC'];
  if (role === 'SUPER_ADMIN' || role === 'ADMIN') return ['SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', ...scoped];
  if (role === 'SYSTEM_ADMIN') return ['TENANT_ADMIN', ...scoped];
  return role === 'TENANT_ADMIN' ? scoped : [];
}

export const auditRoles: UserRole[] = ['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN'];
export function defaultWarehousePath(role?: UserRole): string {
  return role && readRoles.includes(role) ? '/datasets' : '/ai';
}
