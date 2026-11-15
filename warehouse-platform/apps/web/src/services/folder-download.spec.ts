import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import { prepareFolderDownload } from './folder-download';

vi.mock('./api', () => ({ api: { folderPackage: vi.fn() } }));
const taskId = 'a'.repeat(64);

describe('folder package preparation', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); });
  afterEach(() => { vi.useRealTimers(); });

  it('polls the same task with short requests, honours a 2 second minimum, and retries only on the user click', async () => {
    vi.mocked(api.folderPackage).mockResolvedValueOnce({ state: 'processing', taskId, retryAfterMs: 100 })
      .mockResolvedValueOnce({ state: 'processing', taskId, retryAfterMs: 5000 })
      .mockResolvedValueOnce({ state: 'ready', taskId, url: '/ready', fileName: 'camera.tar.zst' });
    const operation = prepareFolderDownload('dataset', '原相机前/时段', new AbortController().signal);
    await vi.advanceTimersByTimeAsync(1999);
    expect(api.folderPackage).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.folderPackage).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(4999);
    expect(api.folderPackage).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    await expect(operation).resolves.toEqual({ url: '/ready', fileName: 'camera.tar.zst' });
    expect(api.folderPackage).toHaveBeenNthCalledWith(1, 'dataset', '原相机前/时段', expect.objectContaining({ async: true, retry: true }));
    for (const call of vi.mocked(api.folderPackage).mock.calls.slice(1)) {
      expect(call.slice(0, 2)).toEqual(['dataset', '原相机前/时段']);
      expect(call[2]?.async).toBe(true);
      expect(call[2]?.retry).toBeUndefined();
      expect(call[2]?.taskId).toBe(taskId);
    }
    expect(vi.getTimerCount()).toBe(0);
  });

  it('accepts an immediately ready response from an older server', async () => {
    vi.mocked(api.folderPackage).mockResolvedValue({ url: '/ready', fileName: 'camera.tar.zst' });
    await expect(prepareFolderDownload('dataset', 'camera', new AbortController().signal)).resolves.toEqual({ url: '/ready', fileName: 'camera.tar.zst' });
    expect(api.folderPackage).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps polling the original snapshot while new files are uploaded to the folder', async () => {
    let uploadFinished = false;
    vi.mocked(api.folderPackage).mockImplementation(async (_id, _path, options) => {
      if (!options?.taskId) return { state: 'processing', taskId: uploadFinished ? 'b'.repeat(64) : taskId, retryAfterMs: 2000 };
      expect(options.taskId).toBe(taskId);
      return { state: 'ready', taskId, url: '/snapshot-before-new-upload', fileName: 'camera.tar.zst' };
    });
    const operation = prepareFolderDownload('dataset', 'camera', new AbortController().signal);
    await vi.advanceTimersByTimeAsync(1000);
    uploadFinished = true;
    await vi.advanceTimersByTimeAsync(1000);
    await expect(operation).resolves.toEqual({ url: '/snapshot-before-new-upload', fileName: 'camera.tar.zst' });
    expect(api.folderPackage).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.folderPackage).mock.calls[1][2]?.retry).toBeUndefined();
  });

  it('rejects missing or changed task identities rather than polling a different snapshot', async () => {
    vi.mocked(api.folderPackage).mockResolvedValueOnce({ state: 'processing', taskId: '' });
    await expect(prepareFolderDownload('dataset', 'camera', new AbortController().signal)).rejects.toThrow('压缩包任务标识无效或已改变');
    vi.mocked(api.folderPackage).mockResolvedValueOnce({ state: 'processing', taskId })
      .mockResolvedValueOnce({ state: 'processing', taskId: 'b'.repeat(64) });
    const outcome = prepareFolderDownload('dataset', 'camera', new AbortController().signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(2000);
    expect(await outcome).toMatchObject({ message: '压缩包任务标识无效或已改变，请重新点击下载' });
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops polling when the page leaves without issuing a server cancellation', async () => {
    vi.mocked(api.folderPackage).mockResolvedValue({ state: 'processing', taskId, retryAfterMs: 2000 });
    const controller = new AbortController();
    const outcome = prepareFolderDownload('dataset', 'camera', controller.signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(1);
    controller.abort();
    expect(await outcome).toMatchObject({ name: 'AbortError' });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.folderPackage).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('aborts an in-flight progress request and ignores its late success', async () => {
    let complete!: (value: { url: string; fileName: string }) => void;
    vi.mocked(api.folderPackage).mockReturnValue(new Promise(resolve => { complete = resolve; }));
    const controller = new AbortController();
    const outcome = prepareFolderDownload('dataset', 'camera', controller.signal).catch(error => error);
    const requestSignal = vi.mocked(api.folderPackage).mock.calls[0][2]?.signal;
    controller.abort();
    expect(await outcome).toMatchObject({ name: 'AbortError' });
    expect(requestSignal?.aborted).toBe(true);
    complete({ url: '/late', fileName: 'camera.tar.zst' });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.folderPackage).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps server failures visible without silently starting another task', async () => {
    vi.mocked(api.folderPackage).mockResolvedValueOnce({ state: 'processing', taskId }).mockRejectedValueOnce(new Error('源文件不存在，无法打包'));
    const outcome = prepareFolderDownload('dataset', 'camera', new AbortController().signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(2000);
    expect(await outcome).toMatchObject({ message: '源文件不存在，无法打包' });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.folderPackage).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('bounds an unresponsive progress request and explains the background task may continue', async () => {
    vi.mocked(api.folderPackage).mockReturnValue(new Promise(() => undefined));
    const outcome = prepareFolderDownload('dataset', 'camera', new AbortController().signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(await outcome).toMatchObject({ message: '查询打包进度超时，后台任务可能仍在继续，请稍后重试' });
    expect(vi.mocked(api.folderPackage).mock.calls[0][2]?.signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('recovers a timed out poll by querying the same task without restarting packaging', async () => {
    vi.mocked(api.folderPackage).mockResolvedValueOnce({ state: 'processing', taskId })
      .mockReturnValueOnce(new Promise(() => undefined))
      .mockResolvedValueOnce({ state: 'ready', taskId, url: '/ready', fileName: 'camera.tar.zst' });
    const operation = prepareFolderDownload('dataset', 'camera', new AbortController().signal);
    await vi.advanceTimersByTimeAsync(34_000);
    await expect(operation).resolves.toEqual({ url: '/ready', fileName: 'camera.tar.zst' });
    expect(api.folderPackage).toHaveBeenCalledTimes(3);
    expect(vi.mocked(api.folderPackage).mock.calls[1][2]?.signal?.aborted).toBe(true);
    expect(vi.mocked(api.folderPackage).mock.calls[2][2]).toMatchObject({ taskId });
    expect(vi.mocked(api.folderPackage).mock.calls[2][2]?.retry).toBeUndefined();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('bounds repeated gateway failures while keeping the original task identity', async () => {
    vi.mocked(api.folderPackage).mockResolvedValueOnce({ state: 'processing', taskId })
      .mockRejectedValue(Object.assign(new Error('网关超时'), { status: 504 }));
    const outcome = prepareFolderDownload('dataset', 'camera', new AbortController().signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(16_000);
    expect(await outcome).toMatchObject({ message: '网关超时' });
    expect(api.folderPackage).toHaveBeenCalledTimes(5);
    for (const call of vi.mocked(api.folderPackage).mock.calls.slice(1)) {
      expect(call[2]?.taskId).toBe(taskId);
      expect(call[2]?.retry).toBeUndefined();
    }
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancels retry backoff immediately without another progress request', async () => {
    vi.mocked(api.folderPackage).mockResolvedValueOnce({ state: 'processing', taskId })
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const controller = new AbortController();
    const outcome = prepareFolderDownload('dataset', 'camera', controller.signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(2001);
    controller.abort();
    expect(await outcome).toMatchObject({ name: 'AbortError' });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(api.folderPackage).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops waiting after 30 minutes while leaving the background package intact', async () => {
    vi.mocked(api.folderPackage).mockResolvedValue({ state: 'processing', taskId, retryAfterMs: 30_000 });
    const outcome = prepareFolderDownload('dataset', 'camera', new AbortController().signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(30 * 60 * 1000);
    expect(await outcome).toMatchObject({ message: '等待打包已超过30分钟，后台任务仍会继续，请稍后重新点击下载' });
    const calls = vi.mocked(api.folderPackage).mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(api.folderPackage).toHaveBeenCalledTimes(calls);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('rejects invalid states instead of following an unready URL', async () => {
    vi.mocked(api.folderPackage).mockResolvedValue({ state: 'failed', url: '/invalid', fileName: 'camera.tar.zst' } as never);
    await expect(prepareFolderDownload('dataset', 'camera', new AbortController().signal)).rejects.toThrow('压缩包状态无效');
    expect(vi.getTimerCount()).toBe(0);
  });
});
