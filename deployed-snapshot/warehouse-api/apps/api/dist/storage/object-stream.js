"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.MAX_MANIFEST_BYTES = exports.OBJECT_IDLE_TIMEOUT_MS = void 0;
exports.inspectStoredObject = inspectStoredObject;
exports.readManifestObject = readManifestObject;
const node_crypto_1 = require("node:crypto");
exports.OBJECT_IDLE_TIMEOUT_MS = 15_000;
exports.MAX_MANIFEST_BYTES = 64 * 1024 * 1024;
async function inspectStoredObject(storage, objectKey, consume, options = {}) {
    const idleTimeoutMs = options.idleTimeoutMs ?? exports.OBJECT_IDLE_TIMEOUT_MS;
    if (!Number.isFinite(idleTimeoutMs) || idleTimeoutMs <= 0)
        throw new Error('Invalid object idle timeout');
    let stream;
    let expired = false;
    const destroy = () => { expired = true; stream?.destroy?.(); };
    const wait = async (operation) => {
        let timer;
        let abort;
        try {
            const pending = new Promise((_, reject) => {
                abort = () => { destroy(); reject(new Error('ObjectStorage read cancelled')); };
                if (options.signal?.aborted) {
                    abort();
                    return;
                }
                options.signal?.addEventListener('abort', abort, { once: true });
                timer = setTimeout(() => { destroy(); reject(new Error(`ObjectStorage read timed out for ${objectKey}`)); }, idleTimeoutMs);
            });
            return await Promise.race([operation, pending]);
        }
        finally {
            if (timer)
                clearTimeout(timer);
            if (abort)
                options.signal?.removeEventListener('abort', abort);
        }
    };
    try {
        if (options.signal?.aborted)
            throw new Error('ObjectStorage read cancelled');
        const opening = storage.getObject(objectKey).then(value => {
            stream = value;
            if (expired)
                stream.destroy?.();
            return stream;
        });
        stream = await wait(opening);
        const iterator = stream[Symbol.asyncIterator]();
        const hash = (0, node_crypto_1.createHash)('sha256');
        let size = 0n;
        while (true) {
            const next = await wait(iterator.next());
            if (next.done)
                break;
            const chunk = Buffer.isBuffer(next.value) ? next.value : Buffer.from(next.value);
            size += BigInt(chunk.length);
            if (options.maxBytes !== undefined && size > BigInt(options.maxBytes))
                throw new Error('Manifest exceeds the bounded metadata inspection budget');
            hash.update(chunk);
            if (consume)
                await consume(chunk);
        }
        return { size, checksum: hash.digest('hex') };
    }
    catch (error) {
        destroy();
        throw error;
    }
}
async function readManifestObject(storage, objectKey) {
    const chunks = [];
    await inspectStoredObject(storage, objectKey, chunk => { chunks.push(chunk); }, { maxBytes: exports.MAX_MANIFEST_BYTES });
    return Buffer.concat(chunks);
}
//# sourceMappingURL=object-stream.js.map