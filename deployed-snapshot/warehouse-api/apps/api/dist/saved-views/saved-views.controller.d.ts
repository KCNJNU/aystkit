import { SavedViewResource } from '@prisma/client';
import { AuthUser } from '../auth/auth.types';
import { SavedViewsService } from './saved-views.service';
declare class CreateViewDto {
    resource: SavedViewResource;
    name: string;
    query: Record<string, unknown>;
    isShared?: boolean;
}
declare class UpdateViewDto {
    name?: string;
    query?: Record<string, unknown>;
    isShared?: boolean;
}
declare class CopyViewDto {
    name?: string;
}
export declare class SavedViewsController {
    private readonly views;
    constructor(views: SavedViewsService);
    list(user: AuthUser, resource?: SavedViewResource): import(".prisma/client").Prisma.PrismaPromise<({
        owner: {
            id: string;
            displayName: string;
        };
    } & {
        query: import("@prisma/client/runtime/library").JsonValue;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        ownerId: string;
        resource: import(".prisma/client").$Enums.SavedViewResource;
        isShared: boolean;
    })[]>;
    create(user: AuthUser, body: CreateViewDto): import(".prisma/client").Prisma.Prisma__SavedViewClient<{
        query: import("@prisma/client/runtime/library").JsonValue;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        ownerId: string;
        resource: import(".prisma/client").$Enums.SavedViewResource;
        isShared: boolean;
    }, never, import("@prisma/client/runtime/library").DefaultArgs, import(".prisma/client").Prisma.PrismaClientOptions>;
    update(id: string, user: AuthUser, body: UpdateViewDto): Promise<{
        query: import("@prisma/client/runtime/library").JsonValue;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        ownerId: string;
        resource: import(".prisma/client").$Enums.SavedViewResource;
        isShared: boolean;
    }>;
    remove(id: string, user: AuthUser): Promise<void>;
    copy(id: string, user: AuthUser, body: CopyViewDto): Promise<{
        query: import("@prisma/client/runtime/library").JsonValue;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        ownerId: string;
        resource: import(".prisma/client").$Enums.SavedViewResource;
        isShared: boolean;
    }>;
}
export {};
