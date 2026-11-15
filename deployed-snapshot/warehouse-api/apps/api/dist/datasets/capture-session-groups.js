"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.CAPTURE_ROBOT_ID = exports.CAPTURE_SESSION_ID = void 0;
exports.groupCaptureSessions = groupCaptureSessions;
const archive_recording_groups_1 = require("./archive-recording-groups");
exports.CAPTURE_SESSION_ID = /^(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$/;
exports.CAPTURE_ROBOT_ID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const SEGMENT_ID = /^[a-f0-9]{32,64}$/;
function groupCaptureSessions(files, proofs) {
    const segmentSessions = new Map();
    const claim = (robotId, segmentId, sessionId) => {
        if (typeof segmentId !== 'string' || !SEGMENT_ID.test(segmentId))
            return;
        const key = `${robotId}:${segmentId}`;
        const sessions = segmentSessions.get(key) ?? new Set();
        sessions.add(sessionId);
        segmentSessions.set(key, sessions);
    };
    for (const file of files) {
        const meta = (0, archive_recording_groups_1.jsonRecord)(file.metadata);
        if (file.recognitionType === archive_recording_groups_1.ARCHIVE_ASSEMBLY_TYPE || meta.archiveRole === archive_recording_groups_1.ARCHIVE_ASSEMBLY_ROLE)
            continue;
        if (typeof meta.captureSessionId === 'string' && exports.CAPTURE_SESSION_ID.test(meta.captureSessionId))
            claim(file.robotId, meta.segmentId, meta.captureSessionId);
    }
    for (const [sessionId, value] of Object.entries((0, archive_recording_groups_1.jsonRecord)(proofs))) {
        const proof = (0, archive_recording_groups_1.jsonRecord)(value);
        const start = Date.parse(String(proof.startedAt)), end = Date.parse(String(proof.endedAt));
        if (!exports.CAPTURE_SESSION_ID.test(sessionId) || typeof proof.robotId !== 'string' || !exports.CAPTURE_ROBOT_ID.test(proof.robotId)
            || !['desktop_stopped', 'recovered_interruption'].includes(String(proof.reason))
            || typeof proof.captureIncomplete !== 'boolean' || !Number.isFinite(start) || !Number.isFinite(end) || end < start
            || !Array.isArray(proof.segmentIds) || !proof.segmentIds.length || proof.segmentIds.length > 5000
            || new Set(proof.segmentIds).size !== proof.segmentIds.length
            || proof.segmentIds.some(id => typeof id !== 'string' || !SEGMENT_ID.test(id)))
            continue;
        if (proof.reason === 'recovered_interruption' && (proof.captureIncomplete !== true
            || typeof proof.recoveryEvidenceSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(proof.recoveryEvidenceSha256)))
            continue;
        for (const segmentId of proof.segmentIds)
            claim(proof.robotId, segmentId, sessionId);
    }
    const groups = new Map();
    const unassigned = [];
    let excludedDerivedFileCount = 0;
    for (const file of files) {
        const meta = (0, archive_recording_groups_1.jsonRecord)(file.metadata);
        if (file.recognitionType === archive_recording_groups_1.ARCHIVE_ASSEMBLY_TYPE || meta.archiveRole === archive_recording_groups_1.ARCHIVE_ASSEMBLY_ROLE) {
            excludedDerivedFileCount++;
            continue;
        }
        let sessionId;
        if (typeof meta.captureSessionId === 'string' && exports.CAPTURE_SESSION_ID.test(meta.captureSessionId))
            sessionId = meta.captureSessionId;
        else if (meta.captureSessionId === undefined || meta.captureSessionId === null || meta.captureSessionId === '') {
            const candidates = typeof meta.segmentId === 'string' ? segmentSessions.get(`${file.robotId}:${meta.segmentId}`) : undefined;
            if (candidates?.size === 1)
                sessionId = [...candidates][0];
        }
        if (!sessionId || !exports.CAPTURE_ROBOT_ID.test(file.robotId)) {
            unassigned.push(file);
            continue;
        }
        const startValue = typeof meta.startedAt === 'string' && Number.isFinite(Date.parse(meta.startedAt)) ? meta.startedAt : file.capturedAt.toISOString();
        const endValue = typeof meta.endAt === 'string' && Number.isFinite(Date.parse(meta.endAt)) ? meta.endAt : startValue;
        const startedAt = new Date(Math.min(Date.parse(startValue), Date.parse(endValue))).toISOString();
        const endAt = new Date(Math.max(Date.parse(startValue), Date.parse(endValue))).toISOString();
        const key = `${file.robotId}:${sessionId}`;
        const group = groups.get(key) ?? { robotId: file.robotId, sessionId, startedAt, endAt, files: [], sensors: [] };
        if (startedAt < group.startedAt)
            group.startedAt = startedAt;
        if (endAt > group.endAt)
            group.endAt = endAt;
        group.files.push(file);
        groups.set(key, group);
    }
    for (const group of groups.values()) {
        const sensors = new Map();
        for (const file of group.files) {
            const name = file.folderPath.split('/')[0] || '其他文件';
            const sensor = sensors.get(name) ?? { name, fileCount: 0, bytes: 0n };
            sensor.fileCount++;
            sensor.bytes += file.fileSize;
            sensors.set(name, sensor);
        }
        group.sensors = [...sensors.values()].sort((a, b) => a.name.localeCompare(b.name))
            .map(sensor => ({ name: sensor.name, fileCount: sensor.fileCount, totalBytes: sensor.bytes.toString() }));
    }
    return { sessions: [...groups.values()].sort((a, b) => b.startedAt.localeCompare(a.startedAt)
            || a.robotId.localeCompare(b.robotId) || a.sessionId.localeCompare(b.sessionId)), unassigned, excludedDerivedFileCount };
}
//# sourceMappingURL=capture-session-groups.js.map