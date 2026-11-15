import { JobStatus, JobType } from '@prisma/client';
import { AuthUser } from '../auth/auth.types';
import { JobsService } from './jobs.service';
declare class JobPageQuery {
    page: number;
    pageSize: number;
    status?: JobStatus;
    type?: JobType;
    search?: string;
}
export declare class JobsController {
    private readonly jobs;
    constructor(jobs: JobsService);
    page(query: JobPageQuery, user: AuthUser): Promise<{
        items: ({
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
        })[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    get(id: string, user: AuthUser): Promise<{
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
    retry(id: string, user: AuthUser): Promise<{
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
    cancel(id: string, user: AuthUser): Promise<{
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
