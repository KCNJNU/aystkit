import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api, type ArchiveRecording, type RobotFile } from '../services/api';
import { ContinuousRecordingsPanel, recordingDuration, recordingRange } from './ContinuousRecordingsPanel';

vi.mock('../services/api', () => ({ api: { datasetRecordings: vi.fn(), assembleDatasetRecording: vi.fn(), archiveRecordingPackage: vi.fn(), download: vi.fn() } }));

const video = { id: 'complete-file', originalName: '原相机前_12-30-47_12-36-44.mkv', fileSize: '93000000' } as RobotFile;
const base: ArchiveRecording = {
  id: 'recording-one', sensor: '原相机前', robotId: 'robot-one', startedAt: '2026-10-31T04:30:47Z', endAt: '2026-10-31T04:36:44Z',
  partCount: 7, sourceBytes: '93000000', frameCount: 8897, durationSeconds: 357, state: 'ready', closed: true, gapBeforeSeconds: null, file: video, jobId: 'job-one', error: null,
};
function mount(rows: ArchiveRecording[], canWrite = false, props: { showRaw?: boolean; onToggleRaw?: () => void; onPreview?: (file: RobotFile) => void } = {}) {
  vi.mocked(api.datasetRecordings).mockResolvedValue({ datasetId: 'dataset-one', path: '原相机前', recordings: rows });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onPreview = props.onPreview ?? vi.fn(), onToggleRaw = props.onToggleRaw ?? vi.fn();
  const rendered = render(<ConfigProvider theme={{ token: { motion: false } }}><QueryClientProvider client={client}><ContinuousRecordingsPanel datasetId="dataset-one" path="原相机前" canWrite={canWrite} showRaw={props.showRaw ?? false} onToggleRaw={onToggleRaw} onPreview={onPreview} /></QueryClientProvider></ConfigProvider>);
  return { client, onPreview, onToggleRaw, unmount: rendered.unmount };
}

describe('Complete archive recordings', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('downloads the actual complete file and opens its details for a viewer', async () => {
    vi.mocked(api.download).mockResolvedValue({ url: '/objects/complete-file' });
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      const { onPreview } = mount([base]);
      expect(await screen.findByText('2026-10-31 12:30:47 — 12:36:44')).toBeVisible();
      expect(screen.getByText('时长 5分57秒')).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: '详情' }));
      expect(onPreview).toHaveBeenCalledWith(video);
      fireEvent.click(screen.getByRole('button', { name: `下载完整视频 ${video.originalName}` }));
      await waitFor(() => expect(api.download).toHaveBeenCalledWith('complete-file'));
      expect(download).toHaveBeenCalledTimes(1);
      expect(api.assembleDatasetRecording).not.toHaveBeenCalled();
    } finally { download.mockRestore(); }
  });

  it('keeps a real capture gap visible and each complete recording distinct', async () => {
    const second = { ...base, id: 'recording-two', startedAt: '2026-10-31T04:37:11Z', endAt: '2026-10-31T04:37:16Z', durationSeconds: 5.2, frameCount: 131, partCount: 1, gapBeforeSeconds: 27.468, file: { ...video, id: 'second-file', originalName: 'second.mkv' } };
    mount([base, second]);
    expect(await screen.findByText('此前有 27秒的采集空档，录像已分开保存')).toBeVisible();
    expect(screen.getAllByRole('article')).toHaveLength(2);
    const secondRow = screen.getByRole('article', { name: '录像 2026-10-31 12:37:11 — 12:37:16' });
    expect(within(secondRow).getByRole('button', { name: '下载完整视频 second.mkv' })).toBeVisible();
  });

  it('shows an ended capture with missing frames without suppressing its verified download', async () => {
    mount([{ ...base, captureIncomplete: true, closureSessionId: 'desktop-session' }]);
    expect(await screen.findByText('采集有缺帧或暂停，视频保留实际采集时间和空档。')).toBeVisible();
    expect(screen.getByRole('button', { name: `下载完整视频 ${video.originalName}` })).toBeEnabled();
    expect(screen.queryByText('等待结束标记')).not.toBeInTheDocument();
  });

  it('downloads a sealed video immediately without a session-end proof', async () => {
    const saved = { id: 'saved-part', originalName: 'video.mkv', fileSize: '12000000', startedAt: base.startedAt, endAt: base.endAt };
    vi.mocked(api.download).mockResolvedValue({ url: '/objects/saved-part' });
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount([{ ...base, closed: false, state: 'waiting_end', file: null, downloadableVideos: [saved] }]);
      fireEvent.click(await screen.findByRole('button', { name: '下载视频 video.mkv' }));
      await waitFor(() => expect(api.download).toHaveBeenCalledWith(saved.id));
      expect(download).toHaveBeenCalledTimes(1);
      expect(screen.getByText('可下载')).toBeVisible();
      expect(screen.queryByText(/结束标记|结束证明/)).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /下载完整视频|下载时间压缩包|详情/ })).not.toBeInTheDocument();
      expect(api.assembleDatasetRecording).not.toHaveBeenCalled();
    } finally { download.mockRestore(); }
  });

  it('selects a saved video by time while additional segments may still arrive', async () => {
    const saved = [
      { id: 'part-one', originalName: 'video.mkv', fileSize: '12000000', startedAt: '2026-10-31T04:30:47Z', endAt: '2026-10-31T04:31:47Z' },
      { id: 'part-two', originalName: 'video.mkv', fileSize: '13000000', startedAt: '2026-10-31T04:31:47Z', endAt: '2026-10-31T04:32:47Z' },
    ];
    vi.mocked(api.download).mockResolvedValue({ url: '/objects/part-two' });
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount([{ ...base, closed: false, state: 'waiting_end', file: null, downloadableVideos: saved }]);
      fireEvent.click(await screen.findByRole('button', { name: '下载视频（2段）' }));
      fireEvent.click(await screen.findByRole('menuitem', { name: /12:31:47 — 12:32:47/ }));
      await waitFor(() => expect(api.download).toHaveBeenCalledWith('part-two'));
      expect(download).toHaveBeenCalledTimes(1);
      expect(screen.queryByText(/结束标记|结束证明/)).not.toBeInTheDocument();
      expect(api.assembleDatasetRecording).not.toHaveBeenCalled();
    } finally { download.mockRestore(); }
  });

  it('offers saved videos after an assembly failure and still permits retry', async () => {
    const saved = { id: 'saved-part', originalName: 'video.mkv', fileSize: '12000000', startedAt: base.startedAt, endAt: base.endAt };
    mount([{ ...base, state: 'failed', file: null, error: '合并失败', downloadableVideos: [saved] }], true);
    expect(await screen.findByRole('button', { name: '下载视频 video.mkv' })).toBeEnabled();
    expect(screen.getByText('可下载')).toBeVisible();
    expect(screen.getByText('合并失败')).toBeVisible();
    expect(screen.getByRole('button', { name: '重新生成' })).toBeEnabled();
  });

  it('replaces saved-part downloads with the merged video when assembly finishes', async () => {
    const saved = { id: 'saved-part', originalName: 'video.mkv', fileSize: '12000000', startedAt: base.startedAt, endAt: base.endAt };
    const { client } = mount([{ ...base, state: 'processing', file: null, downloadableVideos: [saved] }]);
    expect(await screen.findByRole('button', { name: '下载视频 video.mkv' })).toBeEnabled();
    vi.mocked(api.datasetRecordings).mockResolvedValue({ datasetId: 'dataset-one', path: '原相机前', recordings: [{ ...base, downloadableVideos: [] }] });
    await client.invalidateQueries({ queryKey: ['datasets', 'recordings'] });
    expect(await screen.findByRole('button', { name: `下载完整视频 ${video.originalName}` })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '下载视频 video.mkv' })).not.toBeInTheDocument();
  });

  it('marks verified internal capture gaps while keeping the complete video downloadable', async () => {
    const gaps = [
      { afterFrame: 10, startSeconds: .4, endSeconds: 3.9, durationSeconds: 3.5 },
      { afterFrame: 50, startSeconds: 5, endSeconds: 7, durationSeconds: 2 },
      { afterFrame: 60, startSeconds: 8, endSeconds: 10, durationSeconds: '2' },
      { afterFrame: 70, startSeconds: 11, endSeconds: Infinity, durationSeconds: Infinity },
    ];
    mount([{ ...base, file: { ...video, metadata: { durationSeconds: 357, gaps } } }]);
    expect(await screen.findByRole('note')).toHaveTextContent('录像内包含 2 处采集空档，最长 3.5 秒；已保留真实时间轴');
    expect(screen.getByRole('button', { name: `下载完整视频 ${video.originalName}` })).toBeEnabled();
  });

  it('does not invent an internal-gap warning from absent, legacy, or invalid metadata', async () => {
    for (const metadata of [undefined, null, 'legacy', [], { gaps: [] }, { gaps: 'unknown' }, { gaps: [
      null, {},
      { afterFrame: 0, startSeconds: 1, endSeconds: 3, durationSeconds: 2 },
      { afterFrame: 1.5, startSeconds: 1, endSeconds: 3, durationSeconds: 2 },
      { afterFrame: 10, startSeconds: 3, endSeconds: 1, durationSeconds: -2 },
      { afterFrame: 10, startSeconds: NaN, endSeconds: 3, durationSeconds: 2 },
      { afterFrame: 10, startSeconds: 1, endSeconds: 3, durationSeconds: 5 },
      { afterFrame: 10, startSeconds: 1, endSeconds: 1000, durationSeconds: 999 },
    ] }]) {
      const { unmount } = mount([{ ...base, file: { ...video, metadata } }]);
      await screen.findByRole('button', { name: `下载完整视频 ${video.originalName}` });
      expect(screen.queryByRole('note')).not.toBeInTheDocument();
      unmount();
    }
  });

  it('never exposes an unverified file or a download while the recording is collecting or assembling', async () => {
    mount([{ ...base, state: 'collecting', closed: false }, { ...base, id: 'processing', state: 'processing' }]);
    expect((await screen.findAllByText('处理中')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('处理中')).toHaveLength(2);
    expect(screen.queryByRole('button', { name: /下载完整视频/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '详情' })).not.toBeInTheDocument();
    expect(api.assembleDatasetRecording).not.toHaveBeenCalled();
  });

  it('automatically replaces an assembling state with the verified complete file', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      mount([{ ...base, state: 'processing', file: null }]);
      await screen.findByText('处理中');
      vi.mocked(api.datasetRecordings).mockResolvedValue({ datasetId: 'dataset-one', path: '原相机前', recordings: [base] });
      await act(async () => { vi.advanceTimersByTime(3000); });
      expect(await screen.findByRole('button', { name: `下载完整视频 ${video.originalName}` })).toBeVisible();
      expect(api.datasetRecordings).toHaveBeenCalledTimes(2);
      expect(api.assembleDatasetRecording).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });

  it('lets an operator retry one failed assembly and refreshes its server state', async () => {
    vi.mocked(api.assembleDatasetRecording).mockResolvedValue({ jobId: 'new-job' });
    mount([{ ...base, state: 'failed', file: null, error: '校验失败，原始数据保留' }], true);
    fireEvent.click(await screen.findByRole('button', { name: '重新生成' }));
    await waitFor(() => expect(api.assembleDatasetRecording).toHaveBeenCalledWith('dataset-one', 'recording-one'));
    await waitFor(() => expect(api.datasetRecordings).toHaveBeenCalledTimes(2));
  });

  it('keeps failed data visible without a viewer mutation control and preserves the raw-parts switch', async () => {
    const { onToggleRaw } = mount([{ ...base, state: 'failed', file: null, error: '原始分段校验失败' }]);
    expect(await screen.findByText('原始分段校验失败')).toBeVisible();
    expect(screen.queryByRole('button', { name: '重新生成' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看原始分段' }));
    expect(onToggleRaw).toHaveBeenCalledTimes(1);
  });

  it('keeps raw-parts access available when the recording endpoint fails', async () => {
    const { client } = mount([]);
    await screen.findByText('暂无录像');
    vi.mocked(api.datasetRecordings).mockRejectedValue(new Error('服务暂时不可用'));
    await client.invalidateQueries({ queryKey: ['datasets', 'recordings'] });
    expect(await screen.findByText('视频加载失败')).toBeVisible();
    expect(screen.getByRole('button', { name: '查看原始分段' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '重新加载' })).toBeEnabled();
  });

  it('formats capture times in Shanghai through midnight and handles invalid values', () => {
    expect(recordingRange('2026-10-31T15:59:59Z', '2026-10-31T16:00:03Z')).toBe('2026-10-31 23:59:59 — 2026-11-01 00:00:03');
    expect(recordingRange('bad', '2026-10-31T16:00:03Z')).toBe('采集时间待确认');
    expect(recordingDuration(3600)).toBe('1小时');
    expect(recordingDuration(0)).toBe('0秒');
    expect(recordingDuration(Number.NaN)).toBe('--');
  });

  it.each(['waiting_end', 'ready', 'failed'] as const)('never exposes complete-file actions for an unclosed %s group', async state => {
    mount([{ ...base, state, closed: false }], true);
    expect((await screen.findAllByText('处理中')).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /下载完整视频|下载时间压缩包|重新生成|详情/ })).not.toBeInTheDocument();
    expect(api.assembleDatasetRecording).not.toHaveBeenCalled();
    expect(api.archiveRecordingPackage).not.toHaveBeenCalled();
  });

  it('uses the slower refresh interval for a historic group awaiting its end marker', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      mount([{ ...base, state: 'waiting_end', closed: false, file: null }]);
      await screen.findByText('处理中');
      await act(async () => { vi.advanceTimersByTime(3000); });
      expect(api.datasetRecordings).toHaveBeenCalledTimes(1);
      await act(async () => { vi.advanceTimersByTime(12000); });
      await waitFor(() => expect(api.datasetRecordings).toHaveBeenCalledTimes(2));
    } finally { vi.useRealTimers(); }
  });

  it('finds device-clock recordings through their control-session start without changing native times or closure', async () => {
    const first = { ...base, captureSessionId: 'a'.repeat(32), startedAt: '2026-11-04T07:16:58Z', endAt: '2026-11-04T07:25:24Z',
      state: 'waiting_end' as const, closed: false, file: null,
      controlSession: { startedAt: '2026-11-04T08:12:23Z', endedAt: '2026-11-04T08:35:49Z', nativeTimeOutsideSession: true } };
    const second = { ...base, id: 'second-session', captureSessionId: 'b'.repeat(32), startedAt: '2026-11-04T07:27:07Z', endAt: '2026-11-04T07:38:01Z',
      controlSession: { startedAt: '2026-11-04T08:22:37Z', endedAt: '2026-11-04T08:35:49Z', nativeTimeOutsideSession: true } };
    mount([first, second]);
    expect(await screen.findByText('2026-11-04 15:16:58 — 15:25:24')).toBeVisible();
    expect(screen.getByText('控制端会话开始：2026-11-04 16:12:23 · aaaaaaaa')).toBeVisible();
    expect(screen.getAllByText('设备时钟与控制端不一致；上方保留设备原始时间，可按控制端采集会话查找。')).toHaveLength(2);
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '采集会话' }));
    fireEvent.click(await screen.findByText('控制端 2026-11-04 16:12:23 开始 · aaaaaaaa'));
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(1));
    expect(screen.getByRole('article')).toHaveAccessibleName('录像 2026-11-04 15:16:58 — 15:25:24');
    expect(screen.getByText('处理中')).toBeVisible();
    expect(screen.queryByRole('button', { name: /下载完整视频/ })).not.toBeInTheDocument();
    expect(api.assembleDatasetRecording).not.toHaveBeenCalled();
  });

  it('explains an evidenced interruption while keeping its verified video downloadable', async () => {
    mount([{ ...base, captureIncomplete: true, closureReason: 'recovered_interruption', closureSessionId: 'c'.repeat(32) }]);
    expect(await screen.findByText('中断恢复，可能缺帧；仅包含已核对的可恢复视频，原始时间和采集空档已保留。')).toBeVisible();
    expect(screen.getByRole('combobox', { name: '采集会话' })).toBeVisible();
    expect(screen.getByRole('button', { name: `下载完整视频 ${video.originalName}` })).toBeEnabled();
    expect(screen.queryByText(/控制端会话开始/)).not.toBeInTheDocument();
  });
});
