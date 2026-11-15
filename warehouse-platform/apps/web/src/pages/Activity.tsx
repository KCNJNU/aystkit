import { jobNames, resourceNames } from '../services/labels';
import { useQuery } from '@tanstack/react-query';
import { Card, Input, Table, Typography } from 'antd';
import { useState } from 'react';
import { DataState, PageHeader, StatusTag } from '../components/ui';
import { api } from '../services/api';
import { date } from '../services/format';
import './admin-workspace.css';

export function Activity() {
  const [search, setSearch] = useState('');
  const activity = useQuery({ queryKey: ['activity', search], queryFn: () => api.activity({ page: 1, pageSize: 50, search }), refetchInterval: 10_000 });
  return <section className="page-stack admin-workspace"><PageHeader title="活动记录" description="集中查看业务变更、后台任务与最近操作。" />
    <Card variant="borderless" className="filter-bar admin-panel admin-filter-bar"><div className="admin-filter-content"><Input.Search aria-label="搜索活动" allowClear placeholder="搜索活动" onSearch={setSearch} /></div></Card>
    <DataState loading={activity.isLoading} error={(activity.error as Error | null)?.message} empty={!activity.data?.items.length} emptyTitle="暂无活动"><Card variant="borderless" className="table-panel command-table admin-panel admin-table-panel"><Table scroll={{ x: 960 }} rowKey="id" dataSource={activity.data?.items ?? []} pagination={false} columns={[{ title: '事件', render: (_, row) => <div><Typography.Text strong>{jobNames[row.type] ?? row.type}</Typography.Text><small className="table-secondary">{row.source === 'audit' ? '审计记录' : '后台任务'}</small></div> }, { title: '状态', dataIndex: 'status', render: (value) => <StatusTag status={value} /> }, { title: '操作人', render: (_, row) => row.actor?.displayName ?? '系统' }, { title: '资源', render: (_, row) => row.resourceType ? `${resourceNames[row.resourceType] ?? row.resourceType} · ${row.resourceId ?? '--'}` : '--' }, { title: '时间', dataIndex: 'timestamp', render: date }]} /></Card></DataState>
  </section>;
}
