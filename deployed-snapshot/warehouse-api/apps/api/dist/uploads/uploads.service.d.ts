import { Prisma, UserRole } from '@prisma/client';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { SettingsService } from '../settings/settings.service';
import { ObjectStorage } from '../storage/object-storage';
import { AuthUser } from '../auth/auth.types';
export declare const UPLOAD_CHUNK_SIZE: number;
export declare const MAX_UPLOAD_PARTS = 10000;
type CreateUploadInput = {
    folderPath?: string;
    datasetId?: string;
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
};
export declare class UploadsService {
    private readonly prisma;
    private readonly jobs;
    private readonly settings;
    private readonly storage;
    defaults(user: AuthUser, datasetId?: string): Promise<{
        robotId: string;
        recognitionType: string;
        schemaVersion: string;
        capturedAtStrategy: "LAST_MODIFIED";
    }>;
    constructor(prisma: PrismaService, jobs: JobsService, settings: SettingsService, storage: ObjectStorage);
    create(owner: string | AuthUser, input: CreateUploadInput): Promise<{
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
        metadata: Prisma.JsonValue | null;
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
    get(id: string, userId: string, role: UserRole): Promise<{
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
        metadata: Prisma.JsonValue | null;
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
    putPart(id: string, partNumber: number, contentRange: string | undefined, partChecksum: string | undefined, content: Buffer, userId: string, role: UserRole): Promise<{
        id: string;
        createdAt: Date;
        objectKey: string;
        checksum: string;
        size: number;
        uploadSessionId: string;
        partNumber: number;
    }>;
    complete(id: string, userId: string, role: UserRole): Promise<({
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
            output: Prisma.JsonValue | null;
            jobId: string;
        }[];
    } & {
        projectId: string | null;
        tenantId: string | null;
        result: Prisma.JsonValue | null;
        id: string;
        status: import(".prisma/client").$Enums.JobStatus;
        createdAt: Date;
        updatedAt: Date;
        type: import(".prisma/client").$Enums.JobType;
        progress: number;
        resourceType: string | null;
        resourceId: string | null;
        deduplicationKey: string | null;
        payload: Prisma.JsonValue;
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
    abort(id: string, userId: string, role: UserRole): Promise<void>;
    finalize(id: string): Promise<{
        robotFileId: string;
        uploadSessionId: string;
        duplicate?: undefined;
        objectKey?: undefined;
    } | {
        robotFileId: string;
        uploadSessionId: string;
        duplicate: boolean;
        objectKey?: undefined;
    } | {
        robotFileId: string;
        uploadSessionId: string;
        objectKey: string;
        duplicate?: undefined;
    }>;
    private lockFinalObject;
    private validateFinalization;
    private removeUnindexedFinalObject;
    private datasetRobotId;
    private targetDataset;
    private attachToDraft;
    private inspectObject;
    private validateFormat;
    private cleanupParts;
    private find;
    private assertOwner;
    private assertWritable;
    private serialize;
}
export {};
