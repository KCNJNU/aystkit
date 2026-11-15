import { UserRole } from '@prisma/client';
import { AuthUser } from '../auth/auth.types';
import { PrismaService } from '../prisma/prisma.service';
declare class UserPageQuery {
    page: number;
    pageSize: number;
    search?: string;
    role?: UserRole;
}
declare class CreateUserDto {
    email: string;
    displayName: string;
    role: UserRole;
    tenantId?: string;
    projectIds?: string[];
    temporaryPassword: string;
}
declare class UpdateUserDto {
    email?: string;
    displayName?: string;
    isActive?: boolean;
    role?: UserRole;
    tenantId?: string;
    projectIds?: string[];
}
declare class ResetPasswordDto {
    temporaryPassword: string;
}
export declare class UsersController {
    private readonly prisma;
    constructor(prisma: PrismaService);
    scopeOptions(actor: AuthUser): Promise<{
        tenants: {
            id: string;
            code: string;
            name: string;
        }[];
        projects: {
            tenantId: string;
            id: string;
            name: string;
        }[];
        assignableRoles: import(".prisma/client").$Enums.UserRole[];
    }>;
    page(query: UserPageQuery, actor: AuthUser): Promise<{
        items: {
            tenantId: string | null;
            id: string;
            createdAt: Date;
            updatedAt: Date;
            role: import(".prisma/client").$Enums.UserRole;
            email: string;
            displayName: string;
            isActive: boolean;
            mustChangePassword: boolean;
            lastLoginAt: Date | null;
            projectAccess: {
                projectId: string;
            }[];
        }[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    create(body: CreateUserDto, actor: AuthUser): Promise<{
        tenantId: string | null;
        id: string;
        createdAt: Date;
        updatedAt: Date;
        role: import(".prisma/client").$Enums.UserRole;
        email: string;
        displayName: string;
        isActive: boolean;
        mustChangePassword: boolean;
        lastLoginAt: Date | null;
        projectAccess: {
            projectId: string;
        }[];
    }>;
    update(id: string, body: UpdateUserDto, actor: AuthUser): Promise<{
        tenantId: string | null;
        id: string;
        createdAt: Date;
        updatedAt: Date;
        role: import(".prisma/client").$Enums.UserRole;
        email: string;
        displayName: string;
        isActive: boolean;
        mustChangePassword: boolean;
        lastLoginAt: Date | null;
        projectAccess: {
            projectId: string;
        }[];
    }>;
    remove(id: string, actor: AuthUser): Promise<void>;
    reset(id: string, body: ResetPasswordDto, actor: AuthUser): Promise<{
        success: boolean;
    }>;
    revokeSessions(id: string, actor: AuthUser): Promise<void>;
    private scope;
    private visible;
    private validateProjects;
    private assertAssignable;
    private assertManageable;
    private assertOtherSuper;
    private assignmentTenant;
}
export {};
