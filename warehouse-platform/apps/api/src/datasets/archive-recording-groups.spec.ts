import { groupArchiveRecordings } from './archive-recording-groups';
import { source } from './archive-recording-test-fixtures';

describe('native-camera continuous recording boundaries', () => {
  const epoch = Date.parse('2026-10-31T04:30:00Z');
  const part = (id: string, a: number, b: number, close = 'rotation', robot?: string, extras?: object) => source(id, epoch + a, epoch + b, close, robot, extras);
  it('joins rotations in capture order and preserves a genuine disabled-gate gap', () => {
    const groups = groupArchiveRecordings('dataset-a', '前相机', [part('b', 1040, 2000, 'garage_disabled'), part('c', 29468, 30000, 'garage_disabled'), part('a', 0, 1000)]);
    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ partCount: 2, closed: true, frameCount: 4, sourceBytes: '16' });
    expect(groups[1]).toMatchObject({ partCount: 1, closed: true, gapBeforeSeconds: 27.468 });
    expect(groups[0].sources.map(item => item.id)).toEqual(['a', 'b']);
  });
  it('keeps an unclosed rotation and a missing close reason collecting', () => {
    expect(groupArchiveRecordings('dataset-a', '前相机', [part('a', 0, 1000)])[0].closed).toBe(false);
    expect(groupArchiveRecordings('dataset-a', '前相机', [part('a', 0, 1000, '')])[0].closed).toBe(false);
  });
  it('does not join a shutdown even when the next capture starts almost immediately', () => {
    const groups = groupArchiveRecordings('dataset-a', '前相机', [part('a', 0, 1000, 'shutdown'), part('b', 1040, 2000, 'garage_disabled')]);
    expect(groups.map(item => item.partCount)).toEqual([1, 1]);
  });
  it('does not declare incomplete rotation prefixes complete when later uploads have gaps or changed dimensions', () => {
    const groups = groupArchiveRecordings('dataset-a', '前相机', [part('a', 0, 1000), part('b', 1600, 2000), part('c', 2040, 3000, 'rotation', undefined, { width: 1280 })]);
    expect(groups.map(item => item.closed)).toEqual([false, false, false]);
  });
  it('keeps robots and overlapping captures separate', () => {
    expect(groupArchiveRecordings('dataset-a', '前相机', [part('a', 0, 1000), part('b', 1040, 2000, 'shutdown', 'robot-b')])).toHaveLength(2);
    expect(groupArchiveRecordings('dataset-a', '前相机', [part('a', 0, 1000), part('b', 900, 2000, 'shutdown')])).toHaveLength(2);
  });
  it('enforces the half-second boundary with source microseconds intact', () => {
    const a = part('a', 0, 1000, 'rotation', undefined, { endAt: '2026-10-31T04:30:01.000001Z' });
    const exact = part('b', 1500, 2000, 'shutdown', undefined, { startedAt: '2026-10-31T04:30:01.500001Z' });
    const over = part('b', 1500, 2000, 'shutdown', undefined, { startedAt: '2026-10-31T04:30:01.500002Z' });
    expect(groupArchiveRecordings('dataset-a', '前相机', [a, exact])).toHaveLength(1);
    expect(groupArchiveRecordings('dataset-a', '前相机', [a, over])).toHaveLength(2);
  });
  it('excludes derived exports and rejects invalid source timestamps/frame counts', () => {
    const groups = groupArchiveRecordings('dataset-a', '前相机', [part('derived', 0, 1000, 'shutdown', undefined, { archiveRole: 'continuous-export' }), part('bad', 2000, 3000, 'shutdown', undefined, { receivedFrames: 0 })]);
    expect(groups).toHaveLength(1); expect(groups[0].error).toBeTruthy();
  });
  it('fingerprints source checksums and metadata independent of row ordering', () => {
    const a = part('a', 0, 1000), b = part('b', 1040, 2000, 'shutdown');
    const id = groupArchiveRecordings('dataset-a', '前相机', [a, b])[0].id;
    expect(groupArchiveRecordings('dataset-a', '前相机', [b, a])[0].id).toBe(id);
    expect(groupArchiveRecordings('dataset-a', '前相机', [{ ...a, checksum: 'f'.repeat(64) }, b])[0].id).not.toBe(id);
  });

  it('uses independent explicit legacy closure without changing source fingerprints or hiding gaps', () => {
    const a = part('a', 0, 1000, 'rotation', undefined, { segmentId: 'a'.repeat(32), sequenceGaps: 3 });
    const b = part('b', 2000, 3000, 'rotation', undefined, { segmentId: 'b'.repeat(32) });
    const before = groupArchiveRecordings('dataset-a', '前相机', [a, b]);
    const original = JSON.stringify([a, b], (_, value) => typeof value === 'bigint' ? value.toString() : value);
    const sessions = { ['c'.repeat(32)]: { robotId: a.robotId, startedAt: new Date(epoch).toISOString(),
      endedAt: new Date(epoch + 4000).toISOString(), reason: 'desktop_stopped', captureIncomplete: true,
      segmentIds: ['a'.repeat(32), 'b'.repeat(32)] } };
    const closed = groupArchiveRecordings('dataset-a', '前相机', [a, b], sessions);
    expect(closed.map(row => row.id)).toEqual(before.map(row => row.id));
    expect(closed.map(row => row.closed)).toEqual([true, true]);
    expect(closed.every(row => row.captureIncomplete)).toBe(true);
    expect(closed[1].gapBeforeSeconds).toBe(1);
    expect(JSON.stringify([a, b], (_, value) => typeof value === 'bigint' ? value.toString() : value)).toBe(original);
  });

  it('matches modern session IDs only inside the proven robot/time range and leaves other recordings waiting', () => {
    const sessionId = 'c'.repeat(32);
    const a = part('a', 0, 1000, 'rotation', undefined, { captureSessionId: sessionId });
    const proof = { robotId: a.robotId, startedAt: new Date(epoch).toISOString(), endedAt: new Date(epoch + 2000).toISOString(),
      reason: 'desktop_stopped', captureIncomplete: false };
    expect(groupArchiveRecordings('dataset-a', '前相机', [a], { [sessionId]: proof })[0]).toMatchObject({ closed: true, captureIncomplete: false });
    for (const value of [{ ...proof, robotId: 'foreign' }, { ...proof, endedAt: new Date(epoch + 500).toISOString() },
      { ...proof, reason: 'timeout_guess' }, { ...proof, captureIncomplete: undefined }])
      expect(groupArchiveRecordings('dataset-a', '前相机', [a], { [sessionId]: value })[0].closed).toBe(false);
    expect(groupArchiveRecordings('dataset-a', '前相机', [a], { other: proof })[0].closed).toBe(false);
  });

  it('does not join separate modern sessions or reinterpret a storage pause as complete capture', () => {
    const groups = groupArchiveRecordings('dataset-a', '前相机', [
      part('a', 0, 1000, 'rotation', undefined, { captureSessionId: 'a'.repeat(32) }),
      part('b', 1040, 2000, 'storage_paused', undefined, { captureSessionId: 'b'.repeat(32) }),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups[0].closed).toBe(false);
    expect(groups[1]).toMatchObject({ closed: true, captureIncomplete: true });
  });

  it('exposes control-session context for a clock mismatch without closing or rewriting native recordings', () => {
    const sessionId = 'd'.repeat(32);
    const original = part('native', 0, 1000, 'rotation', undefined, { captureSessionId: sessionId });
    const before = groupArchiveRecordings('dataset-a', '前相机', [original])[0];
    const proof = { robotId: original.robotId, startedAt: new Date(epoch + 3600_000).toISOString(),
      endedAt: new Date(epoch + 3700_000).toISOString(), reason: 'desktop_stopped', captureIncomplete: true };
    const displayed = groupArchiveRecordings('dataset-a', '前相机', [original], { [sessionId]: proof })[0];
    expect(displayed).toMatchObject({ id: before.id, startedAt: before.startedAt, endAt: before.endAt,
      closed: false, closureSessionId: null, captureSessionId: sessionId,
      controlSession: { startedAt: proof.startedAt, endedAt: proof.endedAt, nativeTimeOutsideSession: true } });
    expect(displayed.sources).toEqual(before.sources);
    for (const invalid of [{ ...proof, robotId: 'foreign' }, { ...proof, reason: 'timeout' }, { ...proof, startedAt: 'bad' }])
      expect(groupArchiveRecordings('dataset-a', '前相机', [original], { [sessionId]: invalid })[0].controlSession).toBeNull();
  });

  it('exposes an aligned control session while retaining normal closure rules and stable group IDs', () => {
    const sessionId = 'e'.repeat(32), original = part('native', 0, 1000, 'rotation', undefined, { captureSessionId: sessionId });
    const before = groupArchiveRecordings('dataset-a', '前相机', [original])[0];
    const proof = { robotId: original.robotId, startedAt: new Date(epoch).toISOString(),
      endedAt: new Date(epoch + 2000).toISOString(), reason: 'desktop_stopped', captureIncomplete: false };
    expect(groupArchiveRecordings('dataset-a', '前相机', [original], { [sessionId]: proof })[0]).toMatchObject({
      id: before.id, closed: true, captureSessionId: sessionId, controlSession: { nativeTimeOutsideSession: false },
    });
  });

  it('closes only explicitly inventoried modern and legacy recovery parts, while retaining gaps, IDs and native timestamps', () => {
    const sessionId = 'f'.repeat(32), segmentId = 'a'.repeat(32), legacySegmentId = 'b'.repeat(32);
    const modern = part('modern', 0, 1000, 'rotation', undefined, { captureSessionId: sessionId, segmentId });
    const legacy = part('legacy', 2000, 3000, 'rotation', undefined, { segmentId: legacySegmentId });
    const proof = { robotId: modern.robotId, startedAt: new Date(epoch).toISOString(), endedAt: new Date(epoch + 4000).toISOString(),
      reason: 'recovered_interruption', captureIncomplete: true, segmentIds: [segmentId, legacySegmentId], recoveryEvidenceSha256: 'c'.repeat(64) };
    const before = groupArchiveRecordings('dataset-a', '前相机', [modern, legacy]);
    const after = groupArchiveRecordings('dataset-a', '前相机', [modern, legacy], { [sessionId]: proof });
    expect(after.map(row => row.id)).toEqual(before.map(row => row.id));
    expect(after.every(row => row.closed && row.captureIncomplete && row.closureReason === 'recovered_interruption' && row.controlSession === null)).toBe(true);
    expect(after.map(row => [row.startedAt, row.endAt, row.gapBeforeSeconds])).toEqual(before.map(row => [row.startedAt, row.endAt, row.gapBeforeSeconds]));
    const unlisted = part('unlisted', 5000, 6000, 'rotation', undefined, { captureSessionId: sessionId, segmentId: 'd'.repeat(32) });
    const wider = { ...proof, endedAt: new Date(epoch + 7000).toISOString() };
    expect(groupArchiveRecordings('dataset-a', '前相机', [unlisted], { [sessionId]: wider })[0].closed).toBe(false);
    for (const invalid of [{ ...proof, captureIncomplete: false }, { ...proof, segmentIds: [] }, { ...proof, recoveryEvidenceSha256: '' },
      { ...proof, segmentIds: [segmentId, segmentId] }, { ...proof, robotId: 'foreign' }])
      expect(groupArchiveRecordings('dataset-a', '前相机', [modern], { [sessionId]: invalid })[0].closed).toBe(false);
    const foreign = { ...modern, metadata: { ...modern.metadata as object, captureSessionId: 'e'.repeat(32) } };
    expect(groupArchiveRecordings('dataset-a', '前相机', [foreign], { [sessionId]: proof })[0].closed).toBe(false);
  });

  it('marks a recovered interruption incomplete even when no sequence gap was measurable', () => {
    const recovered = part('recovered', 0, 1000, 'power_loss_recovered', undefined, { captureInterrupted: true, sequenceGaps: 0 });
    expect(groupArchiveRecordings('dataset-a', '前相机', [recovered])[0]).toMatchObject({ closed: true, captureIncomplete: true });
  });
});
