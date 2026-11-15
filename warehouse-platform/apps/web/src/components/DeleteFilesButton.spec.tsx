import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { StrictMode, useState, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DeleteFilesButton, FileDeleteDialog } from './DeleteFilesButton';
import { api } from '../services/api';

const state = vi.hoisted(() => ({ canWrite: true }));
vi.mock('../auth/AuthContext', () => ({ useOptionalAuth: () => ({ hasRole: () => state.canWrite }) }));
vi.mock('../services/api', () => ({ api: { fileImpact: vi.fn(), deleteFile: vi.fn() } }));
const files = [{ id: 'a', originalName: 'one.json' }, { id: 'b', originalName: 'two.json' }];
const eligible = { canRecycle: true, blockers: [], references: [], restrictedCount: 0, objectKey: 'test/file' };
function shell(child: ReactNode, strict = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  const tree = <ConfigProvider theme={{ token: { motion: false } }}><QueryClientProvider client={client}>{child}</QueryClientProvider></ConfigProvider>;
  render(strict ? <StrictMode>{tree}</StrictMode> : tree);
  return { invalidate };
}
function mount(targets = files, strict = false) {
  const onDeleted = vi.fn();
  return { onDeleted, ...shell(<DeleteFilesButton files={targets} batch onDeleted={onDeleted} />, strict) };
}

describe('Direct file deletion', () => {
  beforeEach(() => { vi.clearAllMocks(); state.canWrite = true; vi.mocked(api.fileImpact).mockReset().mockResolvedValue(eligible as never); vi.mocked(api.deleteFile).mockReset().mockResolvedValue(undefined as never); });
  it('checks and recycles the clicked files without a confirmation dialog', async () => {
    const { onDeleted, invalidate } = mount();
    expect(api.fileImpact).not.toHaveBeenCalled(); expect(api.deleteFile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '删除选中文件' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(['a', 'b']));
    expect(api.deleteFile).toHaveBeenNthCalledWith(1, 'a'); expect(api.deleteFile).toHaveBeenNthCalledWith(2, 'b');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    for (const key of ['files', 'datasets', 'storage', 'warehouse-overview']) expect(invalidate).toHaveBeenCalledWith({ queryKey: [key] });
  });
  it('executes only once in StrictMode and ignores rapid repeat clicks', async () => {
    const { onDeleted } = mount(files, true); const button = screen.getByRole('button', { name: '删除选中文件' });
    fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    expect(api.fileImpact).toHaveBeenCalledTimes(2); expect(api.deleteFile).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('reports partial failures and retries only the remaining files', async () => {
    vi.mocked(api.deleteFile).mockImplementation(async id => { if (id === 'b') throw new Error('文件仍被数据集版本引用'); return undefined as never; });
    const { onDeleted } = mount();
    fireEvent.click(screen.getByRole('button', { name: '删除选中文件' }));
    await waitFor(() => expect(screen.getByText('two.json：文件仍被数据集版本引用')).toBeVisible());
    expect(onDeleted).toHaveBeenCalledWith(['a']);
    vi.mocked(api.deleteFile).mockResolvedValue(undefined as never);
    fireEvent.click(screen.getByRole('button', { name: '重试删除' }));
    await waitFor(() => expect(onDeleted).toHaveBeenLastCalledWith(['b']));
    expect(vi.mocked(api.deleteFile).mock.calls.map(args => args[0])).toEqual(['a', 'b', 'b']);
    expect(vi.mocked(api.fileImpact).mock.calls.map(args => args[0])).toEqual(['a', 'b', 'b']);
  });
  it('hides deletion from read-only users without checking or mutating files', () => {
    state.canWrite = false; mount();
    expect(screen.queryByRole('button', { name: '删除选中文件' })).not.toBeInTheDocument();
    expect(api.fileImpact).not.toHaveBeenCalled(); expect(api.deleteFile).not.toHaveBeenCalled();
  });
  it('retains protected files and shows their actual blocking reason', async () => {
    vi.mocked(api.fileImpact).mockResolvedValue({ canRecycle: false, objectKey: 'test/one.json', blockers: ['仍被历史快照引用'], restrictedCount: 0, references: [{ versionId: 'version', datasetName: '测试集', version: 2, status: 'FROZEN', reason: '历史快照受保护' }] } as never);
    mount([files[0]]); fireEvent.click(screen.getByRole('button', { name: '删除选中文件' }));
    await waitFor(() => expect(screen.getByText('仍被历史快照引用')).toBeVisible());
    expect(screen.getByRole('dialog', { name: '未能删除的文件 · 1 项' })).toBeVisible();
    expect(api.deleteFile).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /确认/ })).not.toBeInTheDocument();
  });
  it('deletes eligible members of a mixed batch and retains referenced members', async () => {
    vi.mocked(api.fileImpact).mockImplementation(async id => ({ ...eligible, canRecycle: id === 'a', blockers: id === 'b' ? ['仍有版本引用'] : [] } as never));
    const { onDeleted } = mount(); fireEvent.click(screen.getByRole('button', { name: '删除选中文件' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(['a']));
    await waitFor(() => expect(screen.getByRole('dialog', { name: '未能删除的文件 · 1 项' })).toBeVisible());
    expect(api.deleteFile).toHaveBeenCalledExactlyOnceWith('a');
  });
  it('never deletes a file when impact loading fails and permits retry after recovery', async () => {
    vi.mocked(api.fileImpact).mockRejectedValueOnce(new Error('影响检查暂不可用'));
    const { onDeleted } = mount([files[0]]); fireEvent.click(screen.getByRole('button', { name: '删除选中文件' }));
    await waitFor(() => expect(screen.getByText('影响检查暂不可用')).toBeVisible()); expect(api.deleteFile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '重试删除' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(['a']));
    expect(api.deleteFile).toHaveBeenCalledExactlyOnceWith('a');
  });
  it('supports the Files page controlled entry after its explicit delete click', async () => {
    const onDeleted = vi.fn();
    function Entry() {
      const [open, setOpen] = useState(false);
      return <><button onClick={() => setOpen(true)}>页面删除</button>{open && <FileDeleteDialog files={[files[0]]} onDeleted={onDeleted} onClose={() => setOpen(false)} />}</>;
    }
    shell(<Entry />); expect(api.deleteFile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '页面删除' }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(['a']));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
