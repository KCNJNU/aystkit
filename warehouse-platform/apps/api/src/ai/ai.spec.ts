import { HttpException } from '@nestjs/common';
import { AuthUser } from '../auth/auth.types';
import { AIChatService } from './ai-chat.service';
import { AIToolRegistry } from './ai-tool-registry';
import { MockQwenProvider } from './mock-qwen.provider';
import { WarehouseQueryService, warehouseScope } from '../warehouse/warehouse-query.service';

const user: AuthUser = { id: 'a0000000-0000-4000-8000-000000000001', email: 'test@example.invalid', displayName: 'Viewer', role: 'VIEWER', tenantId: 'a0000000-0000-4000-8000-000000000002', projectIds: ['a0000000-0000-4000-8000-000000000003'], mustChangePassword: false };
const datasetId = 'b0000000-0000-4000-8000-000000000001';
const database = () => ({
  dataset: { findMany: jest.fn().mockResolvedValue([{ id: datasetId, name: '高校数据', description: '测试', versions: [{ version: 2 }], updatedAt: new Date() }]), findFirst: jest.fn().mockResolvedValue(null), count: jest.fn().mockResolvedValue(2) },
  datasetVersion: { findMany: jest.fn().mockResolvedValue([]), findFirst: jest.fn().mockResolvedValue(null), count: jest.fn().mockResolvedValue(3) },
  robotFile: { findMany: jest.fn().mockResolvedValue([]), findFirst: jest.fn().mockResolvedValue(null), count: jest.fn().mockResolvedValue(4), aggregate: jest.fn().mockResolvedValue({ _sum: { fileSize: 2048n } }) },
  aiConversation: { create: jest.fn().mockResolvedValue({ id: datasetId }), findFirst: jest.fn().mockResolvedValue(null), findMany: jest.fn().mockResolvedValue([]), update: jest.fn().mockResolvedValue({}) },
  aiMessage: { create: jest.fn().mockResolvedValue({}), findMany: jest.fn().mockResolvedValue([]), deleteMany: jest.fn() },
  aiUsage: { create: jest.fn().mockResolvedValue({}) },
  $transaction: jest.fn(async (ops: Promise<unknown>[]) => Promise.all(ops)),
});

describe('Read-only warehouse AI boundary', () => {
  let prisma: ReturnType<typeof database>; let registry: AIToolRegistry;
  beforeEach(() => { prisma = database(); registry = new AIToolRegistry(new WarehouseQueryService(prisma as never), { check: jest.fn().mockResolvedValue({ status: 'healthy' }) } as never); });
  it.each(['DELETE', 'execute_sql', '__proto__', 'constructor', 'warehouse_upload'])('rejects unregistered tool %s', async name => {
    await expect(registry.execute(name, '{}', user)).rejects.toThrow(); expect(prisma.dataset.findMany).not.toHaveBeenCalled();
  });
  it.each(['{"limit":21}', '{"limit":0}', '{"limit":"10"}', '{"sql":"SELECT *"}', 'null', '[]', 'invalid'])('validates arguments %s before querying', async raw => {
    await expect(registry.execute('warehouse_list_datasets', raw, user)).rejects.toThrow(); expect(prisma.dataset.findMany).not.toHaveBeenCalled();
  });
  it('inherits tenant/project permissions and caps records, selecting no raw file content', async () => {
    const result = await registry.execute('warehouse_search_datasets', '{"keyword":"高校","limit":10}', user);
    expect(prisma.dataset.findMany).toHaveBeenCalledWith(expect.objectContaining({ where: expect.objectContaining(warehouseScope(user)), take: 10 }));
    expect(result.sources[0]).toEqual(expect.objectContaining({ type: 'dataset', id: datasetId }));
    await registry.execute('warehouse_search_files', '{"keyword":"高校"}', user);
    expect(prisma.robotFile.findMany).toHaveBeenCalledWith(expect.objectContaining({ where: expect.objectContaining({ robot: warehouseScope(user) }), take: 10 }));
    expect(prisma.robotFile.findMany.mock.calls[0][0].select).not.toHaveProperty('objectKey');
  });
  it('does not disclose an inaccessible dataset or its versions', async () => {
    await expect(registry.execute('warehouse_list_versions', JSON.stringify({ dataset_id: datasetId }), user)).rejects.toThrow('未找到');
    expect(prisma.datasetVersion.findMany).not.toHaveBeenCalled();
  });
  it('fails closed without a user or tenant', () => {
    expect(() => warehouseScope(undefined as never)).toThrow();
    expect(warehouseScope({ ...user, tenantId: null })).toMatchObject({ tenantId: '00000000-0000-0000-0000-000000000000' });
  });
  it('limits statistics to accessible resources and serializes byte totals', async () => {
    const result = await registry.execute('warehouse_get_dataset_statistics', '{}', user);
    expect(result.data).toMatchObject({ dataset_count: 2, managed_bytes: '2048' });
    expect(prisma.robotFile.aggregate).toHaveBeenCalledWith(expect.objectContaining({ where: { robot: warehouseScope(user) } }));
  });
});

describe('AI chat orchestration', () => {
  function setup(provider = new MockQwenProvider()) {
    const prisma = database(); const limiter = { increment: jest.fn().mockResolvedValue({ isBlocked: false }), acquireAI: jest.fn().mockResolvedValue(true), releaseAI: jest.fn().mockResolvedValue(undefined) };
    const registry = new AIToolRegistry(new WarehouseQueryService(prisma as never), { check: jest.fn() } as never);
    return { prisma, limiter, provider, service: new AIChatService(prisma as never, provider, registry, limiter as never) };
  }
  it('calls a real warehouse tool, then returns sources and persists actual token usage', async () => {
    const provider = new MockQwenProvider([
      { message: { role: 'assistant', content: null, tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'warehouse_search_datasets', arguments: '{"keyword":"高校"}' } }] }, usage: { prompt_tokens: 10, completion_tokens: 4, total_tokens: 14 } },
      { message: { role: 'assistant', content: '找到了高校数据，最新版本 v2。' }, usage: { prompt_tokens: 20, completion_tokens: 8, total_tokens: 28 } },
    ]);
    const { service, prisma } = setup(provider); const result = await service.chat(user, '数仓有哪些高校数据？');
    expect(result.sources).toHaveLength(1); expect(result.warehouse_queries[0].tool).toBe('warehouse_search_datasets');
    expect(provider.calls[1].messages.some(m => m.role === 'tool' && m.content?.includes('高校数据'))).toBe(true);
    expect(prisma.aiUsage.create).toHaveBeenCalledWith({ data: expect.objectContaining({ totalTokens: 42, requestCount: 2, status: 'succeeded' }) });
  });
  it('refuses to invent internal data without tools', async () => {
    const { service } = setup(new MockQwenProvider([{ message: { role: 'assistant', content: '有一万个数据集。' } }]));
    const result = await service.chat(user, '有多少数据集？'); expect(result.answer).toContain('未获得'); expect(result.answer).not.toContain('一万');
  });
  it('rejects another user conversation before any model call', async () => {
    const { service, provider } = setup(); await expect(service.chat(user, '你好', datasetId)).rejects.toThrow('会话不存在'); expect(provider.calls).toHaveLength(0);
  });
  it('rejects requests over the user quota', async () => {
    const { service, limiter, provider } = setup(); limiter.increment.mockResolvedValue({ isBlocked: true });
    await expect(service.chat(user, '你好')).rejects.toThrow('频繁'); expect(provider.calls).toHaveLength(0);
  });
  it('releases the lock on Qwen failure and does not fabricate token usage', async () => {
    const { service, limiter, prisma } = setup(new MockQwenProvider([new HttpException('Qwen 超时', 502)]));
    await expect(service.chat(user, '数据集')).rejects.toThrow('超时'); expect(limiter.releaseAI).toHaveBeenCalled();
    expect(prisma.aiMessage.create).not.toHaveBeenCalled();
    expect(prisma.aiUsage.create).toHaveBeenCalledWith({ data: expect.objectContaining({ status: 'failed', totalTokens: undefined }) });
  });
  it('rejects history after permission change', async () => {
    const { service, prisma } = setup(); prisma.aiConversation.findFirst.mockResolvedValue({ userId: user.id, permissionFingerprint: 'old' } as never);
    await expect(service.history(user, datasetId)).rejects.toThrow('权限已变更');
  });
  it('cancels before any provider request and releases concurrency lock', async () => {
    const { service, limiter, provider } = setup(); const controller = new AbortController(); controller.abort();
    await expect(service.chat(user, '文件', undefined, undefined, controller.signal)).rejects.toThrow('停止');
    expect(provider.calls).toHaveLength(0); expect(limiter.releaseAI).toHaveBeenCalled();
  });
});

