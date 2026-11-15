import { CreateDatasetFolderDto, DatasetContentsQueryDto } from './dto/dataset-folder.dto';
import { DeleteDatasetDto } from './dto/delete-dataset.dto';
import { CreateDatasetDto } from './dto/create-dataset.dto';
import { CreateDatasetVersionDto } from './dto/create-dataset-version.dto';
import { CompareVersionsQueryDto } from './dto/compare-versions-query.dto';
import { ListDatasetsQueryDto } from './dto/list-datasets-query.dto';
import { ListVersionFilesQueryDto } from './dto/list-version-files-query.dto';
import { ListVersionsQueryDto } from './dto/list-versions-query.dto';
import { ReplaceVersionFilesDto } from './dto/replace-version-files.dto';
import { UpdateVersionFilesDto } from './dto/update-version-files.dto';
import { DatasetsService } from './datasets.service';
import { DatasetPublishService } from './dataset-publish.service';
import { AuthUser } from '../auth/auth.types';
import { JobsService } from '../jobs/jobs.service';
export declare class DatasetsController {
    private readonly datasets;
    private readonly publisher;
    private readonly jobs;
    constructor(datasets: DatasetsService, publisher: DatasetPublishService, jobs: JobsService);
    create(body: CreateDatasetDto, user: AuthUser): Promise<{
        projectId: string | null;
        tenantId: string | null;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        metadata: import("@prisma/client/runtime/library").JsonValue | null;
        deletingAt: Date | null;
        description: string | null;
    }>;
    delete(id: string, body: DeleteDatasetDto, user: AuthUser): Promise<{
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
    list(user: AuthUser): Promise<{
        versions: (Omit<{
            files: {
                robotFile: {
                    fileSize: bigint;
                };
            }[];
        } & {
            version: number;
            id: string;
            status: import(".prisma/client").$Enums.DatasetVersionStatus;
            createdAt: Date;
            updatedAt: Date;
            datasetId: string;
            manifestKey: string | null;
            manifestChecksum: string | null;
            fileCount: number;
            totalSize: bigint;
            gitCommit: string | null;
            gitTag: string | null;
            dvcPath: string | null;
            dvcHash: string | null;
            frozenAt: Date | null;
        }, "files"> & {
            fileCount: number;
            totalSize: string;
        })[];
        projectId: string | null;
        tenantId: string | null;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        metadata: import("@prisma/client/runtime/library").JsonValue | null;
        deletingAt: Date | null;
        description: string | null;
    }[]>;
    page(query: ListDatasetsQueryDto, user: AuthUser): Promise<{
        items: {
            versions: (Omit<{
                files: {
                    robotFile: {
                        fileSize: bigint;
                    };
                }[];
            } & {
                version: number;
                id: string;
                status: import(".prisma/client").$Enums.DatasetVersionStatus;
                createdAt: Date;
                updatedAt: Date;
                datasetId: string;
                manifestKey: string | null;
                manifestChecksum: string | null;
                fileCount: number;
                totalSize: bigint;
                gitCommit: string | null;
                gitTag: string | null;
                dvcPath: string | null;
                dvcHash: string | null;
                frozenAt: Date | null;
            }, "files"> & {
                fileCount: number;
                totalSize: string;
            })[];
            latestVersion: Omit<{
                files: {
                    robotFile: {
                        fileSize: bigint;
                    };
                }[];
            } & {
                version: number;
                id: string;
                status: import(".prisma/client").$Enums.DatasetVersionStatus;
                createdAt: Date;
                updatedAt: Date;
                datasetId: string;
                manifestKey: string | null;
                manifestChecksum: string | null;
                fileCount: number;
                totalSize: bigint;
                gitCommit: string | null;
                gitTag: string | null;
                dvcPath: string | null;
                dvcHash: string | null;
                frozenAt: Date | null;
            }, "files"> & {
                fileCount: number;
                totalSize: string;
            };
            versionCount: number;
            projectId: string | null;
            tenantId: string | null;
            id: string;
            name: string;
            createdAt: Date;
            updatedAt: Date;
            metadata: import("@prisma/client/runtime/library").JsonValue | null;
            deletingAt: Date | null;
            description: string | null;
        }[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    pageVersions(query: ListVersionsQueryDto, user: AuthUser): Promise<{
        items: (Omit<{
            dataset: {
                id: string;
                name: string;
            };
            files: {
                robotFile: {
                    fileSize: bigint;
                };
            }[];
        } & {
            version: number;
            id: string;
            status: import(".prisma/client").$Enums.DatasetVersionStatus;
            createdAt: Date;
            updatedAt: Date;
            datasetId: string;
            manifestKey: string | null;
            manifestChecksum: string | null;
            fileCount: number;
            totalSize: bigint;
            gitCommit: string | null;
            gitTag: string | null;
            dvcPath: string | null;
            dvcHash: string | null;
            frozenAt: Date | null;
        }, "files"> & {
            fileCount: number;
            totalSize: string;
        })[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    compare(datasetId: string, query: CompareVersionsQueryDto, user: AuthUser): Promise<{
        dataset: {
            id: string;
            name: string;
        };
        base: {
            id: string;
            version: number;
            status: import(".prisma/client").$Enums.DatasetVersionStatus;
            fileCount: number;
            totalSize: string;
        };
        target: {
            id: string;
            version: number;
            status: import(".prisma/client").$Enums.DatasetVersionStatus;
            fileCount: number;
            totalSize: string;
        };
        summary: {
            added: number;
            removed: number;
            unchanged: number;
            totalSizeDelta: string;
        };
        files: {
            added: {
                id: string;
                originalName: string;
                checksum: string;
                recognitionType: string;
                schemaVersion: string;
                capturedAt: string;
                fileSize: string;
                robot: unknown;
            }[];
            removed: {
                id: string;
                originalName: string;
                checksum: string;
                recognitionType: string;
                schemaVersion: string;
                capturedAt: string;
                fileSize: string;
                robot: unknown;
            }[];
            unchanged: {
                id: string;
                originalName: string;
                checksum: string;
                recognitionType: string;
                schemaVersion: string;
                capturedAt: string;
                fileSize: string;
                robot: unknown;
            }[];
        };
    }>;
    contents(id: string, query: DatasetContentsQueryDto, user: AuthUser): Promise<{
        view?: {
            kind: "archive-time-slot";
            collapsedSegments: number;
        } | undefined;
        dataset: {
            id: string;
            name: string;
            metadata: import("@prisma/client/runtime/library").JsonValue;
        };
        path: string;
        folders: {
            id: string;
            name: string;
            createdAt: Date;
            datasetId: string;
            objectKey: string;
            path: string;
            parentPath: string;
        }[];
        files: (Omit<{
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
        }, "fileSize"> & {
            fileSize: string;
        })[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    folders(id: string, user: AuthUser): Promise<{
        id: string;
        name: string;
        createdAt: Date;
        datasetId: string;
        objectKey: string;
        path: string;
        parentPath: string;
    }[]>;
    createFolder(id: string, body: CreateDatasetFolderDto, user: AuthUser): Promise<{
        id: string;
        name: string;
        createdAt: Date;
        datasetId: string;
        objectKey: string;
        path: string;
        parentPath: string;
    }>;
    get(id: string, user: AuthUser): Promise<{
        versions: (Omit<{
            files: {
                robotFile: {
                    fileSize: bigint;
                };
            }[];
        } & {
            version: number;
            id: string;
            status: import(".prisma/client").$Enums.DatasetVersionStatus;
            createdAt: Date;
            updatedAt: Date;
            datasetId: string;
            manifestKey: string | null;
            manifestChecksum: string | null;
            fileCount: number;
            totalSize: bigint;
            gitCommit: string | null;
            gitTag: string | null;
            dvcPath: string | null;
            dvcHash: string | null;
            frozenAt: Date | null;
        }, "files"> & {
            fileCount: number;
            totalSize: string;
        })[];
        projectId: string | null;
        tenantId: string | null;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        metadata: import("@prisma/client/runtime/library").JsonValue | null;
        deletingAt: Date | null;
        description: string | null;
    }>;
    createVersion(datasetId: string, body: CreateDatasetVersionDto, user: AuthUser): Promise<{
        files: ({
            robotFile: {
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
            };
        } & {
            createdAt: Date;
            robotFileId: string;
            datasetVersionId: string;
        })[];
    } & {
        version: number;
        id: string;
        status: import(".prisma/client").$Enums.DatasetVersionStatus;
        createdAt: Date;
        updatedAt: Date;
        datasetId: string;
        manifestKey: string | null;
        manifestChecksum: string | null;
        fileCount: number;
        totalSize: bigint;
        gitCommit: string | null;
        gitTag: string | null;
        dvcPath: string | null;
        dvcHash: string | null;
        frozenAt: Date | null;
    } & {
        files?: ({
            robotFile?: {
                fileSize: bigint;
            };
        } | {
            robotFile: Omit<{
                fileSize: bigint;
            }, "fileSize"> & {
                fileSize: string;
            };
        })[] | undefined;
        totalSize: string;
    }>;
    getVersion(id: string, user: AuthUser): Promise<{
        dataset: {
            projectId: string | null;
            tenantId: string | null;
            id: string;
        };
        files: ({
            robotFile: {
                robot: {
                    id: string;
                    name: string;
                    externalId: string | null;
                };
            } & {
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
            };
        } & {
            createdAt: Date;
            robotFileId: string;
            datasetVersionId: string;
        })[];
        version: number;
        id: string;
        status: import(".prisma/client").$Enums.DatasetVersionStatus;
        createdAt: Date;
        updatedAt: Date;
        datasetId: string;
        manifestKey: string | null;
        manifestChecksum: string | null;
        fileCount: number;
        totalSize: bigint;
        gitCommit: string | null;
        gitTag: string | null;
        dvcPath: string | null;
        dvcHash: string | null;
        frozenAt: Date | null;
    } & {
        files?: ({
            robotFile?: {
                fileSize: bigint;
            };
        } | {
            robotFile: Omit<{
                fileSize: bigint;
            }, "fileSize"> & {
                fileSize: string;
            };
        })[] | undefined;
        totalSize: string;
    }>;
    files(id: string, query: ListVersionFilesQueryDto, user: AuthUser): Promise<{
        items: (Omit<{
            robot: {
                id: string;
                name: string;
                externalId: string | null;
            };
        } & {
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
        }, "fileSize"> & {
            fileSize: string;
        })[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    candidates(id: string, query: ListVersionFilesQueryDto, user: AuthUser): Promise<{
        items: (Omit<{
            robot: {
                id: string;
                name: string;
                externalId: string | null;
            };
        } & {
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
        }, "fileSize"> & {
            fileSize: string;
        })[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    addFiles(id: string, body: UpdateVersionFilesDto, user: AuthUser): Promise<{
        dataset: {
            projectId: string | null;
            tenantId: string | null;
            id: string;
        };
        files: ({
            robotFile: {
                robot: {
                    id: string;
                    name: string;
                    externalId: string | null;
                };
            } & {
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
            };
        } & {
            createdAt: Date;
            robotFileId: string;
            datasetVersionId: string;
        })[];
        version: number;
        id: string;
        status: import(".prisma/client").$Enums.DatasetVersionStatus;
        createdAt: Date;
        updatedAt: Date;
        datasetId: string;
        manifestKey: string | null;
        manifestChecksum: string | null;
        fileCount: number;
        totalSize: bigint;
        gitCommit: string | null;
        gitTag: string | null;
        dvcPath: string | null;
        dvcHash: string | null;
        frozenAt: Date | null;
    } & {
        files?: ({
            robotFile?: {
                fileSize: bigint;
            };
        } | {
            robotFile: Omit<{
                fileSize: bigint;
            }, "fileSize"> & {
                fileSize: string;
            };
        })[] | undefined;
        totalSize: string;
    }>;
    replaceFiles(id: string, body: ReplaceVersionFilesDto, user: AuthUser): Promise<{
        dataset: {
            projectId: string | null;
            tenantId: string | null;
            id: string;
        };
        files: ({
            robotFile: {
                robot: {
                    id: string;
                    name: string;
                    externalId: string | null;
                };
            } & {
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
            };
        } & {
            createdAt: Date;
            robotFileId: string;
            datasetVersionId: string;
        })[];
        version: number;
        id: string;
        status: import(".prisma/client").$Enums.DatasetVersionStatus;
        createdAt: Date;
        updatedAt: Date;
        datasetId: string;
        manifestKey: string | null;
        manifestChecksum: string | null;
        fileCount: number;
        totalSize: bigint;
        gitCommit: string | null;
        gitTag: string | null;
        dvcPath: string | null;
        dvcHash: string | null;
        frozenAt: Date | null;
    } & {
        files?: ({
            robotFile?: {
                fileSize: bigint;
            };
        } | {
            robotFile: Omit<{
                fileSize: bigint;
            }, "fileSize"> & {
                fileSize: string;
            };
        })[] | undefined;
        totalSize: string;
    }>;
    removeFile(versionId: string, fileId: string, user: AuthUser): Promise<void>;
    freeze(id: string, user: AuthUser): Promise<{
        files: ({
            robotFile: {
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
            };
        } & {
            createdAt: Date;
            robotFileId: string;
            datasetVersionId: string;
        })[];
    } & {
        version: number;
        id: string;
        status: import(".prisma/client").$Enums.DatasetVersionStatus;
        createdAt: Date;
        updatedAt: Date;
        datasetId: string;
        manifestKey: string | null;
        manifestChecksum: string | null;
        fileCount: number;
        totalSize: bigint;
        gitCommit: string | null;
        gitTag: string | null;
        dvcPath: string | null;
        dvcHash: string | null;
        frozenAt: Date | null;
    } & {
        files?: ({
            robotFile?: {
                fileSize: bigint;
            };
        } | {
            robotFile: Omit<{
                fileSize: bigint;
            }, "fileSize"> & {
                fileSize: string;
            };
        })[] | undefined;
        totalSize: string;
    }>;
    validate(id: string, user: AuthUser): Promise<{
        versionId: string;
        status: import(".prisma/client").$Enums.DatasetVersionStatus;
        valid: boolean;
        validatedAt: string;
        summary: {
            files: number;
            totalSize: string;
            checkedObjects: number;
            errors: number;
            warnings: number;
        };
        issues: {
            code: string;
            severity: "error" | "warning";
            message: string;
            fileId?: string;
            objectKey?: string;
        }[];
    }>;
    publish(id: string, user: AuthUser): Promise<{
        version: number;
        id: string;
        status: import(".prisma/client").$Enums.DatasetVersionStatus;
        createdAt: Date;
        updatedAt: Date;
        datasetId: string;
        manifestKey: string | null;
        manifestChecksum: string | null;
        fileCount: number;
        totalSize: bigint;
        gitCommit: string | null;
        gitTag: string | null;
        dvcPath: string | null;
        dvcHash: string | null;
        frozenAt: Date | null;
    }>;
    publishJob(id: string, user: AuthUser): Promise<{
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
    recoveryJob(id: string, user: AuthUser): Promise<{
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
