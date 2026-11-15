import { AuthUser } from '../auth/auth.types';
import { TagsService } from './tags.service';
declare class CreateTagDto {
    name: string;
    color: string;
}
declare class UpdateTagDto {
    name?: string;
    color?: string;
}
declare class BulkTagDto {
    fileIds: string[];
    tagIds: string[];
}
export declare class TagsController {
    private readonly tags;
    constructor(tags: TagsService);
    list(user: AuthUser): import(".prisma/client").Prisma.PrismaPromise<({
        _count: {
            datasets: number;
            robotFiles: number;
        };
    } & {
        tenantId: string | null;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        color: string;
    })[]>;
    create(body: CreateTagDto, user: AuthUser): Promise<{
        tenantId: string | null;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        color: string;
    }>;
    update(id: string, body: UpdateTagDto, user: AuthUser): Promise<{
        tenantId: string | null;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        color: string;
    }>;
    remove(id: string, user: AuthUser): Promise<void>;
    bulk(body: BulkTagDto, user: AuthUser): Promise<{
        attached: number;
    }>;
    attachFile(fileId: string, tagId: string, user: AuthUser): Promise<{
        createdAt: Date;
        robotFileId: string;
        tagId: string;
    }>;
    detachFile(fileId: string, tagId: string, user: AuthUser): Promise<import(".prisma/client").Prisma.BatchPayload>;
    attachDataset(datasetId: string, tagId: string, user: AuthUser): Promise<{
        createdAt: Date;
        datasetId: string;
        tagId: string;
    }>;
    detachDataset(datasetId: string, tagId: string, user: AuthUser): Promise<import(".prisma/client").Prisma.BatchPayload>;
}
export {};
