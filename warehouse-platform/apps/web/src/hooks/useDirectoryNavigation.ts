import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useNavigationType } from 'react-router-dom';

type Entry = { key: string; pathname: string; search: string; hash: string };
type DirectoryHistory = { entries: Entry[]; index: number };

function sameEntry(first: Entry | undefined, second: Entry) {
  return first?.key === second.key && first.pathname === second.pathname
    && first.search === second.search && first.hash === second.hash;
}

function recordLocation(history: DirectoryHistory, entry: Entry, action: string): DirectoryHistory {
  const current = history.entries[history.index];
  if (sameEntry(current, entry)) return history;
  // A new dataset, a remount, or a POP to an unobserved entry establishes a new
  // boundary. Never infer browser history from history.length or a router idx.
  if (current?.pathname !== entry.pathname) return { entries: [entry], index: 0 };
  if (action === 'POP') {
    const index = history.entries.findIndex(known => sameEntry(known, entry));
    return index >= 0 ? { ...history, index } : { entries: [entry], index: 0 };
  }
  if (action === 'REPLACE') {
    return { ...history, entries: history.entries.map((known, index) => index === history.index ? entry : known) };
  }
  return { entries: [...history.entries.slice(0, history.index + 1), entry], index: history.index + 1 };
}

/** Back/forward only through directory entries observed during this mount. */
export function useDirectoryNavigation() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const navigate = useNavigate();
  const pendingEntry = useRef<string | null>(null);
  const [history, setHistory] = useState<DirectoryHistory>(() => ({
    entries: [{ key: location.key, pathname: location.pathname, search: location.search, hash: location.hash }], index: 0,
  }));

  useLayoutEffect(() => {
    pendingEntry.current = null;
    const entry = { key: location.key, pathname: location.pathname, search: location.search, hash: location.hash };
    setHistory(previous => recordLocation(previous, entry, navigationType));
  }, [location.key, location.pathname, location.search, location.hash, navigationType]);

  // During a location transition, the old history must not enable a button
  // against the new browser entry before the layout effect records it.
  const current = { key: location.key, pathname: location.pathname, search: location.search, hash: location.hash };
  const synced = sameEntry(history.entries[history.index], current);
  const canBack = synced && history.index > 0
    && history.entries[history.index - 1].pathname === location.pathname;
  const canForward = synced && history.index + 1 < history.entries.length
    && history.entries[history.index + 1].pathname === location.pathname;
  // Native history traversal completes on a later POP event. Ignore another
  // click until it arrives so rapid clicks cannot jump past a known boundary.
  const back = useCallback(() => {
    if (!canBack || pendingEntry.current === location.key) return;
    pendingEntry.current = location.key;
    void navigate(-1);
  }, [canBack, location.key, navigate]);
  const forward = useCallback(() => {
    if (!canForward || pendingEntry.current === location.key) return;
    pendingEntry.current = location.key;
    void navigate(1);
  }, [canForward, location.key, navigate]);

  return { back, forward, canBack, canForward };
}
