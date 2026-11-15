import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../services/api';
import { FileReferencesButton } from './FileReferencesButton';

const state = vi.hoisted(() => ({ canWrite: true }));
vi.mock('../auth/AuthContext', () => ({ useOptionalAuth: () => ({ hasRole: () => state.canWrite }) }));
vi.mock('../services/api', () => ({ api: { fileReferences: vi.fn(), detachFileReferences: vi.fn() } }));
const file = { id: 'file-a', originalName: '测试.json' };
const references = { fileId: file.id, originalName: file.originalName, total: 2, restrictedCount: 0, references: [
  { versionId: 'draft', datasetId: 'a', datasetName: '测试集', version: 1, status: 'DRAFT' as const, referencedAt: '2026-09-23T00:00:00Z', canDetach: true, reason: null },
  { versionId: 'frozen', datasetId: 'a', datasetName: '测试集', version: 2, status: 'FROZEN' as const, referencedAt: '2026-09-23T00:00:00Z', canDetach: false, reason: '历史快照受保护，无法解除引用' },
] };
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<ConfigProvider theme={{ token: { motion: false } }}><QueryClientProvider client={client}><FileReferencesButton file={file} /></QueryClientProvider></ConfigProvider>);
  return vi.spyOn(client, 'invalidateQueries');
}
describe('File reference actions', () => {
  beforeEach(() => { vi.clearAllMocks(); state.canWrite = true; vi.mocked(api.fileReferences).mockResolvedValue(references); vi.mocked(api.detachFileReferences).mockResolvedValue({ detachedCount: 1, remainingCount: 1 }); });
  it('loads on demand and detaches the selected draft with one action', async () => {
    const invalidate = mount();
    expect(api.fileReferences).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '解除引用 测试.json' }));
    await waitFor(() => expect(screen.getByText('历史快照受保护，无法解除引用')).toBeVisible());
    expect(screen.getByRole('checkbox', { name: '选择 测试集 第 2 版' })).toBeDisabled();
    const submit = screen.getByRole('button', { name: '解除选中引用' });
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 测试集 第 1 版' }));
    expect(submit).toBeEnabled();
    expect(screen.queryByRole('checkbox', { name: /我已了解影响/ })).not.toBeInTheDocument();
    expect(api.detachFileReferences).not.toHaveBeenCalled();
    fireEvent.click(submit);
    await waitFor(() => expect(api.detachFileReferences).toHaveBeenCalledWith(file.id, ['draft']));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['versions'] }));
  });
  it('makes no changes when the selection form is closed', async () => {
    mount(); fireEvent.click(screen.getByRole('button', { name: '解除引用 测试.json' }));
    await screen.findByText('历史快照受保护，无法解除引用');
    fireEvent.click(screen.getByRole('button', { name: /关\s*闭/ }));
    expect(api.detachFileReferences).not.toHaveBeenCalled();
  });
  it('shows a failed mutation and refreshes which versions can be selected', async () => {
    vi.mocked(api.detachFileReferences).mockRejectedValue(new Error('版本刚刚冻结，请刷新'));
    mount(); fireEvent.click(screen.getByRole('button', { name: '解除引用 测试.json' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 测试集 第 1 版' }));
    fireEvent.click(screen.getByRole('button', { name: '解除选中引用' }));
    await screen.findByText('版本刚刚冻结，请刷新');
    await waitFor(() => expect(api.fileReferences).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: '解除选中引用' })).toBeDisabled();
  });
  it('does not expose mutation controls to read-only users', () => {
    state.canWrite = false; mount();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(api.fileReferences).not.toHaveBeenCalled();
  });
  it('ignores repeated submit clicks while a detach request is pending', async () => {
    let finish!: (value: { detachedCount: number; remainingCount: number }) => void;
    vi.mocked(api.detachFileReferences).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    mount(); fireEvent.click(screen.getByRole('button', { name: '解除引用 测试.json' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 测试集 第 1 版' }));
    const submit=screen.getByRole('button', { name: '解除选中引用' });
    fireEvent.click(submit); fireEvent.click(submit);
    expect(api.detachFileReferences).toHaveBeenCalledTimes(1);
    finish({ detachedCount: 1, remainingCount: 1 });
    await waitFor(() => expect(submit).toBeDisabled());
  });

  it('closes an old target form and isolates its pending response from a newly selected file', async () => {
    const other={id:'file-b',originalName:'另一个.json'};
    const otherRefs={...references,fileId:other.id,originalName:other.originalName,total:1,references:[{...references.references[0],versionId:'draft-b',datasetName:'新目标集'}]};
    vi.mocked(api.fileReferences).mockImplementation(async identity=>identity===other.id?otherRefs:references);
    let finishOld!: (value:{detachedCount:number;remainingCount:number})=>void;
    vi.mocked(api.detachFileReferences).mockReturnValueOnce(new Promise(resolve=>{finishOld=resolve;}));
    const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
    const view=(target:typeof file)=><ConfigProvider theme={{token:{motion:false}}}><QueryClientProvider client={client}><FileReferencesButton file={target}/></QueryClientProvider></ConfigProvider>;
    const rendered=render(view(file));
    fireEvent.click(screen.getByRole('button',{name:'解除引用 测试.json'}));
    fireEvent.click(await screen.findByRole('checkbox',{name:'选择 测试集 第 1 版'}));
    fireEvent.click(screen.getByRole('button',{name:'解除选中引用'}));
    expect(api.detachFileReferences).toHaveBeenCalledWith(file.id,['draft']);
    rendered.rerender(view(other));
    await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(api.detachFileReferences).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button',{name:'解除引用 另一个.json'}));
    const newSelection=await screen.findByRole('checkbox',{name:'选择 新目标集 第 1 版'});
    expect(newSelection).not.toBeChecked();
    expect(screen.getByRole('button',{name:'解除选中引用'})).toBeDisabled();
    fireEvent.click(newSelection);
    finishOld({detachedCount:1,remainingCount:1});
    await waitFor(()=>expect(api.fileReferences).toHaveBeenCalledTimes(3));
    await waitFor(()=>expect(screen.getByRole('button',{name:'解除选中引用'})).toBeEnabled());
    expect(newSelection).toBeChecked();expect(screen.getByRole('dialog')).toBeVisible();
    expect(api.detachFileReferences).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button',{name:'解除选中引用'}));
    await waitFor(()=>expect(api.detachFileReferences).toHaveBeenNthCalledWith(2,other.id,['draft-b']));
  });

});
