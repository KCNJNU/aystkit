export declare function datasetFolder(name: string): string;
export declare function datasetFileKey(name: string, uploadId: string, filename: string): string;
export declare function validateFolderPath(path: string): string;
export declare function validateEntryName(name: string): string;
export declare function uploadDestination(folderPath: string, originalName: string): {
    folderPath: string;
    name: string;
};
export declare function folderMarker(datasetName: string, path: string): string;
