import { useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckOutlined, CopyOutlined, DownOutlined, UpOutlined, DatabaseOutlined, FileSearchOutlined, LoadingOutlined, MessageOutlined, PlusOutlined, SearchOutlined, ArrowUpOutlined, ArrowLeftOutlined, CloudUploadOutlined, CloseOutlined, SettingOutlined, StopOutlined, ThunderboltOutlined, UserOutlined } from '@ant-design/icons';
import { Alert, Button, ConfigProvider, Input, Space, Tag, Tooltip, Typography } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ChatViewport } from '../components/ChatViewport';
import { appendQuestionHint, questionIntent, type QuestionHint } from '../services/question-hints';
import { AIGenerationSettings } from '../components/AIGenerationSettings';
import { useOptionalAuth } from '../auth/AuthContext';
import { readRoles, writeRoles } from '../auth/permissions';
import './ai-playground.css';
import { streamChat, warehouseApi, type ChatMessage, type ChatResult, type GenerationOptions, type Source } from '../services/warehouse';
import { statusLabels } from '../services/warehouse-status';
export function AIAssistant() {
  const [params] = useSearchParams();
  const auth = useOptionalAuth();
  const [historyOpen, setHistoryOpen] = useState(() => window.matchMedia('(min-width: 1200px)').matches);
  const [settingsOpen, setSettingsOpen] = useState(() => window.matchMedia('(min-width: 1200px)').matches);
  const [generation, setGeneration] = useState<GenerationOptions>();
  const cache = useQueryClient(); const [id, setId] = useState<string>(); const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState(() => (params.get('q') ?? '').slice(0, 4000)); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [activity, setActivity] = useState('');
  const [historySearch, setHistorySearch] = useState(''); const [copied, setCopied] = useState<string>();
  const [scrollReset, setScrollReset] = useState(0);
  const [expandedInput, setExpandedInput] = useState(false);
  const [hintsCollapsed, setHintsCollapsed] = useState(false);
  const [composing, setComposing] = useState(false);
  const intent = !composing && !busy ? questionIntent(text) : undefined;
  const addHint = (hint: QuestionHint) => setText(previous => appendQuestionHint(previous, hint));
  const abort = useRef<AbortController | null>(null);
  const status = useQuery({ queryKey: ['ai-status'], queryFn: warehouseApi.aiStatus, refetchInterval: 60000 });
  const conversations = useQuery({ queryKey: ['ai-conversations'], queryFn: warehouseApi.conversations });
  const defaults = status.data?.generation ?? { temperature: .7, top_p: 1, max_tokens: 2048, max_tokens_limit: 2048 };
  const parameters = { ...(generation ?? defaults), max_tokens: Math.min(generation?.max_tokens ?? defaults.max_tokens, defaults.max_tokens_limit) };
  const requestOptions = { temperature: parameters.temperature, top_p: parameters.top_p, max_tokens: parameters.max_tokens };
  const togglePanel = (panel: 'history' | 'settings') => {
    if (panel === 'history') { setHistoryOpen(previous => !previous); if (!window.matchMedia('(min-width: 1200px)').matches) setSettingsOpen(false); }
    else { setSettingsOpen(previous => !previous); if (!window.matchMedia('(min-width: 1200px)').matches) setHistoryOpen(false); }
  };
  useEffect(() => {
    const media = window.matchMedia('(min-width: 1200px)');
    const resize = () => { if (!media.matches) { setHistoryOpen(false); setSettingsOpen(false); } };
    media.addEventListener('change', resize); return () => media.removeEventListener('change', resize);
  }, []);
  useEffect(() => () => abort.current?.abort(), []);
  useEffect(() => { if (!copied) return; const timer = window.setTimeout(() => setCopied(undefined), 2000); return () => window.clearTimeout(timer); }, [copied]);
  async function open(value: string) {
    setError(''); setBusy(true);
    try { const history = await warehouseApi.history(value); setId(value); setMessages(history.messages); setScrollReset(previous => previous + 1); if (!window.matchMedia('(min-width: 1200px)').matches) setHistoryOpen(false); } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  async function send() {
    if (!text.trim() || busy) return;
    const message = text.trim(); setScrollReset(previous => previous + 1); setText(''); setBusy(true); setError(''); setActivity('正在查询…');
    const assistantId = `assistant-${Date.now()}`;
    setMessages(previous => [...previous, { id: `user-${Date.now()}`, role: 'user', content: message }, { id: assistantId, role: 'assistant', content: '' }]);
    const controller = new AbortController(); abort.current = controller;
    try {
      await streamChat(message, id, controller.signal, event => {
        if (event.type === 'conversation') setId((event.data as { conversation_id: string }).conversation_id);
        if (event.type === 'tool') setActivity('正在读取相关数据…');
        if (event.type === 'delta') setMessages(previous => previous.map(item => item.id === assistantId ? { ...item, content: item.content + (event.data as { text: string }).text } : item));
        if (event.type === 'done') { const result = event.data as ChatResult; setMessages(previous => previous.map(item => item.id === assistantId ? { ...item, content: result.answer, sources: result.sources, queries: result.warehouse_queries } : item)); }
      }, requestOptions);
    } catch (err) { setError(controller.signal.aborted ? '生成已停止，未完成的回答不会保存。' : (err as Error).message); }
    finally { setBusy(false); setActivity(''); abort.current = null; void cache.invalidateQueries({ queryKey: ['ai-conversations'] }); }
  }
  async function clear() { if (!id) { setMessages([]); return; } try { await warehouseApi.clear(id); setMessages([]); setError(''); } catch (err) { setError((err as Error).message); } }
  async function copy(item: ChatMessage) { try { await navigator.clipboard.writeText(item.content); setCopied(item.id); } catch { setError('暂时无法复制，请选中回答手动复制。'); } }
  const prompts = [{ icon: <DatabaseOutlined />, title: '发现数据资产', text: '数仓有哪些数据集？' }, { icon: <FileSearchOutlined />, title: '追踪最新上传', text: '最近上传了什么？' }, { icon: <ThunderboltOutlined />, title: '检查系统状态', text: '系统现在正常吗？' }];
  return <ConfigProvider theme={{ token: { colorPrimary: '#6856f5', colorInfo: '#6856f5', borderRadius: 8 }, components: { Slider: { trackBg: '#9185fa', trackHoverBg: '#6856f5', handleColor: '#6856f5' } } }}>
    <div className={`ai-workspace ai-playground ${historyOpen ? 'has-history' : ''} ${settingsOpen ? 'has-settings' : ''}`}>
      <header className="playground-topbar">
        <div className="playground-title"><Link to={auth?.hasRole(...readRoles) ? "/datasets" : "/system-status"} aria-label="返回数仓"><ArrowLeftOutlined /> 返回</Link><span className="playground-divider" /><h1>千问助手</h1></div>
        <Space size={6} wrap><Tag color={status.data?.status === 'healthy' ? 'green' : 'default'}>{status.isError ? '状态不可用' : statusLabels[status.data?.status ?? 'not_checked']}</Tag>
          <Button type="text" icon={<MessageOutlined aria-hidden />} aria-expanded={historyOpen} aria-controls="ai-history-panel" onClick={() => togglePanel('history')}>聊天记录</Button>
          <Button type="text" icon={<SettingOutlined aria-hidden />} aria-expanded={settingsOpen} aria-controls="ai-settings-panel" onClick={() => togglePanel('settings')}>模型配置</Button>
        </Space>
      </header>
      <div className="playground-body">
        {(historyOpen || settingsOpen) && <button className="playground-backdrop" aria-label="关闭侧边面板" onClick={() => { setHistoryOpen(false); setSettingsOpen(false); }} />}
        {historyOpen && <aside className="ai-history playground-history" id="ai-history-panel" aria-label="会话记录侧栏">
          <div className="playground-panel-heading"><strong><MessageOutlined /> 聊天记录</strong><Button type="text" aria-label="收起聊天记录" icon={<CloseOutlined />} onClick={() => setHistoryOpen(false)} /></div>
          <Button type="primary" block icon={<PlusOutlined aria-hidden />} disabled={busy} onClick={() => { setId(undefined); setMessages([]); setText(''); setError(''); setHintsCollapsed(false); setScrollReset(previous => previous + 1); }}>新建会话</Button>
          <Input prefix={<SearchOutlined />} aria-label="搜索对话" placeholder="搜索对话记录" value={historySearch} onChange={event => setHistorySearch(event.target.value)} />
          {conversations.isError && <Alert type="error" message="会话加载失败" />}
          <div className="ai-history-list">{(conversations.data ?? []).filter(item => item.title.toLowerCase().includes(historySearch.toLowerCase())).map(item => <button type="button" className={`ai-history-item ${id === item.id ? 'is-active' : ''}`} key={item.id} disabled={busy} onClick={() => void open(item.id)} title={item.title}><MessageOutlined /><span>{item.title}</span></button>)}{!conversations.data?.length && <p className="history-empty">暂无对话记录</p>}</div>
        </aside>}
        <section className="ai-chat-panel playground-chat" aria-label="模型对话">
          <div className="playground-chat-tools"><span>{busy ? '正在生成回答…' : '当前对话'}</span><Button type="text" size="small" disabled={busy || !messages.length} onClick={() => void clear()}>清空会话</Button></div>
          <ChatViewport revision={messages} resetKey={scrollReset}>
            {messages.length === 0 && <div className="ai-welcome playground-welcome">
              <div className="playground-model-card"><div className="model-card-heading"><span className="model-monogram"><ThunderboltOutlined /></span><div><strong>{status.data?.model ?? '正在读取模型…'}</strong></div><span className="model-card-badge">{status.data?.configured ? '已配置' : '待配置'}</span></div>
              </div>
            </div>}
            {messages.map(item => <article className={`ai-message ai-message-${item.role}`} key={item.id}>
              <div className="ai-message-author"><span className="message-avatar">{item.role === 'user' ? <UserOutlined /> : <ThunderboltOutlined />}</span><strong>{item.role === 'user' ? '你' : 'AI 助手'}</strong></div>
              <div className="ai-message-content">{item.content || (busy ? <span className="thinking-indicator"><i /><i /><i />{activity}</span> : '回答未完成')}</div>
              {!!item.sources?.length && <div className="ai-sources"><Typography.Text type="secondary">数据来源</Typography.Text><Space wrap>{item.sources.map(source => <SourceLink source={source} key={`${source.type}:${source.id}`} />)}</Space></div>}
              {item.role === 'assistant' && item.content && <div className="ai-answer-footer"><Typography.Text type="secondary">{item.sources?.length ? `${item.sources.length} 个可追溯来源` : '未引用数仓数据'}</Typography.Text><Tooltip title={copied === item.id ? '已复制' : '复制回答'}><Button aria-label={copied === item.id ? '已复制' : '复制回答'} type="text" size="small" icon={copied === item.id ? <CheckOutlined /> : <CopyOutlined />} onClick={() => void copy(item)} /></Tooltip></div>}
            </article>)}
          </ChatViewport>
          <div className="ai-composer-wrap">{error && <Alert showIcon type="warning" message={error} closable onClose={() => setError('')} />}
            {intent && <section className="ai-intent-panel" aria-label="需求补充建议"><div className="ai-intent-heading"><span><ThunderboltOutlined /> 需求补充建议</span><Button type="text" size="small" icon={hintsCollapsed ? <DownOutlined aria-hidden /> : <UpOutlined aria-hidden />} aria-expanded={!hintsCollapsed} aria-controls="ai-intent-details" onClick={() => setHintsCollapsed(previous => !previous)}>{hintsCollapsed ? '展开建议' : '收起建议'}</Button></div>{!hintsCollapsed && <div id="ai-intent-details"><div className="ai-intent-options">{intent.hints.map(hint => <Tooltip key={hint.label} title={hint.detail}><Button size="small" icon={<PlusOutlined aria-hidden />} disabled={appendQuestionHint(text, hint) === text} onClick={() => addHint(hint)}>{hint.label}</Button></Tooltip>)}</div></div>}</section>}
            <div className="ai-composer"><div className="ai-input-heading"><label htmlFor="ai-question-input">输入你的问题</label><Button type="text" size="small" icon={expandedInput ? <DownOutlined aria-hidden /> : <UpOutlined aria-hidden />} aria-expanded={expandedInput} aria-controls="ai-question-input" onClick={() => setExpandedInput(previous => !previous)}>{expandedInput ? '收起输入框' : '展开输入框'}</Button></div>
              <Input.TextArea id="ai-question-input" aria-label="输入问题" value={text} onChange={event => setText(event.target.value)} maxLength={4000} autoSize={expandedInput ? { minRows: 6, maxRows: 10 } : { minRows: 2, maxRows: 5 }} onCompositionStart={() => setComposing(true)} onCompositionEnd={() => setComposing(false)} placeholder="例如：最近上传了哪些文件？请按数据集汇总。" onKeyDown={event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.nativeEvent.isComposing && status.data?.configured) { event.preventDefault(); void send(); } }} />
              <div className="ai-composer-actions"><Space size={6} wrap>{auth?.hasRole(...writeRoles) && <Link to="/upload" className="playground-upload"><CloudUploadOutlined /> 上传数据</Link>}<span className="composer-hint">{busy ? <><LoadingOutlined /> {activity}</> : <><DatabaseOutlined /> 只读查询</>}</span></Space><Space><small className="composer-counter">{text.length} / 4000</small>{busy ? <Button aria-label="停止生成" icon={<StopOutlined />} onClick={() => abort.current?.abort()}>停止生成</Button> : <Tooltip title="发送问题"><Button aria-label="发送" shape="circle" icon={<ArrowUpOutlined />} type="primary" disabled={!text.trim() || !status.data?.configured} onClick={() => void send()} /></Tooltip>}</Space></div>
            </div>
            {!messages.length && <div className="playground-examples"><div className="ai-prompt-grid">{prompts.map(prompt => <button type="button" key={prompt.title} disabled={busy} onClick={() => setText(prompt.text)}>{prompt.icon}<strong>{prompt.title}</strong></button>)}</div></div>}
            
          </div>
        </section>
        {settingsOpen && <aside className="playground-settings" id="ai-settings-panel" aria-label="模型输出配置"><div className="playground-panel-heading"><strong>模型与输出</strong><Button type="text" aria-label="收起模型配置" icon={<CloseOutlined />} onClick={() => setSettingsOpen(false)} /></div>
          <AIGenerationSettings model={status.data?.model} value={requestOptions} limit={defaults.max_tokens_limit} disabled={busy || !status.data?.configured} onChange={setGeneration} onReset={() => setGeneration(undefined)} />
        </aside>}
      </div>
    </div>
  </ConfigProvider>;
}

function SourceLink({ source }: { source: Source }) {
  const url = source.type === 'dataset' ? `/datasets/${source.id}` : source.type === 'file' ? `/files?file=${source.id}` : source.type === 'version' ? '/dataset-versions' : source.type === 'system' ? '/system-status' : '/system-status';
  return <Link to={url}><Tag>{source.name}{source.version ? ` · v${source.version}` : ''}</Tag></Link>;
}
