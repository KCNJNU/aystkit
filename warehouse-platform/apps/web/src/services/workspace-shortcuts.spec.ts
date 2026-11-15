import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createWorkspaceShortcutsStore, makeWorkspaceShortcut, MAX_WORKSPACE_FAVORITES, MAX_WORKSPACE_RECENT, parseWorkspaceShortcuts, parseWorkspaceUrl, workspaceShortcutsKey } from './workspace-shortcuts';

const datasetId = '00000000-0000-4000-8000-000000000001';
const robotA = '00000000-0000-4000-8000-000000000002';
const robotB = '00000000-0000-4000-8000-000000000003';
const target = (robotId = robotA, path = '') => makeWorkspaceShortcut({ datasetId, datasetName: '地库数据', path }, robotId, 100)!;
beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe('warehouse saved workspace positions', () => {
  it('restores the exact directory/session and explicit robot including all', () => {
    const entry = makeWorkspaceShortcut({ datasetId, datasetName: '地库', path: '前相机/14:50-15:50', captureSession: '123abc-456', sessionLabel: '14:50 – 15:50' }, 'all', 123)!;
    expect(parseWorkspaceUrl(entry.url)).toEqual({ datasetId, robotId: 'all', path: '前相机/14:50-15:50', captureSession: '123abc-456' });
    expect(entry.kind).toBe('capture-session');
    expect(entry.label).toBe('14:50 – 15:50');
    const restored = parseWorkspaceShortcuts(JSON.stringify({ version: 1, favorites: [entry], recent: [entry] }));
    expect(restored.favorites[0]).toEqual(entry);
  });

  it.each([
    `https://evil.invalid/datasets/${datasetId}?robotId=all`,
    `//evil.invalid/datasets/${datasetId}?robotId=all`,
    `/datasets/${datasetId}?robotId=all&X-Amz-Signature=secret`,
    `/datasets/${datasetId}?robotId=all&url=https://evil.invalid`,
    `/datasets/${datasetId}?robotId=all&robotId=${robotA}`,
    `/datasets/${datasetId}?path=folder`,
    `/datasets/${datasetId}?robotId=not-a-car`,
    `/datasets/${datasetId}?robotId=all&path=../secret`,
    `/datasets/${datasetId}?robotId=all&path=%2Fetc%2Fpasswd`,
    `/datasets/${datasetId}?robotId=all&path=one%5Ctwo`,
    `/datasets/${datasetId}?robotId=all&captureSession=<script>`,
    `/datasets/${datasetId}?robotId=all#access_token=secret`,
    `/datasets/${datasetId}/../${datasetId}?robotId=all`,
    `/login?robotId=all`,
  ])('rejects non-workspace or unsafe location %s', url => expect(parseWorkspaceUrl(url)).toBeNull());

  it('isolates accounts and robot contexts while deduplicating revisits', () => {
    const alice = createWorkspaceShortcutsStore('alice');
    const bob = createWorkspaceShortcutsStore('bob');
    alice.record(target(robotA, '相机'));
    alice.record(target(robotB, '相机'));
    alice.record({ ...target(robotA, '相机'), updatedAt: 200 });
    expect(alice.getSnapshot().recent).toHaveLength(2);
    expect(alice.getSnapshot().recent[0].robotId).toBe(robotA);
    expect(bob.getSnapshot()).toEqual({ favorites: [], recent: [] });
    expect(createWorkspaceShortcutsStore('alice').getSnapshot()).toEqual(alice.getSnapshot());
    expect(workspaceShortcutsKey('alice')).not.toBe(workspaceShortcutsKey('bob'));
    alice.toggleFavorite(target(robotA, '相机'));
    expect(alice.getSnapshot().favorites).toHaveLength(1);
    alice.toggleFavorite(target(robotA, '相机'));
    expect(alice.getSnapshot().favorites).toHaveLength(0);
    alice.removeRecent(target(robotB, '相机').key);
    expect(alice.getSnapshot().recent.map(item => item.robotId)).toEqual([robotA]);
    alice.clearRecent();
    expect(alice.getSnapshot().recent).toEqual([]);
  });

  it('never persists a signed download URL even if an invalid caller supplies it', () => {
    const store = createWorkspaceShortcutsStore('unsafe');
    const unsafe = { ...target(), url: `https://object.invalid/video.mkv?X-Amz-Signature=secret` };
    store.record(unsafe);
    expect(store.toggleFavorite(unsafe)).toBe(false);
    expect(store.getSnapshot()).toEqual({ favorites: [], recent: [] });
    expect(localStorage.getItem(store.key)).toBeNull();
  });

  it('bounds history, preserves favorites at capacity, and tolerates damaged data', () => {
    const store = createWorkspaceShortcutsStore('bounded');
    for (let index = 0; index < 60; index++) store.record(target(robotA, `folder-${index}`));
    expect(store.getSnapshot().recent).toHaveLength(MAX_WORKSPACE_RECENT);
    for (let index = 0; index < MAX_WORKSPACE_FAVORITES; index++) expect(store.toggleFavorite(target(robotA, `favorite-${index}`))).toBe(true);
    expect(store.toggleFavorite(target(robotA, 'extra'))).toBe(false);
    expect(store.getSnapshot().favorites).toHaveLength(MAX_WORKSPACE_FAVORITES);
    store.removeFavorite(target(robotA, 'favorite-0').key);
    expect(store.toggleFavorite(target(robotA, 'extra'))).toBe(true);
    const entry = target();
    const restored = parseWorkspaceShortcuts(JSON.stringify({ version: 1, favorites: [entry, entry, { ...entry, url: 'https://evil.invalid' }], recent: {} }));
    expect(restored).toEqual({ favorites: [entry], recent: [] });
    for (const raw of ['broken', 'null', '[]', '{"version":2}', 'x'.repeat(300_001)]) expect(parseWorkspaceShortcuts(raw)).toEqual({ favorites: [], recent: [] });
  });

  it('keeps working in memory when browser storage cannot be read or written', () => {
    const unavailable = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('quota'); } };
    const store = createWorkspaceShortcutsStore('private', unavailable);
    const listener = vi.fn(); const unsubscribe = store.subscribe(listener);
    expect(() => { store.record(target()); store.toggleFavorite(target()); }).not.toThrow();
    expect(store.getSnapshot().recent).toHaveLength(1);
    expect(store.getSnapshot().favorites).toHaveLength(1);
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
  });

  it('updates from same-account storage events and ignores other accounts', () => {
    const store = createWorkspaceShortcutsStore('alice');
    const listener = vi.fn(); const unsubscribe = store.subscribe(listener);
    const saved = JSON.stringify({ version: 1, favorites: [target()], recent: [] });
    window.dispatchEvent(new StorageEvent('storage', { key: workspaceShortcutsKey('bob'), newValue: saved, storageArea: localStorage }));
    expect(store.getSnapshot().favorites).toHaveLength(0);
    window.dispatchEvent(new StorageEvent('storage', { key: workspaceShortcutsKey('alice'), newValue: saved, storageArea: localStorage }));
    expect(store.getSnapshot().favorites).toHaveLength(1);
    window.dispatchEvent(new StorageEvent('storage', { key: null, newValue: null, storageArea: localStorage }));
    expect(store.getSnapshot().favorites).toHaveLength(0);
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
  });
});
