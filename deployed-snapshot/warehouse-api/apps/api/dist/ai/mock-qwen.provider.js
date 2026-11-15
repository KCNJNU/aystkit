"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.MockQwenProvider = void 0;
const ai_provider_1 = require("./ai-provider");
class MockQwenProvider extends ai_provider_1.AIProvider {
    responses;
    model = 'mock-qwen';
    calls = [];
    constructor(responses = []) {
        super();
        this.responses = responses;
    }
    async chat(messages, tools) {
        this.calls.push({ messages: structuredClone(messages), tools });
        const result = this.responses.shift();
        if (result instanceof Error)
            throw result;
        return result ?? { message: { role: 'assistant', content: '根据工具结果回答。' } };
    }
    async streamChat(messages, onDelta) {
        const result = await this.chat(messages, []);
        onDelta(result.message.content ?? '');
        return result;
    }
    async healthCheck() { return { configured: true, provider: 'mock', model: this.model, status: 'healthy', latency_ms: 0, checked_at: new Date().toISOString(), message: 'Mock provider' }; }
}
exports.MockQwenProvider = MockQwenProvider;
//# sourceMappingURL=mock-qwen.provider.js.map