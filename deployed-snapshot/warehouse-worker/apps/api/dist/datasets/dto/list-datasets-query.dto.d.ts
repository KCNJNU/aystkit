import { DatasetVersionStatus } from '@prisma/client';
export declare class ListDatasetsQueryDto {
    search?: string;
    status?: DatasetVersionStatus;
    tagId?: string;
    sortBy: 'createdAt' | 'updatedAt' | 'name';
    sortOrder: 'asc' | 'desc';
    page: number;
    pageSize: number;
}
