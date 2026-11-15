import { UserRole } from '@prisma/client';
export declare const DATA_READ_ROLES: UserRole[];
export declare const canReadData: (role: UserRole) => boolean;
export declare const DataReader: () => import("@nestjs/common").CustomDecorator<string>;
export declare const isGlobalAdmin: (role: UserRole) => boolean;
export declare const isSuperAdmin: (role: UserRole) => role is "ADMIN" | "SUPER_ADMIN";
export declare function assignableRoles(role: UserRole): UserRole[];
