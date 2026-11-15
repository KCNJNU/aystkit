import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp } from 'antd';
import { PROCESSED_PACKAGE_REQUIREMENT } from '../services/processed-package';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../services/api';
import { UploadQueueProvider, useUploadQueue } from './UploadQueueContext';

vi.mock('../services/api', () => ({
  api: { uploadDefaults: vi.fn(), files: vi.fn(), createUpload: vi.fn(), uploadPart: vi.fn(), completeUpload: vi.fn(), file: vi.fn(), abortUpload: vi.fn(), uploadSession: vi.fn(), job: vi.fn() },
}));

const fileRecord = { id: 'file-existing', originalName: 'existing.json' };

function Harness() {
  const queue = useUploadQueue();
  const addFolderFile = () => {
    const file = new File(['arbitrary'], 'trace.custom', { type: '', lastModified: 2 });
    Object.defineProperty(file, 'webkitRelativePath', { value: 'logs/trace.custom' });
    queue.addFiles([file], { id: 'dataset-1', name: 'test01' });
  };
  return <><button onClick={() => queue.addFiles([new File(['{}'], 'scan.json', { type: 'application/json', lastModified: 1 })], { id: 'dataset-1', name: 'test01' })}>Add</button><button onClick={addFolderFile}>Add folder file</button><button onClick={() => queue.items[0] && queue.retry(queue.items[0].id)}>Retry</button><button onClick={() => queue.items[0] && queue.remove(queue.items[0].id)}>Remove</button><output>{queue.items[0]?.status ?? 'EMPTY'}</output></>;
}

function renderQueue(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return { client, ...render(<QueryClientProvider client={client}><AntApp><UploadQueueProvider><Harness /></UploadQueueProvider></AntApp></QueryClientProvider>) };
}

describe('UploadQueueProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.uploadDefaults).mockResolvedValue({ robotId: 'robot-1', recognitionType: 'unclassified', schemaVersion: '1.0', capturedAtStrategy: 'LAST_MODIFIED' });
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues: (bytes: Uint8Array) => bytes.fill(1) } });
  });

  it('marks a checksum match as duplicate without uploading and supports retry', async () => {
    vi.mocked(api.files).mockResolvedValue({ items: [fileRecord], total: 1, page: 1, pageSize: 1 } as never);
    renderQueue();
    fireEvent.click(screen.getByText('Add'));

    expect(await screen.findByText('DUPLICATE')).toBeVisible();
    expect(api.createUpload).not.toHaveBeenCalled();
    expect(api.files).toHaveBeenCalledWith(expect.objectContaining({ datasetId: 'dataset-1' }));
    fireEvent.click(screen.getByText('Retry'));
    expect(screen.getByText('QUEUED')).toBeVisible();
  });

  it('completes an observable upload and invalidates resource queries', async () => {
    vi.mocked(api.files).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 1 });
    vi.mocked(api.createUpload).mockResolvedValue({ id: 'session-1', status: 'CREATED', originalName: 'scan.json', fileSize: '2', checksum: '0'.repeat(64), chunkSize: 8 * 1024 * 1024, totalParts: 1, expiresAt: new Date(Date.now() + 10000).toISOString(), parts: [] });
    vi.mocked(api.uploadPart).mockResolvedValue({ partNumber: 1 });
    vi.mocked(api.completeUpload).mockResolvedValue({ completed: true, robotFileId: 'new-file' });
    vi.mocked(api.file).mockResolvedValue({ id: 'new-file' } as never);
    const { client } = renderQueue(); const invalidate = vi.spyOn(client, 'invalidateQueries');
    await act(async () => fireEvent.click(screen.getByText('Add')));

    await waitFor(() => expect(screen.getByText('COMPLETED')).toBeVisible());
    expect(api.createUpload).toHaveBeenCalledWith(expect.objectContaining({ datasetId: 'dataset-1', robotId: 'robot-1', recognitionType: 'unclassified', schemaVersion: '1.0', originalName: 'scan.json' }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['files'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['dashboard'] });
  });

  it('uploads an arbitrary folder file immediately and preserves its relative path', async () => {
    vi.mocked(api.files).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 1 });
    vi.mocked(api.createUpload).mockResolvedValue({ id: 'session-1', status: 'CREATED', originalName: 'logs/trace.custom', fileSize: '9', checksum: '0'.repeat(64), chunkSize: 8 * 1024 * 1024, totalParts: 1, expiresAt: new Date(Date.now() + 10000).toISOString(), parts: [] });
    vi.mocked(api.uploadPart).mockResolvedValue({ partNumber: 1 });
    vi.mocked(api.completeUpload).mockResolvedValue({ completed: true, robotFileId: 'folder-file' });
    vi.mocked(api.file).mockResolvedValue({ id: 'folder-file' } as never);
    renderQueue();

    fireEvent.click(screen.getByText('Add folder file'));

    await waitFor(() => expect(screen.getByText('COMPLETED')).toBeVisible());
    expect(api.createUpload).toHaveBeenCalledWith(expect.objectContaining({
      originalName: 'logs/trace.custom',
      mimeType: 'application/octet-stream',
      metadata: { relativePath: 'logs/trace.custom' },
    }));
  });
  it('aborts the server session before removing a failed upload so dataset deletion is no longer blocked', async () => {
    vi.mocked(api.files).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 1 });
    vi.mocked(api.createUpload).mockResolvedValue({ id: 'session-failed', status: 'CREATED', originalName: 'scan.json', fileSize: '2', checksum: '0'.repeat(64), chunkSize: 8 * 1024 * 1024, totalParts: 1, expiresAt: new Date(Date.now() + 10000).toISOString(), parts: [] });
    vi.mocked(api.uploadPart).mockRejectedValueOnce(new Error('offline'));
    vi.mocked(api.abortUpload).mockResolvedValue(undefined);
    renderQueue();
    fireEvent.click(screen.getByText('Add'));
    await screen.findByText('FAILED');
    fireEvent.click(screen.getByText('Remove'));
    await screen.findByText('EMPTY');
    expect(api.abortUpload).toHaveBeenCalledWith('session-failed');
  });

});


function ProcessedHarness({ folderPath = '处理好的数据_20261112_153045', fileName = 'loose.jpg', relativePath, fileSize }: { folderPath?: string; fileName?: string; relativePath?: string; fileSize?: number }) {
  const queue = useUploadQueue();
  const add = () => {
    const file = new File(['fixture'], fileName, { lastModified: 7 });
    if (relativePath) Object.defineProperty(file, 'webkitRelativePath', { value: relativePath });
    if (fileSize !== undefined) Object.defineProperty(file, 'size', { value: fileSize });
    queue.addFiles([file], { id: 'dataset-1', name: 'test01', folderPath });
  };
  return <><button onClick={add}>Add processed</button><button onClick={() => queue.items[0] && queue.retry(queue.items[0].id)}>Retry processed</button><output data-testid="processed-status">{queue.items[0]?.status ?? 'EMPTY'}</output><output data-testid="processed-error">{queue.items[0]?.error}</output></>;
}

function renderProcessed(options: Parameters<typeof ProcessedHarness>[0] = {}) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AntApp><UploadQueueProvider><ProcessedHarness {...options} /></UploadQueueProvider></AntApp></QueryClientProvider>);
}

describe('processed result package validation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.uploadDefaults).mockResolvedValue({ robotId: 'robot-1', recognitionType: 'unclassified', schemaVersion: '1.0', capturedAtStrategy: 'LAST_MODIFIED' });
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues: (bytes: Uint8Array) => bytes.fill(1) } });
  });

  it('rejects a loose file before hashing, duplicate lookup and create, including after retry', async () => {
    renderProcessed();
    fireEvent.click(screen.getByText('Add processed'));
    await waitFor(() => expect(screen.getByTestId('processed-status')).toHaveTextContent('FAILED'));
    expect(screen.getByTestId('processed-error')).toHaveTextContent(PROCESSED_PACKAGE_REQUIREMENT);
    await waitFor(() => expect(document.querySelector('.ant-message-error')).toHaveTextContent(PROCESSED_PACKAGE_REQUIREMENT));
    expect(api.files).not.toHaveBeenCalled();
    expect(api.createUpload).not.toHaveBeenCalled();
    expect(api.uploadPart).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Retry processed'));
    await waitFor(() => expect(screen.getByTestId('processed-status')).toHaveTextContent('FAILED'));
    expect(api.createUpload).not.toHaveBeenCalled();
  });

  it('also rejects processed loose files introduced by folder selection from a raw root', async () => {
    renderProcessed({ folderPath: '', relativePath: 'batch/处理好的数据_20261112_153045/frame.jpg' });
    fireEvent.click(screen.getByText('Add processed'));
    await waitFor(() => expect(screen.getByTestId('processed-status')).toHaveTextContent('FAILED'));
    expect(screen.getByTestId('processed-error')).toHaveTextContent(PROCESSED_PACKAGE_REQUIREMENT);
    expect(api.files).not.toHaveBeenCalled();
    expect(api.createUpload).not.toHaveBeenCalled();
  });

  it('reaches the API for a ZIP larger than 8 GiB without allocating a large payload', async () => {
    const declaredSize = 8 * 1024 ** 3 + 1;
    const stop = 'fixture stopped after the upload API accepted the request';
    vi.mocked(api.files).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 1 });
    vi.mocked(api.createUpload).mockRejectedValueOnce(new Error(stop));
    // Only File.size is overridden; slicing still reads the seven fixture bytes.
    // Stop at session creation so the test cannot transfer a synthetic large file.
    renderProcessed({ fileName: 'batch.zip', fileSize: declaredSize });
    await act(async () => fireEvent.click(screen.getByText('Add processed')));
    await waitFor(() => expect(api.createUpload).toHaveBeenCalledWith(expect.objectContaining({
      originalName: 'batch.zip', fileSize: declaredSize,
      folderPath: '处理好的数据_20261112_153045',
    })));
    await waitFor(() => expect(screen.getByTestId('processed-error')).toHaveTextContent(stop));
    expect(api.uploadPart).not.toHaveBeenCalled();
  });

  it('surfaces the server content validation failure in the queue and an immediate message', async () => {
    const reason = '处理结果压缩包缺少处理后图像';
    vi.mocked(api.files).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 1 });
    vi.mocked(api.createUpload).mockResolvedValue({ id: 'processed-session', status: 'CREATED', originalName: 'result.ZIP', fileSize: '7', checksum: '0'.repeat(64), chunkSize: 8 * 1024 * 1024, totalParts: 1, expiresAt: new Date(Date.now() + 10000).toISOString(), parts: [] });
    vi.mocked(api.uploadPart).mockResolvedValue({ partNumber: 1 });
    vi.mocked(api.completeUpload).mockResolvedValue({ id: 'processed-job' } as never);
    vi.mocked(api.job).mockResolvedValue({ id: 'processed-job', status: 'FAILED', progress: 100, errorMessage: reason } as never);
    renderProcessed({ fileName: 'result.ZIP' });
    await act(async () => fireEvent.click(screen.getByText('Add processed')));
    await waitFor(() => expect(screen.getByTestId('processed-status')).toHaveTextContent('FAILED'));
    expect(screen.getByTestId('processed-error')).toHaveTextContent(reason);
    await waitFor(() => expect(document.querySelector('.ant-message-error')).toHaveTextContent(reason));
    expect(api.createUpload).toHaveBeenCalledTimes(1);
    expect(api.file).not.toHaveBeenCalled();
  });
});
