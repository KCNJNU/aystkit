"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.ensureDatasetFolders = ensureDatasetFolders;
const common_1 = require("@nestjs/common");
const dataset_path_1 = require("../storage/dataset-path");
async function ensureDatasetFolders(tx, storage, dataset, path) {
    (0, dataset_path_1.validateFolderPath)(path);
    let parentPath = '';
    for (const name of path ? path.split('/') : []) {
        const childPath = [parentPath, name].filter(Boolean).join('/');
        if (await tx.robotFile.findFirst({ where: { datasetId: dataset.id, folderPath: parentPath, OR: [{ originalName: name }, { objectKey: `${(0, dataset_path_1.datasetFolder)(dataset.name)}${childPath}` }] } }))
            throw new common_1.ConflictException('目录中已有同名文件');
        if (await tx.uploadSession.findFirst({ where: { datasetId: dataset.id, folderPath: parentPath, originalName: name, status: { in: ['CREATED', 'UPLOADING', 'FINALIZING', 'FAILED'] }, expiresAt: { gt: new Date() } } }))
            throw new common_1.ConflictException('目录中有同名文件正在上传');
        const objectKey = (0, dataset_path_1.folderMarker)(dataset.name, childPath);
        const folder = await tx.datasetFolder.upsert({ where: { datasetId_path: { datasetId: dataset.id, path: childPath } }, create: { datasetId: dataset.id, name, path: childPath, parentPath, objectKey }, update: {} });
        await storage.putObject({ objectKey, content: Buffer.from(JSON.stringify({ datasetId: dataset.id, folderId: folder.id })), contentType: 'application/json' });
        parentPath = childPath;
    }
}
//# sourceMappingURL=dataset-folders.js.map