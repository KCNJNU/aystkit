import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp } from 'antd';
import { PROCESSED_PACKAGE_REQUIREMENT } from '../services/processed-package';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UploadCenter } from './UploadCenter';
import { api } from '../services/api';
const queue = vi.hoisted(() => ({ items: [], addFiles: vi.fn(), cancel: vi.fn(), retry: vi.fn(), remove: vi.fn(), clearCompleted: vi.fn() }));
vi.mock('../upload/UploadQueueContext', () => ({ useUploadQueue: () => queue }));
vi.mock('../services/api', () => ({ api: { datasets: vi.fn(), datasetFolders: vi.fn() } }));
function mount(entry = '/upload') {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AntApp><MemoryRouter initialEntries={[entry]}><UploadCenter /></MemoryRouter></AntApp></QueryClientProvider>);
}
describe('Dataset selection before upload', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.datasetFolders).mockResolvedValue([]); vi.mocked(api.datasets).mockResolvedValue([{ id: 'one', name: 'test01' }, { id: 'two', name: 'test02' }] as never); });
  it('disables file inputs until a dataset has been selected', async () => {
    const { container } = mount();
    await waitFor(() => expect(api.datasets).toHaveBeenCalled());
    expect(screen.getByText('请先选择目标数据集')).toBeVisible();
    for (const input of container.querySelectorAll('input[type="file"]')) expect(input).toBeDisabled();
  });
  it('carries the selected dataset from its workspace into each queued file', async () => {
    const { container } = mount('/upload?dataset=two');
    await screen.findByText('即将上传到 test02');
    const input = container.querySelector('input[type="file"]')!;
    expect(input).not.toBeDisabled();
    const file = new File(['{}'], 'sample.json', { type: 'application/json' });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(queue.addFiles).toHaveBeenCalledWith([expect.objectContaining({ name: 'sample.json' })], expect.objectContaining({ id: 'two', name: 'test02' })));
  });
  it('keeps uploading disabled for a dataset outside the accessible list', async () => {
    const { container } = mount('/upload?dataset=not-accessible');
    await waitFor(() => expect(api.datasets).toHaveBeenCalled());
    for (const input of container.querySelectorAll('input[type="file"]')) expect(input).toBeDisabled();
  });
});


describe('processed package selection in upload center', () => {
  it('restricts the selected processed ancestor to ZIP and disables loose folder selection', async () => {
    const path = '处理好的数据_20261112_153045/review';
    vi.mocked(api.datasets).mockResolvedValue([{ id: 'one', name: 'test01' }] as never);
    vi.mocked(api.datasetFolders).mockResolvedValue([{ id: 'processed', name: 'review', path, parentPath: '处理好的数据_20261112_153045' }] as never);
    const { container } = mount('/upload?dataset=one&path=' + encodeURIComponent(path));
    expect(await screen.findByText(/ZIP · 处理前图像/)).toBeVisible();
    await waitFor(() => expect(screen.getByRole('button', { name: /选择整个文件夹/ })).toBeDisabled());
    const inputs = Array.from(container.querySelectorAll('input[type="file"]'));
    const enabled = inputs.filter(input => !(input as HTMLInputElement).disabled);
    expect(enabled).toHaveLength(1);
    expect(enabled[0]).toHaveAttribute('accept', '.zip');
  });
});


describe('processed package drag feedback in upload center', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.datasets).mockResolvedValue([{ id: 'one', name: 'test01' }] as never);
  });
  it('reports non-ZIP drops before accept discards them while allowing valid ZIPs in the same drop', async () => {
    const path = '处理好的数据_20261112_153045';
    vi.mocked(api.datasetFolders).mockResolvedValue([{ id: 'processed', name: path, path, parentPath: '' }] as never);
    const { container } = mount('/upload?dataset=one&path=' + encodeURIComponent(path));
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeDisabled());
    const zip = new File(['fixture zip'], 'result.zip');
    fireEvent.drop(container.querySelector('.ant-upload-drag .ant-upload')!, { dataTransfer: { files: [new File(['image'], 'before.jpg'), zip], items: [] } });
    await waitFor(() => expect(document.querySelector('.ant-message-error')).toHaveTextContent(PROCESSED_PACKAGE_REQUIREMENT));
    await waitFor(() => expect(queue.addFiles).toHaveBeenCalledTimes(1));
    expect(queue.addFiles).toHaveBeenCalledWith([expect.objectContaining({ name: 'result.zip' })], expect.objectContaining({ folderPath: path }));
    expect(document.querySelectorAll('.ant-message-error')).toHaveLength(1);
  });
  it('does not add a processed-format warning to raw destination drops', async () => {
    vi.mocked(api.datasetFolders).mockResolvedValue([]);
    const { container } = mount('/upload?dataset=one');
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeDisabled());
    fireEvent.drop(container.querySelector('.ant-upload-drag .ant-upload')!, { dataTransfer: { files: [new File(['image'], 'frame.jpg')], items: [] } });
    await waitFor(() => expect(queue.addFiles).toHaveBeenCalledWith([expect.objectContaining({ name: 'frame.jpg' })], expect.objectContaining({ folderPath: '' })));
    expect(document.querySelector('.ant-message-error')).toBeNull();
  });
  it('does not report a format error for an inaccessible destination', async () => {
    vi.mocked(api.datasetFolders).mockResolvedValue([]);
    const { container } = mount('/upload?dataset=not-accessible&path=' + encodeURIComponent('处理好的数据_20261112_153045'));
    await screen.findByText('请先选择目标数据集');
    fireEvent.drop(container.querySelector('.ant-upload-drag')!, { dataTransfer: { files: [new File(['image'], 'frame.jpg')], items: [] } });
    expect(document.querySelector('.ant-message-error')).toBeNull();
    expect(queue.addFiles).not.toHaveBeenCalled();
  });
});
