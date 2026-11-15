import type { DatasetFolder, RobotFile } from './api';

type Crumb = { label: string; path: string };
const cameraSensors = new Set(['Berxel彩色', 'Berxel深度', '原相机前', '原相机后', '原相机左', '原相机右',
  '原始相机_前', '原始相机_后', '原始相机_左', '原始相机_右']);
const archiveSensors = new Set([...cameraSensors, 'Berxel点云', '二维雷达scan', '二维雷达蓝海', '定位与TF', '底盘遥测', '覆盖任务', '诊断日志', '雷达MID360']);

function calendarDay(value: unknown): string | undefined {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return undefined;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? value : undefined;
}

/** Capture dates are labels only; navigation and package requests retain stored paths. */
export function archiveFolderDisplayName(metadata: unknown, folder: Pick<DatasetFolder, 'name' | 'path' | 'parentPath' | 'captureDateRange'>): string {
  if (!isArchiveSensorDirectory(metadata, folder.path) || folder.parentPath !== '') return folder.name;
  const from = calendarDay(folder.captureDateRange?.from), to = calendarDay(folder.captureDateRange?.to);
  if (from && to && from <= to) return `${folder.name}_${from}${from === to ? '' : `至${to}`}`;
  // Older servers expose the manifest's capture day on the dataset. Never infer it
  // from folder creation/upload time, the browser clock, or the dataset name.
  const day = metadata && typeof metadata === 'object' && !Array.isArray(metadata)
    ? calendarDay((metadata as { day?: unknown }).day) : undefined;
  return day ? `${folder.name}_${day}` : folder.name;
}

export function isArchiveCameraDirectory(metadata: unknown, path: string) {
  return Boolean(metadata && typeof metadata === 'object' && !Array.isArray(metadata)
    && (metadata as { type?: unknown }).type === 'garage-native-archive' && cameraSensors.has(path));
}

/** A garage sensor opens its complete recordings before the stored part folders. */
export function isArchiveSensorDirectory(metadata: unknown, path: string) {
  return Boolean(metadata && typeof metadata === 'object' && !Array.isArray(metadata)
    && (metadata as { type?: unknown }).type === 'garage-native-archive'
    && archiveSensors.has(path));
}

function isArchiveTimeSlot(metadata: unknown, path: string) {
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)
    || (metadata as { type?: unknown }).type !== 'garage-native-archive') return false;
  const pieces = path.split('/');
  return pieces.length === 2 && Boolean(pieces[0]) && pieces[1].startsWith(pieces[0] + '_')
    && /^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$/.test(pieces[1].slice(pieces[0].length + 1));
}

function archiveSegmentPath(metadata: unknown, path: string) {
  const pieces = path.split('/');
  return pieces.length >= 3 && isArchiveTimeSlot(metadata, pieces.slice(0, 2).join('/'))
    && /^[a-f0-9]{32}$/.test(pieces[2]) ? pieces.slice(0, 3).join('/') : undefined;
}

/** Logical navigation only: returned paths still refer to the real stored folders. */
export function archiveDirectoryCrumbs(metadata: unknown, path: string, sensorFolders: DatasetFolder[] = []): Crumb[] {
  const segment = archiveSegmentPath(metadata, path);
  return (path ? path.split('/') : []).flatMap((label, index, pieces) =>
    segment && index === 2 ? [] : [{
      label: index === 0 ? archiveFolderDisplayName(metadata,
        sensorFolders.find(folder => folder.path === label && folder.parentPath === '')
          ?? { name: label, path: label, parentPath: '' }) : label,
      path: pieces.slice(0, index + 1).join('/'),
    }]);
}

export function archiveParentPath(metadata: unknown, path: string) {
  return archiveDirectoryCrumbs(metadata, path).slice(0, -1).at(-1)?.path ?? '';
}

/** Old links to a managed segment open the time slot without adding a history step. */
export function canonicalArchivePath(metadata: unknown, path: string) {
  return archiveSegmentPath(metadata, path) === path ? path.split('/').slice(0, 2).join('/') : path;
}

function captureTime(value: unknown): string | undefined {
  if (typeof value !== 'string' && typeof value !== 'number') return undefined;
  if (typeof value === 'string' && !value.trim()) return undefined;
  const numeric = typeof value === 'number' ? value : /^\d+(?:\.\d+)?$/.test(value) ? Number(value) : undefined;
  const date = new Date(numeric === undefined ? value : numeric < 1e12 ? numeric * 1000 : numeric);
  if (!Number.isFinite(date.getTime())) return undefined;
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find(item => item.type === type)?.value;
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}`;
}

/** Keep every original ID/name; captions distinguish files and lifted folders. */
export function archiveEntryLabels(metadata: unknown, files: RobotFile[], folders: DatasetFolder[]) {
  const segments = [...new Set([...files.map(file => file.folderPath ?? ''), ...folders.map(folder => folder.path)]
    .map(path => archiveSegmentPath(metadata, path)).filter((path): path is string => Boolean(path)))].sort();
  const segmentLabel = (path: string) => {
    const segment = archiveSegmentPath(metadata, path);
    return segment ? `片段 ${segments.indexOf(segment) + 1}` : undefined;
  };
  const fileLabels = new Map<string, string>();
  for (const file of files) {
    const label = segmentLabel(file.folderPath ?? '');
    if (!label) continue;
    const details = file.metadata && typeof file.metadata === 'object' && !Array.isArray(file.metadata)
      ? file.metadata as { startedAt?: unknown } : undefined;
    const startedAt = captureTime(details?.startedAt) ?? captureTime(file.capturedAt);
    fileLabels.set(file.id, startedAt ? `${label} · ${startedAt}（北京时间）` : label);
  }
  const folderLabels = new Map<string, string>();
  for (const folder of folders) {
    const label = segmentLabel(folder.path);
    if (label && folders.some(other => other.id !== folder.id && other.name === folder.name)) folderLabels.set(folder.id, label);
  }
  return { fileLabels, folderLabels };
}
