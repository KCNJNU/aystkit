import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, type PreparedFolderPackage, type PreparedFolderPackages } from '../services/api';
import { startDownloadURL } from '../services/file-download';
import { PreparedFolderPackagesPanel } from './PreparedFolderPackagesPanel';

vi.mock('../services/api', () => ({ api: {
  preparedFolderPackages: vi.fn(), prepareDatasetFolderPackages: vi.fn(), folderPackage: vi.fn(),
} }));
vi.mock('../services/file-download', () => ({ startDownloadURL: vi.fn() }));
const taskId = 'a'.repeat(64);
const clients: QueryClient[] = [];
function snapshot(items: PreparedFolderPackage[] = []): PreparedFolderPackages {
  return { items, total: items.length, ready: items.filter(row => row.state === 'READY').length,
    active: items.filter(row => ['QUEUED', 'PROCESSING'].includes(row.state)).length };
}
function mount(props: Partial<React.ComponentProps<typeof PreparedFolderPackagesPanel>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return { client, ...render(<QueryClientProvider client={client}><ConfigProvider theme={{ token: { motion: false } }}>
    <PreparedFolderPackagesPanel datasetId="dataset" {...props} />
  </ConfigProvider></QueryClientProvider>) };
}
async function expandDevices() {
  const toggle = await screen.findByRole('button', { name: '展开设备压缩包列表' });
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(toggle);
  expect(screen.getByRole('button', { name: '收起设备压缩包列表' })).toHaveAttribute('aria-expanded', 'true');
}

describe('server prepared device packages', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.preparedFolderPackages).mockResolvedValue(snapshot());
  });
  afterEach(() => {
    for (const client of clients.splice(0)) client.clear();
    vi.useRealTimers();
  });

  it('waits for server state without showing a fabricated completed count', async () => {
    let resolve!: (value: PreparedFolderPackages) => void;
    vi.mocked(api.preparedFolderPackages).mockReturnValue(new Promise(done => { resolve = done; }));
    mount();
    expect(screen.getByRole('status', { name: '设备预打包进度' })).toHaveTextContent('正在读取');
    expect(screen.getByRole('button', { name: '一键预打包全部设备' })).toBeDisabled();
    expect(screen.queryByText(/已就绪 0\/0/)).not.toBeInTheDocument();
    await act(async () => resolve(snapshot()));
    await waitFor(() => expect(screen.getByRole('button', { name: '一键预打包全部设备' })).toBeEnabled());
    expect(api.prepareDatasetFolderPackages).not.toHaveBeenCalled();
    expect(api.folderPackage).not.toHaveBeenCalled();
  });

  it('submits once, then leaves the server queue running without downloading or helper polling', async () => {
    const active = snapshot([{ path: '原相机前', state: 'PROCESSING', taskId }, { path: '雷达MID360', state: 'QUEUED' }]);
    let resolve!: (value: PreparedFolderPackages) => void;
    vi.mocked(api.prepareDatasetFolderPackages).mockReturnValue(new Promise(done => { resolve = done; }));
    const view = mount();
    const button = await screen.findByRole('button', { name: '一键预打包全部设备' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(api.prepareDatasetFolderPackages).toHaveBeenCalledTimes(1));
    expect(api.prepareDatasetFolderPackages).toHaveBeenCalledWith('dataset');
    await act(async () => resolve(active));
    expect(await screen.findByRole('status', { name: '设备预打包进度' })).toHaveTextContent('已就绪 0/2 · 准备中 2 · 失败 0');
    expect(screen.getByRole('button', { name: '预打包进行中' })).toBeDisabled();
    expect(screen.queryByRole('article', { name: '预打包设备 原相机前' })).not.toBeInTheDocument();
    await expandDevices();
    expect(screen.getByRole('article', { name: '预打包设备 原相机前' })).toBeVisible();
    expect(screen.getByRole('article', { name: '预打包设备 雷达MID360' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '收起设备压缩包列表' }));
    expect(screen.getByRole('button', { name: '展开设备压缩包列表' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('article', { name: '预打包设备 原相机前' })).not.toBeInTheDocument();
    expect(api.prepareDatasetFolderPackages).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: /^下载已就绪压缩包/ })).not.toBeInTheDocument();
    view.unmount();
    expect(api.folderPackage).not.toHaveBeenCalled();
    expect(startDownloadURL).not.toHaveBeenCalled();
  });

  it('restores server READY results after remount and only downloads the explicitly clicked exact snapshot', async () => {
    vi.mocked(api.preparedFolderPackages).mockResolvedValue(snapshot([{ path: '原相机前', state: 'PROCESSING', taskId }]));
    const previous = mount();
    await waitFor(() => expect(screen.getByRole('status', { name: '设备预打包进度' })).toHaveTextContent('准备中 1'));
    previous.unmount();
    vi.mocked(api.preparedFolderPackages).mockResolvedValue(snapshot([{ path: '原相机前', state: 'READY', taskId, readyAt: '2026-11-03T08:07:22Z' }]));
    vi.mocked(api.folderPackage).mockResolvedValue({ state: 'ready', taskId, url: '/dbprog/folder-packages/fresh', fileName: 'camera_20261103_180000.tar.zst' });
    mount({ metadata: { type: 'garage-native-archive', day: '2026-11-02' } });
    await expandDevices();
    const button = await screen.findByRole('button', { name: '下载已就绪压缩包 原相机前' });
    expect(screen.getByRole('heading', { name: '原相机前_2026-11-02' })).toBeVisible();
    expect(screen.getByLabelText('压缩包就绪时间 原相机前')).toHaveTextContent('2026年11月03日 16:07（北京时间）');
    expect(startDownloadURL).not.toHaveBeenCalled();
    expect(api.folderPackage).not.toHaveBeenCalled();
    expect(api.prepareDatasetFolderPackages).not.toHaveBeenCalled();
    fireEvent.click(button);
    await waitFor(() => expect(startDownloadURL).toHaveBeenCalledWith('/dbprog/folder-packages/fresh', 'camera_20261103_180000.tar.zst'));
    expect(api.folderPackage).toHaveBeenCalledWith('dataset', '原相机前', { async: true, taskId, signal: expect.any(AbortSignal) });
    expect(vi.mocked(api.folderPackage).mock.calls[0][2]?.retry).toBeUndefined();
  });

  it('preserves real capture-date labels and lets users retry failed devices with their server reason', async () => {
    vi.mocked(api.preparedFolderPackages).mockResolvedValue(snapshot([{ path: '原相机前', state: 'FAILED', errorMessage: '云端临时磁盘空间不足' }]));
    vi.mocked(api.prepareDatasetFolderPackages).mockResolvedValue(snapshot([{ path: '原相机前', state: 'QUEUED', taskId: null }]));
    mount({ metadata: { type: 'garage-native-archive' }, rootFolders: [{ id: 'folder', datasetId: 'dataset', objectKey: 'dataset/camera/.folder', createdAt: '2026-11-03T00:00:00Z', name: '原相机前', path: '原相机前', parentPath: '', captureDateRange: { from: '2026-11-01', to: '2026-11-02' } }] });
    await expandDevices();
    await screen.findByText('云端临时磁盘空间不足');
    expect(screen.getByRole('heading', { name: '原相机前_2026-11-01至2026-11-02' })).toBeVisible();
    expect(screen.queryByRole('button', { name: /^下载已就绪压缩包/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试未完成设备' }));
    await screen.findByText('排队中');
    expect(api.prepareDatasetFolderPackages).toHaveBeenCalledTimes(1);
  });

  it.each([401, 403])('hides previous READY controls if a fresh GET loses access (%s)', async status => {
    vi.mocked(api.preparedFolderPackages).mockResolvedValueOnce(snapshot([{ path: 'camera', state: 'READY', taskId }]))
      .mockRejectedValue(Object.assign(new Error('无权访问该数据集'), { status }));
    const { client } = mount();
    await expandDevices();
    await screen.findByRole('button', { name: '下载已就绪压缩包 camera' });
    await act(async () => { await client.invalidateQueries({ queryKey: ['datasets', 'prepared-folder-packages', 'dataset'] }); });
    await screen.findByText('预打包状态读取失败');
    expect(screen.queryByRole('button', { name: '下载已就绪压缩包 camera' })).not.toBeInTheDocument();
    expect(screen.queryByRole('status', { name: '设备预打包进度' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '一键预打包全部设备' })).toBeDisabled();
    expect(api.prepareDatasetFolderPackages).not.toHaveBeenCalled();
  });

  it('reports an expired prepared snapshot without restarting packaging', async () => {
    vi.mocked(api.preparedFolderPackages).mockResolvedValue(snapshot([{ path: 'camera', state: 'READY', taskId }]));
    vi.mocked(api.folderPackage).mockRejectedValue(Object.assign(new Error('压缩包缓存已过期'), { status: 409 }));
    mount();
    await expandDevices();
    fireEvent.click(await screen.findByRole('button', { name: '下载已就绪压缩包 camera' }));
    await screen.findByText('camera：压缩包缓存已过期');
    expect(api.folderPackage).toHaveBeenCalledTimes(1);
    expect(api.prepareDatasetFolderPackages).not.toHaveBeenCalled();
    expect(startDownloadURL).not.toHaveBeenCalled();
  });

  it('never accepts a different task or downloads a late result after the panel closes', async () => {
    vi.mocked(api.preparedFolderPackages).mockResolvedValue(snapshot([{ path: 'camera', state: 'READY', taskId }]));
    vi.mocked(api.folderPackage).mockResolvedValueOnce({ state: 'ready', taskId: 'b'.repeat(64), url: '/wrong', fileName: 'wrong.tar.zst' });
    const view = mount();
    await expandDevices();
    fireEvent.click(await screen.findByRole('button', { name: '下载已就绪压缩包 camera' }));
    await screen.findByText(/camera：压缩包尚未就绪或状态已变化/);
    expect(startDownloadURL).not.toHaveBeenCalled();
    let complete!: (value: { state: 'ready'; taskId: string; url: string; fileName: string }) => void;
    vi.mocked(api.folderPackage).mockReturnValueOnce(new Promise(resolve => { complete = resolve; }));
    await waitFor(() => expect(screen.getByRole('button', { name: '下载已就绪压缩包 camera' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '下载已就绪压缩包 camera' }));
    const signal = vi.mocked(api.folderPackage).mock.calls[1][2]?.signal;
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => complete({ state: 'ready', taskId, url: '/late', fileName: 'late.tar.zst' }));
    expect(startDownloadURL).not.toHaveBeenCalled();
  });

  it('does not enable READY entries without a valid task id', async () => {
    vi.mocked(api.preparedFolderPackages).mockResolvedValue(snapshot([{ path: 'camera', state: 'READY', taskId: null }]));
    mount();
    await expandDevices();
    const row = await screen.findByRole('article', { name: '预打包设备 camera' });
    expect(within(row).getByRole('button', { name: '下载已就绪压缩包 camera' })).toBeDisabled();
    expect(api.folderPackage).not.toHaveBeenCalled();
  });

  it('resets an old dataset mutation and ignores its late failure after switching datasets', async () => {
    let reject!: (error: Error) => void;
    vi.mocked(api.preparedFolderPackages).mockImplementation(async id => id === 'dataset'
      ? snapshot([{ path: 'previous-camera', state: 'READY', taskId }]) : snapshot());
    vi.mocked(api.prepareDatasetFolderPackages).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }))
      .mockResolvedValue(snapshot([{ path: 'second-camera', state: 'QUEUED' }]));
    const view = mount();
    await expandDevices();
    expect(screen.getByRole('article', { name: '预打包设备 previous-camera' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '更新预打包' }));
    await waitFor(() => expect(api.prepareDatasetFolderPackages).toHaveBeenCalledWith('dataset'));
    view.rerender(<QueryClientProvider client={view.client}><ConfigProvider theme={{ token: { motion: false } }}>
      <PreparedFolderPackagesPanel datasetId="second" />
    </ConfigProvider></QueryClientProvider>);
    await waitFor(() => expect(screen.getByRole('button', { name: '一键预打包全部设备' })).toBeEnabled());
    await act(async () => reject(new Error('上一个数据集提交失败')));
    expect(screen.queryByText('上一个数据集提交失败')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '一键预打包全部设备' }));
    await screen.findByRole('button', { name: '展开设备压缩包列表' });
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
    await expandDevices();
    await screen.findByRole('article', { name: '预打包设备 second-camera' });
    expect(api.prepareDatasetFolderPackages).toHaveBeenNthCalledWith(2, 'second');
    expect(view.client.getQueryData(['datasets', 'prepared-folder-packages', 'second'])).toEqual(snapshot([{ path: 'second-camera', state: 'QUEUED' }]));
  });

  it('polls one dataset status every five seconds rather than polling each device helper', async () => {
    vi.useFakeTimers();
    vi.mocked(api.preparedFolderPackages).mockResolvedValue(snapshot(Array.from({ length: 8 }, (_, index) => ({ path: 'device-'+index, state: 'QUEUED' as const }))));
    mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(api.preparedFolderPackages).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '展开设备压缩包列表' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(4998); });
    expect(api.preparedFolderPackages).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2); });
    expect(api.preparedFolderPackages).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: '展开设备压缩包列表' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
    expect(api.prepareDatasetFolderPackages).not.toHaveBeenCalled();
    expect(api.folderPackage).not.toHaveBeenCalled();
    expect(startDownloadURL).not.toHaveBeenCalled();
  });
});
