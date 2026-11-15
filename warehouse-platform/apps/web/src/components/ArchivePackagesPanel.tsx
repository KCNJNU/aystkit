import { ClockCircleOutlined, DownloadOutlined, ReloadOutlined } from '@ant-design/icons';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Alert, Button, Empty, Skeleton, Tag } from 'antd';
import { api, type ArchivePackage } from '../services/api';
import { bytes } from '../services/format';

const dateFormatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });
const timeFormatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
function range(row: ArchivePackage) {
  const start = new Date(row.startedAt), end = new Date(row.endAt);
  if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) return '采集时间待确认';
  return `${dateFormatter.format(start)} ${timeFormatter.format(start)} — ${timeFormatter.format(end)}`;
}

export function ArchivePackagesPanel({ datasetId, path }: { datasetId: string; path: string }) {
  const packages = useQuery({ queryKey: ['datasets', 'archive-packages', datasetId, path], queryFn: () => api.archivePackages(datasetId, path), refetchInterval: 15000 });
  const download = useMutation({
    mutationFn: (id: string) => api.archiveRecordingPackage(datasetId, id),
    onSuccess: ({ url, fileName }) => { const link = document.createElement('a'); link.href = url; link.download = fileName; link.rel = 'noopener'; document.body.appendChild(link); link.click(); link.remove(); },
  });
  if (packages.isPending) return <section className="continuous-recordings" aria-label="压缩归档"><Skeleton active paragraph={{ rows: 3 }} /></section>;
  if (packages.isError) return <section className="continuous-recordings" aria-label="压缩归档"><Alert showIcon type="error" message="压缩归档加载失败" description={packages.error.message} action={<Button size="small" aria-label="重新加载" icon={<ReloadOutlined />} onClick={() => void packages.refetch()}>重新加载</Button>} /></section>;
  const rows = packages.data?.packages ?? [];
  return <section className="continuous-recordings" aria-label="压缩归档">
    <header className="continuous-recordings-heading"><div><h2><DownloadOutlined /> 时间压缩包<span>{rows.length} 个 · 北京时间</span></h2></div></header>
    {!rows.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可下载压缩包" /> : <ul className="continuous-recording-list">{rows.map(row => <li key={row.id} className="continuous-recording-item"><article className="continuous-recording-row" aria-label={`压缩包 ${range(row)}`}><span className="continuous-recording-icon"><ClockCircleOutlined /></span><div className="continuous-recording-copy"><h3>{range(row)}</h3><div className="continuous-recording-facts"><span>{bytes(row.bytes)}</span><span>{row.fileCount} 个已压缩文件</span><Tag color="success">可下载</Tag></div></div><div className="continuous-recording-actions"><Button type="primary" icon={<DownloadOutlined />} loading={download.isPending && download.variables === row.id} disabled={download.isPending} onClick={() => download.mutate(row.id)}>下载时间压缩包</Button></div></article></li>)}</ul>}
    {download.isError && <Alert className="continuous-recording-retry-error" showIcon type="error" message="压缩包下载失败" description={download.error.message} />}
  </section>;
}
