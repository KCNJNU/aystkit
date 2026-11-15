"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.source = source;
const node_crypto_1 = require("node:crypto");
function source(id, start, end, close = 'rotation', robotId = 'robot-a', extras = {}) {
    return { id, datasetId: 'dataset-a', robotId, folderPath: `前相机/时间段/${id}`, objectKey: `raw/${id}`, originalName: 'video.mkv',
        fileSize: 8n, checksum: (0, node_crypto_1.createHash)('sha256').update(`source-${id}`).digest('hex'), capturedAt: new Date(start),
        metadata: { archiveType: 'garage-native-archive', startedAt: new Date(start).toISOString(), endAt: new Date(end).toISOString(),
            receivedFrames: 2, closeReason: close, cameraId: 'camera2', videoCodec: 'mjpeg', width: 960, height: 720, ...extras },
        recognitionType: 'garage-native-front', mimeType: 'video/x-matroska', fileType: 'mkv', schemaVersion: '1.0', createdAt: new Date(start), updatedAt: new Date(start) };
}
//# sourceMappingURL=archive-recording-test-fixtures.js.map