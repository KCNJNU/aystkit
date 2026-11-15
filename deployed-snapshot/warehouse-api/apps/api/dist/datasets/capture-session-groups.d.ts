import type { RobotFile } from '@prisma/client';
export declare const CAPTURE_SESSION_ID: RegExp;
export declare const CAPTURE_ROBOT_ID: RegExp;
export type CaptureSessionScope = {
    robotId: string;
    sessionId: string;
};
export type CaptureSessionGroup = CaptureSessionScope & {
    startedAt: string;
    endAt: string;
    files: RobotFile[];
    sensors: Array<{
        name: string;
        fileCount: number;
        totalBytes: string;
    }>;
};
export declare function groupCaptureSessions(files: RobotFile[], proofs: unknown): {
    sessions: CaptureSessionGroup[];
    unassigned: {
        id: string;
        createdAt: Date;
        updatedAt: Date;
        metadata: import("@prisma/client/runtime/library").JsonValue | null;
        datasetId: string | null;
        folderPath: string;
        robotId: string;
        objectKey: string;
        originalName: string;
        fileType: string;
        mimeType: string;
        fileSize: bigint;
        checksum: string;
        recognitionType: string;
        schemaVersion: string;
        capturedAt: Date;
    }[];
    excludedDerivedFileCount: number;
};
