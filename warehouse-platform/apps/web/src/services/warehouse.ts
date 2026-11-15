import { apiBase, ApiError, getCsrfToken, redirectObserverAuth, refreshCsrfToken, request } from './api';
export type GenerationOptions = { temperature: number; max_tokens: number; top_p: number };
export type ServiceStatus = { status: string; message: string; latency_ms: number; checked_at: string };
export type SystemReport = { status: string; timestamp: string; services: Record<string, ServiceStatus>; version: string; environment: string; uptime_seconds: number; disk: { total_bytes: string; available_bytes: string } | null };
export type Source = { type: string; id: string; name: string; version?: number };
export type ChatResult = { answer: string; conversation_id: string; sources: Source[]; warehouse_queries: { tool: string; duration_ms: number }[] };
export type Conversation = { id: string; title: string; updatedAt: string };
export type ChatMessage = { id: string; role: string; content: string; sources?: Source[]; queries?: ChatResult['warehouse_queries'] };
export const warehouseApi = {
  status: () => request<SystemReport>('/system/status'),
  aiStatus: () => request<ServiceStatus & { configured: boolean; model: string; generation?: GenerationOptions & { max_tokens_limit: number } }>('/ai/status'),
  conversations: () => request<Conversation[]>('/ai/conversations'),
  history: (id: string) => request<Conversation & { messages: ChatMessage[] }>(`/ai/conversations/${id}`),
  clear: (id: string) => request(`/ai/conversations/${id}/messages`, { method: 'DELETE' }),
};
export async function streamChat(message: string, conversation_id: string | undefined, signal: AbortSignal, onEvent: (event: { type: string; data: unknown }) => void, options?: GenerationOptions, allowCsrfRetry = true): Promise<void> {
  const response = await fetch(`${apiBase}/api/ai/chat/stream`, { method: 'POST', credentials: 'include', signal, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() ?? '' }, body: JSON.stringify({ message, conversation_id, ...options }) });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const observerRedirect = redirectObserverAuth(response.status, error);
    if (!observerRedirect && response.status === 401) window.dispatchEvent(new Event('auth-expired'));
    if (!observerRedirect && response.status === 403 && allowCsrfRetry && /csrf/i.test(String(error.message))) { await refreshCsrfToken(); return streamChat(message, conversation_id, signal, onEvent, options, false); }
    throw new ApiError(error.message ?? `请求失败 (${response.status})`, response.status);
  }
  if (!response.body) throw new Error('未收到回答');
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; let completed = false;
  try {
    while (true) {
      const chunk = await reader.read(); if (chunk.done) break; buffer += decoder.decode(chunk.value, { stream: true });
      let boundary: number;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
        for (const line of frame.split('\n')) if (line.startsWith('data:')) {
          const event = JSON.parse(line.slice(5));
          if (event.type === 'error') throw new Error(event.data.message);
          if (event.type === 'done') completed = true;
          onEvent(event);
        }
      }
    }
    if (!completed && !signal.aborted) throw new Error('回答连接中断，请重试');
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
}
