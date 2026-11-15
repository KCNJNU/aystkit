/** Processed-result requirements apply to destination ancestors, never raw data. */
export const PROCESSED_PACKAGE_REQUIREMENT = '处理结果请上传 ZIP 压缩包，包内需包含‘处理前/’图像、‘处理后/’图像和 .json 文件';
export const PROCESSED_PACKAGE_LABEL = 'ZIP · 处理前图像 + 处理后图像 + JSON';
export const PROCESSED_PACKAGE_EXAMPLE = '结果.zip\n├─ 处理前/原图.jpg\n├─ 处理后/结果.jpg\n└─ result.json';

export function isProcessedPackageDestination(folderPath = '', fileName = ''): boolean {
  const relativeParents = fileName.replaceAll('\\', '/').split('/').slice(0, -1);
  return [...folderPath.replaceAll('\\', '/').split('/'), ...relativeParents]
    .some(part => part.startsWith('处理好的数据_') && part.length > '处理好的数据_'.length);
}

export function requireProcessedPackage(datasetId: string, folderPath: string | undefined, fileName: string, _fileSize?: number): boolean {
  void _fileSize; // Keep existing callers compatible without imposing a size limit.
  const processed = Boolean(datasetId) && isProcessedPackageDestination(folderPath, fileName);
  if (processed && !fileName.toLowerCase().endsWith('.zip')) throw new Error(PROCESSED_PACKAGE_REQUIREMENT);
  return processed;
}
