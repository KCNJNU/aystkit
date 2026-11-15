import { DownOutlined, DownloadOutlined, FileZipOutlined, ReloadOutlined, UpOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Skeleton, Tag } from 'antd';
import { useEffect, useId, useRef, useState } from 'react';
import { api, type DatasetFolder, type PreparedFolderPackage } from '../services/api';
import { archiveFolderDisplayName } from '../services/archive-directory-view';
import { startDownloadURL } from '../services/file-download';

const labels = { QUEUED: '排队中', PROCESSING: '准备中', READY: '已就绪', FAILED: '准备失败' } as const;
const colors = { QUEUED: 'default', PROCESSING: 'processing', READY: 'success', FAILED: 'error' } as const;
const readyFormatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric',
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
function readyTime(value?: string | null) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return undefined;
  const parts = new Map(readyFormatter.formatToParts(new Date(value)).map(part => [part.type, part.value]));
  return `${parts.get('year')}年${parts.get('month')}月${parts.get('day')}日 ${parts.get('hour')}:${parts.get('minute')}（北京时间）`;
}
function accessDenied(error: unknown) {
  return typeof error === 'object' && error !== null && 'status' in error
    && (error.status === 401 || error.status === 403);
}

/** Server-owned preparation survives page navigation, refresh and sign-out. */
export function PreparedFolderPackagesPanel({ datasetId, metadata, rootFolders = [] }: {
  datasetId: string; metadata?: unknown; rootFolders?: DatasetFolder[];
}) {
  const client = useQueryClient();
  const queryKey = ['datasets', 'prepared-folder-packages', datasetId];
  const status = useQuery({
    queryKey,
    queryFn: ({ signal }) => api.preparedFolderPackages(datasetId, signal),
    retry: false,
    refetchOnWindowFocus: true,
    refetchInterval: query => accessDenied(query.state.error) ? false : query.state.data?.active ? 5000 : 10_000,
  });
  const submit = useMutation({ mutationFn: (id: string) => api.prepareDatasetFolderPackages(id) });
  const resetSubmission = submit.reset;
  const submitting = useRef(false);
  const alive = useRef(false);
  const lifetime = useRef(0);
  const downloadController = useRef<AbortController | undefined>(undefined);
  const [downloading, setDownloading] = useState<string>();
  const [downloadError, setDownloadError] = useState<string>();
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  useEffect(() => {
    const scope = ++lifetime.current;
    alive.current = true;
    submitting.current = false;
    resetSubmission(); setDownloading(undefined); setDownloadError(undefined); setExpanded(false);
    return () => {
      lifetime.current = scope + 1; alive.current = false; downloadController.current?.abort();
      downloadController.current = undefined;
    };
  }, [datasetId, resetSubmission]);

  const start = async () => {
    if (submitting.current || !status.data || status.isError || status.data.active) return;
    const scope = lifetime.current;
    submitting.current = true;
    try {
      await client.cancelQueries({ queryKey, exact: true });
      if (!alive.current || lifetime.current !== scope) return;
      const result = await submit.mutateAsync(datasetId);
      if (alive.current && lifetime.current === scope) client.setQueryData(queryKey, result);
    } catch { /* The mutation retains the server's reason for the inline alert. */ }
    finally { if (lifetime.current === scope) submitting.current = false; }
  };
  const download = async (row: PreparedFolderPackage) => {
    if (downloadController.current || row.state !== 'READY' || !row.taskId || !/^[a-f0-9]{64}$/.test(row.taskId)) return;
    const controller = new AbortController();
    downloadController.current = controller;
    setDownloading(row.path); setDownloadError(undefined);
    try {
      // Query only this exact prepared snapshot. Never retry or start generation.
      const result = await api.folderPackage(datasetId, row.path,
        { async: true, taskId: row.taskId, signal: controller.signal });
      if (controller.signal.aborted || !alive.current) return;
      if (result.state !== 'ready' || result.taskId !== row.taskId)
        throw new Error('压缩包尚未就绪或状态已变化，请刷新后重试');
      startDownloadURL(result.url, result.fileName);
    } catch (error) {
      if (!controller.signal.aborted && alive.current) {
        setDownloadError(`${row.path}：${error instanceof Error ? error.message : '下载失败，请稍后重试'}`);
        void status.refetch();
      }
    } finally {
      if (downloadController.current === controller) {
        downloadController.current = undefined;
        if (alive.current) setDownloading(undefined);
      }
    }
  };
  const data = status.isError ? undefined : status.data;
  const failed = data?.items.filter(row => row.state === 'FAILED').length ?? 0;
  const complete = Boolean(data && data.total > 0 && data.ready === data.total);
  const button = submit.isPending ? '正在提交…' : data?.active ? '预打包进行中'
    : failed ? '重试未完成设备' : complete ? '更新预打包' : '一键预打包全部设备';
  return <section className="continuous-recordings" aria-label="全部设备预打包">
    <header className="continuous-recordings-heading"><div><h2><FileZipOutlined /> 全部设备预打包</h2></div>
      <Button icon={<FileZipOutlined />} aria-label={button} loading={submit.isPending}
        disabled={!data || status.isError || Boolean(data.active) || submit.isPending}
        onClick={() => void start()}>{button}</Button>
    </header>
    {status.isPending ? <><p role="status" aria-label="设备预打包进度">正在读取云端预打包状态</p><Skeleton active paragraph={{ rows: 2 }} /></>
      : status.isError ? <Alert type="error" showIcon message="预打包状态读取失败" description={status.error.message}
        action={<Button size="small" icon={<ReloadOutlined />} onClick={() => void status.refetch()}>重新读取预打包状态</Button>} />
      : data && <>
        {data.total ? <p role="status" aria-label="设备预打包进度">已就绪 {data.ready}/{data.total} · 准备中 {data.active} · 失败 {failed}</p>
          : <p>尚未预打包</p>}
        {!!data.items.length && <>
          <Button className="prepared-packages-toggle" block
            icon={expanded ? <UpOutlined /> : <DownOutlined />}
            aria-label={expanded ? '收起设备压缩包列表' : '展开设备压缩包列表'}
            aria-expanded={expanded} aria-controls={listId} onClick={() => setExpanded(value => !value)}>
            {expanded ? '收起设备压缩包列表' : '展开设备压缩包列表'}（{data.total} 个设备）
          </Button>
          <div id={listId} hidden={!expanded}>
          {expanded && <ul className="continuous-recording-list">{data.items.map(row => {
          const folder = rootFolders.find(folder => folder.path === row.path && folder.parentPath === '')
            ?? { name: row.path, path: row.path, parentPath: '' };
          const name = archiveFolderDisplayName(metadata, folder);
          const completedAt = readyTime(row.readyAt);
          return <li key={row.path} className="continuous-recording-item"><article className="continuous-recording-row" aria-label={`预打包设备 ${row.path}`}>
            <span className="continuous-recording-icon"><FileZipOutlined /></span>
            <div className="continuous-recording-copy"><h3>{name}</h3><Tag color={colors[row.state]}>{labels[row.state]}</Tag>
              {row.state === 'READY' && <p>{completedAt ? <time dateTime={row.readyAt!} aria-label={`压缩包就绪时间 ${row.path}`}>就绪于 {completedAt}</time> : '就绪时间待确认'}</p>}
              {row.state === 'FAILED' && <p>{row.errorMessage || '准备失败，请重试未完成设备'}</p>}
              {row.state === 'READY' && !row.taskId && <p>压缩包标识暂不可用，请重新读取状态</p>}
            </div>
            {row.state === 'READY' && <div className="continuous-recording-actions"><Button icon={<DownloadOutlined />}
              aria-label={`下载已就绪压缩包 ${row.path}`} loading={downloading === row.path}
              disabled={Boolean(downloading) || !row.taskId || !/^[a-f0-9]{64}$/.test(row.taskId)}
              onClick={() => void download(row)}>下载压缩包</Button></div>}
          </article></li>;
          })}</ul>}
          </div>
        </>}
      </>}
    {submit.isError && <Alert type="error" showIcon message="预打包提交失败" description={submit.error.message} />}
    {downloadError && <Alert type="error" showIcon message="预打包下载失败" description={downloadError} />}
  </section>;
}
