import { DisconnectOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Modal, Space, Table, Tag, Typography, message } from 'antd';
import { useRef, useState } from 'react';
import { useOptionalAuth } from '../auth/AuthContext';
import { writeRoles } from '../auth/permissions';
import { api } from '../services/api';
import { date } from '../services/format';
import { statusNames } from '../services/labels';

type FileReferencesProps = { file: { id: string; originalName: string }; disabled?: boolean };

export function FileReferencesButton(props: FileReferencesProps) {
  return <FileReferencesAction key={props.file.id} {...props} />;
}

function FileReferencesAction({ file, disabled = false }: FileReferencesProps) {
  const auth = useOptionalAuth();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const running = useRef(false);
  const [error, setError] = useState<string>();
  const refs = useQuery({ queryKey: ['files', 'references', file.id], queryFn: () => api.fileReferences(file.id), enabled: open, staleTime: 0 });
  const actionable = selected.filter(id => refs.data?.references.some(ref => ref.versionId === id && ref.canDetach));
  const detach = async () => {
    if (running.current || refs.isFetching || refs.isError || !actionable.length || !auth?.hasRole(...writeRoles)) return;
    running.current = true; setBusy(true); setError(undefined);
    try {
      const result = await api.detachFileReferences(file.id, actionable);
      setSelected([]);
      void message.success(`已解除 ${result.detachedCount} 处引用${result.remainingCount ? `，仍有 ${result.remainingCount} 处引用` : '，现在可以删除文件'}`);
      await Promise.all(['files', 'datasets', 'versions', 'dataset', 'version', 'dashboard', 'warehouse-overview'].map(key => client.invalidateQueries({ queryKey: [key] })));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '解除引用失败，请刷新后重试');
      setSelected([]); await refs.refetch();
    } finally { running.current = false; setBusy(false); }
  };
  if (!auth?.hasRole(...writeRoles)) return null;
  return <>
    <Button type="link" icon={<DisconnectOutlined />} disabled={disabled} aria-label={`解除引用 ${file.originalName}`} onClick={() => { setSelected([]); setError(undefined); setOpen(true); }}>解除引用</Button>
    <Modal title="查看并解除文件引用" open={open} width={800} okText="解除选中引用" cancelText="关闭" confirmLoading={busy}
      okButtonProps={{ danger: true, disabled: busy || refs.isFetching || refs.isError || !actionable.length }} cancelButtonProps={{ disabled: busy }}
      closable={!busy} maskClosable={!busy} keyboard={!busy} onCancel={() => setOpen(false)} onOk={() => void detach()}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text strong style={{ overflowWrap: 'anywhere' }}>{file.originalName}</Typography.Text>
        <Alert type="warning" showIcon message="解除引用将修改选中版本的文件清单" description="仅允许修改草稿版本。解除后这些版本将不再包含此文件，可能影响后续校验与发布；原始文件仍保留。所有引用解除后，可以删除文件。冻结、已发布及归档版本受保护。" />
        {(error || refs.isError) && <Alert type="error" showIcon message={error ?? (refs.error as Error).message} action={<Button disabled={busy} onClick={() => void refs.refetch()}>刷新</Button>} />}
        {!!refs.data?.restrictedCount && <Alert type="info" showIcon message={`另有 ${refs.data.restrictedCount} 处引用不在当前可操作范围，请联系相关数据集管理员处理。`} />}
        <Table size="small" rowKey="versionId" loading={refs.isFetching} dataSource={refs.data?.references ?? []} scroll={{ x: 620 }} pagination={{ pageSize: 8, hideOnSinglePage: true }} locale={{ emptyText: refs.isError ? '引用信息加载失败' : '没有可见的版本引用' }}
          rowSelection={{ selectedRowKeys: selected, onChange: keys => { setSelected(keys.map(String).slice(0, 100)); }, getCheckboxProps: row => ({ disabled: busy || !row.canDetach, 'aria-label': `选择 ${row.datasetName} 第 ${row.version} 版` }) }}
          columns={[
            { title: '数据集', dataIndex: 'datasetName', ellipsis: true },
            { title: '版本', dataIndex: 'version', width: 70, render: value => `第 ${value} 版` },
            { title: '状态', dataIndex: 'status', width: 90, render: value => <Tag>{statusNames[value] ?? value}</Tag> },
            { title: '引用时间', dataIndex: 'referencedAt', width: 155, render: value => date(value) },
            { title: '操作说明', render: (_, row) => row.reason ?? '可以解除引用' },
          ]} />
        {refs.data?.total === 0 && <Alert type="success" showIcon message="该文件已无版本引用，可以关闭弹窗后删除文件。" />}
      </Space>
    </Modal>
  </>;
}
