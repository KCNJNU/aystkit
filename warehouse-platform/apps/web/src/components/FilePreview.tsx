import { RelocateFileButton } from './RelocateFileButton';
import { FileReferencesButton } from './FileReferencesButton';
import { DeleteFilesButton } from './DeleteFilesButton';
import { statusNames } from '../services/labels';
import { ArrowLeftOutlined, CodeOutlined, FileTextOutlined } from '@ant-design/icons';
import { DownloadFileButton } from './DownloadFileButton';
import { fileKindLabel } from '../services/file-kind';
import { useQuery } from '@tanstack/react-query';
import { Alert, Descriptions, Drawer, Image, Segmented, Skeleton, Space, Tabs, Tag, Tree, Typography } from 'antd';
import { useState } from 'react';
import { api, type RobotFile } from '../services/api';
import { bytes, date } from '../services/format';

export function FilePreview({ fileId, onClose, onDeleted }: { fileId?: string; onClose: () => void; onDeleted?: (ids: string[]) => void }) {
  const [metadataView, setMetadataView] = useState<'tree' | 'code'>('tree');
  const file = useQuery({ queryKey: ['files', 'detail', fileId], queryFn: () => api.file(fileId!), enabled: Boolean(fileId) });
  const preview = useQuery({ queryKey: ['files', 'preview', fileId], queryFn: () => api.preview(file.data!), enabled: Boolean(file.data), retry: false });
  const data = file.data;
  return <Drawer open={Boolean(fileId)} width={820} closeIcon={<span className="file-preview-close"><ArrowLeftOutlined /> 返回</span>} title={<div><Typography.Text strong>文件详情</Typography.Text>{data && <Typography.Text type="secondary" className="drawer-subtitle">{data.originalName}</Typography.Text>}</div>} onClose={onClose} extra={<Space wrap>{data && <RelocateFileButton file={data} />}{data && <FileReferencesButton file={data} />}{data && <DeleteFilesButton files={[data]} onDeleted={ids => { onDeleted?.(ids); onClose(); }} />}{data && <DownloadFileButton file={data} type="default" />}</Space>} className="detail-drawer">
    {file.isLoading && <Skeleton active />}{file.isError && <Alert showIcon type="error" message="无法加载文件" description={(file.error as Error).message} />}
    {data && <Tabs items={[
      { key: 'preview', label: "预览", children: <PreviewContent file={data} preview={preview.data} loading={preview.isLoading} error={preview.error instanceof Error ? preview.error.message : undefined} /> },
      { key: 'metadata', label: "元信息", children: <><Space className="tab-toolbar"><Segmented size="small" value={metadataView} onChange={(value) => setMetadataView(value as 'tree' | 'code')} options={[{ label: "目录树", value: 'tree' }, { label: "代码", value: 'code' }]} /></Space>{metadataView === 'tree' ? <Tree showLine defaultExpandAll treeData={jsonTree(data.metadata ?? {}, 'metadata')} /> : <pre className="json-code">{JSON.stringify(data.metadata ?? {}, null, 2)}</pre>}<Descriptions className="detail-descriptions" size="small" bordered column={2} items={[{ key: 'id', label: 'ID', span: 2, children: <Typography.Text copyable>{data.id}</Typography.Text> }, { key: 'robot', label: "数据来源", children: data.robot?.externalId ?? data.robot?.name ?? data.robotId }, { key: 'type', label: "文件类型", children: fileKindLabel(data) }, { key: 'mime', label: "媒体类型", children: data.mimeType }, { key: 'size', label: "文件大小", children: bytes(data.fileSize) }, { key: 'recognition', label: "识别类型", children: data.recognitionType }, { key: 'schema', label: "数据格式", children: data.schemaVersion }, { key: 'captured', label: "采集时间", children: date(data.capturedAt) }, { key: 'uploaded', label: "上传时间", children: date(data.createdAt) }]} /></> },
      { key: 'storage', label: "存储", children: <Descriptions bordered size="small" column={1} items={[{ key: 'key', label: "存储路径", children: <Typography.Text copyable className="mono-value">{data.objectKey}</Typography.Text> }, { key: 'checksum', label: 'SHA-256', children: <Typography.Text copyable>{data.checksum}</Typography.Text> }, { key: 'provider', label: "服务提供方", children: 'MinIO 对象存储' }, { key: 'integrity', label: "存储引用", children: <Tag color="success">已关联</Tag> }]} /> },
      { key: 'datasets', label: `数据集（${data.datasetFiles?.length ?? 0}）`, children: data.datasetFiles?.length ? <div className="relation-list">{data.datasetFiles.map((membership) => <div key={membership.datasetVersionId}><FileTextOutlined /><span><strong>{membership.datasetVersion.dataset.name}</strong><small>v{membership.datasetVersion.version} · {statusNames[membership.datasetVersion.status] ?? membership.datasetVersion.status}</small></span></div>)}</div> : <Typography.Text type="secondary">此文件尚未被数据集版本引用。</Typography.Text> },
    ]} />}
  </Drawer>;
}

function PreviewContent({ file, preview, loading, error }: { file: RobotFile; preview?: Awaited<ReturnType<typeof api.preview>>; loading: boolean; error?: string }) {
  if (loading) return <Skeleton active />;
  if (error) return <Alert showIcon type="warning" message="预览不可用" description={error} />;
  if (!preview) return null;
  if (preview.kind === 'video') return <Alert showIcon type="info" message="视频文件，可直接下载" action={<DownloadFileButton file={file} type="primary" />} />;
  if (preview.kind === 'image') return <Image src={preview.url} className="file-image-preview" />;
  if (preview.kind === 'json') return <pre className="json-code preview-code">{JSON.stringify(preview.value, null, 2)}</pre>;
  if (preview.kind === 'text') return <pre className="json-code preview-code">{preview.value}</pre>;
  if (preview.kind === 'large') return <Alert showIcon type="info" message="文件较大，请下载查看" />;
  return <div className="inline-empty"><CodeOutlined /> 此文件类型暂不支持在线预览，仍可查看元信息或下载。</div>;
}
function jsonTree(value: unknown, key: string): Array<{ key: string; title: string; children?: ReturnType<typeof jsonTree> }> {
  if (value && typeof value === 'object') return [{ key, title: key, children: Object.entries(value).map(([childKey, child]) => jsonTree(child, `${key}.${childKey}`)[0]) }];
  return [{ key, title: `${key.split('.').at(-1)}: ${String(value)}` }];
}
