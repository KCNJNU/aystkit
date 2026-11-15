import { AuthUser } from '../auth/auth.types';
import { PrismaService } from '../prisma/prisma.service';
import { ObjectStorage } from '../storage/object-storage';
import { FilesService } from './files.service';
import { RecycleQueryDto, RelocateFileDto } from './dto/file-lifecycle.dto';
export declare class FileLifecycleService {
    private readonly prisma;
    private readonly storage;
    private readonly files;
    constructor(prisma: PrismaService, storage: ObjectStorage, files: FilesService);
    private scope;
    private file;
    private lockDataset;
    impact(id: string, user: AuthUser): Promise<{
        objectKey: string;
        folderPath: string;
        datasetId: string | null;
        datasetName: string | null;
        fileSize: string;
        canRecycle: boolean;
        blockers: string[];
        canRelocate: boolean;
        recycleReviewDays: number;
        fileId: string;
        originalName: string;
        total: number;
        restrictedCount: number;
        references: {
            versionId: string;
            datasetId: string;
            datasetName: string;
            version: number;
            status: import(".prisma/client").$Enums.DatasetVersionStatus;
            referencedAt: Date;
            canDetach: boolean;
            reason: string | null;
        }[];
    }>;
    recycle(id: string, user: AuthUser): Promise<void>;
    recycleList(query: RecycleQueryDto, user: AuthUser): Promise<{
        items: {
            fileSize: string;
            id: string;
            datasetId: string | null;
            folderPath: string;
            objectKey: string;
            originalName: string;
            deletedAt: Date;
            reviewAt: Date;
            purgingAt: Date | null;
        }[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    restore(id: string, user: AuthUser): Promise<{
        id: string;
        restored: boolean;
    }>;
    purge(id: string, user: AuthUser): Promise<{
        id: string;
        purged: boolean;
    }>;
    relocate(id: string, input: RelocateFileDto, user: AuthUser): Promise<{
        cleanupPending: boolean;
        id: string;
        objectKey: string;
    }>;
}
