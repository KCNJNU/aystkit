import { ConfigService } from '@nestjs/config';
import { QwenProvider } from './qwen.provider';
describe('Qwen transport', () => {
  const originalFetch = global.fetch;
  afterEach(() => { global.fetch = originalFetch; });
  const provider = (key = 'test-secret-never-returned') => new QwenProvider(new ConfigService({ QWEN_API_KEY: key, QWEN_MODEL: 'qwen-plus' }));
  it('reports not_configured without network traffic or leaking the key', async () => {
    global.fetch = jest.fn(); expect(await provider('').healthCheck()).toMatchObject({ status: 'not_configured' }); expect(fetch).not.toHaveBeenCalled();
  });
  it.each([401, 403, 429, 400])('does not retry HTTP %i', async status => {
    global.fetch = jest.fn().mockResolvedValue(new Response('secret upstream body', { status }));
    await expect(provider().chat([{ role: 'user', content: 'hello' }], [])).rejects.toThrow(/Qwen/); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('retries a transient network error only once', async () => {
    global.fetch = jest.fn().mockRejectedValue(new TypeError('network'));
    await expect(provider().chat([{ role: 'user', content: 'hello' }], [])).rejects.toThrow('连接失败'); expect(fetch).toHaveBeenCalledTimes(2);
  });
  it('validates malformed upstream responses', async () => {
    global.fetch = jest.fn().mockResolvedValue(Response.json({ choices: [] }));
    await expect(provider().chat([], [])).rejects.toThrow('无效响应');
  });
  it('streams actual SSE deltas and usage', async () => {
    const body = 'data: {"choices":[{"delta":{"content":"你好"}}]}\n\ndata: {"choices":[],"usage":{"total_tokens":12}}\n\ndata: [DONE]\n\n';
    global.fetch = jest.fn().mockResolvedValue(new Response(body)); const onDelta = jest.fn();
    const result = await provider().streamChat([], onDelta); expect(onDelta).toHaveBeenCalledWith('你好'); expect(result.usage?.total_tokens).toBe(12);
  });
  it('caches read-only health probes and redacts provider response bodies', async () => {
    global.fetch = jest.fn().mockResolvedValue(new Response('sensitive', { status: 401 })); const value = provider();
    const status = await value.healthCheck(); await value.healthCheck();
    expect(fetch).toHaveBeenCalledTimes(1); expect(JSON.stringify(status)).not.toContain('secret'); expect(JSON.stringify(status)).not.toContain('sensitive');
  });
  it('forwards per-request output parameters while enforcing the administrator token cap', async () => {
    global.fetch = jest.fn().mockImplementation(async (_url, init) => {
      const body = JSON.parse(init.body);
      expect(body).toMatchObject({ model: 'qwen-plus', temperature: 0, top_p: .65, max_tokens: 2048 });
      return Response.json({ choices: [{ message: { role: 'assistant', content: 'hello' } }] });
    });
    const value = provider();
    await value.chat([], [], undefined, { temperature: 0, top_p: .65, max_tokens: 8192 });
    expect(value.generationSettings).toMatchObject({ max_tokens: 2048, max_tokens_limit: 2048 });
  });
  it('uses output settings in streaming requests as well', async () => {
    global.fetch = jest.fn().mockImplementation(async (_url, init) => {
      expect(JSON.parse(init.body)).toMatchObject({ stream: true, temperature: 1.2, top_p: 1, max_tokens: 512 });
      return new Response('data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n');
    });
    await provider().streamChat([], jest.fn(), undefined, { temperature: 1.2, top_p: 1, max_tokens: 512 });
  });
});

