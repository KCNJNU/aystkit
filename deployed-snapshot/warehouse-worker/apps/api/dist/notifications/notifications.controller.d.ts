import { AuthUser } from '../auth/auth.types';
import { PrismaService } from '../prisma/prisma.service';
declare class NotificationQuery {
    page: number;
    pageSize: number;
    unreadOnly?: boolean;
}
export declare class NotificationsController {
    private readonly prisma;
    constructor(prisma: PrismaService);
    list(query: NotificationQuery, user: AuthUser): Promise<{
        items: {
            id: string;
            createdAt: Date;
            message: string;
            type: string;
            jobId: string | null;
            title: string;
            resourceUrl: string | null;
            readAt: Date | null;
            userId: string;
        }[];
        total: number;
        unread: number;
        page: number;
        pageSize: number;
    }>;
    read(id: string, user: AuthUser): Promise<{
        success: boolean;
    }>;
    readAll(user: AuthUser): Promise<{
        success: boolean;
    }>;
}
export {};
