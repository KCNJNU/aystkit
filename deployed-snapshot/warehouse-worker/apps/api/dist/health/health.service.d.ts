import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../prisma/prisma.service';
import { ObjectStorage } from '../storage/object-storage';
import { AIProvider } from '../ai/ai-provider';
export type HealthStatus = 'healthy' | 'degraded' | 'unhealthy' | 'not_checked' | 'not_configured' | 'slow' | 'connection_failed';
export type ServiceHealth = {
    status: HealthStatus;
    message: string;
    latency_ms: number;
    checked_at: string;
    provider?: string;
};
export type HealthReport = {
    status: HealthStatus;
    timestamp: string;
    services: Record<string, ServiceHealth>;
    version: string;
    environment: string;
    uptime_seconds: number;
    disk: {
        total_bytes: string;
        available_bytes: string;
    } | null;
};
export declare class HealthService {
    private readonly prisma;
    private readonly storage;
    private readonly config;
    private readonly ai;
    private cached?;
    private pending?;
    constructor(prisma: PrismaService, storage: ObjectStorage, config: ConfigService, ai: AIProvider);
    check(): Promise<HealthReport>;
    private probe;
    private measure;
    private redis;
}
