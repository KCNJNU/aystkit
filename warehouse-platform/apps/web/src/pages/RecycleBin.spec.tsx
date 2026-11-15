import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../services/api';
import { RecycleBin } from './RecycleBin';
const state = vi.hoisted(() => ({ admin: false }));
vi.mock('../auth/AuthContext', () => ({ useOptionalAuth: () => ({ hasRole: () => state.admin }) }));
vi.mock('../services/api', () => ({ api: { recycledFiles: vi.fn(), restoreFile: vi.fn(), purgeFile: vi.fn() } }));
const file = { id: 'file', originalName: 'sample.json', objectKey: 'dataset/sample.json', fileSize: '12', deletedAt: '2026-09-23T00:00:00Z', reviewAt: '2026-10-23T00:00:00Z', purgingAt: null };
function mount() { render(<ConfigProvider theme={{ token: { motion: false } }}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><RecycleBin /></QueryClientProvider></ConfigProvider>); }

describe('Recycle bin direct actions', () => {
  beforeEach(() => { vi.clearAllMocks(); state.admin = false; vi.mocked(api.recycledFiles).mockResolvedValue({ items: [file], total: 1, page: 1, pageSize: 20 } as never); vi.mocked(api.restoreFile).mockResolvedValue({ restored: true }); vi.mocked(api.purgeFile).mockResolvedValue({ purged: true }); });
  it('restores from one click and hides permanent cleanup from operators', async () => {
    mount();
    const restore=await screen.findByRole('button', { name: /^恢\s*复$/ });
    expect(api.restoreFile).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '彻底清理' })).not.toBeInTheDocument();
    fireEvent.click(restore);
    await waitFor(() => expect(api.restoreFile).toHaveBeenCalledWith('file'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByText(/我已确认/)).not.toBeInTheDocument();
    expect(api.purgeFile).not.toHaveBeenCalled();
  });
  it('executes admin cleanup directly and retries a failure without a confirmation form', async () => {
    state.admin = true; vi.mocked(api.purgeFile).mockRejectedValueOnce(new Error('存储暂时不可用'));
    mount(); const purge=await screen.findByRole('button', { name: '彻底清理' });
    expect(api.purgeFile).not.toHaveBeenCalled();fireEvent.click(purge);
    await screen.findByText('sample.json：存储暂时不可用');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    const retry=screen.getByRole('button', { name: '重试未成功的文件' });
    await waitFor(() => expect(retry).toBeEnabled());fireEvent.click(retry);
    await waitFor(() => expect(api.purgeFile).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText('有文件操作失败')).not.toBeInTheDocument());
  });
  it('retries only failed files after a partial batch restore', async () => {
    const second={ ...file, id:'second', originalName:'second.json', objectKey:'dataset/second.json' };
    vi.mocked(api.recycledFiles).mockResolvedValue({ items:[file,second], total:2, page:1, pageSize:20 } as never);
    vi.mocked(api.restoreFile).mockImplementation(async identity => {
      if (identity==='second') throw new Error('目标位置已被占用');
      return { restored:true };
    });
    mount();await screen.findByText('second.json');
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getByRole('button', { name:/恢复选中文件/ }));
    await screen.findByText('second.json：目标位置已被占用');
    await waitFor(() => expect(screen.getByRole('button', { name:'重试未成功的文件' })).toBeEnabled());
    vi.mocked(api.restoreFile).mockResolvedValue({ restored:true });
    fireEvent.click(screen.getByRole('button', { name:'重试未成功的文件' }));
    await waitFor(() => expect(api.restoreFile).toHaveBeenCalledTimes(3));
    expect(vi.mocked(api.restoreFile).mock.calls.map(call=>call[0])).toEqual(['file','second','second']);
  });
  it('ignores repeated action clicks while a request is pending', async () => {
    let finish!: (value:{restored:boolean})=>void;
    vi.mocked(api.restoreFile).mockReturnValue(new Promise(resolve=>{finish=resolve;}));
    mount();const restore=await screen.findByRole('button', { name:/^恢\s*复$/ });
    fireEvent.click(restore);fireEvent.click(restore);
    expect(api.restoreFile).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name:/恢复选中文件/ })).toBeDisabled();
    finish({restored:true});await waitFor(()=>expect(restore).toBeEnabled());
  });
  it('keeps files with ongoing purge work unavailable for restoration', async () => {
    state.admin=true;
    vi.mocked(api.recycledFiles).mockResolvedValue({items:[{...file,purgingAt:'2026-10-30T00:00:00Z'}],total:1,page:1,pageSize:20} as never);
    mount();expect(await screen.findByRole('button', {name:/^恢\s*复$/})).toBeDisabled();
    expect(screen.getByRole('button', {name:'重试清理'})).toBeEnabled();
    expect(api.restoreFile).not.toHaveBeenCalled();expect(api.purgeFile).not.toHaveBeenCalled();
  });
});
