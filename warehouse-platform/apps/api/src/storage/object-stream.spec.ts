import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { ObjectStorage } from './object-storage';
import { inspectStoredObject, readManifestObject, MAX_MANIFEST_BYTES } from './object-stream';

const storageFor = (stream: Readable) => ({ getObject: jest.fn(async () => stream) }) as unknown as ObjectStorage;

describe('bounded original-file reads', () => {
  afterEach(() => { jest.restoreAllMocks(); jest.useRealTimers(); });

  it('hashes lazy binary chunks without concatenating or retaining the original file', async () => {
    const chunk = Buffer.alloc(64 * 1024, 7);
    const hash = createHash('sha256');
    for (let i = 0; i < 128; i += 1) hash.update(chunk);
    const stream = Readable.from((function* () { for (let i = 0; i < 128; i += 1) yield chunk; })());
    const concat = jest.spyOn(Buffer, 'concat').mockImplementation(() => { throw new Error('Whole-file materialization is forbidden'); });
    let consumed = 0;
    const value = await inspectStoredObject(storageFor(stream), 'camera.mkv', async data => { consumed += data.length; });
    concat.mockRestore();
    expect(value).toEqual({ checksum: hash.digest('hex'), size: 8n * 1024n * 1024n });
    expect(consumed).toBe(8 * 1024 * 1024);
  });

  it('lets a progressing transfer exceed the idle duration', async () => {
    jest.useFakeTimers();
    const stream = Readable.from((async function* () {
      for (let i = 0; i < 4; i += 1) {
        await new Promise(resolve => setTimeout(resolve, 8));
        yield Buffer.from('frame');
      }
    })());
    const result = inspectStoredObject(storageFor(stream), 'camera.mkv', undefined, { idleTimeoutMs: 10 });
    await jest.advanceTimersByTimeAsync(40);
    await expect(result).resolves.toMatchObject({ size: 20n });
  });

  it('destroys an idle object stream and reports a timeout', async () => {
    jest.useFakeTimers();
    const stream = new Readable({ read() { /* intentionally stalled */ } });
    const result = inspectStoredObject(storageFor(stream), 'camera.mkv', undefined, { idleTimeoutMs: 5 }).catch(error => error);
    await jest.advanceTimersByTimeAsync(6);
    expect(await result).toEqual(expect.objectContaining({ message: 'ObjectStorage read timed out for camera.mkv' }));
    expect(stream.destroyed).toBe(true);
  });

  it('destroys a stream that arrives after opening already timed out', async () => {
    jest.useFakeTimers();
    let release!: (stream: Readable) => void;
    const storage = { getObject: () => new Promise<Readable>(resolve => { release = resolve; }) } as unknown as ObjectStorage;
    const result = inspectStoredObject(storage, 'camera.mkv', undefined, { idleTimeoutMs: 5 }).catch(error => error);
    await jest.advanceTimersByTimeAsync(6);
    expect(await result).toBeInstanceOf(Error);
    const late = Readable.from([Buffer.from('unused')]);
    release(late);
    await Promise.resolve();
    expect(late.destroyed).toBe(true);
  });

  it('closes source streams when a snapshot sink fails', async () => {
    const stream = Readable.from([Buffer.from('frame')]);
    await expect(inspectStoredObject(storageFor(stream), 'camera.mkv', () => { throw new Error('disk unavailable'); })).rejects.toThrow('disk unavailable');
    expect(stream.destroyed).toBe(true);
  });

  it('cancels an active source read immediately when the durable lease expires', async () => {
    jest.useFakeTimers();
    const controller = new AbortController();
    const stream = new Readable({ read() { /* blocked source */ } });
    const pending = inspectStoredObject(storageFor(stream), 'camera.mkv', undefined,
      { idleTimeoutMs: 60_000, signal: controller.signal });
    const rejection = expect(pending).rejects.toThrow('cancelled');
    await Promise.resolve(); controller.abort(); await rejection;
    expect(stream.destroyed).toBe(true); expect(jest.getTimerCount()).toBe(0);
  });

  it('destroys a late-opened source after cancellation without consuming it', async () => {
    const controller = new AbortController();
    let release!: (stream: Readable) => void;
    const storage = { getObject: () => new Promise<Readable>(resolve => { release = resolve; }) } as unknown as ObjectStorage;
    const consume = jest.fn();
    const pending = inspectStoredObject(storage, 'camera.mkv', consume, { signal: controller.signal });
    const rejection = expect(pending).rejects.toThrow('cancelled');
    controller.abort(); await rejection;
    const late = Readable.from([Buffer.from('untouched')]); release(late); await Promise.resolve();
    expect(late.destroyed).toBe(true); expect(consume).not.toHaveBeenCalled();
  });

  it('limits materialization to small manifests without allocating an oversized document', async () => {
    const chunk = Buffer.alloc(1024 * 1024);
    const stream = Readable.from((function* () { for (let i = 0; i <= MAX_MANIFEST_BYTES / chunk.length; i += 1) yield chunk; })());
    await expect(readManifestObject(storageFor(stream), 'manifest.json')).rejects.toThrow('Manifest exceeds');
    expect(stream.destroyed).toBe(true);
  });
});
