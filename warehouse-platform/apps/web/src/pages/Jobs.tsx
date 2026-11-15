import { jobNames, statusNames, resourceNames } from '../services/labels';
import { ReloadOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Button, Card, Progress, Select, Table, Typography } from 'antd';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { DataState, PageHeader, StatusTag } from '../components/ui';
import { api, type JobStatus } from '../services/api';
import { date } from '../services/format';
import './admin-workspace.css';

export function Jobs() {
  const [params, setParams] = useSearchParams(); const navigate = useNavigate(); const page = Number(params.get('page') ?? 1); const status = params.get('status') || undefined;
  const jobs = useQuery({ queryKey: ['jobs', page, status], queryFn: () => api.jobs({ page, pageSize: 20, status }), refetchInterval: 5_000 });
  return <section className="page-stack admin-workspace"><PageHeader title="数据处理任务" description="跟踪后台任务的执行进度、处理结果与关联资源。" actions={<Button icon={<ReloadOutlined />} onClick={() => void jobs.refetch()}>刷新</Button>} />
    <Card variant="borderless" className="filter-bar admin-panel admin-filter-bar"><div className="admin-filter-content"><Select allowClear value={status} placeholder="全部状态" onChange={(value) => setParams(value ? { status: value } : {})} options={['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'].map((value) => ({ value, label: statusNames[value] ?? value }))} /></div></Card>
    <DataState loading={jobs.isLoading} error={(jobs.error as Error | null)?.message} empty={!jobs.data?.items.length} emptyTitle="暂无后台任务"><Card variant="borderless" className="table-panel command-table admin-panel admin-table-panel"><Table scroll={{ x: 1020 }} rowKey="id" dataSource={jobs.data?.items ?? []} onRow={(row) => ({ onClick: () => navigate(`/jobs/${row.id}`), className: 'clickable-row' })} pagination={{ current: page, pageSize: 20, total: jobs.data?.total, onChange: (next) => setParams(status ? { page: String(next), status } : { page: String(next) }) }} columns={[{ title: '任务', render: (_, row) => <div><Typography.Text strong>{jobNames[row.type] ?? row.type}</Typography.Text><small className="table-secondary mono-value">{row.id}</small></div> }, { title: '状态', dataIndex: 'status', width: 130, render: (value: JobStatus) => <StatusTag status={value} /> }, { title: '进度', width: 220, render: (_, row) => <Progress percent={row.progress} size="small" status={row.status === 'FAILED' ? 'exception' : undefined} /> }, { title: '资源', render: (_, row) => row.resourceType ? `${resourceNames[row.resourceType] ?? row.resourceType} · ${row.resourceId ?? '--'}` : '--' }, { title: '创建时间', dataIndex: 'createdAt', width: 180, render: date }]} /></Card></DataState>
  </section>;
}
