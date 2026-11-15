import { Injectable } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import * as Minio from 'minio';
import { Readable, Transform, type TransformCallback } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { ListObjectsResult, ObjectStorage, ObjectStorageHealth, PutObjectInput, StoredObject } from './object-storage';

/** Bounded stream whose failure also reaches the SDK's multipart pipe target. */
class CheckedUploadStream extends Transform {
  private bytes = 0;
  constructor(private readonly expectedBytes: number) {
    super();
    // A failed bucket check may destroy this stream before the SDK subscribes.
    this.on('error', () => undefined);
  }
  override _transform(chunk: Buffer, _encoding: BufferEncoding, callback: TransformCallback) {
    this.bytes += chunk.length;
    callback(this.bytes > this.expectedBytes ? new Error('Object upload exceeds contentLength') : null, chunk);
  }
  override _flush(callback: TransformCallback) {
    callback(this.bytes === this.expectedBytes ? undefined : new Error('Object upload ended before contentLength'));
  }
  override pipe<T extends NodeJS.WritableStream>(destination: T, options?: { end?: boolean }): T {
    // MinIO 8's multipart implementation handles errors on its block stream,
    // but does not forward errors emitted by the source of body.pipe(blocks).
    const target = destination as T & { destroy?: (error: Error) => void };
    const forward = (error: Error) => target.destroy ? target.destroy(error) : target.emit('error', error);
    this.once('error', forward);
    const cleanup = () => this.removeListener('error', forward);
    destination.once('close', cleanup);
    destination.once('finish', cleanup);
    if (this.destroyed) queueMicrotask(() => forward(this.errored ?? new Error('Object upload stream closed')));
    return super.pipe(destination, options);
  }
}

/** Development adapter. Domain services must depend on ObjectStorage, not this class. */
@Injectable()
export class MinioObjectStorageService implements ObjectStorage {
  private readonly bucket: string;
  private readonly client: Minio.Client;
  private readonly downloadClient: Minio.Client;
  constructor(config: ConfigService) {
    this.bucket = config.getOrThrow<string>('OBJECT_STORAGE_BUCKET');
    this.client = new Minio.Client({
      endPoint: config.getOrThrow<string>('OBJECT_STORAGE_ENDPOINT'),
      port: Number(config.getOrThrow<string>('OBJECT_STORAGE_PORT')),
      useSSL: config.get<string>('OBJECT_STORAGE_USE_SSL') === 'true',
      accessKey: config.getOrThrow<string>('OBJECT_STORAGE_ACCESS_KEY'),
      secretKey: config.getOrThrow<string>('OBJECT_STORAGE_SECRET_KEY'),
    });
    this.downloadClient = config.get<string>('OBJECT_STORAGE_PUBLIC_ENDPOINT') ? new Minio.Client({
      endPoint: config.getOrThrow<string>('OBJECT_STORAGE_PUBLIC_ENDPOINT'),
      port: Number(config.get<string>('OBJECT_STORAGE_PUBLIC_PORT') || config.getOrThrow<string>('OBJECT_STORAGE_PORT')),
      useSSL: config.get<string>('OBJECT_STORAGE_USE_SSL') === 'true',
      accessKey: config.getOrThrow<string>('OBJECT_STORAGE_ACCESS_KEY'),
      secretKey: config.getOrThrow<string>('OBJECT_STORAGE_SECRET_KEY'),
      region: 'us-east-1',
    }) : this.client;
  }
  private async ensureBucket(): Promise<void> {
    if (!(await this.client.bucketExists(this.bucket))) await this.client.makeBucket(this.bucket);
  }
  async putObject({ objectKey, content, contentType, contentLength }: PutObjectInput): Promise<void> {
    const size = contentLength ?? (Buffer.isBuffer(content) ? content.length : undefined);
    if (size === undefined || !Number.isSafeInteger(size) || size < 0) {
      throw new TypeError('Stream uploads require a nonnegative safe-integer contentLength');
    }
    if (Buffer.isBuffer(content)) {
      if (size !== content.length) throw new TypeError('Buffer contentLength does not match its bytes');
      await this.ensureBucket();
      await this.client.putObject(this.bucket, objectKey, content, size, { 'Content-Type': contentType });
      return;
    }
    if (!(content instanceof Readable)) throw new TypeError('Object content must be a Buffer or Readable');
    const uploadStream = new CheckedUploadStream(size);
    let sourceError: Error | undefined;
    let rejectSource!: (error: Error) => void;
    const sourceFailure = new Promise<never>((_resolve, reject) => { rejectSource = reject; });
    const onError = (error: Error) => { sourceError = error; rejectSource(error); };
    content.on('error', onError);
    const operation = (async () => {
      await this.ensureBucket();
      if (sourceError) throw sourceError;
      if (content.destroyed) throw content.errored ?? new Error('Object source stream already closed');
      const upload = this.client.putObject(this.bucket, objectKey, uploadStream, size, { 'Content-Type': contentType });
      await Promise.all([upload, pipeline(content, uploadStream)]);
    })();
    try {
      await Promise.race([operation, sourceFailure]);
    } catch (error) {
      uploadStream.destroy(error instanceof Error ? error : new Error('Object upload failed'));
      content.destroy();
      throw error;
    } finally {
      // fs.ReadStream can still report a pending open/close error after
      // destroy(). Keep the guard until close when no pipeline was started.
      if (content.closed) content.removeListener('error', onError);
      else content.once('close', () => content.removeListener('error', onError));
    }
  }
  async getObject(objectKey: string): Promise<NodeJS.ReadableStream> { return this.client.getObject(this.bucket, objectKey); }
  async deleteObject(objectKey: string): Promise<void> { await this.client.removeObject(this.bucket, objectKey); }
  async exists(objectKey: string): Promise<boolean> { try { await this.client.statObject(this.bucket, objectKey); return true; } catch { return false; } }
  async getPresignedDownloadUrl(objectKey: string, expiresSeconds = 60 * 15): Promise<string> { return this.downloadClient.presignedGetObject(this.bucket, objectKey, expiresSeconds); }
  async checkHealth(): Promise<ObjectStorageHealth> {
    try { return { provider: 'minio', bucket: this.bucket, reachable: await this.client.bucketExists(this.bucket) }; }
    catch { return { provider: 'minio', bucket: this.bucket, reachable: false }; }
  }
  async statObject(objectKey: string): Promise<StoredObject> {
    const stat = await this.client.statObject(this.bucket, objectKey);
    return { objectKey, size: stat.size, etag: stat.etag, lastModified: stat.lastModified };
  }
  async listObjects(input: { prefix?: string; cursor?: string; limit: number }): Promise<ListObjectsResult> {
    const stream = this.client.listObjectsV2(this.bucket, input.prefix ?? '', true, input.cursor ?? '');
    const items: StoredObject[] = [];
    for await (const value of stream) {
      const item = value as { name?: string; size?: number; etag?: string; lastModified?: Date; prefix?: string };
      if (!item.name) continue;
      items.push({ objectKey: item.name, size: item.size ?? 0, etag: item.etag, lastModified: item.lastModified });
      if (items.length > input.limit) { stream.destroy(); break; }
    }
    const hasMore = items.length > input.limit;
    if (hasMore) items.pop();
    return { items, nextCursor: hasMore ? items.at(-1)?.objectKey : undefined };
  }
  async composeObject(objectKey: string, sourceObjectKeys: string[], contentType: string): Promise<void> {
    if (!sourceObjectKeys.length) throw new Error('At least one source object is required');
    const sources = sourceObjectKeys.map((source) => new Minio.CopySourceOptions({ Bucket: this.bucket, Object: source }));
    const destination = new Minio.CopyDestinationOptions({ Bucket: this.bucket, Object: objectKey, Headers: { 'Content-Type': contentType } });
    await this.client.composeObject(destination, sources);
  }
  async deleteObjects(objectKeys: string[]): Promise<void> {
    if (!objectKeys.length) return;
    const results = await this.client.removeObjects(this.bucket, objectKeys);
    if (results.some(result => result?.Error)) throw new Error('Object storage returned errors while deleting objects');
  }
}
