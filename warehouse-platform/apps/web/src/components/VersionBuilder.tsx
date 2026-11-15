import { DeleteOutlined, PlusOutlined, SaveOutlined, SearchOutlined, UndoOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Input, Space, Table, Tag, Typography, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import { api, type DatasetVersion, type RobotFile } from '../services/api';
import { bytes, date } from '../services/format';
import { useOptionalAuth } from '../auth/AuthContext';

export function VersionBuilder({ version, onDirtyChange }: { version: DatasetVersion; onDirtyChange: (dirty: boolean) => void }) {
  const auth = useOptionalAuth(); const canMutate = auth ? auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR') : true;
  const location = useLocation();
  const [params, setParams] = useSearchParams(); const queryClient = useQueryClient();
  const search = params.get('candidateSearch') ?? ''; const page = Number(params.get('candidatePage') ?? 1);
  const initial = useMemo(() => version.files?.map((membership) => membership.robotFile) ?? [], [version.files]);
  const [selected, setSelected] = useState<RobotFile[]>(initial);
  useEffect(() => setSelected(initial), [initial, version.id]);
  const initialKey = useMemo(() => initial.map((file) => file.id).sort().join(','), [initial]); const selectedKey = useMemo(() => selected.map((file) => file.id).sort().join(','), [selected]); const dirty = initialKey !== selectedKey;
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
  const candidates = useQuery({ queryKey: ['datasets', 'version-candidates', version.id, search, page], queryFn: () => api.versionCandidates(version.id, { search, page, pageSize: 8 }), enabled: version.status === 'DRAFT' });
  const save = useMutation({ mutationFn: () => api.replaceVersionFiles(version.id, selected.map((file) => file.id)), onSuccess: async () => { message.success("草稿文件列表已保存"); await Promise.all([queryClient.invalidateQueries({ queryKey: ['datasets'] }), queryClient.invalidateQueries({ queryKey: ['versions'] })]); }, onError: (reason: Error) => message.error(reason.message) });
  const updateSearch = (value: string) => setParams((current) => { if (value) current.set('candidateSearch', value); else current.delete('candidateSearch'); current.set('candidatePage', '1'); return current; }, { state: location.state });
  if (!canMutate) return <Alert showIcon type="info" message="只读权限" description="文件浏览员可以查看和对比版本，但不能修改文件列表。" />;
  if (version.status !== 'DRAFT') return <Alert showIcon type="info" message="此版本只读" description="已冻结、已发布和已归档版本不能修改文件列表。" />;
  const selectedIds = new Set(selected.map((file) => file.id));
  return <div className="version-builder">
    <div className="builder-toolbar"><Typography.Title level={4}>版本编辑器</Typography.Title><Space><Button icon={<UndoOutlined />} disabled={!dirty} onClick={() => setSelected(initial)}>重置</Button><Button type="primary" icon={<SaveOutlined />} loading={save.isPending} disabled={!dirty} onClick={() => save.mutate()}>保存修改</Button></Space></div>
    {dirty && <Alert className="builder-dirty-alert" type="warning" showIcon message="尚有未保存的修改" description="离开页面或切换版本将丢弃未保存的选择。" />}
    <div className="builder-columns">
      <Card variant="borderless" className="builder-pane"><div className="builder-pane-header"><div><strong>可选文件</strong><small>{candidates.data?.total ?? '--'} 个可选文件</small></div><Input.Search allowClear prefix={<SearchOutlined />} defaultValue={search} placeholder="搜索可选文件" onSearch={updateSearch} /></div><Table size="small" rowKey="id" loading={candidates.isLoading} dataSource={candidates.data?.items ?? []} pagination={{ current: page, pageSize: 8, total: candidates.data?.total, showSizeChanger: false, onChange: (next) => setParams((current) => { current.set('candidatePage', String(next)); return current; }, { state: location.state }) }} columns={[{ title: "文件", render: (_, file) => <div><Typography.Text strong>{file.originalName}</Typography.Text><small className="table-secondary">{file.robot?.externalId ?? file.robot?.name}</small></div> }, { title: "类型", dataIndex: 'recognitionType', width: 120, render: (value) => <Tag>{value}</Tag> }, { title: "大小", dataIndex: 'fileSize', width: 85, render: bytes }, { title: '', width: 44, render: (_, file) => <Button aria-label={`添加 ${file.originalName}`} type="text" icon={<PlusOutlined />} disabled={selectedIds.has(file.id)} onClick={() => setSelected((current) => [...current, file])} /> }]} /></Card>
      <Card variant="borderless" className="builder-pane selected-pane"><div className="builder-pane-header"><div><strong>已选文件</strong><small>{selected.length} 个文件 · {bytes(selected.reduce((sum, file) => sum + BigInt(file.fileSize), 0n).toString())}</small></div></div><Table size="small" rowKey="id" dataSource={selected} pagination={{ pageSize: 8, showSizeChanger: false }} columns={[{ title: "文件", render: (_, file) => <div><Typography.Text strong>{file.originalName}</Typography.Text><small className="table-secondary">采集时间 {date(file.capturedAt)}</small></div> }, { title: "数据格式", dataIndex: 'schemaVersion', width: 85 }, { title: "大小", dataIndex: 'fileSize', width: 85, render: bytes }, { title: '', width: 44, render: (_, file) => <Button aria-label={`移除 ${file.originalName}`} danger type="text" icon={<DeleteOutlined />} onClick={() => setSelected((current) => current.filter((item) => item.id !== file.id))} /> }]} /></Card>
    </div>
  </div>;
}
