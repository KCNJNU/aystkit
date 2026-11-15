"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.isArchiveTimeSlot = isArchiveTimeSlot;
exports.archiveSegmentPaths = archiveSegmentPaths;
exports.archiveCaptureDateRange = archiveCaptureDateRange;
exports.archiveDatasetDateRange = archiveDatasetDateRange;
exports.mergeCaptureDateRange = mergeCaptureDateRange;
function isArchiveTimeSlot(metadata, path) {
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)
        || metadata.type !== 'garage-native-archive')
        return false;
    const pieces = path.split('/');
    if (pieces.length !== 2 || !pieces[0] || !pieces[1].startsWith(pieces[0] + '_'))
        return false;
    return /^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$/.test(pieces[1].slice(pieces[0].length + 1));
}
function archiveSegmentPaths(path, folders) {
    return folders.filter(folder => /^[a-f0-9]{32}$/.test(folder.name)
        && folder.parentPath === path && folder.path === path + '/' + folder.name)
        .map(folder => folder.path);
}
const captureDay = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
});
function record(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}
function archiveCaptureDateRange(metadata, capturedAt) {
    const source = record(metadata);
    const instant = (value) => {
        if (value instanceof Date)
            return value.getTime();
        if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value))
            return NaN;
        const day = new Date(value.slice(0, 10) + 'T00:00:00Z');
        if (!Number.isFinite(day.getTime()) || day.toISOString().slice(0, 10) !== value.slice(0, 10))
            return NaN;
        return Date.parse(value);
    };
    const captured = instant(capturedAt), started = instant(source.startedAt), ended = instant(source.endAt);
    let from = Number.isFinite(started) ? started : Number.isFinite(ended) ? ended : captured;
    let to = Number.isFinite(ended) ? ended : from;
    if (to < from) {
        from = captured;
        to = captured;
    }
    if (!Number.isFinite(from) || !Number.isFinite(to))
        return undefined;
    return { from: captureDay.format(new Date(from)), to: captureDay.format(new Date(to)) };
}
function archiveDatasetDateRange(metadata) {
    const source = record(metadata), day = source.day;
    if (source.type !== 'garage-native-archive' || typeof day !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(day))
        return undefined;
    const date = new Date(day + 'T00:00:00Z');
    if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== day)
        return undefined;
    return { from: day, to: day };
}
function mergeCaptureDateRange(previous, next) {
    return previous ? { from: previous.from < next.from ? previous.from : next.from,
        to: previous.to > next.to ? previous.to : next.to } : next;
}
//# sourceMappingURL=archive-directory-view.js.map