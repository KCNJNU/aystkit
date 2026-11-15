import { UserRole } from '@prisma/client';
import { Roles } from './auth.decorators';

export const DATA_READ_ROLES: UserRole[] = [UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR, UserRole.VIEWER, UserRole.MAINTENANCE_ENGINEER];
export const canReadData = (role: UserRole) => DATA_READ_ROLES.includes(role);
export const DataReader = () => Roles(...DATA_READ_ROLES);
export const isGlobalAdmin = (role: UserRole) => ([UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN] as UserRole[]).includes(role);
export const isSuperAdmin = (role: UserRole) => role === UserRole.ADMIN || role === UserRole.SUPER_ADMIN;
export function assignableRoles(role: UserRole): UserRole[] {
  const scoped = [UserRole.PROJECT_ADMIN, UserRole.OPERATOR, UserRole.VIEWER, UserRole.BASIC];
  if (isSuperAdmin(role)) return [UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, ...scoped];
  if (role === UserRole.SYSTEM_ADMIN) return [UserRole.TENANT_ADMIN, ...scoped];
  return role === UserRole.TENANT_ADMIN ? scoped : [];
}
