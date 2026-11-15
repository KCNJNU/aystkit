export type ToolCall = { id: string; type: 'function'; function: { name: string; arguments: string } };
export type AIMessage = { role: 'system' | 'user' | 'assistant' | 'tool'; content: string | null; tool_calls?: ToolCall[]; tool_call_id?: string };
export type AITool = { type: 'function'; function: { name: string; description: string; parameters: Record<string, unknown> } };
export type Usage = { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number };
export type AICompletion = { message: AIMessage; usage?: Usage };
export type GenerationOptions = { temperature?: number; max_tokens?: number; top_p?: number };
export type AIStatus = { configured: boolean; provider: string; model: string; status: 'healthy' | 'slow' | 'not_configured' | 'connection_failed' | 'unhealthy'; latency_ms: number; checked_at: string; message: string };
export abstract class AIProvider {
  abstract readonly model: string;
  get generationSettings() { return { temperature: 0.7, top_p: 1, max_tokens: 2048, max_tokens_limit: 8192 }; }
  abstract chat(messages: AIMessage[], tools: AITool[], signal?: AbortSignal, options?: GenerationOptions): Promise<AICompletion>;
  abstract streamChat(messages: AIMessage[], onDelta: (text: string) => void, signal?: AbortSignal, options?: GenerationOptions): Promise<AICompletion>;
  abstract healthCheck(): Promise<AIStatus>;
}

