import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../services/api';
import { RelocateFileButton } from './RelocateFileButton';
vi.mock('../auth/AuthContext', () => ({ useOptionalAuth: () => ({ hasRole: () => true }) }));
vi.mock('../services/api', () => ({ api: { fileImpact: vi.fn(), datasetFolders: vi.fn(), relocateFile: vi.fn() } }));
function mount() { render(<ConfigProvider theme={{ token: { motion: false } }}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><RelocateFileButton file={{ id: 'file', originalName: 'old.json' }} /></QueryClientProvider></ConfigProvider>); }
describe('File relocation actions', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.fileImpact).mockResolvedValue({ canRelocate: true, datasetId: 'dataset', folderPath: 'images', objectKey: 'dataset/images/old.json' } as never); vi.mocked(api.datasetFolders).mockResolvedValue([{ path: 'images' }] as never); vi.mocked(api.relocateFile).mockResolvedValue({ id: 'file', objectKey: 'dataset/images/new.json', cleanupPending: false }); });
  it('saves a changed path with one action and no acknowledgement checkbox', async () => {
    mount(); fireEvent.click(screen.getByRole('button', { name: '移动或重命名 old.json' }));
    await screen.findByText('原路径：dataset/images/old.json');
    const save = screen.getByRole('button', { name: '保存路径' }); expect(save).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox', { name: '新文件名称' }), { target: { value: 'new.json' } });
    expect(api.relocateFile).not.toHaveBeenCalled();
    expect(screen.queryByRole('checkbox', { name: '确认更新文件路径及其草稿版本引用' })).not.toBeInTheDocument();
    await waitFor(() => expect(save).toBeEnabled()); fireEvent.click(save);
    await waitFor(() => expect(api.relocateFile).toHaveBeenCalledWith('file', 'new.json', 'images'));
  });
  it('keeps protected references read-only', async () => {
    vi.mocked(api.fileImpact).mockResolvedValue({ canRelocate: false, datasetId: 'dataset', folderPath: '' } as never);
    mount(); fireEvent.click(screen.getByRole('button', { name: '移动或重命名 old.json' }));
    await screen.findByText('文件被受保护或范围外的版本引用，无法整理');
    expect(screen.getByRole('button', { name: '保存路径' })).toBeDisabled();
    expect(api.relocateFile).not.toHaveBeenCalled();
  });
  it('reports a server-side path failure and allows saving the form again', async () => {
    vi.mocked(api.relocateFile).mockRejectedValueOnce(new Error('目标路径已被占用'));
    mount(); fireEvent.click(screen.getByRole('button', { name: '移动或重命名 old.json' }));
    await screen.findByText('原路径：dataset/images/old.json');
    fireEvent.change(screen.getByRole('textbox', { name: '新文件名称' }), { target: { value: 'new.json' } });
    const save=screen.getByRole('button', { name: '保存路径' });
    await waitFor(() => expect(save).toBeEnabled()); fireEvent.click(save);
    await screen.findByText('目标路径已被占用');
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(api.relocateFile).toHaveBeenCalledTimes(2));
  });
  it('ignores repeated save clicks while storage synchronization is pending', async () => {
    let finish!: (value: { id: string; objectKey: string; cleanupPending: boolean }) => void;
    vi.mocked(api.relocateFile).mockReturnValue(new Promise(resolve => { finish=resolve; }));
    mount(); fireEvent.click(screen.getByRole('button', { name: '移动或重命名 old.json' }));
    await screen.findByText('原路径：dataset/images/old.json');
    fireEvent.change(screen.getByRole('textbox', { name: '新文件名称' }), { target: { value: 'new.json' } });
    const save=screen.getByRole('button', { name: '保存路径' });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save); fireEvent.click(save);
    expect(api.relocateFile).toHaveBeenCalledTimes(1);
    finish({ id:'file', objectKey:'dataset/images/new.json', cleanupPending:false });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('resets name and directory for a new file and ignores the old pending form result', async () => {
    const first={id:'file',originalName:'old.json'},second={id:'file-b',originalName:'other.json'};
    vi.mocked(api.fileImpact).mockImplementation(async identity=>({canRelocate:true,datasetId:'dataset',folderPath:identity===first.id?'images':'other',objectKey:'dataset/'+(identity===first.id?'images/old.json':'other/other.json')} as never));
    vi.mocked(api.datasetFolders).mockResolvedValue([{path:'images'},{path:'archive'},{path:'other'}] as never);
    let finishOld!: (value:{id:string;objectKey:string;cleanupPending:boolean})=>void;
    vi.mocked(api.relocateFile).mockReturnValueOnce(new Promise(resolve=>{finishOld=resolve;}));
    const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
    const view=(target:typeof first)=><ConfigProvider theme={{token:{motion:false}}}><QueryClientProvider client={client}><RelocateFileButton file={target}/></QueryClientProvider></ConfigProvider>;
    const rendered=render(view(first));
    fireEvent.click(screen.getByRole('button',{name:'移动或重命名 old.json'}));
    await screen.findByText('原路径：dataset/images/old.json');
    fireEvent.change(screen.getByRole('textbox',{name:'新文件名称'}),{target:{value:'first-new.json'}});
    fireEvent.mouseDown(screen.getByRole('combobox',{name:'目标文件夹'}));
    fireEvent.click(await screen.findByTitle('archive'));
    await waitFor(()=>expect(screen.getByRole('button',{name:'保存路径'})).toBeEnabled());
    fireEvent.click(screen.getByRole('button',{name:'保存路径'}));
    expect(api.relocateFile).toHaveBeenCalledWith(first.id,'first-new.json','archive');
    rendered.rerender(view(second));
    await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button',{name:'移动或重命名 other.json'}));
    await screen.findByText('原路径：dataset/other/other.json');
    await waitFor(()=>expect(screen.getByRole('dialog')).toBeVisible());
    expect(screen.getByRole('textbox',{name:'新文件名称'})).toHaveValue('other.json');
    fireEvent.change(screen.getByRole('textbox',{name:'新文件名称'}),{target:{value:'second-new.json'}});
    finishOld({id:first.id,objectKey:'dataset/archive/first-new.json',cleanupPending:false});
    await waitFor(()=>expect(screen.getByRole('button',{name:'保存路径'})).toBeEnabled());
    await waitFor(()=>expect(client.isFetching()).toBe(0));
    await new Promise(resolve=>setTimeout(resolve,0));
    await waitFor(()=>expect(screen.getByRole('dialog')).toBeVisible());
    expect(screen.getByRole('textbox',{name:'新文件名称'})).toHaveValue('second-new.json');
    expect(api.relocateFile).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button',{name:'保存路径'}));
    await waitFor(()=>expect(api.relocateFile).toHaveBeenNthCalledWith(2,second.id,'second-new.json','other'));
  });

});
