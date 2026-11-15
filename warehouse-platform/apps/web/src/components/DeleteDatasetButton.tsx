import { DeleteOutlined } from '@ant-design/icons';
import { useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Popconfirm, message } from 'antd';
import { useRef, useState } from 'react';
import { useOptionalAuth } from '../auth/AuthContext';
import { writeRoles } from '../auth/permissions';
import { api, ApiError, type DatasetBlockingUpload } from '../services/api';

function uploadBlockers(reason: unknown): DatasetBlockingUpload[] {
  if (!(reason instanceof ApiError) || reason.status !== 409 || !reason.payload || typeof reason.payload !== 'object') return [];
  const payload = reason.payload as { code?: unknown; blockingUploads?: unknown };
  if (payload.code !== 'DATASET_UPLOADS_PENDING' || !Array.isArray(payload.blockingUploads)) return [];
  return payload.blockingUploads.filter((value: unknown): value is DatasetBlockingUpload => {
    if (!value || typeof value !== 'object') return false;
    const row = value as Partial<DatasetBlockingUpload>;
    return typeof row.id === 'string' && !!row.id && typeof row.originalName === 'string' && typeof row.status === 'string'
      && typeof row.partCount === 'number' && Number.isSafeInteger(row.partCount) && row.partCount >= 0
      && typeof row.createdAt === 'string' && typeof row.updatedAt === 'string' && typeof row.expiresAt === 'string'
      && typeof row.canAbortEmpty === 'boolean';
  });
}

function uploadTime(value: string) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未知';
}

export function DeleteDatasetButton({ dataset, onDeleted, beforeOpen }: {
  dataset: { id: string; name: string };
  onDeleted?: () => void;
  beforeOpen?: (action: () => void) => void;
}) {
  const auth = useOptionalAuth();
  const canDelete = auth ? auth.hasRole(...writeRoles) : true;
  const client = useQueryClient();
  const running = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [uploads, setUploads] = useState<DatasetBlockingUpload[]>([]);
  const [notice, setNotice] = useState<string>();
  const aborting = useRef<string | undefined>(undefined);
  const [abortingId, setAbortingId] = useState<string>();
  const remove = async () => {
    if (running.current || aborting.current || !canDelete) return;
    running.current = true; setBusy(true); setError(undefined); setNotice(undefined); setUploads([]);
    try {
      const job = await api.deleteDataset(dataset.id, dataset.name);
      onDeleted?.();
      void message.success({ content: <span>已提交删除，正在清理存储文件。<a href={`${import.meta.env.BASE_URL}jobs/${job.id}`}>查看删除进度</a></span>, duration: 8 });
      await Promise.all(['datasets', 'versions', 'files', 'warehouse-overview', 'dashboard', 'search', 'jobs'].map(key => client.invalidateQueries({ queryKey: [key] })));
    } catch (reason) { setError(reason instanceof Error ? reason.message : '删除失败，请重试'); setUploads(uploadBlockers(reason)); }
    finally { running.current = false; setBusy(false); }
  };
  const abortEmpty = async (upload: DatasetBlockingUpload) => {
    if (running.current || aborting.current || !canDelete || !upload.canAbortEmpty || upload.status !== 'CREATED' || upload.partCount !== 0) return;
    aborting.current = upload.id; setAbortingId(upload.id); setNotice(undefined);
    try {
      await api.abortUpload(upload.id);
      setUploads(current => current.filter(item => item.id !== upload.id));
      setError(undefined);
      setNotice('空上传已取消，可重新点击“删除”重试；尚未删除数据集。');
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : '请重试';
      setError(reason instanceof ApiError && reason.status === 403 ? `无权取消该上传：${detail}` : `取消空上传失败：${detail}`);
    } finally { aborting.current = undefined; setAbortingId(undefined); }
  };
  if (!canDelete) return null;
  return <>
    <Button danger icon={<DeleteOutlined />} loading={busy} disabled={busy || !!abortingId} aria-label={`删除数据集 ${dataset.name}`} onClick={() => beforeOpen ? beforeOpen(() => void remove()) : void remove()}>删除</Button>
    {error && <Alert style={{ marginTop: 12 }} type="error" showIcon message={error} />}
    {uploads.length > 0 && <Alert style={{ marginTop: 12 }} type="warning" showIcon message="未完成的上传阻止了删除" description={<ul>{uploads.map(upload => <li key={upload.id}>
      <strong>{upload.originalName}</strong> · {({ CREATED: '待上传', UPLOADING: '上传中', FAILED: '上传失败', FINALIZING: '正在完成' } as Record<string, string>)[upload.status] ?? upload.status} · {upload.partCount} 个已上传分片
      <div>创建：{uploadTime(upload.createdAt)}；最后更新：{uploadTime(upload.updatedAt)}；到期：{uploadTime(upload.expiresAt)}（北京时间）</div>
      {upload.canAbortEmpty && upload.status === 'CREATED' && upload.partCount === 0 && <Popconfirm title="取消这个空上传？" description={`仅取消 ${upload.originalName} 的空上传任务，不会删除数据集或已保存文件。`} okText="确认取消空上传" cancelText="保留" okButtonProps={{ danger: true }} onConfirm={() => abortEmpty(upload)}>
        <Button danger size="small" loading={abortingId === upload.id} disabled={!!abortingId || busy} aria-label={`取消空上传 ${upload.originalName}`}>取消空上传</Button>
      </Popconfirm>}
    </li>)}</ul>} />}
    {notice && <Alert style={{ marginTop: 12 }} type="success" showIcon message={notice} />}
  </>;
}
