import { ConfigService } from '@nestjs/config';
import { AICompletion, AIMessage, AIProvider, AIStatus, AITool, GenerationOptions } from './ai-provider';
export declare class QwenProvider extends AIProvider {
    private readonly config;
    private cached?;
    private checking?;
    constructor(config: ConfigService);
    get model(): string;
    get generationSettings(): {
        temperature: number;
        top_p: number;
        max_tokens: number;
        max_tokens_limit: number;
    };
    private generationBody;
    private get key();
    private endpoint;
    private number;
    private fail;
    private fetchResponse;
    chat(messages: AIMessage[], tools: AITool[], signal?: AbortSignal, options?: GenerationOptions): Promise<AICompletion>;
    streamChat(messages: AIMessage[], onDelta: (text: string) => void, signal?: AbortSignal, options?: GenerationOptions): Promise<AICompletion>;
    healthCheck(): Promise<AIStatus>;
    private probe;
}
