import { Prisma, UserRole } from '@prisma/client';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { ObjectStorage } from './object-storage';
export declare class StorageOperationsService {
    private readonly prisma;
    private readonly jobs;
    private readonly storage;
    constructor(prisma: PrismaService, jobs: JobsService, storage: ObjectStorage);
    executeDatasetDelete(datasetId: string, objectKeys: string[]): Promise<{
        datasetId: string;
        deleted: boolean;
        objectCount?: undefined;
    } | {
        datasetId: string;
        deleted: boolean;
        objectCount: number;
    }>;
    pageObjects(prefix: string | undefined, cursor: string | undefined, limit: number): Promise<{
        items: {
            mapping: {
                id?: string | undefined;
                objectKey?: string | undefined;
                originalName?: string | undefined;
                type: string;
            } | {
                dataset?: {
                    id: string;
                    name: string;
                } | undefined;
                version?: number | undefined;
                id?: string | undefined;
                manifestKey?: string | null | undefined;
                type: string;
            } | null;
            objectKey: string;
            size: number;
            etag?: string;
            lastModified?: Date;
        }[];
        nextCursor?: string;
    }>;
    enqueueScan(userId: string): Promise<{
        run: {
            jobId: string;
            id: string;
            status: import(".prisma/client").$Enums.ReconciliationStatus;
            createdAt: Date;
            updatedAt: Date;
            errorMessage: string | null;
            startedAt: Date | null;
            requestedById: string | null;
            scannedObjects: number;
            indexedObjects: number;
            issueCount: number;
            completedAt: Date | null;
        };
        job: {
            requestedBy: {
                id: string;
                email: string;
                displayName: string;
            } | null;
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
        };
    }>;
    enqueueOrphanDelete(userId: string, role: UserRole, issueIds: string[], confirmation: string): Promise<{
        requestedBy: {
            id: string;
            email: string;
            displayName: string;
        } | null;
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
    }>;
    pageRuns(page: number, pageSize: number): Promise<{
        items: ({
            requestedBy: {
                id: string;
                displayName: string;
            } | null;
        } & {
            id: string;
            status: import(".prisma/client").$Enums.ReconciliationStatus;
            createdAt: Date;
            updatedAt: Date;
            errorMessage: string | null;
            startedAt: Date | null;
            requestedById: string | null;
            jobId: string | null;
            scannedObjects: number;
            indexedObjects: number;
            issueCount: number;
            completedAt: Date | null;
        })[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    getRun(id: string): Promise<{
        issues: {
            id: string;
            status: import(".prisma/client").$Enums.ReconciliationIssueStatus;
            createdAt: Date;
            updatedAt: Date;
            objectKey: string;
            type: import(".prisma/client").$Enums.ReconciliationIssueType;
            runId: string;
            robotFileId: string | null;
            expectedSize: bigint | null;
            actualSize: bigint | null;
            resolutionNote: string | null;
            resolvedAt: Date | null;
        }[];
    } & {
        id: string;
        status: import(".prisma/client").$Enums.ReconciliationStatus;
        createdAt: Date;
        updatedAt: Date;
        errorMessage: string | null;
        startedAt: Date | null;
        requestedById: string | null;
        jobId: string | null;
        scannedObjects: number;
        indexedObjects: number;
        issueCount: number;
        completedAt: Date | null;
    } & {
        issues: {
            expectedSize: string | null;
            actualSize: string | null;
        }[];
    }>;
    ignoreIssue(id: string, note?: string): Promise<{
        id: string;
        status: import(".prisma/client").$Enums.ReconciliationIssueStatus;
        createdAt: Date;
        updatedAt: Date;
        objectKey: string;
        type: import(".prisma/client").$Enums.ReconciliationIssueType;
        runId: string;
        robotFileId: string | null;
        expectedSize: bigint | null;
        actualSize: bigint | null;
        resolutionNote: string | null;
        resolvedAt: Date | null;
    }>;
    executeScan(runId: string): Promise<{
        runId: string;
        scannedObjects: number;
        indexedObjects: number;
        issueCount: number;
    }>;
    executeOrphanDelete(issueIds: string[]): Promise<{
        deleted: number;
        objectKeys: string[];
    }>;
    private activeUploadWhere;
    private listAllObjects;
    private serializeRun;
}
