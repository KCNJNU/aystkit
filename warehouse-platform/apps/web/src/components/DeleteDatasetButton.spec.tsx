import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ConfigProvider } from 'antd';
import { api, ApiError, type DatasetBlockingUpload } from '../services/api';
import { DeleteDatasetButton } from './DeleteDatasetButton';
const state = vi.hoisted(() => ({ canWrite: true }));
vi.mock('../auth/AuthContext', () => ({ useOptionalAuth: () => ({ hasRole: () => state.canWrite }) }));
vi.mock('../services/api', async importOriginal => ({ ...await importOriginal<typeof import('../services/api')>(), api: { deleteDataset: vi.fn(), abortUpload: vi.fn() } }));
function mount(beforeOpen?: (action: () => void) => void) {
  const onDeleted = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(<QueryClientProvider client={client}><ConfigProvider theme={{ token: { motion: false } }}><DeleteDatasetButton dataset={{ id: 'dataset', name: 'test01' }} onDeleted={onDeleted} beforeOpen={beforeOpen} /></ConfigProvider></QueryClientProvider>);
  return { onDeleted, invalidate };
}

describe('Direct dataset deletion', () => {
  beforeEach(() => { vi.clearAllMocks(); state.canWrite = true; vi.mocked(api.deleteDataset).mockReset().mockResolvedValue({ id: 'delete-job' } as never); vi.mocked(api.abortUpload).mockReset().mockResolvedValue(undefined); });
  it('sends no request on render, then deletes with one click and the actual name', async () => {
    const { invalidate, onDeleted } = mount();
    expect(api.deleteDataset).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '删除数据集 test01' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    expect(api.deleteDataset).toHaveBeenCalledExactlyOnceWith('dataset', 'test01');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['datasets'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['files'] });
  });
  it('shows a server refusal without claiming success, and permits a direct retry', async () => {
    vi.mocked(api.deleteDataset).mockRejectedValueOnce(new Error('文件被其他数据集引用'));
    const { onDeleted } = mount();
    fireEvent.click(screen.getByRole('button', { name: '删除数据集 test01' }));
    expect(await screen.findByText('文件被其他数据集引用')).toBeVisible();
    expect(onDeleted).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '删除数据集 test01' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    expect(api.deleteDataset).toHaveBeenCalledTimes(2);
  });
  it('ignores repeated clicks while the deletion request is pending', async () => {
    let finish!: () => void;
    vi.mocked(api.deleteDataset).mockImplementation(() => new Promise(resolve => { finish = () => resolve({ id: 'job' } as never); }));
    const { onDeleted } = mount(); const button = screen.getByRole('button', { name: '删除数据集 test01' });
    fireEvent.click(button); fireEvent.click(button);
    expect(button).toBeDisabled(); expect(api.deleteDataset).toHaveBeenCalledTimes(1);
    finish(); await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
  });
  it('retains the caller navigation hook without adding a confirmation step', async () => {
    const beforeOpen = vi.fn((action: () => void) => action());
    mount(beforeOpen); fireEvent.click(screen.getByRole('button', { name: '删除数据集 test01' }));
    await waitFor(() => expect(api.deleteDataset).toHaveBeenCalledExactlyOnceWith('dataset', 'test01'));
    expect(beforeOpen).toHaveBeenCalledTimes(1); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('does not expose deletion to a read-only user', () => {
    state.canWrite = false; mount();
    expect(screen.queryByRole('button', { name: '删除数据集 test01' })).not.toBeInTheDocument();
    expect(api.deleteDataset).not.toHaveBeenCalled();
  });

  const upload: DatasetBlockingUpload = { id: 'empty-upload', originalName: 'metadata.yaml', status: 'CREATED', partCount: 0,
    createdAt: '2026-11-01T11:38:17Z', updatedAt: '2026-11-01T11:38:17Z', expiresAt: '2026-11-02T11:38:17Z', canAbortEmpty: true };
  function refuseWithUploads(rows: DatasetBlockingUpload[]) {
    vi.mocked(api.deleteDataset).mockRejectedValueOnce(new ApiError('数据集仍有未完成的上传', 409,
      { code: 'DATASET_UPLOADS_PENDING', blockingUploads: rows }));
  }

  it('shows blockers and requires confirmation before cancelling an eligible empty upload', async () => {
    refuseWithUploads([upload]);
    const { onDeleted } = mount();
    fireEvent.click(screen.getByRole('button', { name: '删除数据集 test01' }));
    expect(await screen.findByText('metadata.yaml')).toBeVisible();
    expect(screen.getByText(/0 个已上传分片/)).toBeVisible();
    expect(api.abortUpload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '取消空上传 metadata.yaml' }));
    await waitFor(() => expect(screen.getByText('取消这个空上传？')).toBeVisible());
    expect(screen.getByText('仅取消 metadata.yaml 的空上传任务，不会删除数据集或已保存文件。')).toBeVisible();
    expect(api.abortUpload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '确认取消空上传' }));
    expect(await screen.findByText('空上传已取消，可重新点击“删除”重试；尚未删除数据集。')).toBeVisible();
    expect(api.abortUpload).toHaveBeenCalledExactlyOnceWith('empty-upload');
    expect(api.deleteDataset).toHaveBeenCalledTimes(1);
    expect(onDeleted).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '删除数据集 test01' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    expect(api.deleteDataset).toHaveBeenCalledTimes(2);
  });

  it('does not cancel an empty upload when the user chooses to keep it', async () => {
    refuseWithUploads([upload]); mount();
    fireEvent.click(screen.getByRole('button', { name: '删除数据集 test01' }));
    fireEvent.click(await screen.findByRole('button', { name: '取消空上传 metadata.yaml' }));
    fireEvent.click(await screen.findByRole('button', { name: /保\s*留/ }));
    expect(api.abortUpload).not.toHaveBeenCalled();
    expect(api.deleteDataset).toHaveBeenCalledTimes(1);
  });

  it.each([
    { ...upload, canAbortEmpty: false },
    { ...upload, partCount: 1 },
    { ...upload, status: 'UPLOADING' },
  ])('never offers abort for an unapproved or nonempty upload: %j', async blocker => {
    refuseWithUploads([blocker]); mount();
    fireEvent.click(screen.getByRole('button', { name: '删除数据集 test01' }));
    expect(await screen.findByText('metadata.yaml')).toBeVisible();
    expect(screen.queryByRole('button', { name: /取消空上传/ })).not.toBeInTheDocument();
    expect(api.abortUpload).not.toHaveBeenCalled();
  });

  it('shows permission refusal without removing the blocker or retrying deletion', async () => {
    refuseWithUploads([upload]);
    vi.mocked(api.abortUpload).mockRejectedValueOnce(new ApiError('该上传不属于当前用户', 403));
    const { onDeleted } = mount();
    fireEvent.click(screen.getByRole('button', { name: '删除数据集 test01' }));
    fireEvent.click(await screen.findByRole('button', { name: '取消空上传 metadata.yaml' }));
    fireEvent.click(await screen.findByRole('button', { name: '确认取消空上传' }));
    expect(await screen.findByText('无权取消该上传：该上传不属于当前用户')).toBeVisible();
    expect(screen.getByText('metadata.yaml')).toBeVisible();
    expect(api.deleteDataset).toHaveBeenCalledTimes(1);
    expect(onDeleted).not.toHaveBeenCalled();
  });
});
