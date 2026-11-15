export declare class ListFilesQueryDto {
    folderPath?: string;
    datasetId?: string;
    robotId?: string;
    fileType?: string;
    recognitionType?: string;
    schemaVersion?: string;
    search?: string;
    checksum?: string;
    tagId?: string;
    startTime?: string;
    endTime?: string;
    uploadedStartTime?: string;
    uploadedEndTime?: string;
    minSize?: number;
    maxSize?: number;
    sortBy: 'createdAt' | 'capturedAt' | 'fileSize' | 'originalName';
    sortOrder: 'asc' | 'desc';
    page: number;
    pageSize: number;
}
