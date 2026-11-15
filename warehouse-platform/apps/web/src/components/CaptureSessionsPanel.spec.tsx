import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CaptureSessionsPanel } from './CaptureSessionsPanel';
import { listCaptureSessions, prepareCaptureSessionDownload, type CaptureSession } from '../services/capture-sessions';
import { startDownloadURL } from '../services/file-download';

vi.mock('../services/capture-sessions', () => ({ listCaptureSessions: vi.fn(), prepareCaptureSessionDownload: vi.fn() }));
vi.mock('../services/file-download', () => ({ startDownloadURL: vi.fn(), downloadFile: vi.fn() }));
const robotA = '365c77c6-0029-4406-a504-739014d0a672', robotB = '365c77c6-0029-4406-a504-739014d0a673';
const row: CaptureSession = { sessionId: 'b'.repeat(32), robotId: robotA, startedAt: '2026-11-10T00:10:00Z', endAt: '2026-11-10T00:11:00Z',
  fileCount: 2, totalBytes: '2048', sensorCount: 2, sensors: [{ name: '前相机', fileCount: 1, totalBytes: '1024' }, { name: '雷达', fileCount: 1, totalBytes: '1024' }], snapshot: true };
function result(sessions: CaptureSession[] = [row]) {
  return { datasetId: 'dataset', robotId: robotA, snapshotAt: '2026-11-10T00:11:01Z', sessions, unassigned: { fileCount: 0, totalBytes: '0' }, excludedDerivedFileCount: 0 };
}
function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe('one capture download controls', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(listCaptureSessions).mockResolvedValue(result()); });

  it('lists the current robot without generating packages, and downloads without a closure marker', async () => {
    vi.mocked(prepareCaptureSessionDownload).mockResolvedValue({ url: '/ready', fileName: 'capture.tar.zst' });
    const select = vi.fn();
    render(<CaptureSessionsPanel datasetId="dataset" robotId={robotA} onSelect={select} />, { wrapper: wrapper() });
    const download = await screen.findByRole('button', { name: `下载本次已入仓数据 ${row.sessionId.slice(0, 8)}` });
    expect(listCaptureSessions).toHaveBeenCalledWith('dataset', robotA, expect.any(AbortSignal));
    expect(prepareCaptureSessionDownload).not.toHaveBeenCalled();
    expect(screen.queryByText(/等待.*结束/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /选择采集/ }));
    expect(select).toHaveBeenCalledWith(row);
    fireEvent.click(download);
    await waitFor(() => expect(startDownloadURL).toHaveBeenCalledWith('/ready', 'capture.tar.zst'));
    expect(prepareCaptureSessionDownload).toHaveBeenCalledWith('dataset', row, expect.any(AbortSignal));
  });

  it('disables duplicate clicks and offers a retry after server failure', async () => {
    let reject!: (error: Error) => void;
    vi.mocked(prepareCaptureSessionDownload).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }))
      .mockResolvedValueOnce({ url: '/retried', fileName: 'capture.tar.zst' });
    render(<CaptureSessionsPanel datasetId="dataset" robotId={robotA} onSelect={vi.fn()} />, { wrapper: wrapper() });
    const button = await screen.findByRole('button', { name: `下载本次已入仓数据 ${row.sessionId.slice(0, 8)}` });
    fireEvent.click(button); fireEvent.click(button);
    expect(prepareCaptureSessionDownload).toHaveBeenCalledTimes(1);
    await act(async () => reject(new Error('源文件暂不可用')));
    expect(await screen.findByText('源文件暂不可用')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '重新下载' }));
    await waitFor(() => expect(startDownloadURL).toHaveBeenCalledWith('/retried', 'capture.tar.zst'));
    expect(prepareCaptureSessionDownload).toHaveBeenCalledTimes(2);
  });

  it.each(['unmount', 'robot', 'dataset'])('cancels and ignores late downloads on %s', async change => {
    let complete!: (value: { url: string; fileName: string }) => void;
    vi.mocked(prepareCaptureSessionDownload).mockReturnValue(new Promise(resolve => { complete = resolve; }));
    const view = render(<CaptureSessionsPanel datasetId="dataset" robotId={robotA} onSelect={vi.fn()} />, { wrapper: wrapper() });
    fireEvent.click(await screen.findByRole('button', { name: `下载本次已入仓数据 ${row.sessionId.slice(0, 8)}` }));
    const signal = vi.mocked(prepareCaptureSessionDownload).mock.calls[0][2];
    if (change === 'unmount') view.unmount();
    else view.rerender(<CaptureSessionsPanel datasetId={change === 'dataset' ? 'next-dataset' : 'dataset'} robotId={change === 'robot' ? robotB : robotA} onSelect={vi.fn()} />);
    expect(signal.aborted).toBe(true);
    await act(async () => complete({ url: '/late', fileName: 'capture.tar.zst' }));
    expect(startDownloadURL).not.toHaveBeenCalled();
  });

  it('retains each row robot when all robots share a session token', async () => {
    const second = { ...row, robotId: robotB };
    vi.mocked(listCaptureSessions).mockResolvedValue(result([row, second]));
    vi.mocked(prepareCaptureSessionDownload).mockResolvedValue({ url: '/robot-b', fileName: 'capture.tar.zst' });
    render(<CaptureSessionsPanel datasetId="dataset" robotId="" onSelect={vi.fn()} />, { wrapper: wrapper() });
    const buttons = await screen.findAllByRole('button', { name: `下载本次已入仓数据 ${row.sessionId.slice(0, 8)}` });
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(startDownloadURL).toHaveBeenCalledWith('/robot-b', 'capture.tar.zst'));
    expect(prepareCaptureSessionDownload).toHaveBeenCalledWith('dataset', second, expect.any(AbortSignal));
    expect(listCaptureSessions).toHaveBeenCalledWith('dataset', '', expect.any(AbortSignal));
  });
});
