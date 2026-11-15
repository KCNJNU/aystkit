import { ConfigService } from '@nestjs/config';
import { ListObjectsResult, ObjectStorage, ObjectStorageHealth, PutObjectInput, StoredObject } from './object-storage';
export declare class MinioObjectStorageService implements ObjectStorage {
    private readonly bucket;
    private readonly client;
    private readonly downloadClient;
    constructor(config: ConfigService);
    private ensureBucket;
    putObject({ objectKey, content, contentType }: PutObjectInput): Promise<void>;
    getObject(objectKey: string): Promise<NodeJS.ReadableStream>;
    deleteObject(objectKey: string): Promise<void>;
    exists(objectKey: string): Promise<boolean>;
    getPresignedDownloadUrl(objectKey: string, expiresSeconds?: number): Promise<string>;
    checkHealth(): Promise<ObjectStorageHealth>;
    statObject(objectKey: string): Promise<StoredObject>;
    listObjects(input: {
        prefix?: string;
        cursor?: string;
        limit: number;
    }): Promise<ListObjectsResult>;
    composeObject(objectKey: string, sourceObjectKeys: string[], contentType: string): Promise<void>;
    deleteObjects(objectKeys: string[]): Promise<void>;
}
