import { ConfigService } from '@nestjs/config';
import { UserRole } from '@prisma/client';
import { AuthUser } from '../auth/auth.types';
import { ArchiveRecordingsService } from './archive-recordings.service';
import { source } from './archive-recording-test-fixtures';

const robotId = '12345678-1234-4234-8234-123456789abc', sessionId = 'a'.repeat(32), segmentId = 'b'.repeat(32);
const actor: AuthUser = { id: 'operator', role: UserRole.OPERATOR, email: '', displayName: '', tenantId: 'tenant', projectIds: ['project'], mustChangePassword: false };
const body = { robotId, startedAt: '2026-11-02T08:26:00.000Z', endedAt: '2026-11-02T08:55:30.000Z', reason: 'desktop_stopped' as const, captureIncomplete: true };
function setup() {
  const dataset = { id: 'dataset', metadata: { type: 'garage-native-archive', robotId, day: '2026-11-02', untouched: 'preserved' } as Record<string, unknown> };
  const segment = source('file', Date.parse(body.startedAt), Date.parse(body.endedAt) - 1000, 'rotation', robotId, { segmentId });
  const tx = { $queryRaw: jest.fn(), dataset: { findFirst: jest.fn(async () => dataset), update: jest.fn(async (args: { data: {metadata: object} }) => {
    dataset.metadata = args.data.metadata as Record<string, unknown>; return dataset;
  }) }, robotFile: { findMany: jest.fn(async () => [segment]), update: jest.fn() } };
  const prisma = { $transaction: jest.fn(async (fn: (client: typeof tx) => Promise<unknown>) => fn(tx)) };
  const service = new ArchiveRecordingsService(prisma as never, {} as never, {} as never, new ConfigService());
  return { service, tx, dataset, segment };
}

describe('authenticated archive-session closure proof', () => {
  it('preserves dataset fields and files, enforces project scope, and is idempotent after JSONB key reordering', async () => {
    const { service, tx, dataset } = setup();
    expect(await service.endSession('dataset', sessionId, { ...body, segmentIds: [segmentId] }, actor)).toMatchObject({ closed: true, idempotent: false });
    expect(tx.dataset.findFirst).toHaveBeenCalledWith({ where: { id: 'dataset', deletingAt: null, tenantId: 'tenant', projectId: { in: ['project'] } } });
    expect(dataset.metadata.untouched).toBe('preserved');
    const sessions = dataset.metadata.archiveRecordingSessions as Record<string, Record<string, unknown>>;
    sessions[sessionId] = Object.fromEntries(Object.entries(sessions[sessionId]).reverse());
    expect(await service.endSession('dataset', sessionId, { ...body, segmentIds: [segmentId] }, actor)).toMatchObject({ idempotent: true });
    expect(tx.dataset.update).toHaveBeenCalledTimes(1);
    expect(tx.robotFile.update).not.toHaveBeenCalled();
  });

  it('accepts a future-upload session proof with no guessed segment binding', async () => {
    const { service, tx } = setup();
    await expect(service.endSession('dataset', sessionId, body, actor)).resolves.toMatchObject({ closed: true });
    expect(tx.robotFile.findMany).not.toHaveBeenCalled();
  });

  it('rejects viewers, inaccessible datasets, foreign robots, wrong dates and conflicting replay', async () => {
    const { service, tx, dataset } = setup();
    await expect(service.endSession('dataset', sessionId, body, { ...actor, role: UserRole.VIEWER })).rejects.toThrow('无权');
    expect(tx.dataset.findFirst).not.toHaveBeenCalled();
    tx.dataset.findFirst.mockResolvedValueOnce(null as never);
    await expect(service.endSession('dataset', sessionId, body, actor)).rejects.toThrow('不存在');
    await expect(service.endSession('dataset', sessionId, { ...body, robotId: '22345678-1234-4234-8234-123456789abc' }, actor)).rejects.toThrow('不匹配');
    dataset.metadata.day = '2026-11-01';
    await expect(service.endSession('dataset', sessionId, body, actor)).rejects.toThrow('日期');
    dataset.metadata.day = '2026-11-02';
    await service.endSession('dataset', sessionId, body, actor);
    await expect(service.endSession('dataset', sessionId, { ...body, captureIncomplete: false }, actor)).rejects.toThrow('不同结束证明');
  });

  it.each(['missing', 'out-of-range', 'other-session'])('refuses %s legacy segment proof without writing metadata', async kind => {
    const { service, tx, segment } = setup();
    if (kind === 'missing') tx.robotFile.findMany.mockResolvedValue([]);
    if (kind === 'out-of-range') (segment.metadata as Record<string, unknown>).endAt = '2026-11-02T09:00:00Z';
    if (kind === 'other-session') (segment.metadata as Record<string, unknown>).captureSessionId = 'c'.repeat(32);
    await expect(service.endSession('dataset', sessionId, { ...body, segmentIds: [segmentId] }, actor)).rejects.toThrow();
    expect(tx.dataset.update).not.toHaveBeenCalled();
  });

  it.each([{ endedAt: 'invalid' }, { startedAt: '2026-11-01T00:00:00Z' }, { reason: 'timeout' },
    { segmentIds: [segmentId, segmentId] }, { segmentIds: ['__proto__'] }])('rejects invalid or oversized/unbounded proof %j', async patch => {
    const { service, tx } = setup();
    await expect(service.endSession('dataset', sessionId, { ...body, ...patch } as never, actor)).rejects.toThrow('参数无效');
    expect(tx.dataset.update).not.toHaveBeenCalled();
  });

  it.each([undefined, sessionId])('persists an evidenced incomplete recovery for an exact legacy or modern segment, preserving source metadata (%s)', async token => {
    const { service, tx, dataset, segment } = setup();
    (segment.metadata as Record<string, unknown>).captureSessionId = token;
    const before = JSON.stringify(segment.metadata);
    const proof = { ...body, reason: 'recovered_interruption' as const, segmentIds: [segmentId], recoveryEvidenceSha256: 'd'.repeat(64) };
    expect(await service.endSession('dataset', sessionId, proof, actor)).toMatchObject({ closed: true, captureIncomplete: true, idempotent: false });
    expect((dataset.metadata.archiveRecordingSessions as Record<string, unknown>)[sessionId]).toMatchObject({
      reason: 'recovered_interruption', captureIncomplete: true, segmentIds: [segmentId], recoveryEvidenceSha256: 'd'.repeat(64),
    });
    expect(await service.endSession('dataset', sessionId, proof, actor)).toMatchObject({ idempotent: true });
    expect(tx.robotFile.update).not.toHaveBeenCalled();
    expect(JSON.stringify(segment.metadata)).toBe(before);
    await expect(service.endSession('dataset', sessionId, { ...proof, recoveryEvidenceSha256: 'e'.repeat(64) }, actor)).rejects.toThrow('不同结束证明');
  });

  it.each([{ captureIncomplete: false }, { segmentIds: undefined }, { segmentIds: [] }, { recoveryEvidenceSha256: undefined },
    { recoveryEvidenceSha256: 'unverified' }, { recoveryEvidenceSha256: 'A'.repeat(64) }])('rejects an unbounded or unevidenced recovery %j', async patch => {
    const { service, tx } = setup();
    const proof = { ...body, reason: 'recovered_interruption' as const, segmentIds: [segmentId], recoveryEvidenceSha256: 'd'.repeat(64), ...patch };
    await expect(service.endSession('dataset', sessionId, proof, actor)).rejects.toThrow('参数无效');
    expect(tx.dataset.update).not.toHaveBeenCalled();
  });

  it.each(['f'.repeat(32), false, 0])('rejects a recovery segment with a foreign or malformed token %j', async token => {
    const { service, tx, segment } = setup();
    (segment.metadata as Record<string, unknown>).captureSessionId = token;
    await expect(service.endSession('dataset', sessionId, { ...body, reason: 'recovered_interruption', segmentIds: [segmentId],
      recoveryEvidenceSha256: 'd'.repeat(64) }, actor)).rejects.toThrow('不匹配');
    expect(tx.dataset.update).not.toHaveBeenCalled();
  });
});
