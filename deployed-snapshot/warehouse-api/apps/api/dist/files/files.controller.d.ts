import { AuthUser } from '../auth/auth.types';
import { ListFilesQueryDto } from './dto/list-files-query.dto';
import { FilesService } from './files.service';
import { DetachFileReferencesDto } from './dto/detach-file-references.dto';
import { FileLifecycleService } from './file-lifecycle.service';
import { ConfirmFileActionDto, RecycleQueryDto, RelocateFileDto } from './dto/file-lifecycle.dto';
import { UploadedFile as UploadedFilePayload } from './uploaded-file';
declare class CreateFileDto {
    robotId: string;
    recognitionType: string;
    capturedAt: string;
    schemaVersion: string;
    metadata?: string;
}
declare class UpdateFileDto {
    recognitionType?: string;
    metadata?: Record<string, unknown>;
}
export declare class FilesController {
    private readonly files;
    private readonly lifecycle;
    constructor(files: FilesService, lifecycle: FileLifecycleService);
    recycleBin(query: RecycleQueryDto, user: AuthUser): Promise<{
        items: {
            fileSize: string;
            id: string;
            datasetId: string | null;
            folderPath: string;
            objectKey: string;
            originalName: string;
            deletedAt: Date;
            reviewAt: Date;
            purgingAt: Date | null;
        }[];
        total: number;
        page: number;
        pageSize: number;
    }>;
    restore(id: string, _body: ConfirmFileActionDto, user: AuthUser): Promise<{
        id: string;
        restored: boolean;
    }>;
    purge(id: string, _body: ConfirmFileActionDto, user: AuthUser): Promise<{
        id: string;
        purged: boolean;
    }>;
    impact(id: string, user: AuthUser): Promise<{
        objectKey: string;
        folderPath: string;
        datasetId: string | null;
        datasetName: string | null;
        fileSize: string;
        canRecycle: boolean;
        blockers: string[];
        canRelocate: boolean;
        recycleReviewDays: number;
        fileId: string;
        originalName: string;
        total: number;
        restrictedCount: number;
        references: {
            versionId: string;
            datasetId: string;
            datasetName: string;
            version: number;
            status: import(".prisma/client").$Enums.DatasetVersionStatus;
            referencedAt: Date;
            canDetach: boolean;
            reason: string | null;
        }[];
    }>;
    relocate(id: string, body: RelocateFileDto, user: AuthUser): Promise<{
        cleanupPending: boolean;
        id: string;
        objectKey: string;
    }>;
    create(body: CreateFileDto, file: UploadedFilePayload, user: AuthUser): Promise<Omit<{
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
    }>;
    list(query: ListFilesQueryDto, user: AuthUser): Promise<{
        items: (Omit<{
            robot: {
                id: string;
                name: string;
                externalId: string | null;
            };
            id: string;
            createdAt: Date;
            updatedAt: Date;
            tags: ({
                tag: {
                    tenantId: string | null;
                    id: string;
                    name: string;
                    createdAt: Date;
                    updatedAt: Date;
                    color: string;
                };
            } & {
                createdAt: Date;
                robotFileId: string;
                tagId: string;
            })[];
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
    get(id: string, user: AuthUser): Promise<Omit<{
        robot: {
            id: string;
            name: string;
            externalId: string | null;
        };
        tags: ({
            tag: {
                tenantId: string | null;
                id: string;
                name: string;
                createdAt: Date;
                updatedAt: Date;
                color: string;
            };
        } & {
            createdAt: Date;
            robotFileId: string;
            tagId: string;
        })[];
        datasetFiles: ({
            datasetVersion: {
                dataset: {
                    id: string;
                    name: string;
                };
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
            };
        } & {
            createdAt: Date;
            robotFileId: string;
            datasetVersionId: string;
        })[];
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
    }>;
    update(id: string, body: UpdateFileDto, user: AuthUser): Promise<Omit<{
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
    }>;
    download(id: string, user: AuthUser): Promise<{
        url: string;
    }>;
    references(id: string, user: AuthUser): Promise<{
        fileId: string;
        originalName: string;
        total: number;
        restrictedCount: number;
        references: {
            versionId: string;
            datasetId: string;
            datasetName: string;
            version: number;
            status: import(".prisma/client").$Enums.DatasetVersionStatus;
            referencedAt: Date;
            canDetach: boolean;
            reason: string | null;
        }[];
    }>;
    detach(id: string, body: DetachFileReferencesDto, user: AuthUser): Promise<{
        detachedCount: number;
        remainingCount: number;
    }>;
    remove(id: string, user: AuthUser): Promise<void>;
}
export {};
