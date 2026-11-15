import { api, type FolderPackageResult } from './api';

const maximumWaitMs = 30 * 60 * 1000;
const requestTimeoutMs = 30 * 1000;
const maximumPollRetries = 3;

function transientProgressFailure(error: unknown): boolean {
  // Fetch rejects network failures with TypeError; gateway timeouts have HTTP status.
  if (error instanceof TypeError) return true;
  if (typeof error !== 'object' || error === null || !('status' in error)) return false;
  if (error.status === 502 || error.status === 504) return true;
  return error.status === 503 && 'payload' in error && typeof error.payload === 'object'
    && error.payload !== null && 'error' in error.payload && error.payload.error === 'DBPROG_UNAVAILABLE';
}

function cancelled() { return new DOMException('已取消等待压缩包', 'AbortError'); }

function abortable<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) { void operation.catch(() => undefined); return Promise.reject(cancelled()); }
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(cancelled()); };
    signal.addEventListener('abort', abort, { once: true });
    operation.then(value => {
      signal.removeEventListener('abort', abort);
      if (signal.aborted) reject(cancelled()); else resolve(value);
    }, error => { signal.removeEventListener('abort', abort); reject(error); });
  });
}

function pause(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(cancelled());
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(cancelled()); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, milliseconds);
    signal.addEventListener('abort', abort, { once: true });
  });
}

/** Poll the same server task with short requests; cancelling only ends this page's wait. */
export async function prepareFolderDownload(id: string, path: string, signal: AbortSignal): Promise<{ url: string; fileName: string }> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal.aborted) controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  let expired = false;
  const deadline = setTimeout(() => { expired = true; controller.abort(); }, maximumWaitMs);
  let retry = true;
  let taskId: string | undefined;
  let pollRetries = 0;
  try {
    while (!controller.signal.aborted) {
      const request = new AbortController();
      const abortRequest = () => request.abort();
      controller.signal.addEventListener('abort', abortRequest, { once: true });
      let requestExpired = false;
      const timeout = setTimeout(() => { requestExpired = true; request.abort(); }, requestTimeoutMs);
      let result: FolderPackageResult;
      try {
        result = await abortable(api.folderPackage(id, path, { async: true, ...(retry ? { retry: true } : {}), ...(taskId ? { taskId } : {}), signal: request.signal }), request.signal);
      } catch (error) {
        if (taskId && !controller.signal.aborted && (requestExpired || transientProgressFailure(error))
          && pollRetries < maximumPollRetries) {
          pollRetries++;
          await pause(2000 * 2 ** (pollRetries - 1), controller.signal);
          continue;
        }
        if (requestExpired && !controller.signal.aborted) throw new Error('查询打包进度超时，后台任务可能仍在继续，请稍后重试');
        throw error;
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener('abort', abortRequest);
      }
      if (controller.signal.aborted) throw cancelled();
      pollRetries = 0;
      retry = false;
      if (result && result.state === 'processing') {
        if (typeof result.taskId !== 'string' || !/^[a-f0-9]{64}$/.test(result.taskId)
          || (taskId && taskId !== result.taskId)) throw new Error('压缩包任务标识无效或已改变，请重新点击下载');
        taskId = result.taskId;
        const delay = typeof result.retryAfterMs === 'number' && Number.isFinite(result.retryAfterMs) ? result.retryAfterMs : 2000;
        await pause(Math.min(30_000, Math.max(2000, delay)), controller.signal);
      } else if (result && (result.state === 'ready' || result.state === undefined) && 'url' in result && 'fileName' in result
        && typeof result.url === 'string' && typeof result.fileName === 'string') {
        if ((result.taskId !== undefined && !/^[a-f0-9]{64}$/.test(result.taskId))
          || (taskId && result.state === 'ready' && result.taskId !== taskId))
          throw new Error('压缩包任务标识无效或已改变，请重新点击下载');
        return { url: result.url, fileName: result.fileName };
      } else {
        throw new Error('压缩包状态无效，请刷新后重试');
      }
    }
    throw cancelled();
  } catch (error) {
    if (expired) throw new Error('等待打包已超过30分钟，后台任务仍会继续，请稍后重新点击下载');
    throw error;
  } finally {
    clearTimeout(deadline);
    signal.removeEventListener('abort', abort);
  }
}
