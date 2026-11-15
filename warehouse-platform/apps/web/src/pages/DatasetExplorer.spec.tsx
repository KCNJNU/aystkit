import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { MemoryRouter, Route, Routes, useLocation, type InitialEntry } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DatasetWorkspace } from './DatasetWorkspace';
import { Datasets } from './Datasets';
import { DatasetVersionsWorkspace } from './DatasetVersionsWorkspace';
import { api } from '../services/api';
import { datasetDirectoryReturn, datasetListReturn } from '../services/dataset-navigation';
const state = vi.hoisted(() => ({ canWrite: true, queue: { items: [], addFiles: vi.fn() } }));
vi.mock('../auth/AuthContext', () => ({ useOptionalAuth: () => ({ hasRole: () => state.canWrite }) }));
vi.mock('../upload/UploadQueueContext', () => ({ useUploadQueue: () => state.queue }));
vi.mock('../components/SavedViews', () => ({ SavedViews: () => null }));
vi.mock('../components/FilePreview', () => ({ FilePreview: ({ fileId, onClose }: { fileId?: string; onClose: () => void }) => fileId ? <section role="dialog" aria-label="文件详情"><span>{fileId}</span><button onClick={onClose}>关闭文件详情</button></section> : null }));
vi.mock('../services/api', () => ({ api: { dataset: vi.fn(), datasetPage: vi.fn(), tags: vi.fn(), version: vi.fn(), compareVersions: vi.fn(), datasetContents: vi.fn(), datasetRecordings: vi.fn(), assembleDatasetRecording: vi.fn(), createDatasetFolder: vi.fn(), download: vi.fn(), folderPackage: vi.fn(), preparedFolderPackages: vi.fn(), prepareDatasetFolderPackages: vi.fn(), deleteDataset: vi.fn(), fileImpact: vi.fn(), deleteFile: vi.fn() } }));
function RouteLocation() { const location = useLocation(); return <output data-testid="route-location">{location.pathname}{location.search}</output>; }
function mount(entry: InitialEntry = '/datasets/one') {
  return render(<ConfigProvider theme={{token:{motion:false}}}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={[entry]}><RouteLocation /><Routes><Route path="/datasets" element={<Datasets />} /><Route path="/datasets/:datasetId" element={<DatasetWorkspace />} /><Route path="/datasets/:datasetId/history" element={<DatasetVersionsWorkspace />} /><Route path="/datasets/:datasetId/versions/:versionId" element={<DatasetVersionsWorkspace />} /><Route path="/datasets/:datasetId/compare" element={<DatasetVersionsWorkspace />} /></Routes></MemoryRouter></QueryClientProvider></ConfigProvider>);
}
const route = () => screen.getByTestId('route-location').textContent;
const backButton = () => screen.getByRole('button', { name: '后退' });
const forwardButton = () => screen.getByRole('button', { name: '前进' });
const timestamp = '2026-10-28T08:05:00Z';
const datasetRow = { id: 'one', name: 'test02', versions: [], createdAt: timestamp, updatedAt: timestamp };
describe('Dataset file explorer', () => {
  beforeEach(() => {
    vi.clearAllMocks(); state.canWrite = true; vi.mocked(api.fileImpact).mockResolvedValue({ canRecycle: true, blockers: [], references: [], restrictedCount: 0, objectKey: 'test/file' } as never);
    vi.mocked(api.dataset).mockResolvedValue(datasetRow as never);
    vi.mocked(api.datasetPage).mockResolvedValue({ items: [datasetRow], total: 60, page: 1, pageSize: 24 } as never);
    vi.mocked(api.tags).mockResolvedValue([{ id: 'tag-1', name: 'garage' }] as never);
    vi.mocked(api.datasetContents).mockImplementation(async (_id, path = '') => ({ dataset:{id:'one',name:'test02'}, path, folders: path ? [] : [{ id:'folder',name:'images',path:'images',parentPath:'' }], files:[],total:0,page:1,pageSize:50 }) as never);
    vi.mocked(api.createDatasetFolder).mockResolvedValue({ id:'new' } as never);
    vi.mocked(api.datasetRecordings).mockResolvedValue({ datasetId: 'one', path: '原相机前', recordings: [] });
    vi.mocked(api.preparedFolderPackages).mockResolvedValue({ items: [], total: 0, ready: 0, active: 0 });
  });
  it('opens folders and returns to their parent without changing datasets', async () => {
    mount(); fireEvent.click(await screen.findByRole('button', {name:'打开文件夹 images'}));
    await waitFor(() => expect(api.datasetContents).toHaveBeenCalledWith('one', 'images', 1));
    fireEvent.click(screen.getByRole('button', {name:'返回上级文件夹'}));
    await screen.findByRole('button', {name:'打开文件夹 images'});
  });
  it('keeps server preparation visible while browsing directories without restarting it', async () => {
    vi.mocked(api.dataset).mockResolvedValue({ ...datasetRow, metadata: { type: 'garage-native-archive' } } as never);
    let prepared = { items: [] as Array<{ path: string; state: 'QUEUED' }>, total: 0, ready: 0, active: 0 };
    vi.mocked(api.preparedFolderPackages).mockImplementation(async () => prepared);
    vi.mocked(api.prepareDatasetFolderPackages).mockImplementation(async () => {
      prepared = { items: [{ path: 'images', state: 'QUEUED' }], total: 1, ready: 0, active: 1 };
      return prepared;
    });
    mount();
    await waitFor(() => expect(screen.getByRole('button', { name: '一键预打包全部设备' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '一键预打包全部设备' }));
    await waitFor(() => expect(screen.getByRole('status', { name: '设备预打包进度' })).toHaveTextContent('准备中 1'));
    expect(screen.getByRole('button', { name: '展开设备压缩包列表' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('article', { name: '预打包设备 images' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开文件夹 images' }));
    await waitFor(() => expect(route()).toBe('/datasets/one?path=images'));
    expect(screen.getByRole('status', { name: '设备预打包进度' })).toHaveTextContent('准备中 1');
    expect(screen.getByRole('button', { name: '预打包进行中' })).toBeDisabled();
    expect(api.prepareDatasetFolderPackages).toHaveBeenCalledTimes(1);
    expect(api.prepareDatasetFolderPackages).toHaveBeenCalledWith('one');
    expect(api.folderPackage).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('button', { name: '下载当前文件夹压缩包' })).toBeEnabled());
  });
  it('does not query device preparation for a regular manually created dataset', async () => {
    mount();
    await screen.findByRole('button', { name: '打开文件夹 images' });
    expect(screen.queryByRole('region', { name: '全部设备预打包' })).not.toBeInTheDocument();
    expect(api.preparedFolderPackages).not.toHaveBeenCalled();
    expect(api.prepareDatasetFolderPackages).not.toHaveBeenCalled();
  });
  it('downloads a folder for a viewer without opening it and preserves the server timestamped filename', async () => {
    state.canWrite = false;
    const fileName = 'images_20261102_220100.tar.zst';
    vi.mocked(api.folderPackage).mockResolvedValue({ url: '/dbprog/folder-packages/signed-package', fileName });
    const links: Array<{ href: string; name: string }> = [];
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      links.push({ href: this.href, name: this.download });
    });
    try {
      mount();
      const open = await screen.findByRole('button', { name: '打开文件夹 images' });
      const pack = screen.getByRole('button', { name: '下载压缩包 images' });
      expect(open.contains(pack)).toBe(false);
      fireEvent.click(pack);
      await waitFor(() => expect(links).toEqual([{ href: window.location.origin + '/dbprog/folder-packages/signed-package', name: fileName }]));
      expect(api.folderPackage).toHaveBeenCalledWith('one', 'images', expect.objectContaining({ async: true, retry: true, signal: expect.any(AbortSignal) }));
      expect(route()).toBe('/datasets/one');
      expect(api.datasetContents).toHaveBeenCalledTimes(1);
      expect(document.querySelector('a[download]')).toBeNull();
      fireEvent.click(open);
      await waitFor(() => expect(api.datasetContents).toHaveBeenCalledWith('one', 'images', 1));
    } finally { download.mockRestore(); }
  });

  it('shows packing status, prevents rapid duplicate requests, and disables other folder downloads until complete', async () => {
    vi.mocked(api.datasetContents).mockResolvedValue({ dataset: datasetRow, path: '', folders: [
      { id: 'one', name: 'images', path: 'images', parentPath: '' },
      { id: 'two', name: 'logs', path: 'logs', parentPath: '' },
    ], files: [], total: 0, page: 1, pageSize: 50 } as never);
    let complete!: (value: { url: string; fileName: string }) => void;
    vi.mocked(api.folderPackage).mockReturnValue(new Promise(resolve => { complete = resolve; }));
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount();
      const button = await screen.findByRole('button', { name: '下载压缩包 images' });
      expect(screen.getAllByRole('button', { name: /^下载压缩包 / })).toHaveLength(2);
      act(() => { fireEvent.click(button); fireEvent.click(button); });
      expect(api.folderPackage).toHaveBeenCalledTimes(1);
      expect(screen.getByRole('status', { name: '文件夹压缩包准备状态' })).toHaveTextContent('正在准备“images”压缩包，包含子文件夹。云端按顺序打包');
      expect(button).toHaveAttribute('aria-busy', 'true');
      expect(button).toBeDisabled();
      expect(screen.getByRole('button', { name: '下载压缩包 logs' })).toBeDisabled();
      expect(screen.getByRole('button', { name: '打开文件夹 images' })).toBeEnabled();
      await act(async () => complete({ url: '/dbprog/folder-packages/one', fileName: 'images_20261102.tar.zst' }));
      expect(download).toHaveBeenCalledTimes(1);
      expect(button).toBeEnabled();
      expect(screen.getByRole('button', { name: '下载压缩包 logs' })).toBeEnabled();
      expect(screen.queryByRole('status', { name: '文件夹压缩包准备状态' })).not.toBeInTheDocument();
    } finally { download.mockRestore(); }
  });

  it('shows sensor capture dates while folder navigation and package requests retain their original paths', async () => {
    const metadata = { type: 'garage-native-archive', day: '2026-11-03', timezone: 'Asia/Shanghai' };
    vi.mocked(api.dataset).mockResolvedValue({ ...datasetRow, metadata } as never);
    vi.mocked(api.datasetContents).mockImplementation(async (_id, path = '') => ({ dataset: { ...datasetRow, metadata }, path, folders: path ? [] : [
      { id: 'camera', name: '原相机前', path: '原相机前', parentPath: '', createdAt: timestamp, captureDateRange: { from: '2026-11-01', to: '2026-11-02' } },
      { id: 'lidar', name: '雷达MID360', path: '雷达MID360', parentPath: '', createdAt: timestamp, captureDateRange: { from: '2026-11-02', to: '2026-11-02' } },
    ], files: [], total: 0, page: 1, pageSize: 50 }) as never);
    let complete!: (value: { url: string; fileName: string }) => void;
    vi.mocked(api.folderPackage).mockReturnValue(new Promise(resolve => { complete = resolve; }));
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount();
      expect(await screen.findByText('原相机前_2026-11-01至2026-11-02')).toBeVisible();
      expect(screen.getByText('雷达MID360_2026-11-02')).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: '下载压缩包 原相机前' }));
      expect(api.folderPackage).toHaveBeenCalledWith('one', '原相机前', expect.objectContaining({ async: true, retry: true, signal: expect.any(AbortSignal) }));
      expect(screen.getByRole('status', { name: '文件夹压缩包准备状态' })).toHaveTextContent('正在准备“原相机前_2026-11-01至2026-11-02”压缩包');
      await act(async () => complete({ url: '/dbprog/folder-packages/one', fileName: 'camera_20261102.tar.zst' }));
      fireEvent.click(screen.getByRole('button', { name: '打开文件夹 原相机前' }));
      await waitFor(() => expect(api.datasetContents).toHaveBeenCalledWith('one', '原相机前', 1));
      expect(new URL(route()!, 'http://localhost').searchParams.get('path')).toBe('原相机前');
      expect(screen.getAllByText('原相机前_2026-11-01至2026-11-02').length).toBeGreaterThan(0);
    } finally { download.mockRestore(); }
  });

  it('uses the dataset manifest day for sensor labels from older servers', async () => {
    const metadata = { type: 'garage-native-archive', day: '2026-11-02' };
    vi.mocked(api.dataset).mockResolvedValue({ ...datasetRow, metadata } as never);
    vi.mocked(api.datasetContents).mockResolvedValue({ dataset: { ...datasetRow, metadata }, path: '', folders: [
      { id: 'camera', name: 'Berxel深度', path: 'Berxel深度', parentPath: '', createdAt: '2030-01-01T00:00:00Z' },
    ], files: [], total: 0, page: 1, pageSize: 50 } as never);
    mount();
    expect(await screen.findByText('Berxel深度_2026-11-02')).toBeVisible();
    expect(screen.getByRole('button', { name: '打开文件夹 Berxel深度' })).toBeVisible();
    expect(screen.getByRole('button', { name: '下载压缩包 Berxel深度' })).toBeVisible();
  });

  it('reports folder package failures in place and permits an explicit retry', async () => {
    vi.mocked(api.folderPackage).mockRejectedValueOnce(new Error('压缩包临时磁盘空间不足')).mockResolvedValueOnce({
      url: '/dbprog/folder-packages/ready', fileName: 'images_20261102.tar.zst',
    });
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount();
      fireEvent.click(await screen.findByRole('button', { name: '下载压缩包 images' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('压缩包下载失败');
      expect(screen.getByRole('alert')).toHaveTextContent('“images”：压缩包临时磁盘空间不足');
      expect(route()).toBe('/datasets/one');
      expect(download).not.toHaveBeenCalled();
      const retry = screen.getByRole('button', { name: '下载压缩包 images' });
      expect(retry).toBeEnabled();
      fireEvent.click(retry);
      await waitFor(() => expect(download).toHaveBeenCalledTimes(1));
      expect(api.folderPackage).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    } finally { download.mockRestore(); }
  });

  it('cancels its wait on folder navigation and never downloads a late package response', async () => {
    let complete!: (value: { url: string; fileName: string }) => void;
    vi.mocked(api.folderPackage).mockReturnValue(new Promise(resolve => { complete = resolve; }));
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount();
      fireEvent.click(await screen.findByRole('button', { name: '下载压缩包 images' }));
      const signal = vi.mocked(api.folderPackage).mock.calls[0][2]?.signal;
      fireEvent.click(screen.getByRole('button', { name: '打开文件夹 images' }));
      await waitFor(() => expect(route()).toBe('/datasets/one?path=images'));
      expect(signal?.aborted).toBe(true);
      await act(async () => complete({ url: '/late', fileName: 'images.tar.zst' }));
      expect(download).not.toHaveBeenCalled();
      expect(screen.queryByRole('status', { name: '文件夹压缩包准备状态' })).not.toBeInTheDocument();
      await waitFor(() => expect(screen.getByRole('button', { name: '下载当前文件夹压缩包' })).toBeEnabled());
    } finally { download.mockRestore(); }
  });

  it('aborts pending progress queries when the workspace unmounts', async () => {
    vi.mocked(api.folderPackage).mockReturnValue(new Promise(() => undefined));
    const view = mount();
    fireEvent.click(await screen.findByRole('button', { name: '下载压缩包 images' }));
    const signal = vi.mocked(api.folderPackage).mock.calls[0][2]?.signal;
    view.unmount();
    await act(async () => undefined);
    expect(signal?.aborted).toBe(true);
    expect(api.folderPackage).toHaveBeenCalledTimes(1);
  });

  it('offers the same recursive download in the current nested directory toolbar', async () => {
    state.canWrite = false;
    vi.mocked(api.folderPackage).mockResolvedValue({ url: '/dbprog/folder-packages/nested', fileName: 'raw_20261102_220100.tar.zst' });
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount('/datasets/one?path=images%2Fraw');
      await waitFor(() => expect(screen.getByRole('button', { name: '下载当前文件夹压缩包' })).toBeEnabled());
      fireEvent.click(screen.getByRole('button', { name: '下载当前文件夹压缩包' }));
      await waitFor(() => expect(api.folderPackage).toHaveBeenCalledWith('one', 'images/raw', expect.objectContaining({ async: true, retry: true, signal: expect.any(AbortSignal) })));
      expect(download).toHaveBeenCalledTimes(1);
      expect(route()).toBe('/datasets/one?path=images%2Fraw');
    } finally { download.mockRestore(); }
  });

  it('does not follow an invalid folder package download URL', async () => {
    vi.mocked(api.folderPackage).mockResolvedValue({ url: 'javascript:alert(1)', fileName: 'images.tar.zst' });
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount(); fireEvent.click(await screen.findByRole('button', { name: '下载压缩包 images' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('下载地址无效');
      expect(download).not.toHaveBeenCalled();
      expect(route()).toBe('/datasets/one');
    } finally { download.mockRestore(); }
  });
  it('creates a folder beneath the currently opened path', async () => {
    mount('/datasets/one?path=images');
    await waitFor(() => expect(screen.getByRole('button', {name:'新建文件夹'})).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', {name:'新建文件夹'}));
    fireEvent.change(screen.getByLabelText('文件夹名称'), {target:{value:'raw'}});
    fireEvent.click(screen.getByRole('button', {name:'创建文件夹'}));
    await waitFor(() => expect(api.createDatasetFolder).toHaveBeenCalledWith('one', 'images', 'raw'));
  });
  it('binds picked files to the nested path, ready for background upload', async () => {
    const {container} = mount('/datasets/one?path=images%2Fraw');
    await waitFor(() => expect(screen.getByRole('button', {name:'上传文件'})).not.toBeDisabled());
    fireEvent.change(container.querySelector('input[type="file"]')!, {target:{files:[new File(['hello'], 'file.txt')]}});
    await waitFor(() => expect(state.queue.addFiles).toHaveBeenCalledWith([expect.objectContaining({name:'file.txt'})], {id:'one',name:'test02',folderPath:'images/raw'}));
  });
  it('shows read-only browsing without mutation controls for viewers', async () => {
    state.canWrite = false; mount(); await screen.findByRole('button', {name:'打开文件夹 images'});
    expect(screen.queryByRole('button', {name:'新建文件夹'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name:'上传文件'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name:'删除选中文件'})).not.toBeInTheDocument();
  });
  it('offers direct downloads and video labels in a nested directory, including viewers', async () => {
    state.canWrite = false;
    vi.mocked(api.datasetContents).mockResolvedValue({ dataset: { id: 'one', name: 'test02' }, path: 'camera', folders: [], files: [{ id: 'video-one', originalName: 'video.mkv', fileType: 'binary', mimeType: 'application/octet-stream', fileSize: '113000000', createdAt: '2026-10-28T08:05:00Z' }], total: 1, page: 1, pageSize: 50 } as never);
    vi.mocked(api.download).mockResolvedValue({ url: '/dbprog/objects/video-one' });
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount('/datasets/one?path=camera');
      fireEvent.click(await screen.findByRole('button', { name: '下载文件 video.mkv' }));
      await waitFor(() => expect(api.download).toHaveBeenCalledWith('video-one'));
      expect(download).toHaveBeenCalledTimes(1);
      expect(screen.getByText(/^视频 · /)).toBeVisible();
      expect(screen.queryByText('binary')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: '删除文件 video.mkv' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: '更多文件操作 video.mkv' })).not.toBeInTheDocument();
    } finally { download.mockRestore(); }
  });
  it('deletes a file within a nested folder and returns from an emptied last page', async () => {
    let deleted = false;
    vi.mocked(api.datasetContents).mockImplementation(async (_id, path = '', page = 1) => ({ dataset: { id: 'one', name: 'test02' }, path, folders: [], files: deleted ? [] : [{ id: 'file-a', originalName: 'sample.json', fileType: 'json', fileSize: '10', createdAt: '2026-09-23T00:00:00Z' }], total: deleted ? 0 : 51, page, pageSize: 50 }) as never);
    vi.mocked(api.deleteFile).mockImplementation(async () => { deleted = true; return undefined as never; });
    mount('/datasets/one?path=images%2Fraw&page=2');
    fireEvent.click(await screen.findByRole('button', { name: '更多文件操作 sample.json' }));
    fireEvent.click(await screen.findByRole('button', { name: '删除文件 sample.json' }));
    await waitFor(() => expect(api.deleteFile).toHaveBeenCalledWith('file-a'));
    expect(api.deleteFile).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog', { name: /删除影响预览/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认移入回收站' })).not.toBeInTheDocument();
    await waitFor(() => expect(api.datasetContents).toHaveBeenCalledWith('one', 'images/raw', 1));
  });

  it('exposes bounded back and forward controls and clears forward history on a new folder branch', async () => {
    vi.mocked(api.datasetContents).mockImplementation(async (_id, path = '', page = 1) => ({
      dataset: { id: 'one', name: 'test02' }, path, page, pageSize: 50, files: [], total: 0,
      folders: path === '' ? [{ id: 'images', name: 'images', path: 'images', parentPath: '' }]
        : path === 'images' ? [{ id: 'raw', name: 'raw', path: 'images/raw', parentPath: 'images' }] : [],
    }) as never);
    mount();
    expect(backButton()).toBeDisabled(); expect(forwardButton()).toBeDisabled();
    fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 images' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 raw' }));
    expect(route()).toBe('/datasets/one?path=images%2Fraw');
    fireEvent.click(backButton());
    expect(route()).toBe('/datasets/one?path=images');
    expect(forwardButton()).toBeEnabled();
    fireEvent.click(forwardButton());
    expect(route()).toBe('/datasets/one?path=images%2Fraw');
    expect(forwardButton()).toBeDisabled();
    fireEvent.click(backButton());
    fireEvent.click(screen.getByRole('button', { name: '返回上级文件夹' }));
    expect(route()).toBe('/datasets/one');
    expect(forwardButton()).toBeDisabled();
    fireEvent.click(backButton());
    expect(route()).toBe('/datasets/one?path=images');
    fireEvent.click(forwardButton());
    expect(route()).toBe('/datasets/one');
  });

  it('opens and closes file details in place without losing a nested path, page, or history boundary', async () => {
    state.canWrite = false;
    vi.mocked(api.datasetContents).mockResolvedValue({ dataset: { id: 'one', name: 'test02' }, path: 'images/raw', folders: [], files: [{ id: 'video-one', originalName: 'video.mkv', fileType: 'binary', fileSize: '113000000', createdAt: timestamp }], total: 51, page: 2, pageSize: 50 } as never);
    mount('/datasets/one?path=images%2Fraw&page=2');
    fireEvent.click((await screen.findByText('video.mkv')).closest('button')!);
    expect(await screen.findByRole('dialog', { name: '文件详情' })).toHaveTextContent('video-one');
    expect(route()).toBe('/datasets/one?path=images%2Fraw&page=2');
    fireEvent.click(screen.getByRole('button', { name: '关闭文件详情' }));
    expect(screen.queryByRole('dialog', { name: '文件详情' })).not.toBeInTheDocument();
    expect(route()).toBe('/datasets/one?path=images%2Fraw&page=2');
    expect(backButton()).toBeDisabled(); expect(forwardButton()).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '详情' }));
    expect(screen.getByRole('dialog', { name: '文件详情' })).toBeVisible();
    expect(route()).toBe('/datasets/one?path=images%2Fraw&page=2');
  });

  it('returns to the exact filtered dataset list after browsing a child directory', async () => {
    const list = '/datasets?search=garage&status=DRAFT&tagId=tag-1&sort=name%3Aasc&page=3&pageSize=24';
    mount(list);
    fireEvent.click(await screen.findByRole('button', { name: 'test02' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开文件夹 images' }));
    expect(route()).toBe('/datasets/one?path=images');
    fireEvent.click(screen.getByRole('button', { name: /数据集列表/ }));
    await waitFor(() => expect(route()).toBe(list));
    expect(screen.getByRole('searchbox', { name: '搜索数据集' })).toHaveValue('garage');
    expect(api.datasetPage).toHaveBeenLastCalledWith({ page: 3, pageSize: 24, search: 'garage', status: 'DRAFT', tagId: 'tag-1', sortBy: 'name', sortOrder: 'asc' });
  });

  it('preserves the nested directory and filtered list return targets through version comparison', async () => {
    const versions = [2, 1].map(number => ({ id: `version-${number}`, datasetId: 'one', version: number, status: 'DRAFT', fileCount: 0, totalSize: '0', createdAt: timestamp, updatedAt: timestamp, files: [] }));
    vi.mocked(api.dataset).mockResolvedValue({ ...datasetRow, versions } as never);
    vi.mocked(api.compareVersions).mockResolvedValue({ dataset: { id: 'one', name: 'test02' }, base: versions[1], target: versions[0], summary: { added: 0, removed: 0, unchanged: 0, totalSizeDelta: '0' }, files: { added: [], removed: [], unchanged: [] } } as never);
    const list = '/datasets?search=garage&page=3';
    const directory = '/datasets/one?path=images%2Fraw&page=2';
    mount({ pathname: '/datasets/one', search: '?path=images%2Fraw&page=2', state: { datasetsReturnTo: list } });
    fireEvent.click(await screen.findByRole('button', { name: /版本管理/ }));
    expect(route()).toBe('/datasets/one/history');
    fireEvent.click(await screen.findByRole('tab', { name: /版本对比/ }));
    await waitFor(() => expect(api.compareVersions).toHaveBeenCalledWith('one', 'version-1', 'version-2'));
    fireEvent.click(screen.getByRole('button', { name: /返回文件目录/ }));
    expect(route()).toBe(directory);
    fireEvent.click(screen.getByRole('button', { name: /数据集列表/ }));
    await waitFor(() => expect(route()).toBe(list));
  });

  const archiveMetadata = { type: 'garage-native-archive' };
  const timeSlot = '原相机前/原相机前_16:04-16:05';
  const firstSegment = 'a0ef57ec33064d5abcdef01234567890';
  const secondSegment = 'b0ef57ec33064d5abcdef01234567890';
  const archiveFile = (id: string, segment: string, startedAt: string) => ({ id, originalName: 'video.mkv', folderPath: `${timeSlot}/${segment}`, fileType: 'binary', fileSize: '11300000', createdAt: timestamp, capturedAt: startedAt, metadata: { startedAt } });
  const useArchive = () => vi.mocked(api.dataset).mockResolvedValue({ ...datasetRow, metadata: archiveMetadata } as never);
  const currentPath = () => new URL(route()!, 'http://localhost').searchParams.get('path');

  it('opens an archive sensor with complete recordings and reveals its original folders on demand', async () => {
    state.canWrite = false; useArchive();
    vi.mocked(api.datasetContents).mockImplementation(async (_id, path = '', page = 1) => ({ dataset: { ...datasetRow, metadata: archiveMetadata }, path, page, pageSize: 50,
      folders: path === '原相机前' ? [{ id: 'time-slot', name: '原相机前_16:04-16:05', path: timeSlot, parentPath: '原相机前' }] : [], files: [], total: 0 } as never));
    const file = { id: 'complete-file', originalName: '完整录像.mkv', fileType: 'binary', fileSize: '13000000' };
    vi.mocked(api.datasetRecordings).mockResolvedValue({ datasetId: 'one', path: '原相机前', recordings: [{ id: 'capture-one', sensor: '原相机前', robotId: 'robot', startedAt: '2026-10-28T08:04:05Z', endAt: '2026-10-28T08:05:00Z', durationSeconds: 55, frameCount: 1375, partCount: 2, sourceBytes: '13000000', state: 'ready', closed: true, gapBeforeSeconds: null, file, jobId: null, error: null }] } as never);
    mount(`/datasets/one?path=${encodeURIComponent('原相机前')}`);
    expect(await screen.findByRole('button', { name: '下载完整视频 完整录像.mkv' })).toBeVisible();
    expect(api.datasetRecordings).toHaveBeenCalledWith('one', '原相机前');
    expect(screen.queryByRole('button', { name: '打开文件夹 原相机前_16:04-16:05' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '详情' }));
    expect(await screen.findByRole('dialog', { name: '文件详情' })).toHaveTextContent('complete-file');
    fireEvent.click(screen.getByRole('button', { name: '关闭文件详情' }));
    fireEvent.click(screen.getByRole('button', { name: '查看原始分段' }));
    fireEvent.click(screen.getByRole('button', { name: '打开文件夹 原相机前_16:04-16:05' }));
    expect(currentPath()).toBe(timeSlot);
    fireEvent.click(screen.getByRole('button', { name: '返回上级文件夹' }));
    expect(currentPath()).toBe('原相机前');
    expect(await screen.findByRole('button', { name: '打开文件夹 原相机前_16:04-16:05' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '收起原始分段' }));
    expect(screen.queryByRole('button', { name: '打开文件夹 原相机前_16:04-16:05' })).not.toBeInTheDocument();
    expect(api.createDatasetFolder).not.toHaveBeenCalled(); expect(api.assembleDatasetRecording).not.toHaveBeenCalled();
  });

  it('refreshes complete recordings with the current-directory refresh action', async () => {
    useArchive(); mount(`/datasets/one?path=${encodeURIComponent('原相机前')}`);
    await screen.findByText('暂无录像');
    const before = vi.mocked(api.datasetRecordings).mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: '刷新当前目录' }));
    await waitFor(() => expect(api.datasetRecordings).toHaveBeenCalledTimes(before + 1));
    expect(api.datasetRecordings).toHaveBeenLastCalledWith('one', '原相机前');
  });

  it('keeps legacy non-camera sensor folders on the normal file browser', async () => {
    useArchive();
    vi.mocked(api.datasetContents).mockResolvedValue({ dataset: { ...datasetRow, metadata: archiveMetadata }, path: '雷达', folders: [{ id: 'radar-bag', name: '雷达_16:04-16:05', path: '雷达/雷达_16:04-16:05', parentPath: '雷达' }], files: [], total: 0, page: 1, pageSize: 50 } as never);
    mount(`/datasets/one?path=${encodeURIComponent('雷达')}`);
    expect(await screen.findByRole('button', { name: '打开文件夹 雷达_16:04-16:05' })).toBeVisible();
    expect(screen.queryByRole('region', { name: '采集视频' })).not.toBeInTheDocument();
    expect(api.datasetRecordings).not.toHaveBeenCalled();
  });

  it('shows a single archive recording directly in its time slot and downloads the original file', async () => {
    state.canWrite = false; useArchive();
    const file = archiveFile('recording-one', firstSegment, '2026-10-28T08:04:05Z');
    vi.mocked(api.datasetContents).mockResolvedValue({ dataset: { ...datasetRow, metadata: archiveMetadata }, path: timeSlot, folders: [], files: [file], total: 1, page: 1, pageSize: 50, view: { kind: 'archive-time-slot', collapsedSegments: 1 } } as never);
    vi.mocked(api.download).mockResolvedValue({ url: '/dbprog/objects/original-recording' });
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount(`/datasets/one?path=${encodeURIComponent(timeSlot)}`);
      expect(await screen.findByText('video.mkv')).toBeVisible();
      expect(screen.getByText('片段 1 · 2026-10-28 16:04:05（北京时间）')).toBeVisible();
      expect(screen.queryByRole('button', { name: `打开文件夹 ${firstSegment}` })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: '下载文件 video.mkv' }));
      await waitFor(() => expect(api.download).toHaveBeenCalledWith('recording-one'));
      expect(file.folderPath).toBe(`${timeSlot}/${firstSegment}`);
      expect(file.originalName).toBe('video.mkv');
      expect(api.createDatasetFolder).not.toHaveBeenCalled(); expect(api.deleteFile).not.toHaveBeenCalled(); expect(state.queue.addFiles).not.toHaveBeenCalled();
    } finally { download.mockRestore(); }
  });

  it('keeps multiple same-named recordings separate and downloads each actual ID', async () => {
    state.canWrite = false; useArchive();
    vi.mocked(api.datasetContents).mockResolvedValue({ dataset: { ...datasetRow, metadata: archiveMetadata }, path: timeSlot, folders: [], files: [archiveFile('recording-one', firstSegment, '2026-10-28T08:04:05Z'), archiveFile('recording-two', secondSegment, '2026-10-28T08:04:35Z')], total: 2, page: 1, pageSize: 50, view: { kind: 'archive-time-slot', collapsedSegments: 2 } } as never);
    vi.mocked(api.download).mockResolvedValue({ url: '/dbprog/objects/original-recording' });
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      mount(`/datasets/one?path=${encodeURIComponent(timeSlot)}`);
      expect(await screen.findAllByText('video.mkv')).toHaveLength(2);
      expect(screen.getByText('片段 1 · 2026-10-28 16:04:05（北京时间）')).toBeVisible();
      expect(screen.getByText('片段 2 · 2026-10-28 16:04:35（北京时间）')).toBeVisible();
      const buttons = screen.getAllByRole('button', { name: '下载文件 video.mkv' });
      fireEvent.click(buttons[0]); fireEvent.click(buttons[1]);
      await waitFor(() => expect(api.download).toHaveBeenCalledTimes(2));
      expect(api.download).toHaveBeenNthCalledWith(1, 'recording-one'); expect(api.download).toHaveBeenNthCalledWith(2, 'recording-two');
    } finally { download.mockRestore(); }
  });

  it('replaces an old managed segment link with the time slot and keeps refresh on the logical directory', async () => {
    useArchive();
    vi.mocked(api.datasetContents).mockImplementation(async (_id, path = '', page = 1) => ({ dataset: { ...datasetRow, metadata: archiveMetadata }, path, folders: [], files: [archiveFile('recording-one', firstSegment, '2026-10-28T08:04:05Z')], total: 1, page, pageSize: 50 }) as never);
    mount(`/datasets/one?path=${encodeURIComponent(`${timeSlot}/${firstSegment}`)}`);
    await waitFor(() => expect(currentPath()).toBe(timeSlot));
    await screen.findByText('video.mkv');
    expect(screen.queryByTitle(firstSegment)).not.toBeInTheDocument();
    expect(backButton()).toBeDisabled(); expect(forwardButton()).toBeDisabled();
    const calls = vi.mocked(api.datasetContents).mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: '刷新当前目录' }));
    await waitFor(() => expect(api.datasetContents).toHaveBeenCalledTimes(calls + 1));
    expect(api.datasetContents).toHaveBeenLastCalledWith('one', timeSlot, 1);
    fireEvent.click(screen.getByRole('button', { name: '返回上级文件夹' }));
    expect(currentPath()).toBe('原相机前');
    expect(api.createDatasetFolder).not.toHaveBeenCalled(); expect(api.deleteFile).not.toHaveBeenCalled();
  });

  it('keeps lifted same-named folders distinct and skips the segment when returning to the parent', async () => {
    useArchive();
    const nested = `${timeSlot}/${secondSegment}/compressed-bag`;
    vi.mocked(api.datasetContents).mockImplementation(async (_id, path = '', page = 1) => ({ dataset: { ...datasetRow, metadata: archiveMetadata }, path, folders: path === timeSlot ? [
      { id: 'bag-one', name: 'compressed-bag', path: `${timeSlot}/${firstSegment}/compressed-bag`, parentPath: `${timeSlot}/${firstSegment}` },
      { id: 'bag-two', name: 'compressed-bag', path: nested, parentPath: `${timeSlot}/${secondSegment}` },
    ] : [], files: [], total: 0, page, pageSize: 50 }) as never);
    mount(`/datasets/one?path=${encodeURIComponent(timeSlot)}`);
    expect(await screen.findByRole('button', { name: '打开文件夹 compressed-bag（片段 1）' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '打开文件夹 compressed-bag（片段 2）' }));
    await waitFor(() => expect(currentPath()).toBe(nested));
    expect(screen.queryByTitle(secondSegment)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '原相机前_16:04-16:05' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '返回上级文件夹' }));
    expect(currentPath()).toBe(timeSlot);
    fireEvent.click(backButton()); expect(currentPath()).toBe(nested);
    fireEvent.click(forwardButton()); expect(currentPath()).toBe(timeSlot);
  });

  it('keeps an ordinary dataset hash folder visible and preserves its real parent', async () => {
    const original = `${timeSlot}/${firstSegment}`;
    mount(`/datasets/one?path=${encodeURIComponent(original)}`);
    expect(await screen.findByTitle(firstSegment)).toBeVisible();
    expect(currentPath()).toBe(original);
    fireEvent.click(screen.getByRole('button', { name: '返回上级文件夹' }));
    expect(currentPath()).toBe(timeSlot);
  });

  it('preserves an archive nested path through versions and a direct reload', async () => {
    useArchive();
    const nested = `${timeSlot}/${firstSegment}/compressed-bag`;
    const directory = `/datasets/one?path=${encodeURIComponent(nested)}&page=2`;
    const first = mount(directory);
    await waitFor(() => expect(screen.queryByTitle(firstSegment)).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /版本管理/ }));
    fireEvent.click(await screen.findByRole('button', { name: /返回文件目录/ }));
    expect(route()).toBe(directory);
    first.unmount();
    mount(directory);
    await waitFor(() => expect(api.datasetContents).toHaveBeenLastCalledWith('one', nested, 2));
    expect(currentPath()).toBe(nested);
    await waitFor(() => expect(screen.queryByTitle(firstSegment)).not.toBeInTheDocument());
  });
});

describe('Dataset return target boundaries', () => {
  it('accepts only the dataset list route and its query', () => {
    expect(datasetListReturn({ datasetsReturnTo: '/datasets?search=garage&page=2' })).toBe('/datasets?search=garage&page=2');
    for (const value of ['https://example.com', '//example.com', '/files', '/datasets/one', '/datasets#outside', '/datasets\\outside', 42, null]) {
      expect(datasetListReturn({ datasetsReturnTo: value })).toBe('/datasets');
    }
    expect(datasetListReturn(null)).toBe('/datasets');
  });
  it('accepts only the current dataset directory route and its query', () => {
    expect(datasetDirectoryReturn({ directoryReturnTo: '/datasets/one?path=images%2Fraw&page=2' }, 'one')).toBe('/datasets/one?path=images%2Fraw&page=2');
    for (const value of ['https://example.com', '//example.com', '/datasets/two', '/datasets/one/history', '/datasets/one#outside', '/datasets/one\\outside', 42, null]) {
      expect(datasetDirectoryReturn({ directoryReturnTo: value }, 'one')).toBe('/datasets/one');
    }
    expect(datasetDirectoryReturn(null, 'one')).toBe('/datasets/one');
  });
});
