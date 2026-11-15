import { archiveCaptureDateRange, archiveDatasetDateRange, archiveSegmentPaths, isArchiveTimeSlot, mergeCaptureDateRange } from './archive-directory-view';

const archive = { type: 'garage-native-archive' };
const parent = '原相机前/原相机前_16:04-16:05';

describe('archive directory presentation', () => {
  it('recognizes only marked archive datasets and the exact sensor time-slot level', () => {
    expect(isArchiveTimeSlot(archive, parent)).toBe(true);
    expect(isArchiveTimeSlot(archive, '雷达MID360/雷达MID360_23:59-00:00')).toBe(true);
    for (const metadata of [null, {}, [], { type: 'user-created' }]) expect(isArchiveTimeSlot(metadata, parent)).toBe(false);
    for (const path of ['', '原相机前', parent + '/a'.repeat(32), '原相机前/其他_16:04-16:05', '原相机前/原相机前_25:00-25:01', '原相机前/原相机前_16:99-17:00', '原相机前/原相机前_16:04-16:05/notes']) {
      expect(isArchiveTimeSlot(archive, path)).toBe(false);
    }
  });
  it('matches exactly generated segment names and their actual immediate paths', () => {
    const id = 'a'.repeat(32);
    const folder = { name: id, path: parent + '/' + id, parentPath: parent };
    expect(archiveSegmentPaths(parent, [folder])).toEqual([folder.path]);
    expect(archiveSegmentPaths(parent, [
      { ...folder, name: 'notes' }, { ...folder, name: id.toUpperCase() },
      { ...folder, parentPath: 'other' }, { ...folder, path: 'other/' + id },
      { ...folder, name: id.slice(1) },
    ])).toEqual([]);
  });
});

describe('archive capture date labels', () => {
  it('uses source capture boundaries in Shanghai rather than the later uploaded capture fallback', () => {
    expect(archiveCaptureDateRange({ startedAt: '2026-11-01T15:59:00Z', endAt: '2026-11-01T16:01:00Z' },
      new Date('2026-11-03T01:00:00Z'))).toEqual({ from: '2026-11-01', to: '2026-11-02' });
  });
  it('falls back to capturedAt and never reads createdAt', () => {
    expect(archiveCaptureDateRange({ createdAt: '2026-11-03T00:00:00Z' }, new Date('2026-11-01T16:00:00Z')))
      .toEqual({ from: '2026-11-02', to: '2026-11-02' });
    expect(archiveCaptureDateRange({ createdAt: '2026-11-03T00:00:00Z' }, undefined)).toBeUndefined();
  });
  it('accepts one valid source endpoint and rejects malformed or inverted boundaries', () => {
    expect(archiveCaptureDateRange({ endAt: '2026-11-01T16:01:00+00:00' }, undefined))
      .toEqual({ from: '2026-11-02', to: '2026-11-02' });
    expect(archiveCaptureDateRange({ startedAt: '2026-02-30T00:00:00Z' }, undefined)).toBeUndefined();
    expect(archiveCaptureDateRange({ startedAt: '2026-11-03T00:00:00Z', endAt: '2026-11-01T00:00:00Z' },
      new Date('2026-11-02T00:00:00Z'))).toEqual({ from: '2026-11-02', to: '2026-11-02' });
    expect(archiveCaptureDateRange([], 'not a date')).toBeUndefined();
  });
  it('uses only a valid explicit archive dataset day for an empty folder', () => {
    expect(archiveDatasetDateRange({ ...archive, day: '2026-11-02' }))
      .toEqual({ from: '2026-11-02', to: '2026-11-02' });
    for (const metadata of [{ day: '2026-11-02' }, { ...archive, day: '2026-02-30' }, { ...archive, day: '11/02/2026' }, []])
      expect(archiveDatasetDateRange(metadata)).toBeUndefined();
  });
  it('merges actual source date ranges across descendants', () => {
    expect(mergeCaptureDateRange({ from: '2026-11-02', to: '2026-11-04' }, { from: '2026-11-01', to: '2026-11-03' }))
      .toEqual({ from: '2026-11-01', to: '2026-11-04' });
  });
});
