import { createHash } from 'node:crypto';
import type { Prisma, RobotFile } from '@prisma/client';

export const ARCHIVE_ASSEMBLY_TYPE = 'garage-continuous-video';
export const ARCHIVE_ASSEMBLY_ROLE = 'continuous-export';
export type ArchiveSource = Pick<RobotFile, 'id' | 'datasetId' | 'robotId' | 'folderPath' | 'objectKey' | 'originalName' | 'fileSize' | 'checksum' | 'capturedAt' | 'metadata' | 'recognitionType' | 'mimeType' | 'fileType' | 'schemaVersion' | 'createdAt' | 'updatedAt'>;
export type RecordingGroup = {
  id: string; datasetId: string; path: string; sensor: string; robotId: string;
  startedAt: string; endAt: string; partCount: number; sourceBytes: string;
  frameCount: number; durationSeconds: number; closed: boolean;
  gapBeforeSeconds: number | null; sources: ArchiveSource[]; error: string | null;
  captureIncomplete: boolean; closureSessionId: string | null;
  closureReason: 'desktop_stopped' | 'recovered_interruption' | null;
  captureSessionId: string | null;
  controlSession: { startedAt: string; endedAt: string; nativeTimeOutsideSession: boolean } | null;
};

export function jsonRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]));
  return typeof value === 'bigint' ? value.toString() : value;
}

export function sourceFingerprint(source: ArchiveSource) {
  return canonical({ id: source.id, robotId: source.robotId, datasetId: source.datasetId, folderPath: source.folderPath,
    objectKey: source.objectKey, checksum: source.checksum, size: source.fileSize.toString(), metadata: source.metadata });
}

export function isOriginalArchiveVideo(source: Pick<ArchiveSource, 'originalName' | 'recognitionType' | 'metadata'>): boolean {
  const metadata = jsonRecord(source.metadata);
  return source.originalName.toLowerCase().endsWith('.mkv') && source.recognitionType.startsWith('garage-native-')
    && metadata.archiveType === 'garage-native-archive' && metadata.archiveRole !== ARCHIVE_ASSEMBLY_ROLE;
}

type TimedSource = { source: ArchiveSource; start: number; end: number; startNs: bigint; endNs: bigint; frames: number; close: string; channel: string; codec: string; error: string | null };
const TERMINAL_CLOSE_REASONS = new Set(['garage_disabled', 'shutdown', 'idle', 'storage_paused', 'power_loss_recovered']);

function timestampNs(value: unknown, milliseconds: number) {
  const fraction = typeof value === 'string' ? value.match(/\.(\d{1,9})(?:Z|[+-]\d{2}:?\d{2})$/)?.[1] ?? '' : '';
  return BigInt(milliseconds) * 1_000_000n + BigInt((fraction + '000000000').slice(3, 9));
}

function timed(source: ArchiveSource, sensor: string): TimedSource {
  const metadata = jsonRecord(source.metadata);
  const start = typeof metadata.startedAt === 'string' ? Date.parse(metadata.startedAt) : NaN;
  const end = typeof metadata.endAt === 'string' ? Date.parse(metadata.endAt) : NaN;
  const frames = Number(metadata.receivedFrames);
  const valid = Number.isFinite(start) && Number.isFinite(end) && end >= start && Number.isSafeInteger(frames) && frames > 0;
  const channel = JSON.stringify([source.robotId, sensor, metadata.cameraId ?? metadata.videoImageTopics ?? metadata.source ?? 'camera']);
  const codec = JSON.stringify([metadata.videoCodec ?? metadata.format, metadata.width ?? metadata.targetWidth,
    metadata.height ?? metadata.targetHeight, metadata.videoPixelFormat ?? '', metadata.videoCompression ?? '']);
  return { source, start: Number.isFinite(start) ? start : source.capturedAt.getTime(),
    end: Number.isFinite(end) ? end : source.capturedAt.getTime(), frames: Number.isSafeInteger(frames) && frames > 0 ? frames : 0,
    startNs: timestampNs(metadata.startedAt, Number.isFinite(start) ? start : source.capturedAt.getTime()), endNs: timestampNs(metadata.endAt, Number.isFinite(end) ? end : source.capturedAt.getTime()),
    close: typeof metadata.closeReason === 'string' ? metadata.closeReason : '', channel, codec,
    error: valid ? null : '归档片段缺少有效的采集时间或帧数，无法验证连续录像' };
}

/** Only an explicit terminal close seals a group; later uploads can still fill a gap. */
export function groupArchiveRecordings(datasetId: string, path: string, input: readonly ArchiveSource[], sessionProofs: unknown = {}): RecordingGroup[] {
  const channels = new Map<string, TimedSource[]>();
  for (const source of input.filter(isOriginalArchiveVideo)) {
    const value = timed(source, path);
    channels.set(value.channel, [...(channels.get(value.channel) ?? []), value]);
  }
  const result: RecordingGroup[] = [];
  for (const channel of channels.values()) {
    channel.sort((a, b) => a.startNs === b.startNs ? a.source.id.localeCompare(b.source.id) : a.startNs < b.startNs ? -1 : 1);
    let parts: TimedSource[] = [], before: number | null = null;
    const publish = () => {
      if (!parts.length) return;
      const first = parts[0], last = parts.at(-1)!;
      const nativeSessionId = jsonRecord(first.source.metadata).captureSessionId;
      const captureSessionId = typeof nativeSessionId === 'string' && /^[a-f0-9]{32}$/.test(nativeSessionId)
        && parts.every(part => jsonRecord(part.source.metadata).captureSessionId === nativeSessionId) ? nativeSessionId : null;
      const controlProof = captureSessionId ? jsonRecord(jsonRecord(sessionProofs)[captureSessionId]) : {};
      const controlStart = Date.parse(String(controlProof.startedAt)), controlEnd = Date.parse(String(controlProof.endedAt));
      // A matching control token is useful for finding recordings after a device
      // clock correction. It is display context, never evidence that closes a
      // native interval outside the proof's clock range.
      const controlSession = controlProof.reason === 'desktop_stopped' && typeof controlProof.captureIncomplete === 'boolean'
        && parts.every(part => part.source.robotId === controlProof.robotId)
        && Number.isFinite(controlStart) && Number.isFinite(controlEnd) && controlEnd >= controlStart
        ? { startedAt: new Date(controlStart).toISOString(), endedAt: new Date(controlEnd).toISOString(),
          nativeTimeOutsideSession: parts.some(part => part.start < controlStart || part.end > controlEnd) } : null;
      const identity = { datasetId, path, sources: parts.map(part => sourceFingerprint(part.source)) };
      const id = createHash('sha256').update(JSON.stringify(canonical(identity))).digest('hex');
      // Closure is independent evidence: do not rewrite source metadata or the
      // fingerprint, so existing verified exports remain addressable.
      const closure = Object.entries(jsonRecord(sessionProofs)).find(([sessionId, raw]) => {
        const proof = jsonRecord(raw), begin = Date.parse(String(proof.startedAt)), end = Date.parse(String(proof.endedAt));
        const recovered = proof.reason === 'recovered_interruption';
        if ((!recovered && proof.reason !== 'desktop_stopped') || typeof proof.captureIncomplete !== 'boolean'
            || !Number.isFinite(begin) || !Number.isFinite(end) || end < begin) return false;
        const explicit = Array.isArray(proof.segmentIds) ? new Set(proof.segmentIds) : new Set();
        if (recovered && (proof.captureIncomplete !== true || !explicit.size || explicit.size > 5000
            || !Array.isArray(proof.segmentIds) || explicit.size !== proof.segmentIds.length
            || [...explicit].some(id => typeof id !== 'string' || !/^[a-f0-9]{32}$/.test(id))
            || typeof proof.recoveryEvidenceSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(proof.recoveryEvidenceSha256))) return false;
        return parts.every(part => {
          const meta = jsonRecord(part.source.metadata);
          const legacyToken = meta.captureSessionId === undefined || meta.captureSessionId === null || meta.captureSessionId === '';
          return part.source.robotId === proof.robotId && part.start >= begin && part.end <= end
            && (!recovered || explicit.has(meta.segmentId))
            && (meta.captureSessionId === sessionId || (legacyToken && explicit.has(meta.segmentId)));
        });
      });
      const captureIncomplete = Boolean(closure && jsonRecord(closure[1]).captureIncomplete)
        || parts.some(part => part.close === 'storage_paused' || part.close === 'power_loss_recovered'
          || jsonRecord(part.source.metadata).captureInterrupted === true || jsonRecord(part.source.metadata).captureIncomplete === true
          || Number(jsonRecord(part.source.metadata).sequenceGaps) > 0);
      const lastFrame = last.frames > 1 ? Number(last.endNs - last.startNs) / (last.frames - 1) / 1e9 : 1 / 30;
      result.push({ id, datasetId, path, sensor: path, robotId: first.source.robotId,
        startedAt: String(jsonRecord(first.source.metadata).startedAt ?? new Date(first.start).toISOString()), endAt: String(jsonRecord(last.source.metadata).endAt ?? new Date(last.end).toISOString()),
        partCount: parts.length, sourceBytes: parts.reduce((sum, part) => sum + part.source.fileSize, 0n).toString(),
        frameCount: parts.reduce((sum, part) => sum + part.frames, 0),
        durationSeconds: Math.max(0, Number(last.endNs - first.startNs) / 1e9) + lastFrame,
        closed: Boolean(last.error || TERMINAL_CLOSE_REASONS.has(last.close) || closure), gapBeforeSeconds: before,
        captureIncomplete, closureSessionId: closure?.[0] ?? null,
        closureReason: closure ? jsonRecord(closure[1]).reason as 'desktop_stopped' | 'recovered_interruption' : null,
        captureSessionId, controlSession,
        sources: parts.map(part => part.source), error: parts.find(part => part.error)?.error ?? null });
    };
    for (const next of channel) {
      const previous = parts.at(-1);
      if (previous) {
        const gapNs = next.startNs - previous.endNs;
        const previousSession = jsonRecord(previous.source.metadata).captureSessionId;
        const nextSession = jsonRecord(next.source.metadata).captureSessionId;
        if (previous.error || next.error || previous.close !== 'rotation' || gapNs < 0n || gapNs > 500_000_000n || previous.codec !== next.codec
            || ((previousSession || nextSession) && previousSession !== nextSession)) {
          publish(); parts = []; before = Math.max(0, Number(gapNs) / 1e9);
        }
      }
      parts.push(next);
    }
    publish();
  }
  return result.sort((a, b) => a.startedAt.localeCompare(b.startedAt) || a.id.localeCompare(b.id));
}

export function inputMetadata(value: unknown): Prisma.InputJsonValue {
  return JSON.parse(JSON.stringify(value, (_key, item: unknown) => typeof item === 'bigint' ? item.toString() : item)) as Prisma.InputJsonValue;
}
