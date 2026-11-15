import { ConflictException } from '@nestjs/common';
import { Prisma } from '@prisma/client';
import { ObjectStorage } from '../storage/object-storage';
import { datasetFolder, folderMarker, validateFolderPath } from '../storage/dataset-path';

// Caller holds the dataset row lock, shared with uploads and deletion.
export async function ensureDatasetFolders(tx: Prisma.TransactionClient, storage: ObjectStorage, dataset: { id: string; name: string }, path: string) {
  validateFolderPath(path);
  let parentPath = '';
  for (const name of path ? path.split('/') : []) {
    const childPath = [parentPath, name].filter(Boolean).join('/');
    if (await tx.robotFile.findFirst({ where: { datasetId: dataset.id, folderPath: parentPath, OR: [{ originalName: name }, { objectKey: `${datasetFolder(dataset.name)}${childPath}` }] } })) throw new ConflictException('目录中已有同名文件');
    if (await tx.uploadSession.findFirst({ where: { datasetId: dataset.id, folderPath: parentPath, originalName: name, status: { in: ['CREATED', 'UPLOADING', 'FINALIZING', 'FAILED'] }, expiresAt: { gt: new Date() } } })) throw new ConflictException('目录中有同名文件正在上传');
    const objectKey = folderMarker(dataset.name, childPath);
    const folder = await tx.datasetFolder.upsert({ where: { datasetId_path: { datasetId: dataset.id, path: childPath } }, create: { datasetId: dataset.id, name, path: childPath, parentPath, objectKey }, update: {} });
    await storage.putObject({ objectKey, content: Buffer.from(JSON.stringify({ datasetId: dataset.id, folderId: folder.id })), contentType: 'application/json' });
    parentPath = childPath;
  }
}
