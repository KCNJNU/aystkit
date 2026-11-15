import { afterEach, describe, expect, it, vi } from 'vitest';
import { createBrowserUuid, sha256Hex } from './browser-crypto';

const originalCrypto = globalThis.crypto;

describe('createBrowserUuid', () => {
  afterEach(() => Object.defineProperty(globalThis, 'crypto', { configurable: true, value: originalCrypto }));

  it('uses randomUUID when the secure-context API is available', () => {
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { randomUUID: () => 'native-uuid' } });
    expect(createBrowserUuid()).toBe('native-uuid');
  });

  it('creates an RFC 4122-shaped UUID when LAN HTTP does not expose randomUUID', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => { bytes.fill(0x11); return bytes; });
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues } });
    expect(createBrowserUuid()).toBe('11111111-1111-4111-9111-111111111111');
    expect(getRandomValues).toHaveBeenCalledOnce();
  });

  it('hashes without relying on secure-context SubtleCrypto', async () => {
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: {} });
    await expect(sha256Hex(new TextEncoder().encode('abc'))).resolves.toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  });
});
