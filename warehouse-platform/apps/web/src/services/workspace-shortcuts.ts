export type WorkspaceVisit = {
  datasetId: string;
  datasetName: string;
  path?: string;
  captureSession?: string;
  sessionLabel?: string;
  robotId?: string;
};
export type WorkspaceShortcut = WorkspaceVisit & {
  key: string;
  url: string;
  kind: 'dataset' | 'folder' | 'capture-session';
  robotId: string;
  label: string;
  updatedAt: number;
};
export type WorkspaceShortcuts = { favorites: WorkspaceShortcut[]; recent: WorkspaceShortcut[] };
export const MAX_WORKSPACE_FAVORITES = 50;
export const MAX_WORKSPACE_RECENT = 30;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const sessionPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
// eslint-disable-next-line no-control-regex -- Reject ASCII control characters in persisted navigation paths.
const control = /[\u0000-\u001f\u007f]/;
const empty: WorkspaceShortcuts = { favorites: [], recent: [] };
// eslint-disable-next-line no-control-regex -- Persisted labels intentionally strip ASCII control characters.
const cleanLabel = (value: unknown) => typeof value === 'string' ? value.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, 200) : '';
const validPath = (path: string) => path.length <= 2048 && !control.test(path) && !path.includes('\\') && (!path || path.split('/').every(part => part.length > 0 && part.length <= 255 && part !== '.' && part !== '..'));

/** Only navigable warehouse positions are persisted; never signed URLs or arbitrary routes. */
export function parseWorkspaceUrl(value: unknown): { datasetId: string; robotId: string; path: string; captureSession?: string } | null {
  if (typeof value !== 'string' || value.length > 4096 || !value.startsWith('/datasets/') || value.includes('\\') || control.test(value)) return null;
  try {
    const url = new URL(value, 'https://warehouse.invalid');
    const datasetId = /^\/datasets\/([a-f0-9-]+)$/i.exec(url.pathname)?.[1];
    if (url.origin !== 'https://warehouse.invalid' || url.hash || !datasetId || !uuid.test(datasetId)) return null;
    if (value.split('?')[0] !== `/datasets/${datasetId}`) return null;
    if ([...url.searchParams.keys()].some(key => !['robotId', 'path', 'captureSession'].includes(key) || url.searchParams.getAll(key).length !== 1)) return null;
    const robotId = url.searchParams.get('robotId');
    const path = url.searchParams.get('path') || '';
    const captureSession = url.searchParams.get('captureSession') ?? undefined;
    if (!robotId || (robotId !== 'all' && !uuid.test(robotId)) || !validPath(path) || (captureSession !== undefined && !sessionPattern.test(captureSession))) return null;
    return { datasetId: datasetId.toLowerCase(), robotId: robotId === 'all' ? 'all' : robotId.toLowerCase(), path, captureSession };
  } catch { return null; }
}

export function makeWorkspaceShortcut(input: WorkspaceVisit, robotId: string, now = Date.now()): WorkspaceShortcut | null {
  if (!uuid.test(input.datasetId) || !cleanLabel(input.datasetName)) return null;
  const scope = robotId || 'all';
  const path = input.path ?? '';
  if ((scope !== 'all' && !uuid.test(scope)) || !validPath(path) || (input.captureSession !== undefined && !sessionPattern.test(input.captureSession))) return null;
  const params = new URLSearchParams({ robotId: scope.toLowerCase() });
  if (path) params.set('path', path);
  if (input.captureSession) params.set('captureSession', input.captureSession);
  const url = `/datasets/${input.datasetId.toLowerCase()}?${params}`;
  return {
    key: url, url, datasetId: input.datasetId.toLowerCase(), datasetName: cleanLabel(input.datasetName), path,
    captureSession: input.captureSession, sessionLabel: cleanLabel(input.sessionLabel) || undefined,
    robotId: scope.toLowerCase(), kind: input.captureSession ? 'capture-session' : path ? 'folder' : 'dataset',
    label: input.captureSession ? cleanLabel(input.sessionLabel) || input.captureSession : path ? path.split('/').at(-1)! : cleanLabel(input.datasetName),
    updatedAt: Number.isFinite(now) && now >= 0 ? now : 0,
  };
}

export function workspaceShortcutsKey(userId: string) { return `warehouse-workspace-shortcuts-v1:${encodeURIComponent(userId)}`; }

function normalizeShortcut(entry: WorkspaceShortcut) {
  const route = parseWorkspaceUrl(entry.url);
  if (!route) return null;
  return makeWorkspaceShortcut({ ...route, datasetName: entry.datasetName, sessionLabel: entry.sessionLabel }, route.robotId, entry.updatedAt);
}

export function parseWorkspaceShortcuts(raw: string | null): WorkspaceShortcuts {
  if (!raw || raw.length > 300_000) return { ...empty };
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || (parsed as { version?: unknown }).version !== 1) return { ...empty };
    const read = (items: unknown, limit: number) => {
      const seen = new Set<string>();
      if (!Array.isArray(items)) return [];
      return items.slice(0, 200).flatMap((item: unknown) => {
        if (!item || typeof item !== 'object') return [];
        const value = item as Record<string, unknown>;
        const route = parseWorkspaceUrl(value.url);
        if (!route || typeof value.datasetName !== 'string' || typeof value.updatedAt !== 'number') return [];
        const entry = makeWorkspaceShortcut({ ...route, datasetName: value.datasetName, sessionLabel: typeof value.sessionLabel === 'string' ? value.sessionLabel : undefined }, route.robotId, value.updatedAt);
        if (!entry || seen.has(entry.key)) return [];
        seen.add(entry.key); return [entry];
      }).slice(0, limit);
    };
    const data = parsed as Record<string, unknown>;
    return { favorites: read(data.favorites, MAX_WORKSPACE_FAVORITES), recent: read(data.recent, MAX_WORKSPACE_RECENT) };
  } catch { return { ...empty }; }
}

/** Each account gets a separate store. Storage denial falls back to this tab's memory. */
export function createWorkspaceShortcutsStore(userId: string, storage: Pick<Storage, 'getItem' | 'setItem'> | null = (() => { try { return window.localStorage; } catch { return null; } })()) {
  const key = workspaceShortcutsKey(userId);
  let state: WorkspaceShortcuts;
  try { state = parseWorkspaceShortcuts(storage?.getItem(key) ?? null); } catch { state = { ...empty }; }
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach(listener => listener());
  const update = (next: WorkspaceShortcuts) => {
    state = next;
    try { storage?.setItem(key, JSON.stringify({ version: 1, ...next })); } catch { /* Tab state remains available. */ }
    emit();
  };
  const onStorage = (event: StorageEvent) => {
    if (event.key !== key && event.key !== null) return;
    if (event.storageArea && event.storageArea !== storage) return;
    state = parseWorkspaceShortcuts(event.key === null ? null : event.newValue); emit();
  };
  return {
    key,
    getSnapshot: () => state,
    subscribe: (listener: () => void) => {
      if (!listeners.size) window.addEventListener('storage', onStorage);
      listeners.add(listener);
      return () => { listeners.delete(listener); if (!listeners.size) window.removeEventListener('storage', onStorage); };
    },
    record: (input: WorkspaceShortcut) => {
      const entry = normalizeShortcut(input);
      if (entry) update({ ...state, recent: [entry, ...state.recent.filter(item => item.key !== entry.key)].slice(0, MAX_WORKSPACE_RECENT) });
    },
    toggleFavorite: (input: WorkspaceShortcut) => {
      const entry = normalizeShortcut(input);
      if (!entry) return false;
      if (state.favorites.some(item => item.key === entry.key)) { update({ ...state, favorites: state.favorites.filter(item => item.key !== entry.key) }); return true; }
      if (state.favorites.length >= MAX_WORKSPACE_FAVORITES) return false;
      update({ ...state, favorites: [entry, ...state.favorites] }); return true;
    },
    removeFavorite: (entryKey: string) => update({ ...state, favorites: state.favorites.filter(item => item.key !== entryKey) }),
    removeRecent: (entryKey: string) => update({ ...state, recent: state.recent.filter(item => item.key !== entryKey) }),
    clearRecent: () => update({ ...state, recent: [] }),
  };
}
export type WorkspaceShortcutsStore = ReturnType<typeof createWorkspaceShortcutsStore>;
