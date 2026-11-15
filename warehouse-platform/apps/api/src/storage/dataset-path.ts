import { BadRequestException } from '@nestjs/common';

const hasControl = (value: string) => [...value].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127);
const reserved = new Set(['raw', 'uploads', 'manifests', 'dvc', '.dvc', '.git']);
export function datasetFolder(name: string): string {
  const value = name.trim();
  if (!value || value !== name || value === '.' || value === '..' || reserved.has(value.toLowerCase()) || (value.includes('/') || value.includes('\\') || hasControl(value)) || Buffer.byteLength(value) > 200) {
    throw new BadRequestException('数据集名称不能包含斜杠、控制字符、首尾空格或使用系统目录名，且长度不能超过 200 字节');
  }
  return `${value}/`;
}
export function validateFolderPath(path: string): string {
  if (typeof path !== 'string' || Buffer.byteLength(path) > 750) throw new BadRequestException('文件夹路径过长');
  if (!path) return '';
  for (const part of path.split('/')) validateEntryName(part);
  return path;
}
export function validateEntryName(name: string): string {
  if (!name || name !== name.trim() || ['.', '..', '.folder', '.dataset'].includes(name) || name.includes('/') || name.includes('\\') || hasControl(name) || Buffer.byteLength(name) > 200) throw new BadRequestException('名称不能包含斜杠、控制字符、首尾空格或使用保留名称，长度不能超过 200 字节');
  return name;
}
export function uploadDestination(folderPath: string, originalName: string) {
  validateFolderPath(folderPath);
  const parts = originalName.replace(/\\/g, '/').split('/');
  parts.forEach(validateEntryName);
  const name = parts.pop()!;
  const path = validateFolderPath([folderPath, ...parts].filter(Boolean).join('/'));
  return { folderPath: path, name };
}
export function folderMarker(datasetName: string, path: string) { return `${datasetFolder(datasetName)}${validateFolderPath(path)}/.folder`; }
