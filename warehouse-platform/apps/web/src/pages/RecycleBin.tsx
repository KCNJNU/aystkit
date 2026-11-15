import './data-workspace.css';
import { DeleteOutlined, ReloadOutlined, UndoOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Input, Space, Table, Tag, Typography, message } from 'antd';
import { useRef, useState } from 'react';
import { useOptionalAuth } from '../auth/AuthContext';
import { PageHeader } from '../components/ui';
import { api, type RecycledFile } from '../services/api';
import { bytes, date } from '../services/format';

type RecycleAction = { type: 'restore' | 'purge'; files: RecycledFile[] };

export function RecycleBin() {
  const auth = useOptionalAuth(); const client = useQueryClient();
  const canPurge = auth?.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN');
  const [page, setPage] = useState(1); const [search, setSearch] = useState(''); const [selected, setSelected] = useState<string[]>([]);
  const [retry, setRetry] = useState<RecycleAction>(); const [busy, setBusy] = useState(false); const [errors, setErrors] = useState<string[]>([]); const running = useRef(false);
  const query = useQuery({ queryKey: ['recycle-bin', page, search], queryFn: () => api.recycledFiles({ page, pageSize: 20, search }) });
  const selectedFiles = (query.data?.items ?? []).filter(file => selected.includes(file.id));
  const execute = async (type: RecycleAction['type'], files: RecycledFile[]) => {
    if (!files.length || running.current || (type === 'purge' && !canPurge) || (type === 'restore' && files.some(file => file.purgingAt))) return;
    running.current = true; setBusy(true); setErrors([]); setRetry(undefined);
    const failed: RecycledFile[] = []; const failures: string[] = []; const done: string[] = [];
    try {
      for (const file of files) {
        try { if (type === 'restore') await api.restoreFile(file.id); else await api.purgeFile(file.id); done.push(file.id); }
        catch (error) { failed.push(file); failures.push(`${file.originalName}：${error instanceof Error ? error.message : '操作失败'}`); }
      }
      if (done.length) void message.success(`已${type === 'restore' ? '恢复' : '彻底清理'} ${done.length} 个文件`);
      setSelected(current => current.filter(id => !done.includes(id))); setErrors(failures);
      setRetry(failed.length ? { type, files: failed } : undefined);
      if (done.length === query.data?.items.length && page > 1) setPage(value => value - 1);
      await Promise.all(['recycle-bin', 'files', 'datasets', 'dashboard', 'warehouse-overview', 'robots', 'storage', 'search'].map(key => client.invalidateQueries({ queryKey: [key] })));
    } finally { running.current = false; setBusy(false); }
  };
  return <div className="page-stack data-workspace recycle-page">
    <PageHeader title="文件回收站" description="查看已移除的文件，恢复或永久清理。" actions={<Button icon={<ReloadOutlined />} disabled={busy} onClick={() => void query.refetch()}>刷新</Button>} />
    <Alert type="info" showIcon message="回收文件不会自动删除" />
    <Card><Space wrap className="recycle-toolbar"><Input.Search placeholder="搜索回收文件" allowClear disabled={busy} onSearch={value => { setSearch(value); setPage(1); setSelected([]); }} /><Button icon={<UndoOutlined />} disabled={busy || !selectedFiles.length || selectedFiles.some(file => file.purgingAt)} onClick={() => void execute('restore', selectedFiles)}>恢复选中文件</Button>{canPurge && <Button danger icon={<DeleteOutlined />} disabled={busy || !selectedFiles.length} onClick={() => void execute('purge', selectedFiles)}>彻底清理选中文件</Button>}</Space>
      {query.isError && <Alert type="error" showIcon message={query.error.message} />}
      {!!errors.length && <Alert type="error" showIcon message="有文件操作失败" description={<ul>{errors.map((error, index) => <li key={`${index}-${error}`}>{error}</li>)}</ul>}
        action={retry && <Button disabled={busy} onClick={() => void execute(retry.type, retry.files)}>重试未成功的文件</Button>} />}
      <Table rowKey="id" loading={query.isFetching} dataSource={query.data?.items ?? []} scroll={{ x: 1000 }} rowSelection={{ selectedRowKeys: selected, onChange: keys => setSelected(keys.map(String)), getCheckboxProps: () => ({ disabled: busy }) }} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, disabled: busy, onChange: value => { setPage(value); setSelected([]); }, showTotal: total => `共 ${total} 个文件` }} locale={{ emptyText: '回收站为空' }} columns={[
        { title: '文件', width: 280, render: (_, file: RecycledFile) => <><Typography.Text strong>{file.originalName}</Typography.Text><div style={{ overflowWrap: 'anywhere' }}><Typography.Text type="secondary">{file.objectKey}</Typography.Text></div></> },
        { title: '大小', dataIndex: 'fileSize', width: 100, render: bytes },
        { title: '移入时间', dataIndex: 'deletedAt', width: 180, render: date },
        { title: '建议检查日期', dataIndex: 'reviewAt', width: 180, render: date },
        { title: '状态', width: 140, render: (_, file) => <Tag color={file.purgingAt ? 'orange' : 'green'}>{file.purgingAt ? '清理中 / 可重试' : '可恢复'}</Tag> },
        { title: '操作', width: 190, render: (_, file) => <Space><Button type="link" disabled={busy || Boolean(file.purgingAt)} onClick={() => void execute('restore', [file])}>恢复</Button>{canPurge && <Button type="link" danger disabled={busy} onClick={() => void execute('purge', [file])}>{file.purgingAt ? '重试清理' : '彻底清理'}</Button>}</Space> },
      ]} />
    </Card>
  </div>;
}
