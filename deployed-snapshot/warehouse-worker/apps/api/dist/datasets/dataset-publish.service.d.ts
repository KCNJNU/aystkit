import { DvcService } from '../dvc/dvc.service';
import { PrismaService } from '../prisma/prisma.service';
import { ObjectStorage } from '../storage/object-storage';
export declare class DatasetPublishService {
    private readonly prisma;
    private readonly storage;
    private readonly dvc;
    constructor(prisma: PrismaService, storage: ObjectStorage, dvc: DvcService);
    publish(id: string, runStep?: <T>(name: string, action: () => Promise<T>) => Promise<T>, signal?: AbortSignal): Promise<{
        version: number;
        id: string;
        status: import(".prisma/client").$Enums.DatasetVersionStatus;
        createdAt: Date;
        updatedAt: Date;
        datasetId: string;
        manifestKey: string | null;
        manifestChecksum: string | null;
        fileCount: number;
        totalSize: bigint;
        gitCommit: string | null;
        gitTag: string | null;
        dvcPath: string | null;
        dvcHash: string | null;
        frozenAt: Date | null;
    }>;
    recoveryVerify(id: string, runStep?: <T>(name: string, action: () => Promise<T>) => Promise<T>, signal?: AbortSignal): Promise<{
        verified: boolean;
        gitTag: string;
        dvcPath: string;
    }>;
    private readObject;
    private requireRemoteReachable;
    private workspaceName;
}
