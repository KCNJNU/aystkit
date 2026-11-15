import { ConfigService } from '@nestjs/config';
import * as Minio from 'minio';
import { Readable, Writable } from 'node:stream';
import { createHash } from 'node:crypto';
import { MinioObjectStorageService } from './minio-object-storage.service';
jest.mock('minio', () => ({ Client: jest.fn() }));
describe('ObjectStorage MinIO adapter', () => {
  const config = { OBJECT_STORAGE_ENDPOINT: 'minio', OBJECT_STORAGE_PORT: 9000, OBJECT_STORAGE_USE_SSL: 'false', OBJECT_STORAGE_BUCKET: 'raw', OBJECT_STORAGE_ACCESS_KEY: 'test', OBJECT_STORAGE_SECRET_KEY: 'secret' };
  beforeEach(() => jest.clearAllMocks());
  it('uses the browser-reachable host only for signed downloads', async () => {
    const internal = { bucketExists: jest.fn().mockResolvedValue(true) }; const download = { presignedGetObject: jest.fn().mockResolvedValue('signed-url') };
    (Minio.Client as unknown as jest.Mock).mockImplementationOnce(() => internal).mockImplementationOnce(() => download);
    const adapter = new MinioObjectStorageService(new ConfigService({ ...config, OBJECT_STORAGE_PUBLIC_ENDPOINT: 'files.example.invalid', OBJECT_STORAGE_PUBLIC_PORT: 9000 }));
    expect((await adapter.checkHealth()).reachable).toBe(true); expect(internal.bucketExists).toHaveBeenCalledWith('raw');
    expect(await adapter.getPresignedDownloadUrl('file.json', 300)).toBe('signed-url'); expect(download.presignedGetObject).toHaveBeenCalledWith('raw', 'file.json', 300);
    expect(Minio.Client).toHaveBeenLastCalledWith(expect.objectContaining({ endPoint: 'files.example.invalid', region: 'us-east-1' }));
  });
  it('keeps the original single-client path outside Docker', async () => {
    const client = { presignedGetObject: jest.fn().mockResolvedValue('url') }; (Minio.Client as unknown as jest.Mock).mockImplementation(() => client);
    const adapter = new MinioObjectStorageService(new ConfigService(config)); await adapter.getPresignedDownloadUrl('file.json'); expect(Minio.Client).toHaveBeenCalledTimes(1);
  });

  function storage(client: { bucketExists: jest.Mock; putObject: jest.Mock }) {
    (Minio.Client as unknown as jest.Mock).mockImplementation(() => client);
    return new MinioObjectStorageService(new ConfigService(config));
  }

  it('preserves Buffer uploads and derives their exact length without a new argument', async () => {
    const client = { bucketExists: jest.fn().mockResolvedValue(true), putObject: jest.fn().mockResolvedValue({}) };
    const adapter = storage(client), content = Buffer.from('legacy metadata');
    await adapter.putObject({ objectKey: 'metadata.yaml', content, contentType: 'text/yaml' });
    expect(client.putObject).toHaveBeenCalledWith('raw', 'metadata.yaml', content, content.length, { 'Content-Type': 'text/yaml' });
  });

  it('streams lazy binary data to the provider with an explicit length and bounded buffering', async () => {
    const chunk = Buffer.alloc(64 * 1024, 7), expected = createHash('sha256');
    for (let i = 0; i < 128; i += 1) expected.update(chunk);
    let produced = 0, consumed = 0, largestAhead = 0;
    const source = Readable.from((function* () {
      for (let i = 0; i < 128; i += 1) { produced += chunk.length; largestAhead = Math.max(largestAhead, produced - consumed); yield chunk; }
    })(), { objectMode: false, highWaterMark: chunk.length });
    const client = { bucketExists: jest.fn().mockResolvedValue(true), putObject: jest.fn(async (_bucket: string, _key: string, stream: Readable, size: number) => {
      expect(stream).toBeInstanceOf(Readable);
      expect(size).toBe(8 * 1024 * 1024);
      const actual = createHash('sha256');
      for await (const data of stream) { consumed += data.length; actual.update(data); await Promise.resolve(); }
      expect(actual.digest('hex')).toBe(expected.digest('hex'));
    }) };
    const adapter = storage(client);
    const concat = jest.spyOn(Buffer, 'concat').mockImplementation(() => { throw new Error('Whole-object materialization is forbidden'); });
    try {
      await adapter.putObject({ objectKey: 'large.tar.zst', content: source, contentLength: 8 * 1024 * 1024, contentType: 'application/zstd' });
    } finally { concat.mockRestore(); }
    expect(consumed).toBe(8 * 1024 * 1024);
    expect(largestAhead).toBeLessThan(512 * 1024);
    expect(source.destroyed).toBe(true);
  });

  it.each([undefined, -1, NaN, Infinity, 1.5, Number.MAX_SAFE_INTEGER + 1])('rejects an invalid stream length %s before provider I/O', async contentLength => {
    const client = { bucketExists: jest.fn(), putObject: jest.fn() };
    await expect(storage(client).putObject({ objectKey: 'stream', content: Readable.from([]), contentLength, contentType: 'application/octet-stream' })).rejects.toThrow('contentLength');
    expect(client.bucketExists).not.toHaveBeenCalled();
    expect(client.putObject).not.toHaveBeenCalled();
  });

  it('accepts an empty stream with an explicit zero length', async () => {
    const client = { bucketExists: jest.fn().mockResolvedValue(true), putObject: jest.fn().mockResolvedValue({}) };
    const source = Readable.from([]);
    await storage(client).putObject({ objectKey: 'empty', content: source, contentLength: 0, contentType: 'text/plain' });
    expect(client.putObject).toHaveBeenCalledWith('raw', 'empty', expect.any(Readable), 0, { 'Content-Type': 'text/plain' });
    expect(source.destroyed).toBe(true);
  });

  it('rejects truncated or oversized source data instead of trusting its declared size', async () => {
    for (const size of [2, 8]) {
      const client = { bucketExists: jest.fn().mockResolvedValue(true), putObject: jest.fn(async (_bucket: string, _key: string, stream: Readable) => {
        for await (const chunk of stream) { void chunk; /* consume the real stream */ }
      }) };
      const source = Readable.from([Buffer.from('four')]);
      await expect(storage(client).putObject({ objectKey: 'wrong-size', content: source, contentLength: size, contentType: 'text/plain' })).rejects.toThrow('contentLength');
      expect(source.destroyed).toBe(true);
    }
  });

  it('propagates an upstream file error through the promise and destroys a piped provider consumer', async () => {
    const failure = new Error('source file unreadable');
    const source = Readable.from((async function* () { yield Buffer.from('prefix'); throw failure; })());
    let destination!: Writable;
    const client = { bucketExists: jest.fn().mockResolvedValue(true), putObject: jest.fn((_bucket: string, _key: string, stream: Readable) => new Promise<void>((resolve, reject) => {
      destination = new Writable({ write(_data, _encoding, done) { done(); } });
      stream.pipe(destination).on('error', reject).on('finish', resolve);
    })) };
    await expect(storage(client).putObject({ objectKey: 'broken', content: source, contentLength: 100, contentType: 'application/octet-stream' })).rejects.toBe(failure);
    await new Promise(resolve => setImmediate(resolve));
    expect(source.destroyed).toBe(true);
    expect(destination.destroyed).toBe(true);
  });

  it('reports a source error during bucket lookup and never starts a late provider upload', async () => {
    let release!: (value: boolean) => void;
    const client = { bucketExists: jest.fn(() => new Promise<boolean>(resolve => { release = resolve; })), putObject: jest.fn() };
    const source = new Readable({ read() { /* wait for a file open */ } });
    const result = storage(client).putObject({ objectKey: 'not-opened', content: source, contentLength: 1, contentType: 'text/plain' });
    const failure = new Error('file open failed');
    source.destroy(failure);
    await expect(result).rejects.toBe(failure);
    release(true);
    await new Promise(resolve => setImmediate(resolve));
    expect(client.putObject).not.toHaveBeenCalled();
  });

  it('closes the source when the provider rejects an upload', async () => {
    const source = new Readable({ read() { this.push(Buffer.alloc(1024)); } });
    const client = { bucketExists: jest.fn().mockResolvedValue(true), putObject: jest.fn().mockRejectedValue(new Error('storage unavailable')) };
    await expect(storage(client).putObject({ objectKey: 'failed', content: source, contentLength: 1024 * 1024, contentType: 'application/octet-stream' })).rejects.toThrow('storage unavailable');
    expect(source.destroyed).toBe(true);
  });

  it('interrupts the installed SDK multipart algorithm on a real source failure without completing the object', async () => {
    const actual = jest.requireActual<typeof import('minio')>('minio');
    const client = new actual.Client({ endPoint: 'never-contacted.invalid', accessKey: 'fixture', secretKey: 'fixture-secret' });
    const complete = jest.fn().mockResolvedValue({ etag: 'never-complete' });
    let firstPart!: () => void;
    const firstPartUploaded = new Promise<void>(resolve => { firstPart = resolve; });
    // Replace only transport/remote metadata operations. Real putObject and
    // uploadStream run with a tiny part boundary, avoiding actual network I/O.
    Object.assign(client, {
      bucketExists: jest.fn().mockResolvedValue(true), calculatePartSize: () => 4,
      findUploadId: jest.fn().mockResolvedValue(undefined),
      initiateNewMultipartUpload: jest.fn().mockResolvedValue('fixture-upload'),
      makeRequestAsyncOmit: jest.fn(async () => { firstPart(); return { headers: { etag: 'fixture-etag' } }; }),
      completeMultipartUpload: complete,
    });
    (Minio.Client as unknown as jest.Mock).mockImplementation(() => client);
    const failure = new Error('file failed after the first uploaded part');
    const source = Readable.from((async function* () {
      yield Buffer.from('abcd');
      await firstPartUploaded;
      throw failure;
    })());
    const adapter = new MinioObjectStorageService(new ConfigService(config));
    await expect(adapter.putObject({ objectKey: 'multipart.tar.zst', content: source, contentLength: 16, contentType: 'application/zstd' })).rejects.toBe(failure);
    await new Promise(resolve => setImmediate(resolve));
    expect(complete).not.toHaveBeenCalled();
    expect(source.destroyed).toBe(true);
  });

  it('handles a late source close error after bucket lookup has already failed', async () => {
    const source = new Readable({
      read() { /* remains paused before the bucket check */ },
      destroy(_error, done) { setImmediate(() => done(new Error('late file close error'))); },
    });
    const client = { bucketExists: jest.fn().mockRejectedValue(new Error('bucket unavailable')), putObject: jest.fn() };
    await expect(storage(client).putObject({ objectKey: 'not-started', content: source, contentLength: 1, contentType: 'text/plain' })).rejects.toThrow('bucket unavailable');
    await new Promise(resolve => setImmediate(resolve));
    expect(source.closed).toBe(true);
    expect(client.putObject).not.toHaveBeenCalled();
  });
});
