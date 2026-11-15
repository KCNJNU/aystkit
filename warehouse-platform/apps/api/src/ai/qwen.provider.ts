import { HttpException, Injectable } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { AICompletion, AIMessage, AIProvider, AIStatus, AITool, GenerationOptions, Usage } from './ai-provider';

@Injectable()
export class QwenProvider extends AIProvider {
  private cached?: AIStatus;
  private checking?: Promise<AIStatus>;
  constructor(private readonly config: ConfigService) { super(); }
  get model() { return this.config.get<string>('QWEN_MODEL') || 'qwen-plus'; }
  override get generationSettings() {
    const limit = this.number('QWEN_MAX_TOKENS', 2048, 8192);
    return { temperature: 0.7, top_p: 1, max_tokens: limit, max_tokens_limit: limit };
  }
  private generationBody(options?: GenerationOptions) {
    if (!options) return {};
    return {
      ...(options.temperature !== undefined && { temperature: options.temperature }),
      ...(options.top_p !== undefined && { top_p: options.top_p }),
      ...(options.max_tokens !== undefined && { max_tokens: Math.min(options.max_tokens, this.generationSettings.max_tokens_limit) }),
    };
  }
  private get key() { return this.config.get<string>('QWEN_API_KEY')?.trim(); }
  private endpoint(path: string) {
    const base = this.config.get<string>('QWEN_BASE_URL') || 'https://dashscope.aliyuncs.com/compatible-mode/v1';
    const url = new URL(`${base.replace(/\/$/, '')}/${path}`);
    if (url.protocol !== 'https:') throw new HttpException('Qwen 地址必须使用 HTTPS', 503);
    return url;
  }
  private number(key: string, fallback: number, max: number) {
    const n = Number(this.config.get(key)); return Number.isInteger(n) && n > 0 ? Math.min(n, max) : fallback;
  }
  private fail(status: number): never {
    const message = status === 401 || status === 403 ? 'Qwen 认证失败，请管理员检查后端配置' : status === 429 ? 'Qwen 限流或额度不足，请稍后重试' : status === 400 ? 'Qwen 请求或模型配置不受支持' : 'Qwen 服务暂时不可用';
    throw new HttpException({ code: `QWEN_${status}`, message }, status === 429 ? 429 : 502);
  }
  private async fetchResponse(body: Record<string, unknown>, signal?: AbortSignal) {
    if (!this.key) throw new HttpException({ code: 'QWEN_NOT_CONFIGURED', message: 'Qwen 尚未配置，请管理员设置后端 API Key' }, 503);
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const timeout = AbortSignal.timeout(this.number('QWEN_TIMEOUT', 30000, 120000));
        const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
        const response = await fetch(this.endpoint('chat/completions'), {
          method: 'POST', redirect: 'error', signal: combined,
          headers: { Authorization: `Bearer ${this.key}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: this.model, max_tokens: this.number('QWEN_MAX_TOKENS', 2048, 8192), enable_thinking: false, ...body }),
        });
        if ([500, 502, 503, 504].includes(response.status) && attempt === 0) { await response.body?.cancel(); continue; }
        if (!response.ok) { await response.body?.cancel(); this.fail(response.status); }
        return response;
      } catch (error) {
        if (signal?.aborted) throw new HttpException('生成已停止', 499);
        if (error instanceof HttpException) throw error;
        if (attempt === 1) throw new HttpException({ code: 'QWEN_CONNECTION_FAILED', message: 'Qwen 连接失败或请求超时' }, 502);
      }
    }
    throw new HttpException('Qwen 服务不可用', 502);
  }
  async chat(messages: AIMessage[], tools: AITool[], signal?: AbortSignal, options?: GenerationOptions): Promise<AICompletion> {
    const response = await this.fetchResponse({ messages, ...this.generationBody(options), ...(tools.length ? { tools, tool_choice: 'auto' } : {}) }, signal);
    const data = await response.json().catch(() => null) as { choices?: { message?: AIMessage }[]; usage?: Usage } | null;
    const message = data?.choices?.[0]?.message;
    if (!message || message.role !== 'assistant' || (typeof message.content !== 'string' && !Array.isArray(message.tool_calls))) throw new HttpException('Qwen 返回了无效响应', 502);
    if (message.tool_calls && (!Array.isArray(message.tool_calls) || message.tool_calls.some(c => !c || typeof c.id !== 'string' || c.type !== 'function' || typeof c.function?.name !== 'string' || typeof c.function?.arguments !== 'string'))) throw new HttpException('Qwen 工具响应无效', 502);
    return { message, usage: data?.usage };
  }
  async streamChat(messages: AIMessage[], onDelta: (text: string) => void, signal?: AbortSignal, options?: GenerationOptions): Promise<AICompletion> {
    const response = await this.fetchResponse({ messages, ...this.generationBody(options), stream: true, stream_options: { include_usage: true } }, signal);
    if (!response.body) throw new HttpException('Qwen 流式响应为空', 502);
    const reader = response.body.getReader(); const decoder = new TextDecoder();
    let buffer = ''; let content = ''; let usage: Usage | undefined; let done = false;
    try {
      while (!done) {
        const next = await reader.read(); if (next.done) break;
        buffer += decoder.decode(next.value, { stream: true });
        if (buffer.length > 262144) throw new Error('Oversized stream');
        let boundary: number;
        while ((boundary = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, boundary).trim(); buffer = buffer.slice(boundary + 1);
          if (!line.startsWith('data:')) continue;
          const text = line.slice(5).trim(); if (text === '[DONE]') { done = true; break; }
          const event = JSON.parse(text); if (event.error) throw new Error('Upstream error');
          const delta = event.choices?.[0]?.delta?.content;
          if (typeof delta === 'string') { content += delta; if (content.length > 64000) throw new Error('Oversized answer'); onDelta(delta); }
          if (event.usage) usage = event.usage;
        }
      }
      if (!done || !content) throw new Error('Incomplete stream');
      return { message: { role: 'assistant', content }, usage };
    } catch { throw new HttpException(signal?.aborted ? '生成已停止' : 'Qwen 流式连接中断，请重试', 502); }
    finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
  }
  healthCheck(): Promise<AIStatus> {
    if (this.cached && Date.now() - Date.parse(this.cached.checked_at) < 60000) return Promise.resolve(this.cached);
    this.checking ??= this.probe().then(value => this.cached = value).finally(() => { this.checking = undefined; });
    return this.checking;
  }
  private async probe(): Promise<AIStatus> {
    const start = Date.now();
    const base = { configured: !!this.key, provider: 'qwen', model: this.model, checked_at: new Date().toISOString() };
    if (!this.key) return { ...base, status: 'not_configured', latency_ms: 0, message: '未配置 Qwen API Key' };
    try {
      const response = await fetch(this.endpoint('models'), { redirect: 'error', headers: { Authorization: `Bearer ${this.key}` }, signal: AbortSignal.timeout(5000) });
      const latency_ms = Date.now() - start;
      await response.body?.cancel();
      return { ...base, latency_ms, status: response.ok ? latency_ms > 2000 ? 'slow' : 'healthy' : 'unhealthy', message: response.ok ? '模型目录接口可达；实际对话能力以调用结果为准' : `Qwen 健康检查失败 (HTTP ${response.status})` };
    } catch { return { ...base, latency_ms: Date.now() - start, status: 'connection_failed', message: 'Qwen 连接失败或超时' }; }
  }
}

