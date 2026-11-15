"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.isArchiveTimeSlot = isArchiveTimeSlot;
exports.archiveSegmentPaths = archiveSegmentPaths;
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
//# sourceMappingURL=archive-directory-view.js.map