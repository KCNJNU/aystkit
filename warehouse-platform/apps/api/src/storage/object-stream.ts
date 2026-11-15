import { createHash } from 'node:crypto';
import { ObjectStorage } from './object-storage';

export const OBJECT_IDLE_TIMEOUT_MS = 15_000;
export const MAX_MANIFEST_BYTES = 64 * 1024 * 1024;
type DestroyableStream = NodeJS.ReadableStream & { destroy?: () => void };

/** Each received chunk renews the idle budget. Total file size never enters RAM. */
export async function inspectStoredObject(storage: ObjectStorage, objectKey: string,
  consume?: (chunk: Buffer) => void | Promise<void>,
  options: { idleTimeoutMs?: number; maxBytes?: number; signal?: AbortSignal } = {}) {
  const idleTimeoutMs = options.idleTimeoutMs ?? OBJECT_IDLE_TIMEOUT_MS;
  if (!Number.isFinite(idleTimeoutMs) || idleTimeoutMs <= 0) throw new Error('Invalid object idle timeout');
  let stream: DestroyableStream | undefined;
  let expired = false;
  const destroy = () => { expired = true; stream?.destroy?.(); };
  const wait = async <T>(operation: Promise<T>): Promise<T> => {
    let timer: NodeJS.Timeout | undefined;
    let abort: (() => void) | undefined;
    try {
      const pending = new Promise<never>((_, reject) => {
        abort = () => { destroy(); reject(new Error('ObjectStorage read cancelled')); };
        if (options.signal?.aborted) { abort(); return; }
        options.signal?.addEventListener('abort', abort, { once: true });
        timer = setTimeout(() => { destroy(); reject(new Error(`ObjectStorage read timed out for ${objectKey}`)); }, idleTimeoutMs);
      });
      return await Promise.race([operation, pending]);
    } finally {
      if (timer) clearTimeout(timer);
      if (abort) options.signal?.removeEventListener('abort', abort);
    }
  };
  try {
    if (options.signal?.aborted) throw new Error('ObjectStorage read cancelled');
    const opening = storage.getObject(objectKey).then(value => {
      stream = value as DestroyableStream;
      if (expired) stream.destroy?.();
      return stream;
    });
    stream = await wait(opening);
    const iterator = stream[Symbol.asyncIterator]();
    const hash = createHash('sha256');
    let size = 0n;
    while (true) {
      const next = await wait(iterator.next());
      if (next.done) break;
      const chunk = Buffer.isBuffer(next.value) ? next.value : Buffer.from(next.value);
      size += BigInt(chunk.length);
      if (options.maxBytes !== undefined && size > BigInt(options.maxBytes)) throw new Error('Manifest exceeds the bounded metadata inspection budget');
      hash.update(chunk);
      if (consume) await consume(chunk);
    }
    return { size, checksum: hash.digest('hex') };
  } catch (error) {
    destroy();
    throw error;
  }
}

/** Only small manifests are materialized; original RobotFiles use inspectStoredObject. */
export async function readManifestObject(storage: ObjectStorage, objectKey: string) {
  const chunks: Buffer[] = [];
  await inspectStoredObject(storage, objectKey, chunk => { chunks.push(chunk); }, { maxBytes: MAX_MANIFEST_BYTES });
  return Buffer.concat(chunks);
}
