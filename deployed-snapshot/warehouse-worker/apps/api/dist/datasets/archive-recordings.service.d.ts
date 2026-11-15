import { ConfigService } from '@nestjs/config';
import { Prisma } from '@prisma/client';
import { AuthUser } from '../auth/auth.types';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { ObjectStorage } from '../storage/object-storage';
export declare class ArchiveAssemblyCancelledError extends Error {
}
export declare class ArchiveAssemblyBusyError extends Error {
}
type JobContext = {
    id: string;
    payload: Prisma.JsonValue;
};
export declare class ArchiveRecordingsService {
    private readonly prisma;
    private readonly jobs;
    private readonly storage;
    private readonly config;
    private nextScanAt;
    private backfillCursor;
    private backfillDone;
    private liveCursor;
    private readonly pendingSensors;
    constructor(prisma: PrismaService, jobs: JobsService, storage: ObjectStorage, config: ConfigService);
    list(datasetId: string, path: string, user: AuthUser): Promise<{
        datasetId: string;
        path: string;
        recordings: {
            state: string;
            file: Prisma.InputJsonValue | null;
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
        }[];
    }>;
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
