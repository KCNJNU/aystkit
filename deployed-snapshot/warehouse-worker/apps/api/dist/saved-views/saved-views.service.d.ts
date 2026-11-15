import { Prisma, SavedViewResource, UserRole } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';
export declare class SavedViewsService {
    private readonly prisma;
    constructor(prisma: PrismaService);
    list(userId: string, resource?: SavedViewResource): Prisma.PrismaPromise<({
        owner: {
            id: string;
            displayName: string;
        };
    } & {
        query: Prisma.JsonValue;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        ownerId: string;
        resource: import(".prisma/client").$Enums.SavedViewResource;
        isShared: boolean;
    })[]>;
    create(userId: string, role: UserRole, input: {
        resource: SavedViewResource;
        name: string;
        query: unknown;
        isShared?: boolean;
    }): Prisma.Prisma__SavedViewClient<{
        query: Prisma.JsonValue;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        ownerId: string;
        resource: import(".prisma/client").$Enums.SavedViewResource;
        isShared: boolean;
    }, never, import("@prisma/client/runtime/library").DefaultArgs, Prisma.PrismaClientOptions>;
    update(id: string, userId: string, role: UserRole, input: {
        name?: string;
        query?: unknown;
        isShared?: boolean;
    }): Promise<{
        query: Prisma.JsonValue;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        ownerId: string;
        resource: import(".prisma/client").$Enums.SavedViewResource;
        isShared: boolean;
    }>;
    remove(id: string, userId: string): Promise<void>;
    copy(id: string, userId: string, name?: string): Promise<{
        query: Prisma.JsonValue;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        ownerId: string;
        resource: import(".prisma/client").$Enums.SavedViewResource;
        isShared: boolean;
    }>;
    private validate;
    private assertShare;
    private getOwned;
}
