import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { request } from './api';
import { listCaptureSessions, prepareCaptureSessionDownload } from './capture-sessions';

vi.mock('./api', () => ({ request: vi.fn() }));
const session = { sessionId: 'b'.repeat(32), robotId: '365c77c6-0029-4406-a504-739014d0a672' };
const taskId = 'a'.repeat(64);
describe('capture session packages', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); });
  afterEach(() => { vi.useRealTimers(); });

  it('lists one selected robot explicitly and omits robotId for the all view', async () => {
    vi.mocked(request).mockResolvedValue({ sessions: [] });
    const signal = new AbortController().signal;
    await listCaptureSessions('dataset', session.robotId, signal);
    expect(request).toHaveBeenLastCalledWith(`/v1/datasets/dataset/capture-sessions?robotId=${session.robotId}`, { signal });
    await listCaptureSessions('dataset', '', signal);
    expect(request).toHaveBeenLastCalledWith('/v1/datasets/dataset/capture-sessions', { signal });
  });

  it('starts only on demand and keeps task and robot identity across processing polls', async () => {
    vi.mocked(request).mockResolvedValueOnce({ state: 'processing', taskId, retryAfterMs: 100 })
      .mockResolvedValueOnce({ state: 'ready', taskId, url: '/ready', fileName: 'session.tar.zst' });
    const operation = prepareCaptureSessionDownload('dataset', session, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(1999);
    expect(request).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await expect(operation).resolves.toEqual({ url: '/ready', fileName: 'session.tar.zst' });
    const first = new URL(vi.mocked(request).mock.calls[0][0], 'https://test.invalid');
    const second = new URL(vi.mocked(request).mock.calls[1][0], 'https://test.invalid');
    expect(first.searchParams.get('retry')).toBe('true');
    expect(first.searchParams.get('taskId')).toBeNull();
    expect(second.searchParams.get('retry')).toBeNull();
    expect(second.searchParams.get('taskId')).toBe(taskId);
    expect(first.searchParams.get('robotId')).toBe(session.robotId);
    expect(second.searchParams.get('robotId')).toBe(session.robotId);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not restart a failed task until the next user click', async () => {
    vi.mocked(request).mockResolvedValueOnce({ state: 'processing', taskId }).mockRejectedValueOnce(new Error('源文件不可用'));
    const outcome = prepareCaptureSessionDownload('dataset', session, new AbortController().signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await outcome).toMatchObject({ message: '源文件不可用' });
    expect(request).toHaveBeenCalledTimes(2);
    vi.mocked(request).mockResolvedValueOnce({ state: 'ready', taskId: 'c'.repeat(64), url: '/retried', fileName: 'session.tar.zst' });
    await expect(prepareCaptureSessionDownload('dataset', session, new AbortController().signal)).resolves.toMatchObject({ url: '/retried' });
    const retried = new URL(vi.mocked(request).mock.calls[2][0], 'https://test.invalid');
    expect(retried.searchParams.get('retry')).toBe('true');
    expect(retried.searchParams.has('taskId')).toBe(false);
  });

  it('rejects a changed task instead of downloading the wrong snapshot', async () => {
    vi.mocked(request).mockResolvedValueOnce({ state: 'processing', taskId })
      .mockResolvedValueOnce({ state: 'ready', taskId: 'd'.repeat(64), url: '/wrong', fileName: 'session.tar.zst' });
    const outcome = prepareCaptureSessionDownload('dataset', session, new AbortController().signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(2000);
    expect(await outcome).toMatchObject({ message: '压缩包任务标识无效或已改变，请重新点击下载' });
  });

  it('aborts in-flight work and ignores a late ready response', async () => {
    let complete!: (value: unknown) => void;
    vi.mocked(request).mockReturnValueOnce(new Promise(resolve => { complete = resolve; }));
    const controller = new AbortController();
    const outcome = prepareCaptureSessionDownload('dataset', session, controller.signal).catch(error => error);
    const requestSignal = vi.mocked(request).mock.calls[0][1]?.signal;
    controller.abort();
    expect(await outcome).toMatchObject({ name: 'AbortError' });
    expect(requestSignal?.aborted).toBe(true);
    complete({ state: 'ready', taskId, url: '/late', fileName: 'session.tar.zst' });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(request).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
