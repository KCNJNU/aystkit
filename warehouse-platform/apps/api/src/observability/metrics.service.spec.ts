import { MetricsService } from './metrics.service';

describe('MetricsService', () => {
  it('keeps HTTP and process metrics scrapeable when warehouse aggregation fails', async () => {
    const prisma = { $queryRaw: jest.fn().mockRejectedValue(new Error('postgresql://user:secret@db')) };
    const metrics = new MetricsService(prisma as never);
    metrics.observeHttp('GET', '/health', 200, 0.01);

    const output = await metrics.render();

    expect(output).toMatch(/platform_http_requests_total\{(?=[^}]*method="GET")(?=[^}]*route="\/health")(?=[^}]*status_code="200")(?=[^}]*service="unknown")[^}]*\} 1/);
    expect(output).toContain('platform_metrics_refresh_success{service="unknown"} 0');
    expect(output).not.toContain('secret');
    metrics.registry.clear();
  });
});
