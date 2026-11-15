import { AICompletion, AIMessage, AIProvider, AIStatus, AITool } from './ai-provider';
export declare class MockQwenProvider extends AIProvider {
    private readonly responses;
    readonly model = "mock-qwen";
    readonly calls: {
        messages: AIMessage[];
        tools: AITool[];
    }[];
    constructor(responses?: (AICompletion | Error)[]);
    chat(messages: AIMessage[], tools: AITool[]): Promise<AICompletion>;
    streamChat(messages: AIMessage[], onDelta: (text: string) => void): Promise<AICompletion>;
    healthCheck(): Promise<AIStatus>;
}
