import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp } from 'antd';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DatasetWorkspace } from './DatasetWorkspace';
import { api } from '../services/api';
import { PROCESSED_PACKAGE_LABEL, PROCESSED_PACKAGE_REQUIREMENT } from '../services/processed-package';

const queue = vi.hoisted(() => ({ items: [], addFiles: vi.fn() }));

vi.mock('../auth/AuthContext', () => ({ useOptionalAuth: () => ({ hasRole: () => true }) }));
vi.mock('../upload/UploadQueueContext', () => ({ useUploadQueue: () => queue }));
vi.mock('../services/api', () => ({ api: { dataset: vi.fn(), datasetContents: vi.fn() } }));
vi.mock('../components/WorkspaceShortcuts', () => ({ useRecordWorkspaceVisit: vi.fn(), WorkspaceFavoriteButton: () => null }));
vi.mock('../components/FilePreview', () => ({ FilePreview: () => null }));

function mount(path: string) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AntApp><MemoryRouter initialEntries={[`/datasets/one?path=${encodeURIComponent(path)}`]}><Routes><Route path="/datasets/:datasetId" element={<DatasetWorkspace />} /></Routes></MemoryRouter></AntApp></QueryClientProvider>);
}

describe('processed result upload controls', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.dataset).mockResolvedValue({ id: 'one', name: 'test01', metadata: {} } as never);
    vi.mocked(api.datasetContents).mockImplementation(async (_id, path = '') => ({ dataset: { id: 'one', name: 'test01' }, path, folders: [], files: [], total: 0, page: 1, pageSize: 50 }) as never);
  });
  it('offers ZIP files and explains the required package while disabling loose folder selection', async () => {
    const { container } = mount('处理好的数据_20261112_153045/review');
    await waitFor(() => expect(screen.getByRole('button', { name: '上传文件' })).toBeEnabled());
    expect(screen.getByText(PROCESSED_PACKAGE_LABEL)).toBeVisible();
    expect(screen.getByText(/处理前\/原图.jpg/)).toBeVisible();
    expect(container.querySelector('pre')?.textContent?.split('\n')).toHaveLength(4);
    const enabled = Array.from(container.querySelectorAll('input[type="file"]')).filter(input => !(input as HTMLInputElement).disabled);
    expect(enabled.length).toBeGreaterThan(0);
    for (const input of enabled) expect(input).toHaveAttribute('accept', '.zip');
    fireEvent.click(screen.getByRole('button', { name: '更多上传选项' }));
    expect(await screen.findByRole('button', { name: /上传整个文件夹/ })).toBeDisabled();
  });
  it('preserves unrestricted raw directory inputs', async () => {
    const { container } = mount('原相机前');
    await waitFor(() => expect(screen.getByRole('button', { name: '上传文件' })).toBeEnabled());
    expect(screen.queryByText(PROCESSED_PACKAGE_LABEL)).not.toBeInTheDocument();
    for (const input of container.querySelectorAll('input[type="file"]')) expect(input.getAttribute('accept')).not.toBe('.zip');
    fireEvent.click(screen.getByRole('button', { name: '更多上传选项' }));
    expect(await screen.findByRole('button', { name: /上传整个文件夹/ })).toBeEnabled();
  });
});


describe('processed package drag feedback in the dataset workspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.dataset).mockResolvedValue({ id: 'one', name: 'test01', metadata: {} } as never);
    vi.mocked(api.datasetContents).mockImplementation(async (_id, path = '') => ({ dataset: { id: 'one', name: 'test01' }, path, folders: [], files: [], total: 0, page: 1, pageSize: 50 }) as never);
  });
  it('shows one clear error when accept filters non-ZIP drops before beforeUpload', async () => {
    const { container } = mount('处理好的数据_20261112_153045');
    await waitFor(() => expect(screen.getByRole('button', { name: '上传文件' })).toBeEnabled());
    fireEvent.drop(container.querySelector('.ant-upload-drag .ant-upload')!, { dataTransfer: { files: [new File(['image'], 'before.jpg'), new File(['{}'], 'result.json')], items: [] } });
    await waitFor(() => expect(document.querySelector('.ant-message-error')).toHaveTextContent(PROCESSED_PACKAGE_REQUIREMENT));
    expect(document.querySelectorAll('.ant-message-error')).toHaveLength(1);
    expect(queue.addFiles).not.toHaveBeenCalled();
  });

  it('preserves loose-file drops without processed-format warnings in a raw directory', async () => {
    queue.addFiles.mockClear();
    const { container } = mount('原相机前');
    await waitFor(() => expect(screen.getByRole('button', { name: '上传文件' })).toBeEnabled());
    const image = new File(['image'], 'frame.jpg');
    fireEvent.drop(container.querySelector('.ant-upload-drag .ant-upload')!, { dataTransfer: { files: [image], items: [] } });
    await waitFor(() => expect(queue.addFiles).toHaveBeenCalledWith([expect.objectContaining({ name: 'frame.jpg' })], expect.objectContaining({ folderPath: '原相机前' })));
    expect(document.querySelector('.ant-message-error')).toBeNull();
  });
});
