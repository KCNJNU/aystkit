import { INestApplication, ValidationPipe } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
// cookie-parser exports a CommonJS function.
// eslint-disable-next-line @typescript-eslint/no-require-imports
import cookieParser = require('cookie-parser');
import { AuthGuard } from '../auth/auth.guard';
import { AuthService } from '../auth/auth.service';
import { AIController } from './ai.controller';
import { WarehouseController } from '../warehouse/warehouse.controller';
import { AIChatService } from './ai-chat.service';
import { AIProvider } from './ai-provider';
import { WarehouseQueryService } from '../warehouse/warehouse-query.service';
import { MockQwenProvider } from './mock-qwen.provider';
import { UploadsController } from '../uploads/uploads.controller';
import { UploadsService } from '../uploads/uploads.service';
describe('Authenticated AI / upload HTTP contract', () => {
  let app: INestApplication; let url: string;
  const chat = { chat: jest.fn(), list: jest.fn(), history: jest.fn(), clear: jest.fn() };
  const uploads = { defaults: jest.fn().mockResolvedValue({ robotId: 'source' }) };
  beforeAll(async () => {
    const module = await Test.createTestingModule({ controllers: [AIController, WarehouseController, UploadsController], providers: [
      { provide: AIProvider, useClass: MockQwenProvider }, { provide: AIChatService, useValue: chat },
      { provide: WarehouseQueryService, useValue: { overview: jest.fn() } }, { provide: UploadsService, useValue: uploads },
      { provide: AuthService, useValue: { resolveSession: async (token: string) => token === 'expired' ? null : ({ user: { id: 'user-1', role: token === 'admin' ? 'ADMIN' : 'VIEWER', projectIds: [], mustChangePassword: false }, sessionId: 'session', csrfTokenHash: 'hash' }), verifyCsrf: (token: string) => token === 'valid-csrf' } },
      { provide: APP_GUARD, useClass: AuthGuard },
    ] }).compile();
    app = module.createNestApplication(); app.setGlobalPrefix('api'); app.use(cookieParser()); app.useGlobalPipes(new ValidationPipe({ transform: true, whitelist: true })); await app.listen(0, '127.0.0.1'); url = await app.getUrl();
  });
  afterAll(async () => { await app?.close(); });
  it('rejects anonymous and expired sessions', async () => {
    for (const cookie of ['', 'cr_session=expired']) expect((await fetch(`${url}/api/ai/status`, { headers: { cookie } })).status).toBe(401);
  });
  it('allows a viewer to read AI status', async () => {
    const response = await fetch(`${url}/api/ai/status`, { headers: { cookie: 'cr_session=viewer' } }); expect(response.status).toBe(200); expect(await response.json()).toMatchObject({ configured: true });
  });
  it('requires CSRF and bounded chat input before provider work', async () => {
    expect((await fetch(`${url}/api/ai/chat`, { method: 'POST', headers: { cookie: 'cr_session=viewer', 'Content-Type': 'application/json' }, body: '{"message":"hi"}' })).status).toBe(403);
    expect((await fetch(`${url}/api/ai/chat`, { method: 'POST', headers: { cookie: 'cr_session=viewer', 'Content-Type': 'application/json', 'x-csrf-token': 'valid-csrf' }, body: JSON.stringify({ message: 'a'.repeat(4001) }) })).status).toBe(400);
    expect(chat.chat).not.toHaveBeenCalled();
  });
  it('preserves upload authorization: viewer denied, administrator allowed', async () => {
    for (const [role, expected] of [['viewer', 403], ['admin', 201]] as const) {
      const response = await fetch(`${url}/api/v1/uploads/defaults`, { method: 'POST', headers: { cookie: `cr_session=${role}`, 'x-csrf-token': 'valid-csrf', 'content-type': 'application/json' }, body: JSON.stringify({ datasetId: '11111111-1111-4111-8111-111111111111' }) }); expect(response.status).toBe(expected);
    }
    expect(uploads.defaults).toHaveBeenCalledWith(expect.objectContaining({ role: 'ADMIN' }), '11111111-1111-4111-8111-111111111111');
  });
  it.each([{ temperature: 2 }, { temperature: null }, { temperature: '1' }, { top_p: 0 }, { top_p: 1.1 }, { max_tokens: 0 }, { max_tokens: 8193 }, { max_tokens: 1.5 }])('rejects invalid output settings %j before model work', async options => {
    const previous = chat.chat.mock.calls.length;
    const response = await fetch(`${url}/api/ai/chat`, { method: 'POST', headers: { cookie: 'cr_session=viewer', 'content-type': 'application/json', 'x-csrf-token': 'valid-csrf' }, body: JSON.stringify({ message: 'hello', ...options }) });
    expect(response.status).toBe(400); expect(chat.chat).toHaveBeenCalledTimes(previous);
  });
  it('emits real SSE contract events with the authenticated user', async () => {
    chat.chat.mockImplementation(async (user, message, _id, emit) => { expect(user.id).toBe('user-1'); emit({ type: 'delta', data: { text: message } }); emit({ type: 'done', data: { answer: message, conversation_id: 'c1', sources: [], warehouse_queries: [] } }); });
    const response = await fetch(`${url}/api/ai/chat/stream`, { method: 'POST', headers: { cookie: 'cr_session=viewer', 'x-csrf-token': 'valid-csrf', 'Content-Type': 'application/json' }, body: JSON.stringify({ message: 'hello', temperature: .3, top_p: .8, max_tokens: 512 }) });
    expect(response.headers.get('content-type')).toContain('text/event-stream'); expect(await response.text()).toContain('"type":"done"');
    expect(chat.chat).toHaveBeenLastCalledWith(expect.anything(), 'hello', undefined, expect.any(Function), expect.any(AbortSignal), expect.objectContaining({ temperature: .3, top_p: .8, max_tokens: 512 }));
  });
});

