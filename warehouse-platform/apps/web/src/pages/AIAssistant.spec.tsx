import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AIAssistant } from './AIAssistant';
import { streamChat, warehouseApi } from '../services/warehouse';
vi.mock('../services/warehouse', () => ({ streamChat: vi.fn(), warehouseApi: { aiStatus: vi.fn(), conversations: vi.fn(), history: vi.fn(), clear: vi.fn() } }));
function mount(entry = '/ai') { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={[entry]}><AIAssistant /></MemoryRouter></QueryClientProvider>); }
describe('Warehouse AI interface', () => {
  beforeEach(() => {
    vi.clearAllMocks(); vi.mocked(warehouseApi.aiStatus).mockResolvedValue({ configured: true, model: 'qwen-plus', status: 'healthy', message: '', latency_ms: 0, checked_at: '' }); vi.mocked(warehouseApi.conversations).mockResolvedValue([]);
  });
  it('prefills workspace questions without spending tokens until the user sends', async () => {
    mount('/ai?q=' + encodeURIComponent('有哪些数据集？'));
    await screen.findAllByText('qwen-plus');
    expect(screen.getByLabelText('输入问题')).toHaveValue('有哪些数据集？');
    expect(streamChat).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /追踪最新上传/ }));
    expect(screen.getByLabelText('输入问题')).toHaveValue('最近上传了什么？');
    expect(streamChat).not.toHaveBeenCalled();
  });
  it('shows actual answer sources and supports clearing a conversation', async () => {
    vi.mocked(streamChat).mockImplementation(async (_message, _id, _signal, emit) => { emit({ type: 'conversation', data: { conversation_id: 'c1' } }); emit({ type: 'delta', data: { text: '找到高校数据。' } }); emit({ type: 'done', data: { answer: '找到高校数据。', sources: [{ type: 'dataset', id: 'd1', name: '高校数据', version: 2 }], warehouse_queries: [] } }); });
    vi.mocked(warehouseApi.clear).mockResolvedValue({ success: true }); mount();
    await screen.findAllByText('qwen-plus'); fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '高校数据有哪些？' } }); fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByText('找到高校数据。')).toBeVisible(); expect(screen.getByRole('link', { name: /高校数据/ })).toHaveAttribute('href', '/datasets/d1');
    await waitFor(() => expect(screen.getByRole('button', { name: '清空会话' })).toBeEnabled()); fireEvent.click(screen.getByRole('button', { name: '清空会话' })); await waitFor(() => expect(warehouseApi.clear).toHaveBeenCalledWith('c1'));
  });
  it('disables generation when Qwen is not configured', async () => {
    vi.mocked(warehouseApi.aiStatus).mockResolvedValue({ configured: false, model: 'qwen-plus', status: 'not_configured', message: '', latency_ms: 0, checked_at: '' }); mount(); await screen.findByText('未配置'); fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '数据集' } }); expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
  });
  it('reports provider failure without losing the page', async () => {
    vi.mocked(streamChat).mockRejectedValue(new Error('Qwen 连接失败')); mount(); await screen.findAllByText('qwen-plus'); fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '数据集' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); expect(await screen.findByText('Qwen 连接失败')).toBeVisible(); expect(screen.getByLabelText('输入问题')).toBeVisible();
  });
  it('suggests editable requirements without sending the draft and supports collapsing the controls', async () => {
    mount(); await screen.findAllByText('qwen-plus');
    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '有哪些数据集？' } });
    expect(screen.getByRole('region', { name: '需求补充建议' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '展示数据集概况' }));
    expect(screen.getByLabelText('输入问题')).toHaveValue('有哪些数据集？\n\n补充说明：\n请列出数据集名称、描述、文件数量及最近更新时间。');
    expect(streamChat).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '展示数据集概况' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '收起建议' }));
    expect(screen.queryByRole('button', { name: '表格展示' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '展开建议' }));
    expect(screen.getByRole('button', { name: '表格展示' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '展开输入框' }));
    expect(screen.getByRole('button', { name: '收起输入框' })).toHaveAttribute('aria-expanded', 'true');
    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '' } });
    expect(screen.queryByRole('region', { name: '需求补充建议' })).not.toBeInTheDocument();
  });
  it('waits until Chinese input composition finishes before showing suggestions', async () => {
    mount(); await screen.findAllByText('qwen-plus');
    const input = screen.getByLabelText('输入问题');
    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: '系统状态' } });
    expect(screen.queryByRole('region', { name: '需求补充建议' })).not.toBeInTheDocument();
    fireEvent.compositionEnd(input);
    expect(screen.getByRole('region', { name: '需求补充建议' })).toBeVisible();
    expect(streamChat).not.toHaveBeenCalled();
  });
  it('opens the model configuration, sends effective output settings and resets defaults', async () => {
    vi.mocked(warehouseApi.aiStatus).mockResolvedValue({ configured: true, model: 'qwen-plus', status: 'healthy', message: '', latency_ms: 0, checked_at: '', generation: { temperature: .7, top_p: 1, max_tokens: 1024, max_tokens_limit: 1024 } });
    mount(); await screen.findAllByText('qwen-plus');
    fireEvent.click(screen.getByRole('button', { name: '模型配置' }));
    const setting = screen.getByRole('spinbutton', { name: '回答随机性' });
    fireEvent.change(setting, { target: { value: '0.3' } }); fireEvent.blur(setting);
    fireEvent.change(screen.getByLabelText('输入问题'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledWith('你好', undefined, expect.any(AbortSignal), expect.any(Function), { temperature: .3, top_p: 1, max_tokens: 1024 }));
    await waitFor(() => expect(screen.getByRole('button', { name: '恢复默认参数' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '恢复默认参数' }));
    expect(screen.getByRole('spinbutton', { name: '回答随机性' })).toHaveValue('0.70');
    fireEvent.click(screen.getByRole('button', { name: '收起模型配置' }));
    expect(screen.queryByRole('complementary', { name: '模型输出配置' })).not.toBeInTheDocument();
  });
});
