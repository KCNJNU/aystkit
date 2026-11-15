import './data-workspace.css';
import { statusNames } from '../services/labels';
import { CloudUploadOutlined, ArrowLeftOutlined, BranchesOutlined, CheckCircleOutlined, DatabaseOutlined, PlusOutlined, SafetyCertificateOutlined, SwapOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Col, Descriptions, Modal, Row, Select, Tabs, Timeline, Typography, message } from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { DeleteDatasetButton } from '../components/DeleteDatasetButton';
import { VersionBuilder } from '../components/VersionBuilder';
import { VersionCompare } from '../components/VersionCompare';
import { VersionValidation } from '../components/VersionValidation';
import { DataState, PageHeader, StatCard, VersionTag } from '../components/ui';
import { api, type Dataset, type DatasetVersion } from '../services/api';
import { bytes, date, shortHash } from '../services/format';
import { useUnsavedChanges } from '../components/UnsavedChanges';
import { datasetDirectoryReturn, datasetListReturn } from '../services/dataset-navigation';
import { useOptionalAuth } from '../auth/AuthContext';

export function DatasetVersionsWorkspace() {
  const auth = useOptionalAuth(); const canMutate = auth ? auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR') : true;
  const { datasetId, versionId } = useParams(); const location = useLocation(); const navigate = useNavigate(); const [params] = useSearchParams(); const queryClient = useQueryClient();
  const directoryReturnTo = datasetDirectoryReturn(location.state, datasetId!);
  const navigationState = { directoryReturnTo, datasetsReturnTo: datasetListReturn(location.state) };
  const visit = (target: string) => navigate(target, { state: navigationState });
  const [createOpen, setCreateOpen] = useState(false); const { setDirty, confirmDiscard } = useUnsavedChanges();
  const dataset = useQuery({ queryKey: ['datasets', 'detail', datasetId], queryFn: () => api.dataset(datasetId!), enabled: Boolean(datasetId) });
  const version = useQuery({ queryKey: ['versions', 'detail', versionId], queryFn: () => api.version(versionId!), enabled: Boolean(versionId) });
  const active = location.pathname.endsWith('/compare') ? 'compare' : params.get('tab') ?? 'overview';
  const guard = useCallback((action: () => void) => confirmDiscard(action), [confirmDiscard]);
  const selectVersion = (id: string) => guard(() => visit(`/datasets/${datasetId}/versions/${id}`));
  const selectTab = (key: string) => guard(() => { if (key === 'overview') visit(versionId ? `/datasets/${datasetId}/versions/${versionId}` : `/datasets/${datasetId}/history`); else if (key === 'compare') visit(`/datasets/${datasetId}/compare${versionId ? `?target=${versionId}` : ''}`); else { const target = versionId ?? dataset.data?.versions[0]?.id; if (target) visit(`/datasets/${datasetId}/versions/${target}?tab=${key}`); } });
  const refresh = async () => { await Promise.all([dataset.refetch(), versionId ? version.refetch() : Promise.resolve(), queryClient.invalidateQueries({ queryKey: ['versions', 'page'] })]); };
  const data = dataset.data; const selected = version.data;
  const latest = useMemo(() => data?.versions[0], [data]);
  return <section className="data-workspace versions-workspace">
    <PageHeader title={data?.name ?? "数据集"} eyebrow="数据集版本管理" actions={<><Button icon={<ArrowLeftOutlined />} onClick={() => guard(() => visit(directoryReturnTo))}>返回文件目录</Button>{canMutate && data && <Button icon={<CloudUploadOutlined />} onClick={() => guard(() => visit(`/upload?dataset=${data.id}`))}>上传文件</Button>}{canMutate && <Button type="primary" icon={<PlusOutlined />} onClick={() => guard(() => setCreateOpen(true))}>创建版本</Button>}{canMutate && data && <DeleteDatasetButton dataset={data} beforeOpen={guard} onDeleted={() => { setDirty(false); visit(navigationState.datasetsReturnTo); }} />}</>} />
    {data?.description && <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>{data.description}</Typography.Paragraph>}
    <DataState loading={dataset.isLoading} error={(dataset.error as Error | null)?.message} empty={!data} emptyTitle="数据集不可用">
      {data && <><Row gutter={[12, 12]} className="summary-row"><Col xs={12} lg={6}><StatCard title="版本" value={data.versions.length} /></Col><Col xs={12} lg={6}><StatCard title="最新版本" value={latest ? `v${latest.version}` : '--'} /></Col><Col xs={12} lg={6}><StatCard title="最新版本文件数" value={latest?.fileCount ?? 0} /></Col><Col xs={12} lg={6}><StatCard title="最新版本大小" value={bytes(latest?.totalSize ?? 0)} /></Col></Row>
        <div className="dataset-workspace-layout"><Card variant="borderless" className="workspace-sidebar"><div className="workspace-dataset-icon"><DatabaseOutlined /></div><Typography.Title level={4} ellipsis={{ rows: 2, tooltip: data.name }}>{data.name}</Typography.Title><Typography.Text type="secondary">创建时间 {date(data.createdAt)}</Typography.Text><div className="workspace-version-label">版本列表</div>{data.versions.length ? <Timeline items={data.versions.map((item) => ({ color: item.id === versionId ? 'blue' : item.status === 'PUBLISHED' ? 'green' : 'gray', children: <button className={`workspace-version-button ${item.id === versionId ? 'active' : ''}`} onClick={() => selectVersion(item.id)}><span><strong>v{item.version}</strong><VersionTag status={item.status} /></span><small>{item.fileCount} 个文件 · {bytes(item.totalSize)}</small></button> }))} /> : <Alert type="info" showIcon message="暂无版本" />}</Card>
          <Card variant="borderless" className="workspace-main"><Tabs activeKey={active} onChange={selectTab} items={[{ key: 'overview', label: <span><BranchesOutlined /> 概览</span>, children: <WorkspaceOverview dataset={data} version={selected} onSelectVersion={selectVersion} /> }, { key: 'builder', disabled: !versionId, label: <span><SwapOutlined /> 版本编辑</span>, children: selected ? <VersionBuilder version={selected} onDirtyChange={setDirty} /> : <WorkspacePrompt /> }, { key: 'validation', disabled: !versionId, label: <span><SafetyCertificateOutlined /> 校验</span>, children: selected ? <VersionValidation version={selected} onChanged={() => void refresh()} /> : <WorkspacePrompt /> }, { key: 'compare', label: <span><CheckCircleOutlined /> 版本对比</span>, children: <VersionCompare dataset={data} currentVersionId={versionId} /> }]} /></Card></div></>}
    </DataState>
    {canMutate && data && <CreateVersionModal dataset={data} open={createOpen} onClose={() => setCreateOpen(false)} onCreated={(created) => { setCreateOpen(false); void queryClient.invalidateQueries({ queryKey: ['datasets'] }); visit(`/datasets/${data.id}/versions/${created.id}?tab=builder`); }} />}
  </section>;
}

function WorkspaceOverview({ dataset, version, onSelectVersion }: { dataset: Dataset; version?: DatasetVersion; onSelectVersion: (id: string) => void }) {
  if (!version) return <div className="workspace-overview"><Typography.Title level={4}>数据集概览</Typography.Title><Descriptions bordered size="small" column={{ xs: 1, md: 2 }} items={[{ key: 'name', label: "数据集", children: dataset.name }, { key: 'versions', label: "版本", children: dataset.versions.length }, { key: 'description', label: "说明", span: 2, children: dataset.description ?? '--' }, { key: 'created', label: "创建时间", children: date(dataset.createdAt) }, { key: 'updated', label: "更新时间", children: date(dataset.updatedAt) }]} />{dataset.versions[0] && <Button className="workspace-primary-action" type="primary" onClick={() => onSelectVersion(dataset.versions[0].id)}>打开最新版本</Button>}</div>;
  return <div className="workspace-overview"><div className="version-overview-heading"><div><Typography.Text type="secondary">当前版本</Typography.Text><Typography.Title level={3}>v{version.version}</Typography.Title></div><VersionTag status={version.status} /></div><Descriptions bordered size="small" column={{ xs: 1, md: 2 }} items={[{ key: 'status', label: "生命周期", children: <VersionTag status={version.status} /> }, { key: 'files', label: "文件", children: version.fileCount }, { key: 'size', label: "总大小", children: bytes(version.totalSize) }, { key: 'created', label: "创建时间", children: date(version.createdAt) }, { key: 'manifest', label: "清单存储路径", span: 2, children: version.manifestKey ?? '--' }, { key: 'checksum', label: "清单校验和", span: 2, children: shortHash(version.manifestChecksum) }, { key: 'git', label: "版本标签", children: version.gitTag ?? '--' }, { key: 'dvc', label: "数据版本哈希", children: shortHash(version.dvcHash) }]} /></div>;
}

function WorkspacePrompt() { return <Alert type="info" showIcon message="请选择数据版本" />; }

function CreateVersionModal({ dataset, open, onClose, onCreated }: { dataset: Dataset; open: boolean; onClose: () => void; onCreated: (version: DatasetVersion) => void }) {
  const latest = dataset.versions[0]?.id ?? '__EMPTY__'; const [source, setSource] = useState(latest);
  useEffect(() => { if (open) setSource(latest); }, [latest, open]);
  const create = useMutation({ mutationFn: () => api.createVersion(dataset.id, source === '__EMPTY__' ? { fileIds: [] } : { sourceVersionId: source }), onSuccess: (created) => { message.success(`草稿 v${created.version} 已创建`); onCreated(created); }, onError: (reason: Error) => message.error(reason.message) });
  return <Modal open={open} title="创建数据集版本" okText="创建草稿" confirmLoading={create.isPending} onCancel={onClose} onOk={() => create.mutate()}><div className="create-version-source"><span>初始文件列表</span><Select value={source} onChange={setSource} options={[...dataset.versions.map((version, index) => ({ value: version.id, label: `复制 v${version.version} · ${statusNames[version.status] ?? version.status}${index === 0 ? ' · 最新' : ''}` })), { value: '__EMPTY__', label: "从空版本开始" }]} /></div></Modal>;
}
