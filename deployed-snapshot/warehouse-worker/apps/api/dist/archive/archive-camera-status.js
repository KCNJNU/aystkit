"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.ArchiveCameraMonitor = void 0;
const INVENTORY = [
    { id: 'camera2', name: '前相机' }, { id: 'camera1', name: '后相机' },
    { id: 'camera3', name: '左相机' }, { id: 'camera4', name: '右相机' },
    { id: 'berxelColor', name: '深度相机 · 彩色', topic: '/berxel/color/image_raw' },
    { id: 'berxelDepth', name: '深度相机 · 深度', topic: '/berxel/depth/image_raw' },
];
function object(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}
function count(value) {
    return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}
function seconds(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}
function timestamp(value) {
    const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
    return Number.isFinite(parsed) ? parsed : null;
}
function fresh(value, now, lifetime) {
    const parsed = timestamp(value);
    return parsed !== null && now - parsed >= -5_000 && now - parsed < lifetime;
}
const DROP_KEYS = ['queueDropped', 'storageDropped', 'writerDropped', 'gateClosedDropped',
    'invalidPackets', 'corruptPackets', 'oversizedPackets', 'duplicatePackets', 'writeErrors'];
class ArchiveCameraMonitor {
    samples = new Map();
    writing(id, written, reportTime, epoch, resets, eligible, now) {
        if (!eligible || written === null || reportTime === null) {
            this.samples.delete(id);
            return false;
        }
        const previous = this.samples.get(id);
        const reset = !previous || previous.epoch !== epoch || previous.resets !== resets
            || reportTime < previous.reportTime || written < previous.count;
        const progressAt = reset ? null : written > previous.count && reportTime > previous.reportTime
            ? now : previous.progressAt;
        this.samples.set(id, { count: written, reportTime, progressAt, epoch, resets });
        return progressAt !== null && now - progressAt >= 0 && now - progressAt < 10_000;
    }
    snapshot(sourceValue, gateValue, now = Date.now()) {
        const source = object(sourceValue), gate = object(gateValue);
        const sourcePresent = Object.keys(source).length > 0;
        const sourceFresh = source.connected === true && fresh(source.updatedAt, now, 25_000);
        const gateValid = gate.scope === 'garage' && gate.schemaVersion === 1 && typeof gate.enabled === 'boolean';
        const acknowledged = gateValid && sourceFresh && source.gateAcknowledged === true
            && source.gateRevision === gate.updatedAt && source.gateEnabled === gate.enabled;
        const capture = object(source.capture), captureRows = object(capture.cameras);
        const captureFresh = sourceFresh && fresh(capture.updatedAt, now, 20_000);
        const native = object(source.camera), nativeStore = object(native.store);
        const nativeFresh = sourceFresh && fresh(native.updatedAt, now, 20_000);
        const nativeCounters = object(nativeStore.cameras), nativeActive = object(nativeStore.active);
        const recorder = object(source.recorder), ros = object(recorder.ros), rosTopics = object(ros.topics);
        const rosFresh = sourceFresh && fresh(recorder.updatedAt, now, 20_000)
            && recorder.state !== 'stopped' && recorder.state !== 'stop_failed';
        return INVENTORY.map(camera => {
            const captureRow = object(captureRows[camera.id]);
            const captureTime = timestamp(capture.updatedAt);
            const reportedAge = seconds(captureRow.ageS);
            const ageS = reportedAge !== null && captureTime !== null
                ? reportedAge + Math.max(0, (now - captureTime) / 1000) : null;
            let acquisition = Object.keys(capture).length && !captureFresh ? 'stale' : 'unknown';
            const receivedFrames = captureFresh ? count(captureRow.frames) : null;
            if (captureFresh) {
                if (captureRow.state === 'capturing' && receivedFrames !== null && receivedFrames > 0 && ageS !== null && ageS <= 5)
                    acquisition = 'capturing';
                else if (captureRow.state === 'not_capturing')
                    acquisition = 'not_capturing';
                else if (captureRow.state === 'capturing')
                    acquisition = 'stale';
            }
            const usb = !('topic' in camera);
            const topic = 'topic' in camera ? object(rosTopics[camera.topic]) : {};
            const counters = usb ? object(nativeCounters[camera.id]) : topic;
            const reportFresh = usb ? nativeFresh : rosFresh;
            const reportTime = timestamp(usb ? native.updatedAt : recorder.updatedAt);
            const writtenFrames = reportFresh ? count(counters[usb ? 'writtenFrames' : 'written']) : null;
            const droppedFrames = !reportFresh ? null : usb
                ? Object.keys(counters).length ? count(DROP_KEYS.reduce((sum, key) => sum + (count(counters[key]) ?? 0), 0)) : null
                : count(topic.dropped);
            const hasActive = usb ? (count(object(nativeActive[camera.id]).frames) ?? 0) > 0
                : topic.state === 'AVAILABLE' && (seconds(topic.ageS) ?? Infinity) <= 5;
            const errors = usb ? object(nativeStore.errors) : object(object(recorder.store).errors);
            const error = usb ? errors[camera.id] || errors.storage
                : topic.error || errors[topic.sensor] || errors.storage || ros.gateError || ros.tickError;
            const reportEnabled = usb ? nativeStore.enabled === true : ros.active === true;
            const eligible = acknowledged && gate.enabled === true && reportFresh && reportEnabled
                && hasActive && acquisition === 'capturing' && !error;
            const progressed = this.writing(camera.id, writtenFrames, reportTime, String(gate.updatedAt ?? '') + ':' + String(usb ? '' : ros.epoch ?? ''), usb ? count(counters.sequenceResets) : null, eligible, now);
            let recording;
            let detail;
            if (!sourceFresh) {
                recording = sourcePresent ? 'stale' : 'unknown';
                detail = '源端未连接或状态已过期，无法确认是否仍在本地保存';
            }
            else if (!acknowledged) {
                recording = 'unknown';
                detail = '源端尚未确认当前地库开关';
            }
            else if (gate.enabled === false)
                recording = 'disabled';
            else if (!reportFresh) {
                recording = 'stale';
                detail = '未收到新的相机保存状态';
            }
            else if (error) {
                recording = 'error';
                detail = '源端保存异常，请检查采集日志或磁盘空间';
            }
            else if (progressed)
                recording = 'recording';
            else {
                recording = 'waiting';
                detail = acquisition === 'capturing' ? '已收到画面，等待确认写入帧数增长' : '等待相机新帧';
            }
            return { id: camera.id, name: camera.name, acquisition, recording, receivedFrames,
                writtenFrames, droppedFrames, frameAgeS: captureFresh ? ageS : null,
                updatedAt: captureFresh && typeof capture.updatedAt === 'string' ? capture.updatedAt : null,
                ...(detail ? { detail } : {}) };
        });
    }
}
exports.ArchiveCameraMonitor = ArchiveCameraMonitor;
//# sourceMappingURL=archive-camera-status.js.map