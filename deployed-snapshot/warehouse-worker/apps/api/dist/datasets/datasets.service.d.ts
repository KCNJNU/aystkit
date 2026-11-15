import { Prisma } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';
import { ObjectStorage } from '../storage/object-storage';
import { CreateDatasetDto } from './dto/create-dataset.dto';
import { ListDatasetsQueryDto } from './dto/list-datasets-query.dto';
import { ListVersionFilesQueryDto } from './dto/list-version-files-query.dto';
import { ListVersionsQueryDto } from './dto/list-versions-query.dto';
import { DatasetContentsQueryDto } from './dto/dataset-folder.dto';
import { AuthUser } from '../auth/auth.types';
type ValidationSeverity = 'error' | 'warning';
type ValidationIssue = {
    code: string;
    severity: ValidationSeverity;
    message: string;
    fileId?: string;
    objectKey?: string;
};
export declare class DatasetsService {
    private readonly prisma;
    private readonly storage;
    constructor(prisma: PrismaService, storage: ObjectStorage);
    create(input: CreateDatasetDto, scope?: AuthUser): Promise<{
        projectId: string | null;
        tenantId: string | null;
        id: string;
        name: string;
        createdAt: Date;
        updatedAt: Date;
        metadata: Prisma.JsonValue | null;
        deletingAt: Date | null;
        description: string | null;
    }>;
    delete(id: string, confirmationName: string, scope: AuthUser): Promise<{
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
    folders(id: string, scope: AuthUser): Promise<{
        id: string;
        name: string;
        createdAt: Date;
        datasetId: string;
        objectKey: string;
        path: string;
        parentPath: string;
    }[]>;
    contents(id: string, query: DatasetContentsQueryDto, scope: AuthUser): Promise<{
        view?: {
            kind: "archive-time-slot";
            collapsedSegments: number;
        } | undefined;
        dataset: {
            id: string;
            name: string;
            metadata: Prisma.JsonValue;
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
            metadata: Prisma.JsonValue | null;
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
    createFolder(id: string, parentPath: string, name: string, scope: AuthUser): Promise<{
        id: string;
        name: string;
        createdAt: Date;
        datasetId: string;
        objectKey: string;
        path: string;
        parentPath: string;
    }>;
    get(id: string, scope?: AuthUser): Promise<{
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
        metadata: Prisma.JsonValue | null;
        deletingAt: Date | null;
        description: string | null;
    }>;
    list(scope?: AuthUser): Promise<{
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
        metadata: Prisma.JsonValue | null;
        deletingAt: Date | null;
        description: string | null;
    }[]>;
    page(query: ListDatasetsQueryDto, scope?: AuthUser): Promise<{
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
            metadata: Prisma.JsonValue | null;
            deletingAt: Date | null;
            description: string | null;
        }[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    pageVersions(query: ListVersionsQueryDto, scope?: AuthUser): Promise<{
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
    createVersion(datasetId: string, fileIds: string[], sourceVersionId?: string, scope?: AuthUser): Promise<{
        files: ({
            robotFile: {
                id: string;
                createdAt: Date;
                updatedAt: Date;
                metadata: Prisma.JsonValue | null;
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
    getVersion(id: string, scope?: AuthUser): Promise<{
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
                metadata: Prisma.JsonValue | null;
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
    addFiles(versionId: string, fileIds: string[], scope?: AuthUser): Promise<{
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
                metadata: Prisma.JsonValue | null;
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
    replaceFiles(versionId: string, fileIds: string[], scope?: AuthUser): Promise<{
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
                metadata: Prisma.JsonValue | null;
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
    listVersionFiles(versionId: string, query: ListVersionFilesQueryDto, candidates?: boolean, scope?: AuthUser): Promise<{
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
            metadata: Prisma.JsonValue | null;
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
    removeFile(versionId: string, fileId: string, scope?: AuthUser): Promise<void>;
    freeze(id: string, scope?: AuthUser): Promise<{
        files: ({
            robotFile: {
                id: string;
                createdAt: Date;
                updatedAt: Date;
                metadata: Prisma.JsonValue | null;
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
    validateVersion(id: string, scope?: AuthUser): Promise<{
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
        issues: ValidationIssue[];
    }>;
    compareVersions(datasetId: string, baseVersionId: string, targetVersionId: string, scope?: AuthUser): Promise<{
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
    private findVersion;
    private ensureFilesExist;
    private datasetScope;
    private robotScope;
    private assignment;
    private validateManifest;
    private readObjectWithTimeout;
    private readObject;
    private withTimeout;
    private mapWithConcurrency;
    private assertDraft;
    private createManifest;
    private serializeVersion;
    private serializeVersionWithLiveStats;
    private serializeDetailedVersion;
    private serializeFile;
    private serializeCompareFile;
    private throwUniqueConflict;
}
export {};
