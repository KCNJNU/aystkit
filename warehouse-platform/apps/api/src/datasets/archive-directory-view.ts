/** Presentation-only archive paths. Physical folders and object keys stay intact. */
export function isArchiveTimeSlot(metadata: unknown, path: string): boolean {
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)
      || (metadata as { type?: unknown }).type !== 'garage-native-archive') return false;
  const pieces = path.split('/');
  if (pieces.length !== 2 || !pieces[0] || !pieces[1].startsWith(pieces[0] + '_')) return false;
  return /^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$/.test(pieces[1].slice(pieces[0].length + 1));
}

export function archiveSegmentPaths(path: string, folders: ReadonlyArray<{ name: string; path: string; parentPath: string }>): string[] {
  return folders.filter(folder => /^[a-f0-9]{32}$/.test(folder.name)
    && folder.parentPath === path && folder.path === path + '/' + folder.name)
    .map(folder => folder.path);
}

export type CaptureDateRange = { from: string; to: string };

const captureDay = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
});

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

/** Capture dates describe the recorded scene, never its later upload time. */
export function archiveCaptureDateRange(metadata: unknown, capturedAt: unknown): CaptureDateRange | undefined {
  const source = record(metadata);
  const instant = (value: unknown) => {
    if (value instanceof Date) return value.getTime();
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return NaN;
    const day = new Date(value.slice(0, 10) + 'T00:00:00Z');
    if (!Number.isFinite(day.getTime()) || day.toISOString().slice(0, 10) !== value.slice(0, 10)) return NaN;
    return Date.parse(value);
  };
  const captured = instant(capturedAt), started = instant(source.startedAt), ended = instant(source.endAt);
  let from = Number.isFinite(started) ? started : Number.isFinite(ended) ? ended : captured;
  let to = Number.isFinite(ended) ? ended : from;
  if (to < from) { from = captured; to = captured; }
  if (!Number.isFinite(from) || !Number.isFinite(to)) return undefined;
  return { from: captureDay.format(new Date(from)), to: captureDay.format(new Date(to)) };
}

export function archiveDatasetDateRange(metadata: unknown): CaptureDateRange | undefined {
  const source = record(metadata), day = source.day;
  if (source.type !== 'garage-native-archive' || typeof day !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return undefined;
  const date = new Date(day + 'T00:00:00Z');
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== day) return undefined;
  return { from: day, to: day };
}

export function mergeCaptureDateRange(previous: CaptureDateRange | undefined, next: CaptureDateRange): CaptureDateRange {
  return previous ? { from: previous.from < next.from ? previous.from : next.from,
    to: previous.to > next.to ? previous.to : next.to } : next;
}
