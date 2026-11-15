import { ClockCircleOutlined, DownOutlined, ReloadOutlined, VideoCameraOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Dropdown, Empty, Select, Skeleton, Tag } from 'antd';
import { useEffect, useState } from 'react';
import { api, type ArchiveRecording, type RobotFile } from '../services/api';
import { bytes } from '../services/format';
import { downloadFile } from '../services/file-download';
import { DownloadFileButton } from './DownloadFileButton';

const stateLabels: Record<ArchiveRecording['state'], string> = {
  collecting: '处理中', waiting_end: '处理中', pending: '处理中', processing: '处理中', ready: '可下载', failed: '处理失败',
};
const stateColours: Record<ArchiveRecording['state'], string> = {
  collecting: 'default', waiting_end: 'default', pending: 'default', processing: 'processing', ready: 'success', failed: 'error',
};
const dateFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
});
const timeFormatter = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});

function recordingTime(value: string) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? { day: dateFormatter.format(date), time: timeFormatter.format(date) } : undefined;
}

export function recordingRange(startedAt: string, endAt: string) {
  const start = recordingTime(startedAt), end = recordingTime(endAt);
  if (!start || !end) return '采集时间待确认';
  return `${start.day} ${start.time} — ${start.day === end.day ? '' : `${end.day} `}${end.time}`;
}

export function recordingDuration(value: number) {
  if (!Number.isFinite(value) || value < 0) return '--';
  const total = Math.round(value), hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), seconds = total % 60;
  return `${hours ? `${hours}小时` : ''}${minutes ? `${minutes}分` : ''}${seconds || (!hours && !minutes) ? `${seconds}秒` : ''}`;
}

function internalCaptureGaps(file: RobotFile | null, recording: ArchiveRecording) {
  const metadata = file?.metadata;
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return undefined;
  const value = metadata as { gaps?: unknown; durationSeconds?: unknown };
  if (!Array.isArray(value.gaps) || value.gaps.length > 1000) return undefined;
  const duration = typeof value.durationSeconds === 'number' && Number.isFinite(value.durationSeconds)
    ? Math.max(recording.durationSeconds, value.durationSeconds) : recording.durationSeconds;
  const gaps = value.gaps.filter((row: unknown): row is { durationSeconds: number } => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return false;
    const gap = row as { afterFrame?: unknown; startSeconds?: unknown; endSeconds?: unknown; durationSeconds?: unknown };
    const { afterFrame, startSeconds, endSeconds, durationSeconds } = gap;
    return typeof afterFrame === 'number' && Number.isSafeInteger(afterFrame) && afterFrame > 0 && afterFrame < recording.frameCount
      && typeof startSeconds === 'number' && Number.isFinite(startSeconds) && startSeconds >= 0
      && typeof endSeconds === 'number' && Number.isFinite(endSeconds) && endSeconds > startSeconds && endSeconds <= duration + .1
      && typeof durationSeconds === 'number' && Number.isFinite(durationSeconds) && durationSeconds > 0
      && Math.abs(endSeconds - startSeconds - durationSeconds) <= Math.max(.001, durationSeconds * .000001);
  });
  return gaps.length ? { count: gaps.length, longest: Math.max(...gaps.map(gap => gap.durationSeconds)) } : undefined;
}

function gapSeconds(value: number) {
  return value < .001 ? '小于 0.001' : value.toLocaleString('zh-CN', { maximumFractionDigits: 3 });
}

function sessionTime(value: string) {
  const time = recordingTime(value);
  return time ? `${time.day} ${time.time}` : '时间待确认';
}

function captureSession(recording: ArchiveRecording) {
  return recording.captureSessionId || recording.closureSessionId || '';
}

/** Sealed uploads remain downloadable while whole-recording assembly continues. */
export function ContinuousRecordingsPanel({ datasetId, path, canWrite, showRaw, onToggleRaw, onPreview }: {
  datasetId: string; path: string; canWrite: boolean; showRaw: boolean;
  onToggleRaw: () => void; onPreview: (file: RobotFile) => void;
}) {
  const client = useQueryClient();
  const [selectedSession, setSelectedSession] = useState('');
  useEffect(() => { setSelectedSession(''); }, [datasetId, path]);
  const queryKey = ['datasets', 'recordings', datasetId, path];
  const recordings = useQuery({
    queryKey, queryFn: () => api.datasetRecordings(datasetId, path),
    refetchInterval: query => query.state.data?.recordings.some(recording => recording.closed && ['pending', 'processing'].includes(recording.state)) ? 3000 : 15000,
  });
  const packageDownload = useMutation({
    mutationFn: (recordingId: string) => api.archiveRecordingPackage(datasetId, recordingId),
    onSuccess: ({ url, fileName }) => { const link = document.createElement('a'); link.href = url; link.download = fileName; link.rel = 'noopener'; document.body.appendChild(link); link.click(); link.remove(); },
  });
  const retry = useMutation({
    mutationFn: (recordingId: string) => api.assembleDatasetRecording(datasetId, recordingId),
    onSuccess: () => client.invalidateQueries({ queryKey }),
  });
  const savedVideoDownload = useMutation({ mutationFn: downloadFile });
  const rows = recordings.data?.recordings ?? [];
  const sessions = new Map<string, { startedAt: string; hasControlTime: boolean }>();
  for (const recording of rows) {
    const id = captureSession(recording);
    if (!id) continue;
    const startedAt = recording.controlSession?.startedAt ?? recording.startedAt;
    const old = sessions.get(id);
    if (!old || startedAt < old.startedAt) sessions.set(id, { startedAt, hasControlTime: Boolean(recording.controlSession) });
  }
  const visibleRows = selectedSession ? rows.filter(recording => captureSession(recording) === selectedSession) : rows;
  return <section className="continuous-recordings" aria-label="采集视频">
    <header className="continuous-recordings-heading">
      <div><h2><VideoCameraOutlined /> 采集视频{recordings.data && <span>{recordings.data.recordings.length} 条</span>}</h2></div>
      <Button aria-expanded={showRaw} aria-controls="archive-original-segments" onClick={onToggleRaw}>{showRaw ? '收起原始分段' : '查看原始分段'}</Button>
    </header>
    {sessions.size > 0 && <div className="continuous-recording-filter"><label htmlFor="capture-session-filter">采集会话</label><Select id="capture-session-filter" aria-label="采集会话" value={selectedSession} onChange={setSelectedSession}
      showSearch optionFilterProp="label" popupMatchSelectWidth={false} options={[{ value: '', label: `全部采集会话（${rows.length} 条录像）` },
        ...[...sessions].sort((a, b) => b[1].startedAt.localeCompare(a[1].startedAt)).map(([id, session]) => ({ value: id,
          label: `${session.hasControlTime ? '控制端' : '设备'} ${sessionTime(session.startedAt)} 开始 · ${id.slice(0, 8)}` }))]} /></div>}
    {recordings.isPending ? <Skeleton active paragraph={{ rows: 3 }} /> : recordings.isError
      ? <Alert showIcon type="error" message="视频加载失败" description={recordings.error.message} action={<Button size="small" aria-label="重新加载" icon={<ReloadOutlined />} onClick={() => void recordings.refetch()}>重新加载</Button>} />
      : !recordings.data?.recordings.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无录像" />
        : !visibleRows.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前会话暂无录像" />
        : <ul className="continuous-recording-list">{visibleRows.map(recording => {
          const waitingForEnd = !recording.closed || ['collecting', 'waiting_end'].includes(recording.state);
          const state = waitingForEnd ? 'waiting_end' : recording.state;
          const file = !waitingForEnd && state === 'ready' ? recording.file : null;
          const savedVideos = file ? [] : recording.downloadableVideos ?? [];
          const downloadable = Boolean(file || savedVideos.length);
          const displayState = downloadable ? 'ready' : state;
          const gap = recording.gapBeforeSeconds;
          const internalGaps = internalCaptureGaps(file, recording);
          return <li key={recording.id} className="continuous-recording-item">
            {typeof gap === 'number' && gap > .5 && <div className="continuous-recording-gap"><ClockCircleOutlined /> 此前有 {recordingDuration(gap)}的采集空档，录像已分开保存</div>}
            <article className="continuous-recording-row" aria-label={`录像 ${recordingRange(recording.startedAt, recording.endAt)}`}>
              <span className="continuous-recording-icon"><VideoCameraOutlined /></span>
              <div className="continuous-recording-copy">
                <h3>{recordingRange(recording.startedAt, recording.endAt)}</h3>
                {recording.controlSession && <p className="continuous-recording-session">控制端会话开始：{sessionTime(recording.controlSession.startedAt)} · {captureSession(recording).slice(0, 8)}</p>}
                {recording.controlSession?.nativeTimeOutsideSession && <p role="note">设备时钟与控制端不一致；上方保留设备原始时间，可按控制端采集会话查找。</p>}
                <div className="continuous-recording-facts"><span>时长 {recordingDuration(recording.durationSeconds)}</span><span>{recording.frameCount.toLocaleString('zh-CN')} 帧</span>{file && <span>{bytes(file.fileSize)}</span>}<Tag color={stateColours[displayState]}>{stateLabels[displayState]}</Tag></div>
                {recording.captureIncomplete && <p role="note">{recording.closureReason === 'recovered_interruption' ? '中断恢复，可能缺帧；仅包含已核对的可恢复视频，原始时间和采集空档已保留。' : '采集有缺帧或暂停，视频保留实际采集时间和空档。'}</p>}
                {internalGaps && <p role="note" className="continuous-recording-internal-gap"><ClockCircleOutlined /> 录像内包含 {internalGaps.count} 处采集空档，最长 {gapSeconds(internalGaps.longest)} 秒；已保留真实时间轴</p>}
                {savedVideos.length > 0 && <p>已保存 {savedVideos.length} 段视频</p>}
                {state === 'failed' && <p className="continuous-recording-error">{recording.error || '完整视频生成失败，原始分段仍可查看和下载。'}</p>}
              </div>
              <div className="continuous-recording-actions">
                {file && <><Button aria-label="详情" onClick={() => onPreview(file)}>详情</Button><DownloadFileButton file={file} type="default" label="下载完整视频" /><Button type="primary" loading={packageDownload.isPending && packageDownload.variables === recording.id} disabled={packageDownload.isPending} onClick={() => packageDownload.mutate(recording.id)}>下载时间压缩包</Button></>}
                {savedVideos.length === 1 && <DownloadFileButton file={savedVideos[0]} type="default" label="下载视频" />}
                {savedVideos.length > 1 && <Dropdown trigger={['click']} menu={{ items: savedVideos.map((video, index) => ({ key: video.id,
                  label: <span className="continuous-recording-part-option"><span>{recordingRange(video.startedAt, video.endAt)}</span><small>第 {index + 1} 段 · {bytes(video.fileSize)}</small></span> })),
                  onClick: ({ key }) => { const video = savedVideos.find(item => item.id === key); if (video) savedVideoDownload.mutate(video); } }}>
                  <Button aria-label={`下载视频（${savedVideos.length}段）`} loading={savedVideoDownload.isPending}>下载视频（{savedVideos.length}段）<DownOutlined aria-hidden /></Button>
                </Dropdown>}
                {canWrite && !waitingForEnd && state === 'failed' && <Button aria-label="重新生成" icon={<ReloadOutlined />} loading={retry.isPending && retry.variables === recording.id} disabled={retry.isPending} onClick={() => retry.mutate(recording.id)}>重新生成</Button>}
              </div>
            </article>
          </li>;
        })}</ul>}
    {retry.isError && <Alert className="continuous-recording-retry-error" showIcon type="error" message="重新生成失败" description={retry.error.message} />}
    {packageDownload.isError && <Alert className="continuous-recording-retry-error" showIcon type="error" message="压缩包下载失败" description={packageDownload.error.message} />}
    {savedVideoDownload.isError && <Alert className="continuous-recording-retry-error" showIcon type="error" message="视频下载失败" description={savedVideoDownload.error.message} />}
  </section>;
}
