import { DatasetVersionStatus } from '@prisma/client';
export declare class ListVersionsQueryDto {
    robotId?: string;
    datasetId?: string;
    search?: string;
    status?: DatasetVersionStatus;
    sortBy: 'createdAt' | 'updatedAt' | 'version' | 'status';
    sortOrder: 'asc' | 'desc';
    page: number;
    pageSize: number;
}
