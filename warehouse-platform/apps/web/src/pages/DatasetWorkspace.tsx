import './data-workspace.css';
import './industrial-explorer.css';
import { RelocateFileButton } from '../components/RelocateFileButton';
import { ArrowLeftOutlined, ArrowRightOutlined, ArrowUpOutlined, BarsOutlined, CloudUploadOutlined, DatabaseOutlined, DownloadOutlined, FileOutlined, FolderAddOutlined, FolderOpenOutlined, FolderOutlined, HistoryOutlined, HomeOutlined, MoreOutlined, ReloadOutlined, RightOutlined, VideoCameraOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, App, Breadcrumb, Button, Checkbox, Empty, Grid, Input, Modal, Pagination, Popover, Progress, Skeleton, Space, Table, Typography, Upload } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useOptionalAuth } from '../auth/AuthContext';
import { DeleteFilesButton } from '../components/DeleteFilesButton';
import { DeleteDatasetButton } from '../components/DeleteDatasetButton';
import { DownloadFileButton } from '../components/DownloadFileButton';
import { FilePreview } from '../components/FilePreview';
import { ContinuousRecordingsPanel } from '../components/ContinuousRecordingsPanel';
import { ArchivePackagesPanel } from '../components/ArchivePackagesPanel';
import { PreparedFolderPackagesPanel } from '../components/PreparedFolderPackagesPanel';
import { CaptureSessionsPanel } from '../components/CaptureSessionsPanel';
import { useRecordWorkspaceVisit, WorkspaceFavoriteButton } from '../components/WorkspaceShortcuts';
import { useDirectoryNavigation } from '../hooks/useDirectoryNavigation';
import { useDirectoryShortcuts } from '../hooks/useDirectoryShortcuts';
import { datasetListReturn } from '../services/dataset-navigation';
import { archiveDirectoryCrumbs, archiveEntryLabels, archiveFolderDisplayName, archiveParentPath, canonicalArchivePath, isArchiveCameraDirectory, isArchiveSensorDirectory } from '../services/archive-directory-view';
import { api, type DatasetContents, type RobotFile } from '../services/api';
import { bytes, date } from '../services/format';
import { fileKindLabel, isVideoFile } from '../services/file-kind';
import { startDownloadURL } from '../services/file-download';
import { prepareFolderDownload } from '../services/folder-download';
import { robotPath, selectedRobot } from '../services/robot-selection';
import type { CaptureSession } from '../services/capture-sessions';
import { recordingRange } from '../components/ContinuousRecordingsPanel';
import { useUploadQueue } from '../upload/UploadQueueContext';
import { isProcessedPackageDestination, PROCESSED_PACKAGE_EXAMPLE, PROCESSED_PACKAGE_LABEL, PROCESSED_PACKAGE_REQUIREMENT } from '../services/processed-package';

export function DatasetWorkspace() {
  const { message } = App.useApp();
  const { datasetId } = useParams();
  const [params, setParams] = useSearchParams();
  const path = params.get('path') ?? '';
  const robotId = selectedRobot();
  const captureSession = params.get('captureSession') ?? undefined;
  const [loadedSessionState, setLoadedSession] = useState<{ datasetId: string; session: CaptureSession }>();
  const loadedSession = loadedSessionState && loadedSessionState.datasetId === datasetId && loadedSessionState.session.robotId === robotId ? loadedSessionState.session : undefined;
  const page = Math.max(1, Number(params.get('page') ?? 1) || 1);
  const navigate = useNavigate();
  const screens = Grid.useBreakpoint();
  const compact = screens.md === false;
  const location = useLocation();
  const breadcrumbRef = useRef<HTMLDivElement>(null);
  useEffect(() => { const row = breadcrumbRef.current?.querySelector('ol'); if (row) row.scrollLeft = row.scrollWidth; }, [path]);
  const history = useDirectoryNavigation();
  const navigationState = { datasetsReturnTo: datasetListReturn(location.state) };
  const [previewFile, setPreviewFile] = useState<string>();
  const [rawDirectory, setRawDirectory] = useState<string>();
  const [directoryRailOpen, setDirectoryRailOpen] = useState(() => {
    try { return localStorage.getItem('warehouse-directory-rail') !== 'false'; } catch { return true; }
  });
  const toggleDirectoryRail = () => setDirectoryRailOpen(previous => {
    const next = !previous;
    try { localStorage.setItem('warehouse-directory-rail', String(next)); } catch { /* Keep the session preference when storage is unavailable. */ }
    return next;
  });
  const client = useQueryClient();
  const auth = useOptionalAuth();
  const canWrite = auth?.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR') ?? false;
  const queue = useUploadQueue();
  const active = queue.items.filter(item => item.datasetId === datasetId && (item.folderPath ?? '') === path && !['COMPLETED', 'DUPLICATE', 'CANCELLED'].includes(item.status));
  const dataset = useQuery({ queryKey: ['datasets', 'detail', datasetId], queryFn: () => api.dataset(datasetId!), enabled: Boolean(datasetId) });
  const contents = useQuery({ queryKey: ['datasets', 'contents', datasetId, path, page], queryFn: () => api.datasetContents(datasetId!, path, page), enabled: Boolean(datasetId), refetchInterval: active.some(item => item.status !== 'FAILED') ? 2000 : false });
  useRecordWorkspaceVisit({ ready: Boolean(dataset.data && contents.data && !dataset.isError && !contents.isError
      && (!captureSession || (loadedSession?.sessionId === captureSession && loadedSession.robotId === robotId))),
    datasetId, datasetName: dataset.data?.name, path, captureSession,
    sessionLabel: loadedSession ? recordingRange(loadedSession.startedAt, loadedSession.endAt) : undefined });
  const [selectedFiles, setSelectedFiles] = useState<string[]>([]);
  useEffect(() => { setSelectedFiles([]); setPreviewFile(undefined); }, [datasetId, path, page]);
  const afterDelete = (ids: string[]) => {
    setSelectedFiles(previous => previous.filter(id => !ids.includes(id)));
    if (page > 1 && contents.data?.files.length && contents.data.files.every(file => ids.includes(file.id))) {
      const next = new URLSearchParams(params); next.set('page', String(page - 1));
      setParams(next, { replace: true, state: navigationState });
    }
  };
  const [creating, setCreating] = useState(false);
  const [folderName, setFolderName] = useState('');
  const [saving, setSaving] = useState(false);
  const [createError, setCreateError] = useState<string>();
  const [packingFolder, setPackingFolder] = useState<{ key: string; name: string }>();
  const packingFolderRef = useRef<string | undefined>(undefined);
  const folderDownloadController = useRef<AbortController | undefined>(undefined);
  const [folderDownloadError, setFolderDownloadError] = useState<string>();
  useEffect(() => {
    setPackingFolder(undefined); setFolderDownloadError(undefined);
    return () => {
      folderDownloadController.current?.abort();
      folderDownloadController.current = undefined;
      packingFolderRef.current = undefined;
    };
  }, [datasetId, path]);
  const data = dataset.data;
  const metadata = data?.metadata ?? contents.data?.dataset.metadata;
  const archiveDataset = Boolean(metadata && typeof metadata === 'object' && !Array.isArray(metadata)
    && (metadata as { type?: unknown }).type === 'garage-native-archive');
  const archiveSensor = isArchiveSensorDirectory(metadata, path);
  const archiveCamera = isArchiveCameraDirectory(metadata, path);
  const directoryKey = `${datasetId}:${path}`;
  const showRaw = rawDirectory === directoryKey;
  const rootFolders = path ? client.getQueryData<DatasetContents>(['datasets', 'contents', datasetId, '', 1])?.folders : contents.data?.folders;
  const pageFiles = contents.isError ? [] : contents.data?.files ?? [];
  const pageBytes = pageFiles.reduce((sum, file) => {
    const size = Number(file.fileSize);
    return sum + (Number.isFinite(size) && size > 0 ? size : 0);
  }, 0);
  const crumbs = archiveDirectoryCrumbs(metadata, path, rootFolders);
  const canonicalPath = canonicalArchivePath(metadata, path);
  const { fileLabels, folderLabels } = archiveEntryLabels(metadata, contents.data?.files ?? [], contents.data?.folders ?? []);
  useEffect(() => {
    if (canonicalPath === path) return;
    const next = new URLSearchParams(params);
    next.set('path', canonicalPath);
    setParams(next, { replace: true, state: { datasetsReturnTo: datasetListReturn(location.state) } });
  }, [canonicalPath, path, params, setParams, location.state]);
  const go = (nextPath: string) => {
    if (nextPath !== path || page !== 1 || captureSession) {
      const next = new URLSearchParams(params);
      if (nextPath) next.set('path', nextPath); else next.delete('path');
      next.delete('page'); next.delete('captureSession');
      setParams(next, { state: navigationState });
    }
  };
  const selectCaptureSession = (session: CaptureSession) => {
    const next = new URLSearchParams(params);
    next.set('captureSession', session.sessionId);
    navigate(robotPath(`${location.pathname}?${next}`, session.robotId), { state: navigationState });
  };
  useDirectoryShortcuts({ ...history, canUp: Boolean(path), up: () => go(archiveParentPath(metadata, path)) });
  const addFile = (file: File) => { if (archiveSensor) setRawDirectory(directoryKey); if (data && canWrite && contents.data && !contents.isError) queue.addFiles([file], { id: data.id, name: data.name, folderPath: path }); return Upload.LIST_IGNORE; };
  const uploadReady = Boolean(canWrite && data && contents.data && !contents.isError);
  const processedDirectory = isProcessedPackageDestination(path);
  const showCreate = () => { if (archiveSensor) setRawDirectory(directoryKey); setFolderName(''); setCreateError(undefined); setCreating(true); };
  const create = async () => {
    if (!data || saving || !folderName.trim()) return;
    setSaving(true); setCreateError(undefined);
    try { await api.createDatasetFolder(data.id, path, folderName); setCreating(false); await client.invalidateQueries({ queryKey: ['datasets'] }); }
    catch (error) { setCreateError(error instanceof Error ? error.message : '创建文件夹失败'); }
    finally { setSaving(false); }
  };
  const downloadFolder = async (folderPath: string, name: string) => {
    if (!datasetId || packingFolderRef.current) return;
    const key = `${datasetId}:${folderPath}`;
    const controller = new AbortController();
    folderDownloadController.current = controller;
    packingFolderRef.current = key;
    setPackingFolder({ key, name }); setFolderDownloadError(undefined);
    try {
      const result = await prepareFolderDownload(datasetId, folderPath, controller.signal);
      if (controller.signal.aborted) return;
      startDownloadURL(result.url, result.fileName);
    } catch (error) {
      if (!controller.signal.aborted) setFolderDownloadError(`“${name}”：${error instanceof Error ? error.message : '打包失败，请稍后重试'}`);
    } finally {
      if (folderDownloadController.current === controller) {
        folderDownloadController.current = undefined;
        packingFolderRef.current = undefined;
        setPackingFolder(undefined);
      }
    }
  };
  const fileActions = (file: RobotFile) => <Space size={0} className="explorer-row-actions"><DownloadFileButton file={file} /><Button type="link" onClick={() => setPreviewFile(file.id)}>详情</Button>{canWrite && <Popover trigger="click" placement="bottomRight" content={<Space direction="vertical" size={0}><RelocateFileButton file={file} /><DeleteFilesButton files={[file]} type="link" onDeleted={afterDelete} /></Space>}><Button type="text" aria-label={`更多文件操作 ${file.originalName}`} icon={<MoreOutlined />} /></Popover>}</Space>;
  const directoryContents = <>
      {contents.isError ? <Alert type="error" showIcon message="目录加载失败" description={contents.error.message} action={<Button onClick={() => go('')}>返回根目录</Button>} /> : contents.isPending ? <div className="explorer-loading"><Skeleton active paragraph={{ rows: 6 }} /></div> : <>
        {!!contents.data?.folders.length && <div className="explorer-folders">{contents.data.folders.map(folder => <div className="explorer-folder-card" key={folder.id}>
          <button className="explorer-folder" onClick={() => go(folder.path)} aria-label={`打开文件夹 ${folder.name}${folderLabels.has(folder.id) ? `（${folderLabels.get(folder.id)}）` : ''}`}><span className="folder-art"><FolderOutlined /></span><span className="folder-card-text"><strong className={archiveFolderDisplayName(metadata, folder) !== folder.name ? 'archive-folder-name' : undefined} title={archiveFolderDisplayName(metadata, folder)}>{archiveFolderDisplayName(metadata, folder)}</strong>{folderLabels.has(folder.id) && <span className="archive-entry-label">{folderLabels.get(folder.id)}</span>}</span><RightOutlined /></button>
          <Button type="text" size="small" className="explorer-folder-download" icon={<DownloadOutlined />} aria-label={`下载压缩包 ${folder.path}`} title="包含子文件夹，下载带时间戳的 .tar.zst 压缩包" aria-busy={packingFolder?.key === `${datasetId}:${folder.path}`} loading={packingFolder?.key === `${datasetId}:${folder.path}`} disabled={Boolean(packingFolder)} onClick={event => { event.stopPropagation(); void downloadFolder(folder.path, archiveFolderDisplayName(metadata, folder)); }}>{packingFolder?.key === `${datasetId}:${folder.path}` ? '打包中…' : '下载'}</Button>
        </div>)}</div>}
        {!contents.data?.folders.length && !contents.data?.total ? <div className="explorer-empty"><div className="explorer-empty-icon"><FolderOpenOutlined /></div><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="空文件夹" />{canWrite && <Button icon={<FolderAddOutlined />} onClick={showCreate}>新建子文件夹</Button>}</div> : <>
          {compact && !!contents.data?.files.length && <ul className="explorer-mobile-files" aria-label="文件列表">
            {contents.data.files.map(file => <li key={file.id}>
              <div className="explorer-mobile-file-heading">
                {canWrite && <Checkbox aria-label={`选择文件 ${file.originalName}`} checked={selectedFiles.includes(file.id)} onChange={event => setSelectedFiles(previous => event.target.checked ? [...previous, file.id] : previous.filter(id => id !== file.id))} />}
                <button className="explorer-file-link" onClick={() => setPreviewFile(file.id)}><span>{isVideoFile(file) ? <VideoCameraOutlined /> : <FileOutlined />}</span><span className="explorer-file-copy"><strong>{file.originalName.split('/').at(-1)}</strong><small>{fileKindLabel(file)} · {bytes(file.fileSize)}</small>{fileLabels.has(file.id) && <span className="archive-entry-label">{fileLabels.get(file.id)}</span>}</span></button>
              </div>
              <div className="explorer-mobile-file-footer"><time>{date(file.createdAt)}</time>{fileActions(file)}</div>
            </li>)}
          </ul>}
          {!compact && !!contents.data?.files.length && <Table<RobotFile> size="small" className="explorer-file-table" rowKey="id" dataSource={contents.data?.files ?? []} pagination={false} rowSelection={canWrite ? { selectedRowKeys: selectedFiles, onChange: keys => setSelectedFiles(keys.map(String)) } : undefined} scroll={{ x: 550 }} locale={{ emptyText: '暂无文件' }} columns={[
            { title: '文件名称', dataIndex: 'originalName', render: (name: string, file) => <button className="explorer-file-link" onClick={() => setPreviewFile(file.id)}><span>{isVideoFile(file) ? <VideoCameraOutlined /> : <FileOutlined />}</span><span className="explorer-file-copy"><strong title={name}>{name.split('/').at(-1)}</strong><small>{fileKindLabel(file)} · {bytes(file.fileSize)}</small>{fileLabels.has(file.id) && <span className="archive-entry-label">{fileLabels.get(file.id)}</span>}</span></button> },
            { title: '类型', dataIndex: 'fileType', width: 90, responsive: ['md'], render: (_, file) => fileKindLabel(file) },
            { title: '大小', dataIndex: 'fileSize', width: 100, responsive: ['sm'], render: bytes },
            { title: '上传时间', dataIndex: 'createdAt', width: 170, responsive: ['lg'], render: date },
            { title: '操作', width: canWrite ? 185 : 145, fixed: 'right', render: (_, file) => fileActions(file) },
          ]} />}
          {(contents.data?.total ?? 0) > 50 && <Pagination className="workspace-pagination" current={page} pageSize={50} total={contents.data?.total} showSizeChanger={false} itemRender={(_, type, original) => type === 'prev' ? <span className="pagination-direction">上一页</span> : type === 'next' ? <span className="pagination-direction">下一页</span> : original} onChange={value => { const next = new URLSearchParams(params); next.set('page', String(value)); setParams(next, { state: navigationState }); }} />}
        </>}
      </>}
      {canWrite && <Upload.Dragger className="explorer-drop" multiple accept={processedDirectory ? '.zip' : undefined} onDrop={event => { if (uploadReady && processedDirectory && Array.from(event.dataTransfer.files).some(file => !file.name.toLowerCase().endsWith('.zip'))) void message.error(PROCESSED_PACKAGE_REQUIREMENT); }} disabled={!uploadReady} showUploadList={false} beforeUpload={addFile}><CloudUploadOutlined /><span>将文件拖到这里，上传到当前目录</span></Upload.Dragger>}
  </>;
  return <div className="page-stack data-workspace dataset-explorer">
    <div className="explorer-heading">
      <div><h1><span className="explorer-dataset-icon"><FolderOpenOutlined /></span>{data?.name ?? '数据集文件'}</h1>{data?.description && <p>{data.description}</p>}
        {data && <div className="industrial-dataset-identity"><span><DatabaseOutlined />{archiveDataset ? '地库采集归档' : '数据集'}</span><span>更新于 {date(data.updatedAt)}</span></div>}
      </div>
      <div className="explorer-heading-actions">
        <WorkspaceFavoriteButton />
        <Button icon={<HistoryOutlined />} onClick={() => navigate(`/datasets/${datasetId}/history`, { state: { ...navigationState, directoryReturnTo: location.pathname + location.search } })}>版本管理</Button>
        {data && canWrite && <Popover trigger="click" placement="bottomRight" content={<DeleteDatasetButton dataset={data} onDeleted={() => navigate(navigationState.datasetsReturnTo)} />}><Button aria-label="数据集更多操作" icon={<MoreOutlined />}>更多</Button></Popover>}
      </div>
    </div>
    {dataset.isError && <Alert type="error" showIcon message="无法加载数据集" description={dataset.error.message} />}
    <nav className="explorer-navigation" aria-label="数据集目录导航">
      <div className="explorer-navigation-buttons">
        <Space.Compact className="explorer-history-controls"><Button aria-label="后退" aria-keyshortcuts="Alt+ArrowLeft" title="后退 · Alt + ←" icon={<ArrowLeftOutlined />} disabled={!history.canBack} onClick={history.back}>后退</Button><Button aria-label="前进" aria-keyshortcuts="Alt+ArrowRight" title="前进 · Alt + →" icon={<ArrowRightOutlined />} disabled={!history.canForward} onClick={history.forward}>前进</Button></Space.Compact>
        <Button className="explorer-parent-button" aria-label="返回上级文件夹" aria-keyshortcuts="Alt+ArrowUp" title="上一级 · Alt + ↑" icon={<ArrowUpOutlined />} disabled={!path} onClick={() => go(archiveParentPath(metadata, path))}>上一级</Button>
        <Button icon={<HomeOutlined />} onClick={() => navigate(navigationState.datasetsReturnTo)}>数据集列表</Button>
      </div>
      <div ref={breadcrumbRef} className="explorer-location" aria-label="当前目录">
        <Breadcrumb items={[{ title: <button className="explorer-crumb" onClick={() => go('')}><FolderOutlined /> {data?.name ?? '根目录'}</button> }, ...crumbs.map((crumb, index) => ({ title: index === crumbs.length - 1 ? <span className="explorer-current-crumb" title={crumb.label} aria-current="location">{crumb.label}</span> : <button className="explorer-crumb" title={crumb.label} onClick={() => go(crumb.path)}>{crumb.label}</button> }))]} />
      </div>
    </nav>
    {data && archiveDataset && (!path || captureSession) && !dataset.isError && <CaptureSessionsPanel key={`sessions:${datasetId}:${robotId}`} datasetId={datasetId!} robotId={robotId} robotNames={Object.fromEntries((client.getQueryData<Array<{ id: string; name: string }>>(['fleet-robots']) ?? []).map(robot => [robot.id, robot.name]))} selectedSession={captureSession} onSelect={selectCaptureSession}
      onSessionLoaded={session => setLoadedSession(previous => !session ? undefined : previous && previous.datasetId === datasetId && previous.session.sessionId === session.sessionId && previous.session.robotId === session.robotId && previous.session.startedAt === session.startedAt && previous.session.endAt === session.endAt ? previous : { datasetId: datasetId!, session })}
      renderShortcut={session => <WorkspaceFavoriteButton compact target={{ datasetId: datasetId!, datasetName: data.name, path: '', captureSession: session.sessionId, robotId: session.robotId, sessionLabel: recordingRange(session.startedAt, session.endAt) }} />} />}
    {data && archiveDataset && !dataset.isError && <PreparedFolderPackagesPanel key={datasetId} datasetId={datasetId!} metadata={metadata} rootFolders={rootFolders} />}
    <div className={`industrial-explorer-grid ${directoryRailOpen && !compact ? 'has-directory-rail' : ''}`}>
      {directoryRailOpen && !compact && <aside className="industrial-directory-rail" aria-label="数据集目录与信息">
        <div className="industrial-rail-section">
          <div className="industrial-rail-heading"><span><BarsOutlined />数据目录</span><span>{rootFolders?.length ?? '—'}</span></div>
          <nav className="industrial-directory-index" aria-label="数据集快速目录">
            <button className={!path ? 'is-current' : ''} aria-current={!path ? 'location' : undefined} onClick={() => go('')}><FolderOpenOutlined /><span>全部文件</span></button>
            {(rootFolders ?? []).map(folder => <button key={folder.id} className={path === folder.path || path.startsWith(`${folder.path}/`) ? 'is-current' : ''} aria-current={path === folder.path ? 'location' : undefined} aria-label={`切换至目录 ${folder.name}`} title={archiveFolderDisplayName(metadata, folder)} onClick={() => go(folder.path)}><FolderOutlined /><span>{archiveFolderDisplayName(metadata, folder)}</span><RightOutlined /></button>)}
          </nav>
        </div>
        <div className="industrial-rail-section industrial-directory-info">
          <div className="industrial-rail-heading"><span><DatabaseOutlined />数据集信息</span></div>
          <dl>
            <div><dt>数据集 ID</dt><dd><Typography.Text copyable={data ? { text: data.id, tooltips: ['复制 ID', '已复制'] } : false} className="industrial-dataset-id" title={data?.id}>{data?.id ? data.id.slice(0, 12) : '—'}</Typography.Text></dd></div>
            <div><dt>创建时间</dt><dd>{data ? date(data.createdAt) : '—'}</dd></div>
            <div><dt>版本数量</dt><dd>{data?.versions?.length ?? '—'}</dd></div>
            <div><dt>当前目录文件数</dt><dd>{contents.isError ? '—' : contents.data?.total ?? '—'}</dd></div>
            <div><dt>本页文件容量</dt><dd>{contents.data && !contents.isError ? bytes(pageBytes) : '—'}</dd></div>
            <div><dt>操作权限</dt><dd className="industrial-access-level">{canWrite ? '上传与管理' : '只读访问'}</dd></div>
          </dl>
        </div>
      </aside>}
    <section className="explorer-surface" aria-label="目录内容">
      <div className="explorer-toolbar">
        <div className="explorer-directory-summary"><strong>{crumbs.at(-1)?.label || '全部文件'}</strong><span>{archiveCamera && !showRaw ? '完整录像 · 北京时间' : archiveSensor ? '时间压缩包 · 北京时间' : contents.data ? `${contents.data.folders.length} 个文件夹 · ${contents.data.total} 个文件` : '正在读取目录'}</span></div>
        <Space wrap className="explorer-actions">
          {!compact && <Button className="industrial-directory-toggle" aria-label="切换目录导航" aria-pressed={directoryRailOpen} title={directoryRailOpen ? '收起目录导航' : '展开目录导航'} icon={<BarsOutlined />} onClick={toggleDirectoryRail} />}
          {!!selectedFiles.length && <><span className="explorer-selection-count">已选 {selectedFiles.length} 项</span><DeleteFilesButton batch files={(contents.data?.files ?? []).filter(file => selectedFiles.includes(file.id))} onDeleted={afterDelete} /><Button type="text" onClick={() => setSelectedFiles([])}>取消选择</Button></>}
          <Button aria-label="刷新当前目录" icon={<ReloadOutlined />} loading={contents.isFetching} onClick={() => { void contents.refetch(); if (archiveDataset) void client.invalidateQueries({ queryKey: ['datasets', 'capture-sessions', datasetId, robotId] }); if (archiveSensor) void client.invalidateQueries({ queryKey: ['datasets', 'recordings', datasetId, path] }); }}>刷新</Button>
          {!!path && <Button icon={<DownloadOutlined />} aria-label="下载当前文件夹压缩包" loading={packingFolder?.key === directoryKey} disabled={Boolean(packingFolder) || !data || !contents.data || contents.isError} onClick={() => void downloadFolder(path, crumbs.at(-1)?.label || path)}>下载当前文件夹</Button>}
          {canWrite && <>
            <Button aria-label="新建文件夹" icon={<FolderAddOutlined />} onClick={showCreate} disabled={!uploadReady}>新建文件夹</Button>
            <Upload multiple accept={processedDirectory ? '.zip' : undefined} showUploadList={false} beforeUpload={addFile} disabled={!uploadReady}><Button aria-label="上传文件" type="primary" icon={<CloudUploadOutlined />} disabled={!uploadReady}>上传文件</Button></Upload>
            <Popover trigger="click" placement="bottomRight" content={<Upload multiple directory showUploadList={false} beforeUpload={addFile} disabled={!uploadReady || processedDirectory}><Button icon={<FolderOpenOutlined />} disabled={!uploadReady || processedDirectory}>上传整个文件夹</Button></Upload>}><Button aria-label="更多上传选项" icon={<MoreOutlined />} /></Popover>
          </>}
        </Space>
      </div>
      {canWrite && processedDirectory && <Alert type="info" showIcon message={PROCESSED_PACKAGE_LABEL} description={<pre>{PROCESSED_PACKAGE_EXAMPLE}</pre>} />}
      {packingFolder && <p role="status" aria-label="文件夹压缩包准备状态" className="explorer-folder-package-status">正在准备“{packingFolder.name}”压缩包，包含子文件夹。云端按顺序打包，完成后自动下载。</p>}
      {folderDownloadError && <Alert className="explorer-folder-package-error" type="error" showIcon message="压缩包下载失败" description={folderDownloadError} />}
      {archiveCamera && <ContinuousRecordingsPanel datasetId={datasetId!} path={path} canWrite={canWrite} showRaw={showRaw} onToggleRaw={() => { setRawDirectory(showRaw ? undefined : directoryKey); setSelectedFiles([]); }} onPreview={file => setPreviewFile(file.id)} />}
      {archiveSensor && !archiveCamera && <ArchivePackagesPanel datasetId={datasetId!} path={path} />}
      {archiveSensor ? <div id="archive-original-segments" className="archive-original-segments" hidden={!showRaw}>{directoryContents}</div> : directoryContents}
      {contents.data && !contents.isError && (!archiveSensor || showRaw) && <footer className="industrial-directory-status" aria-label="当前目录文件统计"><span>{contents.data.folders.length} 个文件夹<span className="industrial-status-separator">/</span>{contents.data.total} 个文件</span><span>本页 {bytes(pageBytes)}<span className="industrial-status-separator">/</span>第 {page} 页</span></footer>}
    </section>
    </div>
    {!!active.length && <section className="explorer-uploads"><header><strong>当前目录的上传任务</strong><Button type="link" onClick={() => navigate(`/upload?dataset=${datasetId}&path=${encodeURIComponent(path)}`)}>查看全部上传任务</Button></header>{active.slice(0, 4).map(item => <div className="explorer-upload-row" key={item.id}><CloudUploadOutlined /><Typography.Text ellipsis>{item.fileName}</Typography.Text><Progress percent={item.progress} size="small" status={item.status === 'FAILED' ? 'exception' : 'active'} /><small>{item.error ?? (item.status === 'FINALIZING' ? '正在归档' : item.status === 'QUEUED' ? '等待上传' : '上传中')}</small></div>)}</section>}
    <FilePreview fileId={previewFile} onClose={() => setPreviewFile(undefined)} onDeleted={afterDelete} />
    <Modal open={creating} title="新建文件夹" okText="创建文件夹" cancelText="取消" confirmLoading={saving} okButtonProps={{ disabled: !folderName.trim() || saving }} closable={!saving} maskClosable={!saving} onCancel={() => { if (!saving) setCreating(false); }} onOk={() => void create()}>
      <Typography.Paragraph type="secondary">创建位置：{data?.name}/{path ? `${path}/` : ''}</Typography.Paragraph><Input autoFocus aria-label="文件夹名称" placeholder="例如：图片、标注数据、2026年" maxLength={200} value={folderName} onChange={event => setFolderName(event.target.value)} onPressEnter={() => void create()} />
      <Typography.Paragraph className="folder-name-hint">名称不能包含斜杠。</Typography.Paragraph>{createError && <Alert type="error" showIcon message={createError} />}
    </Modal>
  </div>;
}
