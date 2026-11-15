/* eslint-disable react-refresh/only-export-components -- provider and hooks share the workspace boundary */
import './workspace-shortcuts.css';
import { ClockCircleOutlined, CloseOutlined, FolderOutlined, RobotOutlined, StarFilled, StarOutlined, VideoCameraOutlined } from '@ant-design/icons';
import { Button, Drawer, Empty, Tabs, Tooltip, message } from 'antd';
import { createContext, useCallback, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { robotFromSearch, selectedRobot } from '../services/robot-selection';
import { createWorkspaceShortcutsStore, makeWorkspaceShortcut, MAX_WORKSPACE_FAVORITES, parseWorkspaceUrl, type WorkspaceShortcut, type WorkspaceShortcuts, type WorkspaceVisit } from '../services/workspace-shortcuts';
import { useUnsavedChanges } from './UnsavedChanges';

type Context = {
  data: WorkspaceShortcuts;
  current?: WorkspaceShortcut;
  open: () => void;
  recordVisit: (entry: WorkspaceShortcut) => void;
  toggleFavorite: (entry: WorkspaceShortcut) => void;
};
const WorkspaceShortcutsContext = createContext<Context | null>(null);
const kindLabels = { dataset: '数据集', folder: '目录', 'capture-session': '采集' };

function routeKey(pathname: string, search: string) {
  const match = /^\/datasets\/([^/]+)$/.exec(pathname);
  if (!match) return undefined;
  const params = new URLSearchParams(search);
  return makeWorkspaceShortcut({ datasetId: match[1], datasetName: '_', path: params.get('path') ?? '', captureSession: params.get('captureSession') ?? undefined }, robotFromSearch(search) ?? selectedRobot())?.key;
}

export function WorkspaceShortcutsProvider({ userId, robots = [], children }: { userId?: string; robots?: Array<{ id: string; name?: string }>; children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const { confirmDiscard } = useUnsavedChanges();
  const store = useMemo(() => createWorkspaceShortcutsStore(userId ?? '', userId ? undefined : null), [userId]);
  const data = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState('favorites');
  const [current, setCurrent] = useState<{ userId: string; entry: WorkspaceShortcut }>();
  const [messageApi, messageContext] = message.useMessage();
  const currentKey = routeKey(location.pathname, location.search);
  const active = current && current.userId === userId && current.entry.key === currentKey ? current.entry : undefined;
  const recordVisit = useCallback((entry: WorkspaceShortcut) => {
    if (!userId) return;
    setCurrent({ userId, entry }); store.record(entry);
  }, [store, userId]);
  const toggleFavorite = useCallback((entry: WorkspaceShortcut) => {
    if (!userId) return;
    if (!store.toggleFavorite(entry)) void messageApi.info(`最多收藏 ${MAX_WORKSPACE_FAVORITES} 个位置`);
  }, [messageApi, store, userId]);
  const value = useMemo<Context>(() => ({ data, current: active, open: () => setOpen(true), recordVisit, toggleFavorite }), [active, data, recordVisit, toggleFavorite]);
  useEffect(() => { setOpen(false); }, [userId]);
  const visit = (entry: WorkspaceShortcut) => {
    if (!parseWorkspaceUrl(entry.url)) return;
    confirmDiscard(() => { navigate(entry.url); setOpen(false); });
  };
  const robotLabel = (entry: WorkspaceShortcut) => entry.robotId === 'all' ? '全部机器人' : robots.find(robot => robot.id === entry.robotId)?.name || `机器人 ${entry.robotId.slice(0, 8)}`;
  const entries = tab === 'favorites' ? data.favorites : data.recent;
  return <WorkspaceShortcutsContext.Provider value={value}>{children}{messageContext}
    <Drawer className="workspace-shortcuts-drawer" title="收藏与最近" open={open} onClose={() => setOpen(false)} width="min(480px, 100vw)" destroyOnHidden>
      <Tabs activeKey={tab} onChange={setTab} items={[{ key: 'favorites', label: <span><StarOutlined /> 收藏 <span className="shortcut-tab-count">{data.favorites.length}</span></span> }, { key: 'recent', label: <span><ClockCircleOutlined /> 最近 <span className="shortcut-tab-count">{data.recent.length}</span></span> }]} />
      <div className="workspace-shortcuts-actions">
        <WorkspaceFavoriteButton />
        {tab === 'recent' && <Button type="text" disabled={!data.recent.length} onClick={() => store.clearRecent()}>清空最近</Button>}
      </div>
      {!entries.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={tab === 'favorites' ? '暂无收藏' : '暂无访问记录'} /> : <ul className="workspace-shortcuts-list" aria-label={tab === 'favorites' ? '收藏位置' : '最近访问'}>
        {entries.map(entry => <li key={entry.key} className={entry.key === currentKey ? 'is-current' : ''}>
          <button className="workspace-shortcut-target" onClick={() => visit(entry)} title={`${entry.datasetName}${entry.path ? ` / ${entry.path}` : ''}${entry.captureSession ? ` / ${entry.label}` : ''}`}>
            <span className={`workspace-shortcut-icon is-${entry.kind}`}>{entry.kind === 'capture-session' ? <VideoCameraOutlined /> : <FolderOutlined />}</span>
            <span className="workspace-shortcut-copy"><strong>{entry.label}</strong><span>{kindLabels[entry.kind]}{entry.kind !== 'dataset' && <> · {entry.datasetName}</>}{entry.kind === 'folder' && entry.path && <span className="workspace-shortcut-path">{entry.path}</span>}</span><small><RobotOutlined /> {robotLabel(entry)}</small></span>
          </button>
          <div className="workspace-shortcut-tools">
            {tab === 'recent' && <Button type="text" aria-label={`${data.favorites.some(item => item.key === entry.key) ? '取消收藏' : '收藏'} ${entry.label}`} aria-pressed={data.favorites.some(item => item.key === entry.key)} icon={data.favorites.some(item => item.key === entry.key) ? <StarFilled /> : <StarOutlined />} onClick={() => toggleFavorite(entry)} />}
            <Tooltip title={tab === 'favorites' ? '取消收藏' : '移除记录'}><Button type="text" icon={<CloseOutlined />} aria-label={`${tab === 'favorites' ? '取消收藏' : '移除记录'} ${entry.label}`} onClick={() => tab === 'favorites' ? store.removeFavorite(entry.key) : store.removeRecent(entry.key)} /></Tooltip>
          </div>
        </li>)}
      </ul>}
    </Drawer>
  </WorkspaceShortcutsContext.Provider>;
}

export function WorkspaceShortcutsTrigger({ compact = false, onOpen }: { compact?: boolean; onOpen?: () => void }) {
  const context = useContext(WorkspaceShortcutsContext);
  return <button className={`workspace-shortcuts-trigger ${compact ? 'is-compact' : ''}`} aria-label="收藏与最近" title="收藏与最近" onClick={() => { context?.open(); onOpen?.(); }}><StarOutlined />{!compact && <><span>收藏与最近</span><span className="workspace-shortcuts-count">{context?.data.favorites.length ?? 0}</span></>}</button>;
}

export function useWorkspaceShortcuts() { return useContext(WorkspaceShortcutsContext); }

/** Call only when the dataset/directory or selected capture session has loaded successfully. */
export function useRecordWorkspaceVisit(input: Partial<WorkspaceVisit> & { ready: boolean }) {
  const location = useLocation();
  const context = useContext(WorkspaceShortcutsContext);
  const recordVisit = context?.recordVisit;
  const { ready, datasetId, datasetName, path = '', captureSession, sessionLabel, robotId } = input;
  const routeRobot = robotFromSearch(location.search) ?? selectedRobot();
  useEffect(() => {
    if (!ready || !recordVisit || !datasetId || !datasetName) return;
    const entry = makeWorkspaceShortcut({ datasetId, datasetName, path, captureSession, sessionLabel }, robotId ?? routeRobot);
    if (entry && entry.key === routeKey(location.pathname, location.search)) recordVisit(entry);
  }, [captureSession, datasetId, datasetName, location.pathname, location.search, path, ready, recordVisit, robotId, routeRobot, sessionLabel]);
}

export function WorkspaceFavoriteButton({ target, compact = false }: { target?: WorkspaceVisit; compact?: boolean }) {
  const context = useContext(WorkspaceShortcutsContext);
  const location = useLocation();
  const entry = target ? makeWorkspaceShortcut(target, target.robotId ?? robotFromSearch(location.search) ?? selectedRobot()) : context?.current;
  const favorite = Boolean(entry && context?.data.favorites.some(item => item.key === entry.key));
  return <Button className={`workspace-favorite-button ${favorite ? 'is-favorite' : ''}`} type={compact ? 'text' : 'default'} disabled={!entry || !context} aria-label={favorite ? '取消收藏当前位置' : '收藏当前位置'} aria-pressed={favorite} title={favorite ? '取消收藏' : '收藏'} icon={favorite ? <StarFilled /> : <StarOutlined />} onClick={event => { event.stopPropagation(); if (entry) context?.toggleFavorite(entry); }}>{!compact && (favorite ? '已收藏' : '收藏')}</Button>;
}
