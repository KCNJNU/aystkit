type FileDescription = { originalName: string; fileType?: string; mimeType?: string };

const videoExtensions = /\.(mkv|mp4|mov|avi|webm|m4v|mjpeg|mjpg|ts)$/i;

export function isVideoFile(file: FileDescription) {
  return file.mimeType?.toLowerCase().startsWith('video/') === true
    || file.fileType?.toLowerCase() === 'video'
    || videoExtensions.test(file.originalName);
}

export function fileKindLabel(file: FileDescription) {
  if (isVideoFile(file)) return '视频';
  return file.fileType || '文件';
}
