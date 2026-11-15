import { useQuery } from '@tanstack/react-query';
import { Card, Input, Table } from 'antd';
import { useState } from 'react';
import { DataState, PageHeader, StatusTag } from '../components/ui';
import { api, apiBase } from '../services/api';
import { date } from '../services/format';
import './admin-workspace.css';

export function AuditLogs() {
  const [search, setSearch] = useState('');
  const logs = useQuery({ queryKey: ['audit', search], queryFn: () => api.auditLogs({ page: 1, pageSize: 100, search }) });
  return <section className="page-stack admin-workspace"><PageHeader title="审计日志" description="追溯业务变更的操作人、请求结果与发生时间。" actions={<a className="ant-btn" href={`${apiBase}/api/v1/audit-logs/export`} target="_blank" rel="noreferrer">导出 CSV</a>} />
    <Card variant="borderless" className="filter-bar admin-panel admin-filter-bar"><div className="admin-filter-content"><Input.Search aria-label="搜索审计日志" allowClear placeholder="搜索动作或资源" onSearch={setSearch} /></div></Card>
    <DataState loading={logs.isLoading} error={(logs.error as Error | null)?.message} empty={!logs.data?.items.length} emptyTitle="暂无审计记录"><Card variant="borderless" className="table-panel command-table admin-panel admin-table-panel"><Table scroll={{ x: 1050 }} rowKey="id" dataSource={logs.data?.items ?? []} pagination={false} columns={[{ title: '动作', dataIndex: 'action' }, { title: '操作人', render: (_, row) => row.actor ? `${row.actor.displayName} · ${row.actor.email}` : '系统' }, { title: '路由', dataIndex: 'route' }, { title: '结果', dataIndex: 'statusCode', render: (value) => <StatusTag status={value && value >= 400 ? 'FAILED' : 'SUCCEEDED'} label={String(value ?? '--')} /> }, { title: '时间', dataIndex: 'createdAt', render: date }]} /></Card></DataState>
  </section>;
}
