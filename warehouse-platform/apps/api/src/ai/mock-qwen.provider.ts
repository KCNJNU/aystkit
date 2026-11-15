import { AICompletion, AIMessage, AIProvider, AIStatus, AITool } from './ai-provider';
// Test dependency only: production ProviderModule always binds QwenProvider.
export class MockQwenProvider extends AIProvider {
  readonly model = 'mock-qwen';
  readonly calls: { messages: AIMessage[]; tools: AITool[] }[] = [];
  constructor(private readonly responses: (AICompletion | Error)[] = []) { super(); }
  async chat(messages: AIMessage[], tools: AITool[]): Promise<AICompletion> {
    this.calls.push({ messages: structuredClone(messages), tools });
    const result = this.responses.shift(); if (result instanceof Error) throw result;
    return result ?? { message: { role: 'assistant', content: '根据工具结果回答。' } };
  }
  async streamChat(messages: AIMessage[], onDelta: (text: string) => void) {
    const result = await this.chat(messages, []); onDelta(result.message.content ?? ''); return result;
  }
  async healthCheck(): Promise<AIStatus> { return { configured: true, provider: 'mock', model: this.model, status: 'healthy', latency_ms: 0, checked_at: new Date().toISOString(), message: 'Mock provider' }; }
}

