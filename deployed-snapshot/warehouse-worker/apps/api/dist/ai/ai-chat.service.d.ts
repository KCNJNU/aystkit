import { Prisma } from '@prisma/client';
import { AuthUser } from '../auth/auth.types';
import { RedisThrottlerStorage } from '../auth/redis-throttler.storage';
import { PrismaService } from '../prisma/prisma.service';
import { AIProvider, GenerationOptions } from './ai-provider';
import { AIToolRegistry } from './ai-tool-registry';
import { Source } from '../warehouse/warehouse-query.service';
type QueryLog = {
    tool: string;
    arguments: Record<string, unknown>;
    duration_ms: number;
    status: string;
};
export type ChatEvent = {
    type: 'conversation' | 'delta' | 'tool' | 'done';
    data: unknown;
};
export type ChatResult = {
    answer: string;
    conversation_id: string;
    sources: Source[];
    warehouse_queries: QueryLog[];
};
export declare class AIChatService {
    private readonly prisma;
    private readonly provider;
    private readonly registry;
    private readonly limiter;
    private readonly logger;
    constructor(prisma: PrismaService, provider: AIProvider, registry: AIToolRegistry, limiter: RedisThrottlerStorage);
    list(user: AuthUser): Prisma.PrismaPromise<{
        id: string;
        updatedAt: Date;
        title: string;
    }[]>;
    history(user: AuthUser, id: string): Promise<{
        messages: {
            id: string;
            createdAt: Date;
            sources: Prisma.JsonValue | null;
            role: string;
            content: string;
            conversationId: string;
            queries: Prisma.JsonValue | null;
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
    private owned;
    private validateSources;
    chat(user: AuthUser, message: string, conversationId?: string, emit?: (event: ChatEvent) => void, cancel?: AbortSignal, options?: GenerationOptions): Promise<ChatResult>;
}
export {};
