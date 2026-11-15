import { ObjectStorage } from './object-storage';
export declare const OBJECT_IDLE_TIMEOUT_MS = 15000;
export declare const MAX_MANIFEST_BYTES: number;
export declare function inspectStoredObject(storage: ObjectStorage, objectKey: string, consume?: (chunk: Buffer) => void | Promise<void>, options?: {
    idleTimeoutMs?: number;
    maxBytes?: number;
}): Promise<{
    size: bigint;
    checksum: string;
}>;
export declare function readManifestObject(storage: ObjectStorage, objectKey: string): Promise<Buffer<ArrayBuffer>>;
