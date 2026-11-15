import { FolderOpenOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Form, Input, Modal, Select, Space, Typography, message } from 'antd';
import { useRef, useState } from 'react';
import { useOptionalAuth } from '../auth/AuthContext';
import { writeRoles } from '../auth/permissions';
import { api } from '../services/api';
import { FileReferencesButton } from './FileReferencesButton';

type RelocateFileProps = { file: { id: string; originalName: string } };

export function RelocateFileButton(props: RelocateFileProps) {
  return <RelocateFileAction key={props.file.id} {...props} />;
}

function RelocateFileAction({ file }: RelocateFileProps) {
  const auth = useOptionalAuth(); const [open, setOpen] = useState(false);
  if (!auth?.hasRole(...writeRoles)) return null;
  return <><Button type="link" icon={<FolderOpenOutlined />} aria-label={`移动或重命名 ${file.originalName}`} onClick={() => setOpen(true)}>整理</Button>{open && <RelocateDialog key={file.id} file={file} onClose={() => setOpen(false)} />}</>;
}
function RelocateDialog({ file, onClose }: { file: { id: string; originalName: string }; onClose: () => void }) {
  const client = useQueryClient(); const [name, setName] = useState(file.originalName); const [path, setPath] = useState<string>(); const [busy, setBusy] = useState(false); const [error, setError] = useState<string>(); const running = useRef(false);
  const impact = useQuery({ queryKey: ['files', 'impact', file.id], queryFn: () => api.fileImpact(file.id), staleTime: 0 });
  const folders = useQuery({ queryKey: ['datasets', 'folders', impact.data?.datasetId], queryFn: () => api.datasetFolders(impact.data!.datasetId!), enabled: Boolean(impact.data?.datasetId) });
  const target = path ?? impact.data?.folderPath ?? '';
  const changed = name !== file.originalName || target !== impact.data?.folderPath;
  const save = async () => {
    if (running.current || !impact.data?.canRelocate || !changed || !name.trim() || impact.isFetching || folders.isFetching || folders.isError) return;
    running.current = true; setBusy(true); setError(undefined);
    try {
      const result = await api.relocateFile(file.id, name, target);
      if (result.cleanupPending) void message.warning('新路径已生效，旧副本待管理员通过存储扫描清理'); else void message.success('文件位置已更新，MinIO 路径已同步');
      await Promise.all(['files', 'datasets', 'versions', 'storage', 'search'].map(key => client.invalidateQueries({ queryKey: [key] })));
      onClose();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '整理失败'); await impact.refetch(); }
    finally { running.current = false; setBusy(false); }
  };
  return <Modal open title="移动 / 重命名文件" okText="保存路径" cancelText="取消" confirmLoading={busy} okButtonProps={{ disabled: busy || !changed || !name.trim() || !impact.data?.canRelocate || impact.isFetching || folders.isFetching || folders.isError }} cancelButtonProps={{ disabled: busy }} closable={!busy} maskClosable={!busy} keyboard={!busy} onCancel={onClose} onOk={() => void save()}>
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert type="info" showIcon message="在当前数据集内整理文件" description="同步修改 MinIO 路径，保留文件 ID、内容和标签。草稿版本引用随之更新，受保护版本引用不允许修改路径。同名目标不会被覆盖。" />
      {(error || impact.isError || folders.isError) && <Alert type="error" message={error ?? impact.error?.message ?? folders.error?.message} />}
      {impact.data && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>原路径：{impact.data.objectKey}</Typography.Text>}
      {impact.data && !impact.data.canRelocate && <Alert type="warning" message={impact.data.datasetId ? '文件被受保护或范围外的版本引用，无法整理' : '此文件未归属数据集，暂不支持整理路径'} />}
      <Form layout="vertical" style={{ width: '100%' }}><Form.Item label="文件名称（保留原扩展名）"><Input aria-label="新文件名称" value={name} disabled={busy} onChange={event => { setName(event.target.value); }} /></Form.Item><Form.Item label="目标文件夹"><Select aria-label="目标文件夹" style={{ width: '100%' }} value={target} loading={folders.isLoading} disabled={busy || !impact.data?.datasetId} showSearch optionFilterProp="label" options={[{ label: '根目录', value: '' }, ...(folders.data ?? []).map(folder => ({ label: folder.path, value: folder.path }))]} onChange={value => { setPath(value); }} /></Form.Item></Form>
      <FileReferencesButton file={file} disabled={busy} />
    </Space>
  </Modal>;
}
