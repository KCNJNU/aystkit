import type { Response } from 'express';
import { AuthUser } from '../auth/auth.types';
import { ArchiveRecordingsController } from './archive-recordings.controller';

const user = { id: 'user' } as AuthUser;

describe('capture-session package HTTP contract', () => {
  const setup = () => {
    const service = { captureSessions: jest.fn(async () => ({ sessions: [] })),
      captureSessionPackage: jest.fn(async () => ({ state: 'processing', taskId: 'a'.repeat(64), retryAfterMs: 2000 })) };
    const response = { status: jest.fn(), setHeader: jest.fn() };
    const controller = new ArchiveRecordingsController(service as never, {} as never);
    const request = (background?: string, retry?: string, taskId?: string) => controller.captureSessionPackage(
      'dataset', 'session', 'robot', user, background, retry, taskId, response as unknown as Response);
    return { controller, service, response, request };
  };
  it('lists the requested robot and submits background-only immutable snapshot packages', async () => {
    const { controller, service, response, request } = setup();
    await controller.captureSessions('dataset', 'robot', user);
    expect(service.captureSessions).toHaveBeenCalledWith('dataset', 'robot', user);
    await request('true');
    expect(service.captureSessionPackage).toHaveBeenCalledWith('dataset', 'session', 'robot', user, { background: true, retry: false });
    expect(response.status).toHaveBeenCalledWith(202); expect(response.setHeader).toHaveBeenCalledWith('Retry-After', '2');
  });
  it.each([[undefined, undefined, undefined], ['false', undefined, undefined], ['true', 'yes', undefined],
    ['true', 'false', 'a'.repeat(64)], ['true', undefined, 'bad']])('rejects blocking and malformed task options', async (background, retry, taskId) => {
    const { service, request } = setup();
    await expect(request(background, retry, taskId)).rejects.toMatchObject({ status: 400 });
    expect(service.captureSessionPackage).not.toHaveBeenCalled();
  });
});

describe('folder package HTTP contract', () => {
  it('submits and lists the durable batch through its independent service', async () => {
    const summary = { items: [], total: 0, ready: 0, active: 0 };
    const prepared = { list: jest.fn(async () => summary), request: jest.fn(async () => summary) };
    const controller = new ArchiveRecordingsController({} as never, prepared as never);
    await expect(controller.preparedFolderPackages('dataset', user)).resolves.toEqual(summary);
    await expect(controller.prepareFolderPackages('dataset', user)).resolves.toEqual(summary);
    expect(prepared.list).toHaveBeenCalledWith('dataset', user);
    expect(prepared.request).toHaveBeenCalledWith('dataset', user);
  });
  const setup = (result: unknown) => {
    const service = { folderPackage: jest.fn().mockResolvedValue(result) };
    const response = { status: jest.fn(), setHeader: jest.fn() };
    const controller = new ArchiveRecordingsController(service as never, {} as never);
    const request = (background?: string, retry?: string, taskId?: string) => controller.folderPackage('dataset', '原相机前', user,
      background, retry, taskId, response as unknown as Response);
    return { service, response, request };
  };
  it('returns 202 and a retry interval while an asynchronous package is generating', async () => {
    const { request, response, service } = setup({ state: 'processing', retryAfterMs: 2000 });
    expect(await request('true', 'true')).toEqual({ state: 'processing', retryAfterMs: 2000 });
    expect(service.folderPackage).toHaveBeenCalledWith('dataset', '原相机前', user, { background: true, retry: true });
    expect(response.status).toHaveBeenCalledWith(202);
    expect(response.setHeader).toHaveBeenCalledWith('Retry-After', '2');
  });
  it.each([{ state: 'ready', url: '/download', fileName: 'files.tar.zst' }, { url: '/download', fileName: 'files.tar.zst' }])(
    'preserves ready and legacy synchronous download responses', async result => {
      const { request, response } = setup(result);
      expect(await request()).toEqual(result);
      expect(response.status).toHaveBeenCalledWith(200);
      expect(response.setHeader).not.toHaveBeenCalled();
    });
  it.each([['yes', undefined], ['true', '1'], ['true', ['true', 'false'] as never]])('rejects malformed async/retry flags %j %j', async (background, retry) => {
    const { request, service } = setup({});
    await expect(request(background, retry)).rejects.toMatchObject({ status: 400 });
    expect(service.folderPackage).not.toHaveBeenCalled();
  });
  it.each([[undefined, 'false', undefined], ['false', undefined, 'a'.repeat(64)], ['true', 'false', 'a'.repeat(64)],
    ['true', undefined, 'invalid'], ['true', undefined, ['a'.repeat(64)] as never]])(
    'rejects inconsistent async/retry/task flags %j %j %j', async (background, retry, taskId) => {
      const { request, service } = setup({});
      await expect(request(background, retry, taskId)).rejects.toMatchObject({ status: 400 });
      expect(service.folderPackage).not.toHaveBeenCalled();
    });
  it('passes the fixed snapshot task identity through polling', async () => {
    const taskId = 'a'.repeat(64), { request, service } = setup({ state: 'processing', taskId, retryAfterMs: 2000 });
    await request('true', undefined, taskId);
    expect(service.folderPackage).toHaveBeenCalledWith('dataset', '原相机前', user, { background: true, retry: false, taskId });
  });
});
