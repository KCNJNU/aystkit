import { describe, expect, it } from 'vitest';
import type { DatasetFolder, RobotFile } from './api';
import { archiveDirectoryCrumbs, archiveEntryLabels, archiveFolderDisplayName, archiveParentPath, canonicalArchivePath } from './archive-directory-view';

const metadata = { type: 'garage-native-archive' };
const timeSlot = '原相机前/原相机前_16:04-16:05';
const first = 'a0ef57ec33064d5abcdef01234567890';
const second = 'b0ef57ec33064d5abcdef01234567890';
const segmentPath = `${timeSlot}/${first}`;

describe('managed archive directory presentation', () => {
  it('labels each sensor with its capture date without changing its stored identity', () => {
    for (const name of ['Berxel彩色', 'Berxel深度', '原相机前', '原相机右', '原相机后', '原相机左', '定位与TF', '雷达MID360']) {
      const folder = { name, path: name, parentPath: '', createdAt: '2030-01-01T00:00:00Z' } as DatasetFolder;
      expect(archiveFolderDisplayName({ ...metadata, day: '2026-11-02', timezone: 'Asia/Shanghai' }, folder)).toBe(`${name}_2026-11-02`);
      expect(folder.path).toBe(name);
      expect(folder.name).toBe(name);
    }
  });

  it('prefers each folder real date range over the manifest day and upload time', () => {
    const folder = { name: '原相机前', path: '原相机前', parentPath: '', createdAt: '2030-01-01T00:00:00Z' } as DatasetFolder;
    expect(archiveFolderDisplayName({ ...metadata, day: '2026-11-03' }, { ...folder, captureDateRange: { from: '2026-11-01', to: '2026-11-02' } })).toBe('原相机前_2026-11-01至2026-11-02');
    expect(archiveFolderDisplayName(metadata, { ...folder, captureDateRange: { from: '2026-11-02', to: '2026-11-02' } })).toBe('原相机前_2026-11-02');
  });

  it('uses the sensor capture range for breadcrumbs while retaining the original URL paths', () => {
    const value = { ...metadata, day: '2026-11-02' };
    const folder = { id: 'front', name: '原相机前', path: '原相机前', parentPath: '', captureDateRange: { from: '2026-11-01', to: '2026-11-02' } } as DatasetFolder;
    expect(archiveDirectoryCrumbs(value, segmentPath, [folder])).toEqual([
      { label: '原相机前_2026-11-01至2026-11-02', path: '原相机前' },
      { label: '原相机前_16:04-16:05', path: timeSlot },
    ]);
    expect(archiveDirectoryCrumbs(value, segmentPath).at(0)).toEqual({ label: '原相机前_2026-11-02', path: '原相机前' });
    expect(archiveParentPath(value, segmentPath)).toBe('原相机前');
  });

  it('never invents capture dates or changes ordinary and nested folder labels', () => {
    const folder = { name: '原相机前', path: '原相机前', parentPath: '', createdAt: '2030-01-01T00:00:00Z' } as DatasetFolder;
    for (const day of [undefined, '', 'invalid', '2026-02-29', '2026-11-02T00:00:00Z', '2026-1-2']) {
      expect(archiveFolderDisplayName({ ...metadata, day }, folder)).toBe('原相机前');
    }
    expect(archiveFolderDisplayName({ type: 'other', day: '2026-11-02' }, folder)).toBe('原相机前');
    expect(archiveFolderDisplayName({ ...metadata, day: '2026-11-02' }, { ...folder, path: '用户文件夹', name: '用户文件夹' })).toBe('用户文件夹');
    expect(archiveFolderDisplayName({ ...metadata, day: '2026-11-02' }, { ...folder, path: timeSlot, name: '原相机前_16:04-16:05', parentPath: '原相机前' })).toBe('原相机前_16:04-16:05');
    expect(archiveFolderDisplayName(metadata, { ...folder, captureDateRange: { from: '2026-11-03', to: '2026-11-02' } })).toBe('原相机前');
    expect(archiveFolderDisplayName(metadata, { ...folder, captureDateRange: { from: '2026-02-29', to: '2026-03-01' } })).toBe('原相机前');
  });

  it('skips only the managed segment in breadcrumbs and parent navigation', () => {
    const nested = `${segmentPath}/compressed-bag`;
    expect(archiveDirectoryCrumbs(metadata, nested)).toEqual([
      { label: '原相机前', path: '原相机前' },
      { label: '原相机前_16:04-16:05', path: timeSlot },
      { label: 'compressed-bag', path: nested },
    ]);
    expect(archiveParentPath(metadata, nested)).toBe(timeSlot);
    expect(archiveParentPath(metadata, segmentPath)).toBe('原相机前');
    expect(canonicalArchivePath(metadata, segmentPath)).toBe(timeSlot);
    expect(canonicalArchivePath(metadata, nested)).toBe(nested);
  });

  it('leaves ordinary hashes, other metadata and invalid archive time slots alone', () => {
    for (const value of [undefined, null, [], { type: 'other' }]) {
      expect(canonicalArchivePath(value, segmentPath)).toBe(segmentPath);
      expect(archiveDirectoryCrumbs(value, segmentPath).at(-1)?.label).toBe(first);
      expect(archiveParentPath(value, segmentPath)).toBe(timeSlot);
    }
    for (const path of [`images/raw/${first}`, `原相机前/原相机前_24:04-16:05/${first}`, `原相机前/其他相机_16:04-16:05/${first}`, `${timeSlot}/${first.toUpperCase()}`, `${timeSlot}/short-hash`]) {
      expect(canonicalArchivePath(metadata, path)).toBe(path);
      expect(archiveDirectoryCrumbs(metadata, path)).toHaveLength(3);
    }
  });

  it('keeps separate identities and adds Beijing recording times and segment labels', () => {
    const files = [
      { id: 'one', originalName: 'video.mkv', folderPath: segmentPath, capturedAt: '2026-10-28T08:04:05Z', metadata: { startedAt: '2026-10-28T08:04:01Z' } },
      { id: 'two', originalName: 'video.mkv', folderPath: `${timeSlot}/${second}`, capturedAt: '2026-10-28T08:04:35Z' },
      { id: 'three', originalName: 'video.mkv', folderPath: segmentPath, capturedAt: 'invalid', metadata: { startedAt: 1793174641 } },
      { id: 'four', originalName: 'video.mkv', folderPath: segmentPath, capturedAt: 'invalid', metadata: { startedAt: 'invalid' } },
    ] as RobotFile[];
    const folders = [
      { id: 'bag-one', name: 'compressed-bag', path: `${segmentPath}/compressed-bag` },
      { id: 'bag-two', name: 'compressed-bag', path: `${timeSlot}/${second}/compressed-bag` },
    ] as DatasetFolder[];
    const labels = archiveEntryLabels(metadata, files, folders);
    expect(labels.fileLabels.get('one')).toBe('片段 1 · 2026-10-28 16:04:01（北京时间）');
    expect(labels.fileLabels.get('two')).toBe('片段 2 · 2026-10-28 16:04:35（北京时间）');
    expect(labels.fileLabels.get('three')).toBe('片段 1 · 2026-10-28 16:04:01（北京时间）');
    expect(labels.fileLabels.get('four')).toBe('片段 1');
    expect(labels.folderLabels.get('bag-one')).toBe('片段 1');
    expect(labels.folderLabels.get('bag-two')).toBe('片段 2');
    expect(files.map(file => file.id)).toEqual(['one', 'two', 'three', 'four']);
    expect(files.every(file => file.originalName === 'video.mkv')).toBe(true);
    expect(archiveEntryLabels(undefined, files, folders).fileLabels.size).toBe(0);
  });
});
