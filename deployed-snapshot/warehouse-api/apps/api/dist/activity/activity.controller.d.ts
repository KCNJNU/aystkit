import { AuthUser } from '../auth/auth.types';
import { PrismaService } from '../prisma/prisma.service';
declare class ActivityQuery {
    page: number;
    pageSize: number;
    search?: string;
}
export declare class ActivityController {
    private readonly prisma;
    constructor(prisma: PrismaService);
    page(query: ActivityQuery, actor: AuthUser): Promise<{
        items: {
            id: string;
            source: string;
            type: string;
            status: string;
            actor: {
                id: string;
                email: string;
                displayName: string;
            } | null;
            resourceType: string | null;
            resourceId: string | null;
            timestamp: Date;
        }[];
        total: number;
        page: number;
        pageSize: number;
    }>;
}
export {};
