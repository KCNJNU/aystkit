import { Inject, Injectable } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { statfs } from 'node:fs/promises';
import { createClient } from 'redis';
import { PrismaService } from '../prisma/prisma.service';
import { OBJECT_STORAGE, ObjectStorage } from '../storage/object-storage';
import { AIProvider } from '../ai/ai-provider';

export type HealthStatus = 'healthy' | 'degraded' | 'unhealthy' | 'not_checked' | 'not_configured' | 'slow' | 'connection_failed';
export type ServiceHealth = { status: HealthStatus; message: string; latency_ms: number; checked_at: string; provider?: string };
export type HealthReport = { status: HealthStatus; timestamp: string; services: Record<string, ServiceHealth>; version: string; environment: string; uptime_seconds: number; disk: { total_bytes: string; available_bytes: string } | null };

@Injectable()
export class HealthService {
  private cached?: HealthReport;
  private pending?: Promise<HealthReport>;
  constructor(private readonly prisma: PrismaService, @Inject(OBJECT_STORAGE) private readonly storage: ObjectStorage, private readonly config: ConfigService, private readonly ai: AIProvider) {}
  check(): Promise<HealthReport> {
    if (this.cached && Date.now() - Date.parse(this.cached.timestamp) < 15000) return Promise.resolve(this.cached);
    this.pending ??= this.probe().then(value => this.cached = value).finally(() => { this.pending = undefined; });
    return this.pending;
  }
  private async probe(): Promise<HealthReport> {
    const [postgresql, redis, objectStorage, qwen, disk] = await Promise.all([
      this.measure(async () => { await this.prisma.$queryRaw`SELECT 1`; }, 'PostgreSQL'),
      this.config.get('REDIS_HOST') ? this.measure(() => this.redis(), 'Redis') : Promise.resolve({ status: 'not_configured' as const, message: 'Redis not configured', latency_ms: 0, checked_at: new Date().toISOString() }),
      this.measure(async () => { const result = await this.storage.checkHealth(); if (!result.reachable) throw new Error(); }, 'Object Storage'),
      this.ai.healthCheck(),
      statfs(process.cwd()).then(value => ({ total_bytes: String(BigInt(value.blocks) * BigInt(value.bsize)), available_bytes: String(BigInt(value.bavail) * BigInt(value.bsize)) })).catch(() => null),
    ]);
    const timestamp = new Date().toISOString();
    const services = { api: { status: 'healthy' as const, message: 'NestJS API responding', latency_ms: 0, checked_at: timestamp }, postgresql, redis, objectStorage, qwen };
    return { status: Object.values(services).every(value => value.status === 'healthy') ? 'healthy' : 'degraded', timestamp, services, version: this.config.get<string>('APP_VERSION') || '0.1.0', environment: this.config.get<string>('DEPLOYMENT_ENVIRONMENT') || 'development', uptime_seconds: Math.floor(process.uptime()), disk };
  }
  private async measure(operation: () => Promise<unknown>, label: string): Promise<ServiceHealth> {
    const start = Date.now(); let timer: NodeJS.Timeout | undefined;
    try {
      await Promise.race([operation(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error()), 3000); })]);
      return { status: Date.now() - start > 1500 ? 'slow' : 'healthy', message: `${label} connected`, latency_ms: Date.now() - start, checked_at: new Date().toISOString() };
    } catch { return { status: 'connection_failed', message: `${label} connection failed or timed out`, latency_ms: Date.now() - start, checked_at: new Date().toISOString() }; }
    finally { clearTimeout(timer); }
  }
  private async redis() {
    const client = createClient({ socket: { host: this.config.getOrThrow<string>('REDIS_HOST'), port: Number(this.config.getOrThrow('REDIS_PORT')), connectTimeout: 2500, reconnectStrategy: false } });
    client.on('error', () => undefined);
    const deadline = setTimeout(() => client.destroy(), 2800);
    try { await client.connect(); await client.ping(); } finally { clearTimeout(deadline); if (client.isOpen) client.destroy(); }
  }
}
