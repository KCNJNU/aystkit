import { api } from './api';

export async function downloadFile(file: { id: string; originalName: string }) {
  const { url } = await api.download(file.id);
  startDownloadURL(url, file.originalName);
}

/** Let the browser stream a download; never buffer an archive in page memory. */
export function startDownloadURL(url: string, fileName: string) {
  if (typeof url !== 'string' || !url.trim()) throw new Error('下载地址无效，请刷新后重试');
  if (typeof fileName !== 'string' || !fileName.trim()) throw new Error('下载文件名无效，请刷新后重试');
  let address: URL;
  try { address = new URL(url, window.location.href); }
  catch { throw new Error('下载地址无效，请刷新后重试'); }
  if (!['http:', 'https:'].includes(address.protocol) || address.username || address.password) throw new Error('下载地址无效，请刷新后重试');
  const link = document.createElement('a');
  link.href = address.href;
  // eslint-disable-next-line no-control-regex -- Download names intentionally remove ASCII control characters.
  const name = fileName.replaceAll('\\', '/').split('/').at(-1)?.replace(/[\u0000-\u001f\u007f]/g, '_');
  link.download = name && name !== '.' && name !== '..' ? name : 'download';
  link.rel = 'noopener noreferrer';
  document.body.append(link);
  try { link.click(); } finally { link.remove(); }
}
