import { AuthUser } from '../auth/auth.types';
import { StorageOperationsService } from './storage-operations.service';
declare class ObjectsQuery {
    prefix?: string;
    cursor?: string;
    limit: number;
}
declare class PageQuery {
    page: number;
    pageSize: number;
}
declare class IgnoreIssueDto {
    note?: string;
}
declare class DeleteOrphansDto {
    issueIds: string[];
    confirmation: string;
}
export declare class StorageController {
    private readonly operations;
    constructor(operations: StorageOperationsService);
    objects(query: ObjectsQuery): Promise<{
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
    scan(user: AuthUser): Promise<{
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
        };
    }>;
    runs(query: PageQuery): Promise<{
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
    run(id: string): Promise<{
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
    ignore(id: string, body: IgnoreIssueDto): Promise<{
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
    deleteOrphans(user: AuthUser, body: DeleteOrphansDto): Promise<{
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
    }>;
}
export {};
