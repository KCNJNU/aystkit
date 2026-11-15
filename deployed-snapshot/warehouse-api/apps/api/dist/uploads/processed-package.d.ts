import type { ObjectStorage } from '../storage/object-storage';
export type ProcessedPackageSummary = {
    beforeImages: number;
    afterImages: number;
    jsonFiles: number;
};
export declare function isProcessedFolder(folderPath: string): boolean;
export declare function assertProcessedPackageName(folderPath: string, name: string): void;
export declare function validateProcessedPackage(storage: ObjectStorage, objectKey: string, compressedBytes: number): Promise<ProcessedPackageSummary>;
