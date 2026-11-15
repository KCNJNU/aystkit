import { AuthUser } from '../auth/auth.types';
import { Prisma } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';
export declare class TagsService {
    private readonly prisma;
    constructor(prisma: PrismaService);
    list(user: AuthUser): Prisma.PrismaPromise<({
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
    create(name: string, color: string, user: AuthUser): Promise<{
        tenantId: string | null;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        color: string;
    }>;
    update(id: string, input: {
        name?: string;
        color?: string;
    }, user: AuthUser): Promise<{
        tenantId: string | null;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        color: string;
    }>;
    remove(id: string, user: AuthUser): Promise<void>;
    attachFile(fileId: string, tagId: string, user: AuthUser): Promise<{
        createdAt: Date;
        robotFileId: string;
        tagId: string;
    }>;
    detachFile(fileId: string, tagId: string, user: AuthUser): Promise<Prisma.BatchPayload>;
    attachDataset(datasetId: string, tagId: string, user: AuthUser): Promise<{
        createdAt: Date;
        datasetId: string;
        tagId: string;
    }>;
    detachDataset(datasetId: string, tagId: string, user: AuthUser): Promise<Prisma.BatchPayload>;
    bulkAttachFiles(fileIds: string[], tagIds: string[], user: AuthUser): Promise<{
        attached: number;
    }>;
    private tagScope;
    private checkTags;
    private checkFiles;
    private checkDataset;
    private assertColor;
    private throwConflict;
}
