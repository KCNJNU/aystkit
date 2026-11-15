import { Prisma } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';
import { ObjectStorage } from '../storage/object-storage';
import { ListFilesQueryDto } from './dto/list-files-query.dto';
import { UploadedFile } from './uploaded-file';
import { SettingsService } from '../settings/settings.service';
import { AuthUser } from '../auth/auth.types';
export declare class FilesService {
    private readonly prisma;
    private readonly storage;
    private readonly settings?;
    constructor(prisma: PrismaService, storage: ObjectStorage, settings?: SettingsService | undefined);
    create(input: {
        robotId: string;
        recognitionType: string;
        capturedAt: string;
        schemaVersion: string;
        metadata?: string;
        file: UploadedFile;
    }, scope?: AuthUser): Promise<Omit<{
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
    }>;
    list(query: ListFilesQueryDto, scope?: AuthUser): Promise<{
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
    get(id: string, scope?: AuthUser): Promise<Omit<{
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
    }>;
    getDownloadUrl(id: string, scope?: AuthUser): Promise<{
        url: string;
    }>;
    update(id: string, input: {
        recognitionType?: string;
        metadata?: Record<string, unknown>;
    }, scope: AuthUser): Promise<Omit<{
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
    }>;
    references(id: string, scope: AuthUser): Promise<{
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
    detachReferences(id: string, input: {
        versionIds: string[];
        confirmed: boolean;
    }, scope: AuthUser): Promise<{
        detachedCount: number;
        remainingCount: number;
    }>;
    private datasetScope;
    private find;
    private robotScope;
    private serialize;
}
