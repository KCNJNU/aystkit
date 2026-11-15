"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
var __metadata = (this && this.__metadata) || function (k, v) {
    if (typeof Reflect === "object" && typeof Reflect.metadata === "function") return Reflect.metadata(k, v);
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.MinioObjectStorageService = void 0;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const Minio = require("minio");
const node_stream_1 = require("node:stream");
const promises_1 = require("node:stream/promises");
class CheckedUploadStream extends node_stream_1.Transform {
    expectedBytes;
    bytes = 0;
    constructor(expectedBytes) {
        super();
        this.expectedBytes = expectedBytes;
        this.on('error', () => undefined);
    }
    _transform(chunk, _encoding, callback) {
        this.bytes += chunk.length;
        callback(this.bytes > this.expectedBytes ? new Error('Object upload exceeds contentLength') : null, chunk);
    }
    _flush(callback) {
        callback(this.bytes === this.expectedBytes ? undefined : new Error('Object upload ended before contentLength'));
    }
    pipe(destination, options) {
        const target = destination;
        const forward = (error) => target.destroy ? target.destroy(error) : target.emit('error', error);
        this.once('error', forward);
        const cleanup = () => this.removeListener('error', forward);
        destination.once('close', cleanup);
        destination.once('finish', cleanup);
        if (this.destroyed)
            queueMicrotask(() => forward(this.errored ?? new Error('Object upload stream closed')));
        return super.pipe(destination, options);
    }
}
let MinioObjectStorageService = class MinioObjectStorageService {
    bucket;
    client;
    downloadClient;
    constructor(config) {
        this.bucket = config.getOrThrow('OBJECT_STORAGE_BUCKET');
        this.client = new Minio.Client({
            endPoint: config.getOrThrow('OBJECT_STORAGE_ENDPOINT'),
            port: Number(config.getOrThrow('OBJECT_STORAGE_PORT')),
            useSSL: config.get('OBJECT_STORAGE_USE_SSL') === 'true',
            accessKey: config.getOrThrow('OBJECT_STORAGE_ACCESS_KEY'),
            secretKey: config.getOrThrow('OBJECT_STORAGE_SECRET_KEY'),
        });
        this.downloadClient = config.get('OBJECT_STORAGE_PUBLIC_ENDPOINT') ? new Minio.Client({
            endPoint: config.getOrThrow('OBJECT_STORAGE_PUBLIC_ENDPOINT'),
            port: Number(config.get('OBJECT_STORAGE_PUBLIC_PORT') || config.getOrThrow('OBJECT_STORAGE_PORT')),
            useSSL: config.get('OBJECT_STORAGE_USE_SSL') === 'true',
            accessKey: config.getOrThrow('OBJECT_STORAGE_ACCESS_KEY'),
            secretKey: config.getOrThrow('OBJECT_STORAGE_SECRET_KEY'),
            region: 'us-east-1',
        }) : this.client;
    }
    async ensureBucket() {
        if (!(await this.client.bucketExists(this.bucket)))
            await this.client.makeBucket(this.bucket);
    }
    async putObject({ objectKey, content, contentType, contentLength }) {
        const size = contentLength ?? (Buffer.isBuffer(content) ? content.length : undefined);
        if (size === undefined || !Number.isSafeInteger(size) || size < 0) {
            throw new TypeError('Stream uploads require a nonnegative safe-integer contentLength');
        }
        if (Buffer.isBuffer(content)) {
            if (size !== content.length)
                throw new TypeError('Buffer contentLength does not match its bytes');
            await this.ensureBucket();
            await this.client.putObject(this.bucket, objectKey, content, size, { 'Content-Type': contentType });
            return;
        }
        if (!(content instanceof node_stream_1.Readable))
            throw new TypeError('Object content must be a Buffer or Readable');
        const uploadStream = new CheckedUploadStream(size);
        let sourceError;
        let rejectSource;
        const sourceFailure = new Promise((_resolve, reject) => { rejectSource = reject; });
        const onError = (error) => { sourceError = error; rejectSource(error); };
        content.on('error', onError);
        const operation = (async () => {
            await this.ensureBucket();
            if (sourceError)
                throw sourceError;
            if (content.destroyed)
                throw content.errored ?? new Error('Object source stream already closed');
            const upload = this.client.putObject(this.bucket, objectKey, uploadStream, size, { 'Content-Type': contentType });
            await Promise.all([upload, (0, promises_1.pipeline)(content, uploadStream)]);
        })();
        try {
            await Promise.race([operation, sourceFailure]);
        }
        catch (error) {
            uploadStream.destroy(error instanceof Error ? error : new Error('Object upload failed'));
            content.destroy();
            throw error;
        }
        finally {
            if (content.closed)
                content.removeListener('error', onError);
            else
                content.once('close', () => content.removeListener('error', onError));
        }
    }
    async getObject(objectKey) { return this.client.getObject(this.bucket, objectKey); }
    async deleteObject(objectKey) { await this.client.removeObject(this.bucket, objectKey); }
    async exists(objectKey) { try {
        await this.client.statObject(this.bucket, objectKey);
        return true;
    }
    catch {
        return false;
    } }
    async getPresignedDownloadUrl(objectKey, expiresSeconds = 60 * 15) { return this.downloadClient.presignedGetObject(this.bucket, objectKey, expiresSeconds); }
    async checkHealth() {
        try {
            return { provider: 'minio', bucket: this.bucket, reachable: await this.client.bucketExists(this.bucket) };
        }
        catch {
            return { provider: 'minio', bucket: this.bucket, reachable: false };
        }
    }
    async statObject(objectKey) {
        const stat = await this.client.statObject(this.bucket, objectKey);
        return { objectKey, size: stat.size, etag: stat.etag, lastModified: stat.lastModified };
    }
    async listObjects(input) {
        const stream = this.client.listObjectsV2(this.bucket, input.prefix ?? '', true, input.cursor ?? '');
        const items = [];
        for await (const value of stream) {
            const item = value;
            if (!item.name)
                continue;
            items.push({ objectKey: item.name, size: item.size ?? 0, etag: item.etag, lastModified: item.lastModified });
            if (items.length > input.limit) {
                stream.destroy();
                break;
            }
        }
        const hasMore = items.length > input.limit;
        if (hasMore)
            items.pop();
        return { items, nextCursor: hasMore ? items.at(-1)?.objectKey : undefined };
    }
    async composeObject(objectKey, sourceObjectKeys, contentType) {
        if (!sourceObjectKeys.length)
            throw new Error('At least one source object is required');
        const sources = sourceObjectKeys.map((source) => new Minio.CopySourceOptions({ Bucket: this.bucket, Object: source }));
        const destination = new Minio.CopyDestinationOptions({ Bucket: this.bucket, Object: objectKey, Headers: { 'Content-Type': contentType } });
        await this.client.composeObject(destination, sources);
    }
    async deleteObjects(objectKeys) {
        if (!objectKeys.length)
            return;
        const results = await this.client.removeObjects(this.bucket, objectKeys);
        if (results.some(result => result?.Error))
            throw new Error('Object storage returned errors while deleting objects');
    }
};
exports.MinioObjectStorageService = MinioObjectStorageService;
exports.MinioObjectStorageService = MinioObjectStorageService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [config_1.ConfigService])
], MinioObjectStorageService);
//# sourceMappingURL=minio-object-storage.service.js.map