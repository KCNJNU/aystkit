import { robotPath, selectedRobot } from '../services/robot-selection';
import { useOptionalAuth } from '../auth/AuthContext';
import { writeRoles } from '../auth/permissions';
import { DatabaseOutlined, FileOutlined, FolderOutlined, SearchOutlined, StarOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Empty, Input, List, Modal, Skeleton } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type SearchItem } from '../services/api';
import { useUnsavedChanges } from './UnsavedChanges';
import { useWorkspaceShortcuts } from './WorkspaceShortcuts';

export function CommandPalette({ open, onClose, onCreateDataset }: { open: boolean; onClose: () => void; onCreateDataset: () => void }) {
  const auth = useOptionalAuth();
  const shortcuts = useWorkspaceShortcuts();
  const canWrite = auth ? auth.hasRole(...writeRoles) : true;
  const [value, setValue] = useState(''); const [query, setQuery] = useState(''); const navigate = useNavigate(); const { confirmDiscard } = useUnsavedChanges();
  useEffect(() => { const handle = window.setTimeout(() => setQuery(value.trim()), 250); return () => window.clearTimeout(handle); }, [value]);
  useEffect(() => { const keydown = (event: KeyboardEvent) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); if (open) onClose(); else document.querySelector<HTMLButtonElement>('.global-search-button')?.click(); } }; window.addEventListener('keydown', keydown); return () => window.removeEventListener('keydown', keydown); }, [open, onClose]);
  useEffect(() => { if (!open) { setValue(''); setQuery(''); } }, [open]);
  const result = useQuery({ queryKey: ['search', selectedRobot(), query], queryFn: () => api.search(query), enabled: open && query.length > 0 });
  const items = useMemo(() => result.data ? Object.values(result.data.groups).flat() : [], [result.data]);
  const commands: SearchItem[] = [{ type: 'file', id: 'command-upload', title: "上传文件", subtitle: "打开上传中心" }, { type: 'dataset', id: 'command-dataset', title: "创建数据集", subtitle: "整理数仓文件" }];
  const select = (item: SearchItem) => {
    if (item.id === 'command-shortcuts') { onClose(); shortcuts?.open(); return; }
    if (item.id === 'command-dataset') { onCreateDataset(); return; }
    confirmDiscard(() => {
      if (item.id === 'command-upload') navigate(robotPath('/upload')); else if (item.type === 'file') navigate(robotPath(`/files?file=${encodeURIComponent(item.id)}`)); else if (item.type === 'dataset') navigate(robotPath(`/datasets/${encodeURIComponent(item.id)}`)); else navigate(robotPath(`/datasets/${encodeURIComponent(item.datasetId ?? '')}/versions/${encodeURIComponent(item.id)}`));
      onClose();
    });
  };
  const displayed = query ? items : [...(shortcuts ? [{ type: 'dataset' as const, id: 'command-shortcuts', title: '收藏与最近', subtitle: '' }] : []), ...(canWrite ? commands : [])];
  return <Modal open={open} onCancel={onClose} footer={null} width={680} className="command-modal" title={null} destroyOnHidden><Input autoFocus size="large" variant="borderless" prefix={<SearchOutlined />} placeholder="搜索文件、数据集、版本、路径或校验和" value={value} onChange={(event) => setValue(event.target.value)} /><div className="command-results">{result.isFetching ? <Skeleton active paragraph={{ rows: 4 }} /> : displayed.length ? <List dataSource={displayed} renderItem={(item) => <List.Item className="command-result" tabIndex={0} role="button" onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(item); } }} onClick={() => select(item)}><span className="command-icon">{item.id === 'command-shortcuts' ? <StarOutlined /> : item.type === 'file' ? <FileOutlined /> : item.type === 'dataset' ? <FolderOutlined /> : <DatabaseOutlined />}</span><List.Item.Meta title={item.title} description={item.subtitle} /></List.Item>} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未找到匹配资源" />}</div></Modal>;
}
