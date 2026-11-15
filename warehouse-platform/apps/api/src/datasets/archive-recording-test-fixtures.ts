import { createHash } from 'node:crypto';
import { ArchiveSource } from './archive-recording-groups';

export function source(id: string, start: number, end: number, close = 'rotation', robotId = 'robot-a', extras: object = {}): ArchiveSource {
  return { id, datasetId: 'dataset-a', robotId, folderPath: `前相机/时间段/${id}`, objectKey: `raw/${id}`, originalName: 'video.mkv',
    fileSize: 8n, checksum: createHash('sha256').update(`source-${id}`).digest('hex'), capturedAt: new Date(start),
    metadata: { archiveType: 'garage-native-archive', startedAt: new Date(start).toISOString(), endAt: new Date(end).toISOString(),
      receivedFrames: 2, closeReason: close, cameraId: 'camera2', videoCodec: 'mjpeg', width: 960, height: 720, ...extras },
    recognitionType: 'garage-native-front', mimeType: 'video/x-matroska', fileType: 'mkv', schemaVersion: '1.0', createdAt: new Date(start), updatedAt: new Date(start) };
}
