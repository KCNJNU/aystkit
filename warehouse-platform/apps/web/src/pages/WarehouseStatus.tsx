import { useQuery } from '@tanstack/react-query';
import { ApiOutlined, CloudServerOutlined, DatabaseOutlined, HddOutlined, ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Descriptions, Table, Tag } from 'antd';
import { PageHeader } from '../components/ui';
import { warehouseApi } from '../services/warehouse';
import { bytes, date } from '../services/format';
import { statusLabels } from '../services/warehouse-status';
import './admin-workspace.css';
export function WarehouseStatus({ apiOnly = false }: { apiOnly?: boolean }) {
  const report = useQuery({ queryKey: ['warehouse-status'], queryFn: warehouseApi.status, refetchInterval: 60000 });
  const data = report.data;
  const icons = { api: <ApiOutlined />, postgresql: <DatabaseOutlined />, redis: <CloudServerOutlined />, objectStorage: <HddOutlined />, qwen: <ThunderboltOutlined /> };
  const names: Record<string, string> = { api: '应用服务', postgresql: '数据库', redis: '任务缓存', objectStorage: '文件存储', qwen: '千问 AI' };
  return <div className="page-stack admin-workspace status-workspace"><PageHeader description="查看服务健康、响应耗时与平台运行信息。" title={apiOnly ? 'API 状态' : '系统状态'} actions={<Button icon={<ReloadOutlined />} loading={report.isFetching} onClick={() => void report.refetch()}>刷新状态</Button>} />
    {report.isError && <Alert type="error" showIcon message="无法获取系统状态" description={report.error.message} />}
    {data && <div className="status-service-grid">{Object.entries(data.services).map(([key, service]) => <a href="#service-details" key={key} className={`status-service-tile ${service.status === 'healthy' ? 'service-ok' : 'service-attention'}`}><div className="status-tile-heading"><span>{icons[key as keyof typeof icons] ?? <ApiOutlined />}</span><span className={`pulse-dot ${service.status === 'healthy' ? 'is-healthy' : 'is-warning'}`} /></div><strong>{names[key] ?? key}</strong><span className="status-tile-value">{service.latency_ms ?? '—'}<small>ms</small></span><span className="status-tile-state">{statusLabels[service.status] ?? service.status}</span></a>)}</div>}
    {!apiOnly && <Card className="admin-panel" title="运行概况" loading={report.isPending}><Descriptions column={{ xs: 1, md: 2 }} items={[
      { key: 'status', label: '平台状态', children: data ? statusLabels[data.status] : '—' },
      { key: 'web', label: "前端版本", children: import.meta.env.VITE_APP_VERSION || '0.1.0' },
      { key: 'version', label: "后端版本", children: data?.version ?? '—' },
      { key: 'environment', label: '环境', children: data ? ({ DEV: '开发环境', DEVELOPMENT: '开发环境', PRODUCTION: '生产环境', TEST: '测试环境', STAGING: '预发布环境' } as Record<string, string>)[data.environment.toUpperCase()] ?? data.environment : '—' },
      { key: 'uptime', label: '运行时长', children: data ? `${data.uptime_seconds} 秒` : '—' },
      { key: 'disk', label: '后端磁盘可用 / 总量', children: data?.disk ? `${bytes(data.disk.available_bytes)} / ${bytes(data.disk.total_bytes)}` : '不可用' },
      { key: 'timestamp', label: '最后更新', children: data ? date(data.timestamp) : '—' },
    ]} /></Card>}
    <Card className="admin-panel admin-table-panel" id="service-details" title="服务检测详情"><Table loading={report.isPending} rowKey="key" pagination={false} scroll={{ x: 720 }} dataSource={Object.entries(data?.services ?? {}).map(([key, value]) => ({ key, ...value }))} columns={[
      { title: '服务', dataIndex: 'key', render: (key: string) => names[key] ?? key },
      { title: '状态', dataIndex: 'status', render: (status: string) => <Tag color={status === 'healthy' ? 'green' : status === 'not_configured' ? 'default' : 'orange'}>{statusLabels[status] ?? status}</Tag> },
      { title: '耗时', dataIndex: 'latency_ms', render: (value?: number) => value == null ? '—' : `${value} ms` },
      { title: '最后检测', dataIndex: 'checked_at', render: date }, { title: '详情', dataIndex: 'message' },
    ]} /></Card>
  </div>;
}
