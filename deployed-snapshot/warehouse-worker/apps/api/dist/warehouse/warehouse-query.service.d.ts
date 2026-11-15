import { AuthUser } from '../auth/auth.types';
import { PrismaService } from '../prisma/prisma.service';
export type Source = {
    type: 'dataset' | 'file' | 'version' | 'summary' | 'system';
    id: string;
    name: string;
    version?: number;
};
export type QueryResult = {
    data: unknown;
    sources: Source[];
};
export declare const safeJson: (value: unknown) => string;
export declare function warehouseScope(user: AuthUser): {};
export declare class WarehouseQueryService {
    private readonly prisma;
    constructor(prisma: PrismaService);
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
    datasets(user: AuthUser, keyword?: string, limit?: number): Promise<QueryResult>;
    dataset(user: AuthUser, id: string): Promise<QueryResult>;
    versions(user: AuthUser, id: string, limit?: number): Promise<QueryResult>;
    files(user: AuthUser, keyword?: string, limit?: number): Promise<QueryResult>;
    file(user: AuthUser, id: string): Promise<QueryResult>;
    statistics(user: AuthUser): Promise<QueryResult>;
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
