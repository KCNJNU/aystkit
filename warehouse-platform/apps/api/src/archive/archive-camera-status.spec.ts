import { ArchiveCameraMonitor } from './archive-camera-status';

const NOW = Date.parse('2026-10-30T06:00:00Z');
const iso = (offset = 0) => new Date(NOW + offset).toISOString();
const gate = (enabled = true) => ({ schemaVersion: 1, scope: 'garage', enabled, updatedAt: iso(-1000) });
function source(offset = 0, written = 10, enabled = true) {
  return {
    updatedAt: iso(offset), connected: true, gateAcknowledged: true,
    gateEnabled: enabled, gateRevision: iso(-1000),
    capture: { updatedAt: iso(offset), cameras: {
      camera2: { state: 'capturing', frames: 100 + written, ageS: 0.1 },
      camera1: { state: 'capturing', frames: 200 + written, ageS: 0.1 },
      berxelColor: { state: 'capturing', frames: 30, ageS: 0.1 },
    } },
    camera: { updatedAt: iso(offset), store: { enabled,
      cameras: { camera2: { writtenFrames: written }, camera1: { writtenFrames: 8 } },
      active: { camera2: { frames: written }, camera1: { frames: 8 } }, errors: {} } },
    recorder: { updatedAt: iso(offset), ros: { active: enabled, epoch: 1, topics: {
      '/berxel/color/image_raw': { sensor: 'Berxel彩色', state: 'AVAILABLE', ageS: 0.1, received: 30, written: 20, dropped: 2 },
      '/berxel/color/camera_info': { sensor: 'Berxel彩色', state: 'AVAILABLE', ageS: 0.1, received: 500, written: 400, dropped: 1 },
    } }, store: { errors: {} } },
  };
}
const row = (rows: ReturnType<ArchiveCameraMonitor['snapshot']>, id = 'camera2') => rows.find(item => item.id === id)!;

describe('per-camera read-only archive evidence', () => {
  it('always exposes six fixed camera rows without inventing zero counters', () => {
    const rows = new ArchiveCameraMonitor().snapshot(null, null, NOW);
    expect(rows.map(item => item.id)).toEqual(['camera2', 'camera1', 'camera3', 'camera4', 'berxelColor', 'berxelDepth']);
    for (const item of rows) expect(item).toMatchObject({ acquisition: 'unknown', recording: 'unknown',
      receivedFrames: null, writtenFrames: null, droppedFrames: null });
  });
  it('detects actual camera capture while the archive switch is off', () => {
    const result = row(new ArchiveCameraMonitor().snapshot(source(0, 10, false), gate(false), NOW));
    expect(result).toMatchObject({ name: '前相机', acquisition: 'capturing', recording: 'disabled', receivedFrames: 110 });
  });
  it('requires independent actual write progress instead of active segments or cumulative counts', () => {
    const monitor = new ArchiveCameraMonitor();
    const initial = monitor.snapshot(source(), gate(), NOW);
    expect(row(initial)).toMatchObject({ acquisition: 'capturing', recording: 'waiting', writtenFrames: 10 });
    const next = monitor.snapshot(source(2000, 11), gate(), NOW + 2000);
    expect(row(next).recording).toBe('recording');
    expect(row(next, 'camera1').recording).toBe('waiting');
  });
  it('does not renew writing evidence by repeatedly reading one unchanged report', () => {
    const monitor = new ArchiveCameraMonitor();
    monitor.snapshot(source(), gate(), NOW);
    monitor.snapshot(source(2000, 11), gate(), NOW + 2000);
    const value = source(2000, 11);
    value.capture.updatedAt = iso(13_000);
    expect(row(monitor.snapshot(value, gate(), NOW + 13_000)).recording).toBe('waiting');
  });
  it('does not accept a changed counter under the same report timestamp as new write progress', () => {
    const monitor = new ArchiveCameraMonitor();
    monitor.snapshot(source(), gate(), NOW);
    expect(row(monitor.snapshot(source(0, 11), gate(), NOW)).recording).toBe('waiting');
  });
  it('resets progression after a source restart or counter rollback', () => {
    const monitor = new ArchiveCameraMonitor();
    monitor.snapshot(source(), gate(), NOW);
    expect(row(monitor.snapshot(source(2000, 11), gate(), NOW + 2000)).recording).toBe('recording');
    expect(row(monitor.snapshot(source(4000, 1), gate(), NOW + 4000)).recording).toBe('waiting');
    expect(row(monitor.snapshot(source(6000, 2), gate(), NOW + 6000)).recording).toBe('recording');
  });
  it('does not claim a disconnected source stopped recording or show stale counters as current', () => {
    const value = source(); value.connected = false;
    expect(row(new ArchiveCameraMonitor().snapshot(value, gate(), NOW))).toMatchObject({
      acquisition: 'stale', recording: 'stale', receivedFrames: null, writtenFrames: null, droppedFrames: null });
  });
  it('does not claim recording disabled until the source acknowledges the exact off-switch revision', () => {
    const value = source();
    const result = row(new ArchiveCameraMonitor().snapshot(value, gate(false), NOW));
    expect(result.recording).toBe('unknown');
    expect(result.detail).toContain('尚未确认');
  });
  it('keeps stale positive frame counts from being considered current capture', () => {
    const value = source(); value.capture.cameras.camera2.ageS = 60;
    expect(row(new ArchiveCameraMonitor().snapshot(value, gate(), NOW))).toMatchObject({ acquisition: 'stale', recording: 'waiting' });
    value.capture.updatedAt = iso(-60_000);
    expect(row(new ArchiveCameraMonitor().snapshot(value, gate(), NOW))).toMatchObject({ acquisition: 'stale', receivedFrames: null });
  });
  it('counts only actual depth color images, not CameraInfo messages, and requires depth write progress', () => {
    const monitor = new ArchiveCameraMonitor();
    const value = source();
    const first = row(monitor.snapshot(value, gate(), NOW), 'berxelColor');
    expect(first).toMatchObject({ receivedFrames: 30, writtenFrames: 20, droppedFrames: 2, recording: 'waiting' });
    const next = source(2000);
    next.recorder.ros.topics['/berxel/color/image_raw'].written = 21;
    expect(row(monitor.snapshot(next, gate(), NOW + 2000), 'berxelColor').recording).toBe('recording');
    expect(row(monitor.snapshot(next, gate(), NOW + 2000), 'berxelDepth').acquisition).toBe('unknown');
  });
  it('reports relevant writer and storage failures without leaking raw paths', () => {
    const value = source();
    value.camera.store.errors = { storage: 'private /home/source/file.raw is full' };
    const result = row(new ArchiveCameraMonitor().snapshot(value, gate(), NOW));
    expect(result.recording).toBe('error');
    expect(result.detail).not.toContain('/home');
  });
  it('rejects invalid numerical evidence without converting it to zero', () => {
    const value = source();
    value.capture.cameras.camera2.frames = -1;
    value.camera.store.cameras.camera2.writtenFrames = NaN;
    expect(row(new ArchiveCameraMonitor().snapshot(value, gate(), NOW))).toMatchObject({
      acquisition: 'stale', receivedFrames: null, writtenFrames: null });
  });
});
