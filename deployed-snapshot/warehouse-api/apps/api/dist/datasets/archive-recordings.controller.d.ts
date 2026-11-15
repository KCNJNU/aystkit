import type { Response } from 'express';
import { AuthUser } from '../auth/auth.types';
import { ArchiveRecordingsService } from './archive-recordings.service';
import { PreparedFolderPackagesService } from './prepared-folder-packages.service';
declare class EndRecordingSessionDto {
    robotId: string;
    startedAt: string;
    endedAt: string;
    reason: 'desktop_stopped' | 'recovered_interruption';
    captureIncomplete: boolean;
    segmentIds?: string[];
    recoveryEvidenceSha256?: string;
}
export declare class ArchiveRecordingsController {
    private readonly recordings;
    private readonly prepared;
    constructor(recordings: ArchiveRecordingsService, prepared: PreparedFolderPackagesService);
    captureSessions(id: string, robotId: string | undefined, user: AuthUser): Promise<{
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
    captureSessionPackage(id: string, sessionId: string, robotId: string, user: AuthUser, background: string | undefined, retry: string | undefined, taskId: string | undefined, response: Response): Promise<{
        url: string;
        fileName: string;
        state?: undefined;
        taskId?: undefined;
    } | {
        state: "processing";
        taskId: string;
        retryAfterMs: number;
    } | {
        state: "ready";
        taskId: string;
        url: string;
        fileName: string;
    }>;
    preparedFolderPackages(id: string, user: AuthUser): Promise<{
        items: {
            id: string;
            path: string;
            taskId: string;
            state: "READY" | "QUEUED" | "FAILED" | "PROCESSING";
            fileName: string;
            readyAt: Date | null;
            errorMessage: string | null;
            createdAt: Date;
            updatedAt: Date;
        }[];
        total: number;
        ready: number;
        active: number;
    }>;
    prepareFolderPackages(id: string, user: AuthUser): Promise<{
        items: {
            id: string;
            path: string;
            taskId: string;
            state: "READY" | "QUEUED" | "FAILED" | "PROCESSING";
            fileName: string;
            readyAt: Date | null;
            errorMessage: string | null;
            createdAt: Date;
            updatedAt: Date;
        }[];
        total: number;
        ready: number;
        active: number;
    }>;
    folderPackage(id: string, path: string, user: AuthUser, background: string | undefined, retry: string | undefined, taskId: string | undefined, response: Response): Promise<{
        url: string;
        fileName: string;
        state?: undefined;
        taskId?: undefined;
    } | {
        state: "processing";
        taskId: string;
        retryAfterMs: number;
    } | {
        state: "ready";
        taskId: string;
        url: string;
        fileName: string;
    }>;
    endSession(id: string, sessionId: string, body: EndRecordingSessionDto, user: AuthUser): Promise<{
        sessionId: string;
        closed: boolean;
        captureIncomplete: boolean;
        idempotent: boolean;
    }>;
    list(id: string, path: string | undefined, user: AuthUser): Promise<{
        datasetId: string;
        path: string;
        recordings: {
            state: string;
            file: import("@prisma/client/runtime/library").InputJsonValue | null;
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
    package(id: string, recordingId: string, user: AuthUser): Promise<{
        url: string;
        fileName: string;
    }>;
    packages(id: string, path: string | undefined, user: AuthUser): Promise<{
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
    assemble(id: string, recordingId: string, user: AuthUser): Promise<{
        jobId: string | null;
    }>;
}
export {};
