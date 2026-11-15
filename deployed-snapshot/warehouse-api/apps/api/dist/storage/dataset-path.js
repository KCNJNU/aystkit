"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.datasetFolder = datasetFolder;
exports.datasetFileKey = datasetFileKey;
exports.validateFolderPath = validateFolderPath;
exports.validateEntryName = validateEntryName;
exports.uploadDestination = uploadDestination;
exports.folderMarker = folderMarker;
const common_1 = require("@nestjs/common");
const hasControl = (value) => [...value].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127);
const reserved = new Set(['raw', 'uploads', 'manifests', 'dvc', '.dvc', '.git']);
function datasetFolder(name) {
    const value = name.trim();
    if (!value || value !== name || value === '.' || value === '..' || reserved.has(value.toLowerCase()) || (value.includes('/') || value.includes('\\') || hasControl(value)) || Buffer.byteLength(value) > 200) {
        throw new common_1.BadRequestException('数据集名称不能包含斜杠、控制字符、首尾空格或使用系统目录名，且长度不能超过 200 字节');
    }
    return `${value}/`;
}
function datasetFileKey(name, uploadId, filename) {
    const parts = filename.replace(/\\/g, '/').split('/');
    if (parts.some(part => !part || part === '.' || part === '..' || hasControl(part)))
        throw new common_1.BadRequestException('文件路径无效');
    const key = `${datasetFolder(name)}${uploadId}/${parts.join('/')}`;
    if (Buffer.byteLength(key) > 1024)
        throw new common_1.BadRequestException('文件路径过长');
    return key;
}
function validateFolderPath(path) {
    if (typeof path !== 'string' || Buffer.byteLength(path) > 750)
        throw new common_1.BadRequestException('文件夹路径过长');
    if (!path)
        return '';
    for (const part of path.split('/'))
        validateEntryName(part);
    return path;
}
function validateEntryName(name) {
    if (!name || name !== name.trim() || ['.', '..', '.folder', '.dataset'].includes(name) || name.includes('/') || name.includes('\\') || hasControl(name) || Buffer.byteLength(name) > 200)
        throw new common_1.BadRequestException('名称不能包含斜杠、控制字符、首尾空格或使用保留名称，长度不能超过 200 字节');
    return name;
}
function uploadDestination(folderPath, originalName) {
    validateFolderPath(folderPath);
    const parts = originalName.replace(/\\/g, '/').split('/');
    parts.forEach(validateEntryName);
    const name = parts.pop();
    const path = validateFolderPath([folderPath, ...parts].filter(Boolean).join('/'));
    return { folderPath: path, name };
}
function folderMarker(datasetName, path) { return `${datasetFolder(datasetName)}${validateFolderPath(path)}/.folder`; }
//# sourceMappingURL=dataset-path.js.map