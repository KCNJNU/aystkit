import { Response } from 'express';
import { AuthUser } from '../auth/auth.types';
import { AIProvider } from './ai-provider';
import { AIChatService } from './ai-chat.service';
export declare class ChatDto {
    message: string;
    conversation_id?: string;
    temperature?: number;
    top_p?: number;
    max_tokens?: number;
}
export declare class AIController {
    private readonly provider;
    private readonly chatService;
    constructor(provider: AIProvider, chatService: AIChatService);
    status(): Promise<{
        generation: {
            temperature: number;
            top_p: number;
            max_tokens: number;
            max_tokens_limit: number;
        };
        configured: boolean;
        provider: string;
        model: string;
        status: "healthy" | "slow" | "not_configured" | "connection_failed" | "unhealthy";
        latency_ms: number;
        checked_at: string;
        message: string;
    }>;
    list(user: AuthUser): import(".prisma/client").Prisma.PrismaPromise<{
        id: string;
        updatedAt: Date;
        title: string;
    }[]>;
    history(user: AuthUser, id: string): Promise<{
        messages: {
            id: string;
            createdAt: Date;
            sources: import("@prisma/client/runtime/library").JsonValue | null;
            role: string;
            content: string;
            conversationId: string;
            queries: import("@prisma/client/runtime/library").JsonValue | null;
        }[];
        id: string;
        createdAt: Date;
        updatedAt: Date;
        title: string;
        userId: string;
        permissionFingerprint: string;
    }>;
    clear(user: AuthUser, id: string): Promise<{
        success: boolean;
    }>;
    chat(user: AuthUser, body: ChatDto): Promise<import("./ai-chat.service").ChatResult>;
    stream(user: AuthUser, body: ChatDto, response: Response): Promise<void>;
}
