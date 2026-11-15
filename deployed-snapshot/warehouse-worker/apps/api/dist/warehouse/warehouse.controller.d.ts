import { AuthUser } from '../auth/auth.types';
import { WarehouseQueryService } from './warehouse-query.service';
export declare class WarehouseController {
    private readonly warehouse;
    constructor(warehouse: WarehouseQueryService);
    projects(user: AuthUser): import(".prisma/client").Prisma.PrismaPromise<{
        tenant: {
            name: string;
        };
        tenantId: string;
        id: string;
        name: string;
    }[]>;
    sources(user: AuthUser): import(".prisma/client").Prisma.PrismaPromise<{
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        externalId: string | null;
    }[]>;
    overview(user: AuthUser): Promise<{
        summary: {
            dataset_count: number;
            version_count: number;
            file_count: number;
            managed_bytes: string;
            scope: string;
        };
        datasets: {
            id: string;
            name: string;
            createdAt: Date;
            updatedAt: Date;
            description: string | null;
            versions: {
                version: number;
                id: string;
                status: import(".prisma/client").$Enums.DatasetVersionStatus;
                updatedAt: Date;
                datasetId: string;
                fileCount: number;
                totalSize: bigint;
            }[];
        }[];
        uploads: {
            id: string;
            createdAt: Date;
            originalName: string;
            fileType: string;
            mimeType: string;
            fileSize: bigint;
            recognitionType: string;
            schemaVersion: string;
            capturedAt: Date;
        }[];
    }>;
}
