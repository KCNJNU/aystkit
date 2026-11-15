import { HealthController } from './health.controller';
describe('Health endpoints', () => {
  it('liveness does not touch dependencies', () => {
    const service = { check: jest.fn() }; expect(new HealthController(service as never).liveness().status).toBe('healthy'); expect(service.check).not.toHaveBeenCalled();
  });
  it('readiness ignores Qwen while requiring database, Redis and storage', async () => {
    const report = { services: { postgresql: { status: 'healthy' }, redis: { status: 'healthy' }, objectStorage: { status: 'healthy' }, qwen: { status: 'not_configured' } }, timestamp: 'now' };
    const controller = new HealthController({ check: jest.fn().mockResolvedValue(report) } as never); const response = { status: jest.fn() };
    expect((await controller.ready(response as never)).status).toBe('healthy'); expect(response.status).toHaveBeenCalledWith(200);
    report.services.postgresql.status = 'connection_failed'; await controller.ready(response as never); expect(response.status).toHaveBeenCalledWith(503);
  });
  it('returns the consolidated report to authenticated system routes', async () => {
    const report = { status: 'healthy' }; expect(await new HealthController({ check: jest.fn().mockResolvedValue(report) } as never).status()).toEqual(report);
  });
});
