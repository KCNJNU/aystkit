import './capture-sessions.css';
import { ClockCircleOutlined, DownloadOutlined, ReloadOutlined, RightOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Empty, Pagination, Skeleton, Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { listCaptureSessions, prepareCaptureSessionDownload, type CaptureSession } from '../services/capture-sessions';
import { startDownloadURL } from '../services/file-download';
import { bytes } from '../services/format';
import { recordingRange } from './ContinuousRecordingsPanel';

const noSessions: CaptureSession[] = [];
function accessDenied(error: unknown) {
  return Boolean(error && typeof error === 'object' && 'status' in error && (error.status === 401 || error.status === 403));
}

export function CaptureSessionsPanel({ datasetId, robotId, robotNames = {}, selectedSession = '', onSelect, onSessionLoaded, renderShortcut }: {
  datasetId: string; robotId: string; selectedSession?: string;
  robotNames?: Record<string, string>;
  onSelect: (session: CaptureSession) => void;
  onSessionLoaded?: (session: CaptureSession | undefined) => void;
  renderShortcut?: (session: CaptureSession) => React.ReactNode;
}) {
  const sessions = useQuery({
    queryKey: ['datasets', 'capture-sessions', datasetId, robotId],
    queryFn: ({ signal }) => listCaptureSessions(datasetId, robotId, signal),
    retry: false, refetchInterval: query => accessDenied(query.state.error) ? false : 30_000,
  });
  const [page, setPage] = useState(1);
  const [packing, setPacking] = useState<string>();
  const [downloadError, setDownloadError] = useState<{ session: CaptureSession; message: string }>();
  const controller = useRef<AbortController | undefined>(undefined);
  const generation = useRef(0);
  const rows = sessions.isError ? noSessions : sessions.data?.sessions ?? noSessions;
  const selected = rows.find(row => row.sessionId === selectedSession && (!robotId || row.robotId === robotId));
  const loaded = useRef(onSessionLoaded);
  loaded.current = onSessionLoaded;
  useEffect(() => {
    const scope = ++generation.current;
    setPage(1); setPacking(undefined); setDownloadError(undefined);
    return () => { generation.current = scope + 1; controller.current?.abort(); controller.current = undefined; };
  }, [datasetId, robotId]);
  useEffect(() => {
    if (selected) {
      setPage(Math.floor(rows.indexOf(selected) / 5) + 1);
      loaded.current?.(selected);
    } else loaded.current?.(undefined);
  }, [selected, rows]);
  useEffect(() => { setPage(previous => Math.min(previous, Math.max(1, Math.ceil(rows.length / 5)))); }, [rows.length]);
  const download = async (row: CaptureSession) => {
    if (controller.current || !row.fileCount) return;
    const scope = generation.current;
    const operation = new AbortController();
    controller.current = operation;
    const key = `${row.robotId}:${row.sessionId}`;
    setPacking(key); setDownloadError(undefined);
    try {
      const result = await prepareCaptureSessionDownload(datasetId, row, operation.signal);
      if (!operation.signal.aborted && scope === generation.current) startDownloadURL(result.url, result.fileName);
    } catch (error) {
      if (!operation.signal.aborted && scope === generation.current)
        setDownloadError({ session: row, message: error instanceof Error ? error.message : '打包失败，请稍后重试' });
    } finally {
      if (controller.current === operation) {
        controller.current = undefined;
        if (scope === generation.current) setPacking(undefined);
      }
    }
  };
  return <section className="capture-sessions" aria-label="整次采集">
    <header className="capture-sessions-heading"><h2><ClockCircleOutlined /> 按次采集{sessions.data && <span>{rows.length} 次</span>}</h2>
      <Button size="small" icon={<ReloadOutlined />} aria-label="刷新采集记录" loading={sessions.isFetching} onClick={() => void sessions.refetch()}>刷新</Button>
    </header>
    {sessions.isPending ? <Skeleton active paragraph={{ rows: 2 }} /> : sessions.isError
      ? <Alert type="error" showIcon message="采集记录加载失败" description={sessions.error.message} action={<Button size="small" onClick={() => void sessions.refetch()}>重试</Button>} />
      : !rows.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可识别的采集记录" />
      : <>
        <ul className="capture-session-list">{rows.slice((page - 1) * 5, page * 5).map(row => {
          const key = `${row.robotId}:${row.sessionId}`;
          const range = recordingRange(row.startedAt, row.endAt);
          return <li className={row === selected ? 'capture-session-row is-selected' : 'capture-session-row'} key={key}>
            <button className="capture-session-select" aria-label={`选择采集 ${range} ${row.sessionId.slice(0, 8)}`} aria-pressed={row === selected} onClick={() => onSelect(row)}>
              <span className="capture-session-time">{range}<RightOutlined /></span>
              <span className="capture-session-facts"><span>{row.sensorCount} 个传感器</span><span>已入仓 {row.fileCount} 个文件</span><span>{bytes(row.totalBytes)}</span><span className="capture-session-id">{row.sessionId.slice(0, 8)}</span></span>
              <span className="capture-session-sensors">{row.sensors.map(sensor => <Tag key={sensor.name}>{sensor.name}</Tag>)}{!robotId && <span title={row.robotId}>{robotNames[row.robotId] ?? `机器人 ${row.robotId.slice(0, 8)}`}</span>}</span>
            </button>
            <div className="capture-session-actions">{renderShortcut?.(row)}<Button icon={<DownloadOutlined />} aria-label={`下载本次已入仓数据 ${row.sessionId.slice(0, 8)}`} loading={packing === key} disabled={Boolean(packing) || !row.fileCount} onClick={() => void download(row)}>整次下载</Button></div>
          </li>;
        })}</ul>
        {rows.length > 5 && <Pagination size="small" current={page} pageSize={5} total={rows.length} showSizeChanger={false} onChange={setPage} />}
      </>}
    {selectedSession && !sessions.isPending && !sessions.isError && !selected && <Alert type="warning" showIcon message="当前机器人未找到这次采集，请选择其他记录。" />}
    {sessions.data && sessions.data.unassigned.fileCount > 0 && <p className="capture-sessions-unassigned">另有 {sessions.data.unassigned.fileCount} 个文件未关联采集记录，可在下方目录下载。</p>}
    <p className="capture-sessions-note">下载本数据集内本次采集的已入仓数据；跨日数据请前往对应日期的数据集。</p>
    {packing && <p className="capture-sessions-progress" role="status">正在打包本次已入仓数据，完成后自动下载。</p>}
    {downloadError && <Alert type="error" showIcon message="整次下载失败" description={downloadError.message} action={<Button size="small" disabled={Boolean(packing)} onClick={() => void download(downloadError.session)}>重新下载</Button>} />}
  </section>;
}
