import { selectedRobot } from '../services/robot-selection';
import './data-workspace.css';
import { statusNames } from '../services/labels';
import { HistoryOutlined, SearchOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Card, Col, Grid, Input, Row, Select, Table, Typography } from 'antd';
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom';
import { DataState, PageHeader, StatCard, VersionTag } from '../components/ui';
import { api, type DatasetVersion } from '../services/api';
import { bytes, date, shortHash } from '../services/format';

export function Versions() {
  const robotId = selectedRobot();
  const screens = Grid.useBreakpoint();
  const [params, setParams] = useSearchParams(); const navigate = useNavigate();
  const page = Number(params.get('page') ?? 1); const pageSize = Number(params.get('pageSize') ?? 20); const search = params.get('search') ?? ''; const status = params.get('status') ?? undefined; const sort = params.get('sort') ?? 'updatedAt:desc'; const legacyVersion = params.get('version') ?? undefined;
  const legacy = useQuery({ queryKey: ['datasets', 'legacy-version', legacyVersion], queryFn: () => api.version(legacyVersion!), enabled: Boolean(legacyVersion) });
  const versions = useQuery({ queryKey: ['versions', 'page', robotId, page, pageSize, search, status, sort], queryFn: () => { const [sortBy, sortOrder] = sort.split(':'); return api.versionPage({ page, pageSize, search, status, sortBy, sortOrder }); } });
  const summary = useQuery({ queryKey: ['versions', 'summary', robotId], queryFn: async () => {
    const statuses = ['DRAFT', 'FROZEN', 'PUBLISHED', 'ARCHIVED'] as const;
    const counts = await Promise.all(statuses.map(async (status) => [status, (await api.versionPage({ page: 1, pageSize: 1, status })).total] as const));
    return Object.fromEntries(counts) as Record<DatasetVersion['status'], number>;
  } });
  const update = (values: Record<string, string | number | undefined>) => setParams((current) => { for (const [key, value] of Object.entries(values)) { if (value === undefined || value === '') current.delete(key); else current.set(key, String(value)); } return current; });
  if (legacy.data) return <Navigate replace to={`/datasets/${legacy.data.datasetId}/versions/${legacy.data.id}`} />;
  const pipeline = summary.data;
  return <section className="data-workspace versions-page">
    <PageHeader title="数据集版本" description="跟踪草稿、冻结与发布状态，查看每个版本的文件快照。" />
    <Row gutter={[12, 12]} className="summary-row"><Col xs={12} md={6}><StatCard title="全部版本" value={pipeline ? Object.values(pipeline).reduce((sum, count) => sum + count, 0) : '--'} /></Col><Col xs={12} md={6}><StatCard title="草稿" value={pipeline?.DRAFT ?? '--'} /></Col><Col xs={12} md={6}><StatCard title="已冻结" value={pipeline?.FROZEN ?? '--'} tone="warning" /></Col><Col xs={12} md={6}><StatCard title="已发布" value={pipeline?.PUBLISHED ?? '--'} tone="success" /></Col></Row>
    <Card variant="borderless" className="filter-bar"><div className="filter-section-heading"><strong>查找版本</strong></div><div className="version-filter-grid"><Input.Search allowClear prefix={<SearchOutlined />} defaultValue={search} placeholder="数据集、版本标签或提交编号" onSearch={(value) => update({ search: value, page: 1 })} /><Select allowClear value={status} placeholder="版本状态" onChange={(value) => update({ status: value, page: 1 })} options={['DRAFT', 'FROZEN', 'PUBLISHED', 'ARCHIVED'].map((value) => ({ value, label: statusNames[value] ?? value }))} /><Select value={sort} onChange={(value) => update({ sort: value, page: 1 })} options={[{ value: 'updatedAt:desc', label: "最近更新" }, { value: 'createdAt:desc', label: "最近创建" }, { value: 'version:desc', label: "版本号降序" }, { value: 'status:asc', label: "状态" }]} /></div></Card>
    <DataState loading={versions.isLoading} error={(versions.error as Error | null)?.message} empty={!versions.data?.items.length} emptyTitle="暂无数据集版本">
      <Card variant="borderless" className="table-panel"><Table rowKey="id" dataSource={versions.data?.items ?? []} scroll={{ x: 1320 }} onRow={(record) => ({ onClick: () => navigate(`/datasets/${record.datasetId}/versions/${record.id}`), className: 'clickable-row' })} pagination={{ current: page, pageSize, total: versions.data?.total, showSizeChanger: true, showTotal: (total) => `共 ${total} 个版本`, onChange: (nextPage, nextSize) => update({ page: nextPage, pageSize: nextSize }) }} columns={[{ title: "数据集", width: 210, fixed: screens.lg ? 'left' : undefined, render: (_, row) => <span className="entity-name"><span className="entity-icon"><HistoryOutlined /></span><Typography.Text strong>{row.dataset.name}</Typography.Text></span> }, { title: "版本", dataIndex: 'version', width: 85, render: (value) => <Typography.Text strong>v{value}</Typography.Text> }, { title: "状态", dataIndex: 'status', width: 115, render: (value: DatasetVersion['status']) => <VersionTag status={value} /> }, { title: "文件", dataIndex: 'fileCount', width: 75, align: 'right' }, { title: "大小", dataIndex: 'totalSize', width: 110, align: 'right', render: bytes }, { title: "文件清单", dataIndex: 'manifestChecksum', width: 150, render: (value) => shortHash(value) }, { title: "版本提交编号", dataIndex: 'gitCommit', width: 145, render: (value) => shortHash(value) }, { title: "版本标签", dataIndex: 'gitTag', width: 220, ellipsis: true, render: (value) => value ?? '--' }, { title: "数据版本哈希", dataIndex: 'dvcHash', width: 145, render: (value) => shortHash(value) }, { title: "更新时间", dataIndex: 'updatedAt', width: 175, render: date }]} /></Card>
    </DataState>
  </section>;
}
