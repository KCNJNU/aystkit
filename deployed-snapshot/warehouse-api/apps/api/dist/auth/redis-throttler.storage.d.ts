import { OnModuleDestroy } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { ThrottlerStorage } from '@nestjs/throttler';
type ThrottlerStorageRecord = {
    totalHits: number;
    timeToExpire: number;
    isBlocked: boolean;
    timeToBlockExpire: number;
};
export declare class RedisThrottlerStorage implements ThrottlerStorage, OnModuleDestroy {
    private readonly config;
    private client?;
    private connecting?;
    constructor(config: ConfigService);
    increment(key: string, ttl: number, limit: number, blockDuration: number, throttlerName: string): Promise<ThrottlerStorageRecord>;
    onModuleDestroy(): Promise<void>;
    acquireAI(userId: string, token: string): Promise<boolean>;
    releaseAI(userId: string, token: string): Promise<void>;
    private connection;
}
export {};
