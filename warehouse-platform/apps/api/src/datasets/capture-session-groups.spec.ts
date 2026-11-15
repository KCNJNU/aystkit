import type { RobotFile } from '@prisma/client';
import { groupCaptureSessions } from './capture-session-groups';

const robotA = '11111111-1111-4111-8111-111111111111';
const robotB = '22222222-2222-4222-8222-222222222222';
const sessionA = 'a'.repeat(32), sessionB = 'b'.repeat(32), segment = 'c'.repeat(32);
const file = (id: string, metadata: object = {}, robotId = robotA) => ({ id, datasetId: 'dataset', robotId,
  folderPath: '前相机/slot', originalName: 'video.mkv', fileSize: 12n,
  capturedAt: new Date('2026-11-10T00:00:00Z'), metadata, recognitionType: 'garage-native-camera' }) as RobotFile;
const proof = (segmentIds: string[], robotId = robotA) => ({ robotId, segmentIds,
  startedAt: '2026-11-10T00:00:00Z', endedAt: '2026-11-10T00:01:00Z', reason: 'desktop_stopped', captureIncomplete: false });

describe('exact capture-session membership', () => {
  it('joins camera, lidar and related files by token, keeping same-time sessions and other robots separate', () => {
    const result = groupCaptureSessions([file('camera', { captureSessionId: sessionA }),
      { ...file('lidar', { captureSessionId: sessionA }), folderPath: 'MID360/slot', originalName: 'data.db3.zst' },
      { ...file('yaml', { captureSessionId: sessionA }), folderPath: 'MID360/slot', originalName: 'metadata.yaml' },
      file('next', { captureSessionId: sessionB }), file('other-robot', { captureSessionId: sessionA }, robotB)], {});
    expect(result.sessions).toHaveLength(3);
    const selected = result.sessions.find(item => item.sessionId === sessionA && item.robotId === robotA)!;
    expect(selected.files.map(item => item.id)).toEqual(['camera', 'lidar', 'yaml']);
    expect(selected.sensors).toEqual(expect.arrayContaining([
      { name: 'MID360', fileCount: 2, totalBytes: '24' }, { name: '前相机', fileCount: 1, totalBytes: '12' },
    ]));
  });
  it('recovers only legacy members sharing an explicit robot/segment claim; never uses timestamps', () => {
    const result = groupCaptureSessions([file('anchor', { captureSessionId: sessionA, segmentId: segment }),
      file('index', { segmentId: segment }), file('same-time', {}), file('other-robot', { segmentId: segment }, robotB)], {});
    expect(result.sessions[0].files.map(item => item.id)).toEqual(['anchor', 'index']);
    expect(result.unassigned.map(item => item.id)).toEqual(['same-time', 'other-robot']);
  });
  it('accepts explicit validated proof segment mapping but leaves conflicting legacy mappings unassigned', () => {
    const legacy = file('legacy', { segmentId: segment });
    expect(groupCaptureSessions([legacy], { [sessionA]: proof([segment]) }).sessions[0].files).toEqual([legacy]);
    const ambiguous = groupCaptureSessions([legacy], { [sessionA]: proof([segment]), [sessionB]: proof([segment]) });
    expect(ambiguous.sessions).toEqual([]); expect(ambiguous.unassigned).toEqual([legacy]);
  });
  it('does not replace malformed tokens, trust invalid proofs or infer a session from a bare end time', () => {
    const rows = [file('malformed', { captureSessionId: 'bad', segmentId: segment }), file('legacy', { segmentId: segment })];
    const result = groupCaptureSessions(rows, { [sessionA]: { ...proof([segment]), reason: 'untrusted' } });
    expect(result.sessions).toEqual([]); expect(result.unassigned).toEqual(rows);
    expect(groupCaptureSessions(rows, { [sessionA]: proof([segment]) }).unassigned.map(item => item.id)).toEqual(['malformed']);
  });
  it('omits duplicate continuous exports without changing any source metadata or closure state', () => {
    const source = file('raw', { captureSessionId: sessionA, closeReason: 'rotation' });
    const before = structuredClone(source);
    const result = groupCaptureSessions([source, { ...source, id: 'export', recognitionType: 'garage-continuous-video' }], {});
    expect(result.sessions[0].files.map(item => item.id)).toEqual(['raw']);
    expect(result.excludedDerivedFileCount).toBe(1); expect(source).toEqual(before);
  });
});
