import { DownloadOutlined } from '@ant-design/icons';
import { Button, message } from 'antd';
import { useState } from 'react';
import { downloadFile } from '../services/file-download';

export function DownloadFileButton({ file, type = 'link', label = '下载' }: {
  file: { id: string; originalName: string };
  type?: 'link' | 'default' | 'primary';
  label?: string;
}) {
  const [loading, setLoading] = useState(false);
  const download = async () => {
    if (loading) return;
    setLoading(true);
    try { await downloadFile(file); }
    catch (error) { message.error(error instanceof Error ? error.message : '下载失败，请稍后重试'); }
    finally { setLoading(false); }
  };
  return <Button type={type} icon={<DownloadOutlined />} aria-label={`${label === '下载' ? '下载文件' : label} ${file.originalName}`}
    loading={loading} onClick={() => void download()}>{label}</Button>;
}
