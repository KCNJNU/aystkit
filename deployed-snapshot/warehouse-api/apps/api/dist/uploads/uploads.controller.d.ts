import { AuthUser } from '../auth/auth.types';
import { UploadsService } from './uploads.service';
declare class UploadDefaultsDto {
    datasetId: string;
}
export declare class CreateUploadDto {
    folderPath?: string;
    datasetId: string;
    robotId: string;
    originalName: string;
    mimeType: string;
    fileSize: number;
    checksum: string;
    recognitionType: string;
    schemaVersion: string;
    capturedAt: string;
    metadata?: Record<string, unknown>;
    lastModified?: number;
}
export declare class UploadsController {
    private readonly uploads;
    constructor(uploads: UploadsService);
    defaults(user: AuthUser, body: UploadDefaultsDto): Promise<{
        robotId: string;
        recognitionType: string;
        schemaVersion: string;
        capturedAtStrategy: "LAST_MODIFIED";
    }>;
    create(user: AuthUser, body: CreateUploadDto): Promise<{
        parts: {
            id: string;
            createdAt: Date;
            objectKey: string;
            checksum: string;
            size: number;
            uploadSessionId: string;
            partNumber: number;
        }[];
    } & {
        id: string;
        status: import(".prisma/client").$Enums.UploadStatus;
        createdAt: Date;
        updatedAt: Date;
        metadata: import("@prisma/client/runtime/library").JsonValue | null;
        datasetId: string | null;
        folderPath: string;
        robotId: string;
        originalName: string;
        fileType: string;
        mimeType: string;
        fileSize: bigint;
        checksum: string;
        recognitionType: string;
        schemaVersion: string;
        capturedAt: Date;
        errorMessage: string | null;
        expiresAt: Date;
        completedAt: Date | null;
        robotFileId: string | null;
        ownerId: string;
        chunkSize: number;
        totalParts: number;
        maxFileSize: bigint;
        tempPrefix: string;
        finalObjectKey: string | null;
    } & {
        fileSize: string;
        maxFileSize: string;
    }>;
    get(id: string, user: AuthUser): Promise<{
        parts: {
            id: string;
            createdAt: Date;
            objectKey: string;
            checksum: string;
            size: number;
            uploadSessionId: string;
            partNumber: number;
        }[];
    } & {
        id: string;
        status: import(".prisma/client").$Enums.UploadStatus;
        createdAt: Date;
        updatedAt: Date;
        metadata: import("@prisma/client/runtime/library").JsonValue | null;
        datasetId: string | null;
        folderPath: string;
        robotId: string;
        originalName: string;
        fileType: string;
        mimeType: string;
        fileSize: bigint;
        checksum: string;
        recognitionType: string;
        schemaVersion: string;
        capturedAt: Date;
        errorMessage: string | null;
        expiresAt: Date;
        completedAt: Date | null;
        robotFileId: string | null;
        ownerId: string;
        chunkSize: number;
        totalParts: number;
        maxFileSize: bigint;
        tempPrefix: string;
        finalObjectKey: string | null;
    } & {
        fileSize: string;
        maxFileSize: string;
    }>;
    putPart(id: string, part: number, range: string | undefined, checksum: string | undefined, content: Buffer, user: AuthUser): Promise<{
        id: string;
        createdAt: Date;
        objectKey: string;
        checksum: string;
        size: number;
        uploadSessionId: string;
        partNumber: number;
    }>;
    complete(id: string, user: AuthUser): Promise<({
        steps: {
            id: string;
            name: string;
            status: import(".prisma/client").$Enums.JobStepStatus;
            createdAt: Date;
            updatedAt: Date;
            progress: number;
            errorMessage: string | null;
            startedAt: Date | null;
            finishedAt: Date | null;
            stepOrder: number;
            output: import("@prisma/client/runtime/library").JsonValue | null;
            jobId: string;
        }[];
    } & {
        projectId: string | null;
        tenantId: string | null;
        result: import("@prisma/client/runtime/library").JsonValue | null;
        id: string;
        status: import(".prisma/client").$Enums.JobStatus;
        createdAt: Date;
        updatedAt: Date;
        type: import(".prisma/client").$Enums.JobType;
        progress: number;
        resourceType: string | null;
        resourceId: string | null;
        deduplicationKey: string | null;
        payload: import("@prisma/client/runtime/library").JsonValue;
        errorCode: string | null;
        errorMessage: string | null;
        attempts: number;
        maxAttempts: number;
        availableAt: Date;
        lockedAt: Date | null;
        lockedBy: string | null;
        heartbeatAt: Date | null;
        cancelRequested: boolean;
        startedAt: Date | null;
        finishedAt: Date | null;
        requestedById: string | null;
    }) | {
        completed: boolean;
        robotFileId: string;
    }>;
    abort(id: string, user: AuthUser): Promise<void>;
}
export {};
