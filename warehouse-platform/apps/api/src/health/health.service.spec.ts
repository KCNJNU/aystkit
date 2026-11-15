import { ConfigService } from '@nestjs/config';
import { createClient } from 'redis';
import { HealthService } from './health.service';
import { MockQwenProvider } from '../ai/mock-qwen.provider';
jest.mock('redis', () => ({ createClient: jest.fn() }));
describe('Warehouse health', () => {
  function setup() {
    const prisma = { $queryRaw: jest.fn().mockResolvedValue([1]) };
    const storage = { checkHealth: jest.fn().mockResolvedValue({ reachable: true }) };
    const redis = { on: jest.fn(), connect: jest.fn().mockResolvedValue(undefined), ping: jest.fn().mockResolvedValue('PONG'), destroy: jest.fn(), isOpen: true };
    (createClient as jest.Mock).mockReturnValue(redis);
    const provider = new MockQwenProvider();
    const service = new HealthService(prisma as never, storage as never, new ConfigService({ REDIS_HOST: 'localhost', REDIS_PORT: '6379' }), provider);
    return { service, prisma, storage, redis, provider };
  }
  it('reports only active dependencies with timings', async () => {
    const { service } = setup(); const result = await service.check();
    expect(Object.keys(result.services)).toEqual(['api', 'postgresql', 'redis', 'objectStorage', 'qwen']);
    expect(result.status).toBe('healthy');
    expect(result.services.redis.latency_ms).toBeGreaterThanOrEqual(0);
  });
  it('reports database connection failure without sensitive errors', async () => {
    const { service, prisma } = setup(); prisma.$queryRaw.mockRejectedValue(new Error('password=secret'));
    const result = await service.check(); expect(result.services.postgresql.status).toBe('connection_failed'); expect(JSON.stringify(result)).not.toContain('secret');
  });
  it('caches status and shares concurrent probes', async () => {
    const { service, prisma } = setup(); await Promise.all([service.check(), service.check()]); await service.check(); expect(prisma.$queryRaw).toHaveBeenCalledTimes(1);
  });
  it('keeps warehouse dependencies healthy when Qwen is unconfigured', async () => {
    const { service, provider } = setup(); jest.spyOn(provider, 'healthCheck').mockResolvedValue({ configured: false, status: 'not_configured', provider: 'qwen', model: 'qwen-plus', message: 'Not configured', checked_at: new Date().toISOString(), latency_ms: 0 });
    const result = await service.check(); expect(result.status).toBe('degraded'); expect(result.services.postgresql.status).toBe('healthy');
  });
});
