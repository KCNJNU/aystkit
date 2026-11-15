import { Prisma } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';
import { AuthUser } from '../auth/auth.types';
export declare class AuditService {
    private readonly prisma;
    constructor(prisma: PrismaService);
    record(input: {
        actorUserId?: string;
        action: string;
        method?: string;
        route?: string;
        resourceType?: string;
        resourceId?: string;
        statusCode?: number;
        requestId?: string;
        ipAddress?: string;
        userAgent?: string;
        durationMs?: number;
        metadata?: Prisma.InputJsonValue;
    }): Prisma.Prisma__AuditLogClient<{
        projectId: string | null;
        tenantId: string | null;
        id: string;
        createdAt: Date;
        metadata: Prisma.JsonValue | null;
        method: string | null;
        resourceType: string | null;
        resourceId: string | null;
        durationMs: number | null;
        action: string;
        route: string | null;
        statusCode: number | null;
        requestId: string | null;
        ipAddress: string | null;
        userAgent: string | null;
        beforeValue: Prisma.JsonValue | null;
        afterValue: Prisma.JsonValue | null;
        actorUserId: string | null;
    }, never, import("@prisma/client/runtime/library").DefaultArgs, Prisma.PrismaClientOptions>;
    page(query: {
        page: number;
        pageSize: number;
        search?: string;
        action?: string;
        actorUserId?: string;
    }, scope?: AuthUser): Promise<{
        items: ({
            actor: {
                id: string;
                role: import(".prisma/client").$Enums.UserRole;
                email: string;
                displayName: string;
            } | null;
        } & {
            projectId: string | null;
            tenantId: string | null;
            id: string;
            createdAt: Date;
            metadata: Prisma.JsonValue | null;
            method: string | null;
            resourceType: string | null;
            resourceId: string | null;
            durationMs: number | null;
            action: string;
            route: string | null;
            statusCode: number | null;
            requestId: string | null;
            ipAddress: string | null;
            userAgent: string | null;
            beforeValue: Prisma.JsonValue | null;
            afterValue: Prisma.JsonValue | null;
            actorUserId: string | null;
        })[];
        total: number;
        page: number;
        pageSize: number;
    }>;
}
