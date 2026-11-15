import { AuthUser } from '../auth/auth.types';
import { AuditService } from './audit.service';
declare class AuditQuery {
    page: number;
    pageSize: number;
    search?: string;
    action?: string;
    actorUserId?: string;
}
export declare class AuditController {
    private readonly audit;
    constructor(audit: AuditService);
    page(query: AuditQuery, user: AuthUser): Promise<{
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
            metadata: import("@prisma/client/runtime/library").JsonValue | null;
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
            beforeValue: import("@prisma/client/runtime/library").JsonValue | null;
            afterValue: import("@prisma/client/runtime/library").JsonValue | null;
            actorUserId: string | null;
        })[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    export(query: AuditQuery, user: AuthUser): Promise<string>;
}
export {};
