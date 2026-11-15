import { ConfigService } from '@nestjs/config';
import { Prisma } from '@prisma/client';
import { AuthUser } from '../auth/auth.types';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { ObjectStorage } from '../storage/object-storage';
import { CaptureSessionScope } from './capture-session-groups';
export declare class ArchiveAssemblyCancelledError extends Error {
}
export declare class ArchiveAssemblyBusyError extends Error {
}
export type PreparedFolderSnapshot = {
    schemaVersion: 1;
    datasetId: string;
    path: string;
    datasetName: string;
    at: string;
    files: Array<{
        id: string;
        hash: string;
    }>;
    folders: string[];
    objectKey: string;
    taskId: string;
    fileName: string;
};
type FolderPackageControl = {
    check: (force?: boolean) => Promise<void>;
    signal: AbortSignal;
};
type FolderPackageResponse = {
    url: string;
    fileName: string;
    state?: undefined;
    taskId?: undefined;
} | {
    state: 'processing';
    taskId: string;
    retryAfterMs: number;
} | {
    state: 'ready';
    taskId: string;
    url: string;
    fileName: string;
};
type JobContext = {
    id: string;
    payload: Prisma.JsonValue;
};
type SessionEnd = {
    robotId: string;
    startedAt: string;
    endedAt: string;
    reason: 'desktop_stopped' | 'recovered_interruption';
    captureIncomplete: boolean;
    segmentIds?: string[];
    recoveryEvidenceSha256?: string;
};
export declare class ArchiveRecordingsService {
    private readonly prisma;
    private readonly jobs;
    private readonly storage;
    private readonly config;
    private packaging;
    private readonly packageWaiters;
    private readonly folderPackageTasks;
    private nextScanAt;
    private backfillCursor;
    private backfillDone;
    private liveCursor;
    private readonly pendingSensors;
    private closureCursor;
    constructor(prisma: PrismaService, jobs: JobsService, storage: ObjectStorage, config: ConfigService);
    endSession(datasetId: string, sessionId: string, body: SessionEnd, user: AuthUser): Promise<{
        sessionId: string;
        closed: boolean;
        captureIncomplete: boolean;
        idempotent: boolean;
    }>;
    list(datasetId: string, path: string, user: AuthUser): Promise<{
        datasetId: string;
        path: string;
        recordings: {
            state: string;
            file: Prisma.InputJsonValue | null;
            downloadableVideos: {
                id: string;
                originalName: string;
                fileSize: string;
                startedAt: string;
                endAt: string;
            }[];
            jobId: string | null;
            error: string | null;
            id: string;
            datasetId: string;
            path: string;
            sensor: string;
            robotId: string;
            startedAt: string;
            endAt: string;
            partCount: number;
            sourceBytes: string;
            frameCount: number;
            durationSeconds: number;
            closed: boolean;
            gapBeforeSeconds: number | null;
            captureIncomplete: boolean;
            closureSessionId: string | null;
            closureReason: "desktop_stopped" | "recovered_interruption" | null;
            captureSessionId: string | null;
            controlSession: {
                startedAt: string;
                endedAt: string;
                nativeTimeOutsideSession: boolean;
            } | null;
        }[];
    }>;
    listPackages(datasetId: string, path: string, user: AuthUser): Promise<{
        datasetId: string;
        path: string;
        packages: {
            id: string;
            sensor: string;
            startedAt: string;
            endAt: string;
            bytes: string;
            fileCount: number;
            state: "ready";
        }[];
    }>;
    captureSessions(datasetId: string, robotId: string | undefined, user: AuthUser): Promise<{
        datasetId: string;
        robotId: string | null;
        snapshotAt: string;
        sessions: {
            fileCount: number;
            totalBytes: string;
            sensorCount: number;
            sensors: {
                name: string;
                fileCount: number;
                totalBytes: string;
            }[];
            snapshot: true;
            robotId: string;
            sessionId: string;
            startedAt: string;
            endAt: string;
        }[];
        unassigned: {
            fileCount: number;
            totalBytes: string;
        };
        excludedDerivedFileCount: number;
    }>;
    captureSessionPackage(datasetId: string, sessionId: string, robotId: string, user: AuthUser, options?: {
        background?: boolean;
        retry?: boolean;
        taskId?: string;
    }): Promise<FolderPackageResponse>;
    private captureSessionInventory;
    private captureSessionSnapshot;
    private packageScopeKey;
    capturePreparedFolderSnapshot(datasetId: string, path: string, user: AuthUser): Promise<PreparedFolderSnapshot>;
    validatePreparedFolderPackage(datasetId: string, path: string, user: AuthUser, saved: Prisma.JsonValue): Promise<void>;
    runPreparedFolderPackage(datasetId: string, path: string, user: AuthUser, saved: Prisma.JsonValue, control: FolderPackageControl): Promise<void>;
    folderPackage(datasetId: string, path: string, user: AuthUser, options?: {
        background?: boolean;
        retry?: boolean;
        taskId?: string;
        preparedSnapshot?: Prisma.JsonValue;
        control?: FolderPackageControl;
        wait?: boolean;
        captureSession?: CaptureSessionScope;
    }): Promise<FolderPackageResponse>;
    private folderPackageDescriptor;
    private restorePreparedFolderSnapshot;
    private folderPackageFileHash;
    private folderPackageTaskResult;
    private cleanupFolderPackageCache;
    private folderPackageSnapshot;
    private verifyFolderPackageSnapshot;
    private folderPackageFileIdentity;
    package(datasetId: string, recordingId: string, user: AuthUser): Promise<{
        url: string;
        fileName: string;
    }>;
    private compactPackageGroups;
    private createCompactPackage;
    private withPackageSlot;
    private packageQueueTimeout;
    private releasePackageSlot;
    private packageScratch;
    private publishPackage;
    private packageTime;
    private createTarZstd;
    requestAssembly(datasetId: string, recordingId: string, user: AuthUser): Promise<{
        jobId: string | null;
    }>;
    private dataset;
    private sensorPath;
    private sensorPaths;
    private groups;
    private enqueue;
    private resetMissingExport;
    scanClosedRecordings(owner: string): Promise<void>;
    assemble(job: JobContext, workerId: string): Promise<{
        fileId: string;
        recordingId: string;
        frameCount?: undefined;
        bytes?: undefined;
    } | {
        fileId: string;
        recordingId: string;
        frameCount: number;
        bytes: string;
    }>;
    private videoName;
    private validateResult;
    private downloadSource;
    private hashStream;
    private hashFile;
    private guardStream;
    protected runHelper(input: string, output: string, result: string, check: () => Promise<void>): Promise<void>;
}
export {};
