import { Injectable } from '@nestjs/common';
import { Prisma } from '@prisma/client';
import { collectDefaultMetrics, Counter, Gauge, Histogram, Registry } from 'prom-client';
import { PrismaService } from '../prisma/prisma.service';

@Injectable()
export class MetricsService {
  readonly registry = new Registry();
  readonly httpRequests = new Counter({ name: 'platform_http_requests_total', help: 'HTTP requests processed', labelNames: ['method', 'route', 'status_code'], registers: [this.registry] });
  readonly httpDuration = new Histogram({ name: 'platform_http_request_duration_seconds', help: 'HTTP request latency', labelNames: ['method', 'route', 'status_code'], buckets: [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5], registers: [this.registry] });
  readonly jobBacklog = new Gauge({ name: 'platform_job_backlog', help: 'Queued and running PostgreSQL jobs', labelNames: ['status'], registers: [this.registry] });
  readonly refreshSuccessful = new Gauge({ name: 'platform_metrics_refresh_success', help: 'Whether the latest database-backed metric refresh succeeded', registers: [this.registry] });

  constructor(private readonly prisma: PrismaService) {
    this.registry.setDefaultLabels({ service: process.env.RUNTIME_SERVICE_NAME ?? 'unknown' });
    collectDefaultMetrics({ register: this.registry, prefix: 'platform_process_' });
  }

  observeHttp(method: string, route: string, statusCode: number, seconds: number) {
    const labels = { method, route, status_code: String(statusCode) };
    this.httpRequests.inc(labels);
    this.httpDuration.observe(labels, seconds);
  }

  async render() {
    try { await this.refreshPlatformGauges(); this.refreshSuccessful.set(1); }
    catch { this.refreshSuccessful.set(0); }
    return this.registry.metrics();
  }

  private async refreshPlatformGauges() {
    const jobs = await this.prisma.$queryRaw<Array<{ status: string; count: bigint }>>(Prisma.sql`SELECT status, COUNT(*) AS count FROM "Job" WHERE status IN ('QUEUED', 'RUNNING') GROUP BY status`);
    this.jobBacklog.reset();
    jobs.forEach((item) => this.jobBacklog.set({ status: item.status }, Number(item.count)));
  }
}
