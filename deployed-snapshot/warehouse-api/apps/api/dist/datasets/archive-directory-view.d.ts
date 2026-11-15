export declare function isArchiveTimeSlot(metadata: unknown, path: string): boolean;
export declare function archiveSegmentPaths(path: string, folders: ReadonlyArray<{
    name: string;
    path: string;
    parentPath: string;
}>): string[];
