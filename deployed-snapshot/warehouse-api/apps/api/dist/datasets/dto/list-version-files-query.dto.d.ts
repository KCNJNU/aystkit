export declare class ListVersionFilesQueryDto {
    search?: string;
    robotId?: string;
    recognitionType?: string;
    schemaVersion?: string;
    startTime?: string;
    endTime?: string;
    sortBy: 'createdAt' | 'capturedAt' | 'fileSize' | 'originalName';
    sortOrder: 'asc' | 'desc';
    page: number;
    pageSize: number;
}
