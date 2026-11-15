import { request, type FolderPackageResult } from './api';

export type CaptureSession = {
  sessionId: string;
  robotId: string;
  startedAt: string;
  endAt: string;
  fileCount: number;
  totalBytes: string;
  sensorCount: number;
  sensors: Array<{ name: string; fileCount: number; totalBytes: string }>;
  snapshot: true;
};
export type CaptureSessions = {
  datasetId: string;
  robotId: string | null;
  snapshotAt: string;
  sessions: CaptureSession[];
  unassigned: { fileCount: number; totalBytes: string };
  excludedDerivedFileCount: number;
};

export function listCaptureSessions(datasetId: string, robotId: string, signal?: AbortSignal) {
  const query = new URLSearchParams(robotId ? { robotId } : {});
  return request<CaptureSessions>(`/v1/datasets/${encodeURIComponent(datasetId)}/capture-sessions${query.size ? `?${query}` : ''}`, { signal });
}

export function captureSessionPackage(datasetId: string, session: Pick<CaptureSession, 'sessionId' | 'robotId'>,
  options: { taskId?: string; retry?: boolean; signal: AbortSignal }) {
  const query = new URLSearchParams({ robotId: session.robotId, async: 'true',
    ...(options.taskId ? { taskId: options.taskId } : options.retry ? { retry: 'true' } : {}) });
  return request<FolderPackageResult>(`/v1/datasets/${encodeURIComponent(datasetId)}/capture-sessions/${encodeURIComponent(session.sessionId)}/package?${query}`, { signal: options.signal });
}

function cancelled() { return new DOMException('已取消等待压缩包', 'AbortError'); }
function abortable<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) { void operation.catch(() => undefined); return Promise.reject(cancelled()); }
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener('abort', abort); reject(cancelled()); };
    signal.addEventListener('abort', abort, { once: true });
    operation.then(value => { signal.removeEventListener('abort', abort); if (signal.aborted) reject(cancelled()); else resolve(value); },
      error => { signal.removeEventListener('abort', abort); reject(error); });
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
function transient(error: unknown) {
  if (error instanceof TypeError) return true;
  if (!error || typeof error !== 'object' || !('status' in error)) return false;
  if (error.status === 502 || error.status === 504) return true;
  return error.status === 503 && 'payload' in error && typeof error.payload === 'object' && error.payload !== null
    && 'error' in error.payload && error.payload.error === 'DBPROG_UNAVAILABLE';
}
const taskPattern = /^[a-f0-9]{64}$/;

/** A user click captures one server snapshot. Polls never start a replacement task. */
export async function prepareCaptureSessionDownload(datasetId: string, session: Pick<CaptureSession, 'sessionId' | 'robotId'>,
  signal: AbortSignal): Promise<{ url: string; fileName: string }> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal.aborted) controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  let expired = false;
  const deadline = setTimeout(() => { expired = true; controller.abort(); }, 30 * 60 * 1000);
  let taskId: string | undefined;
  let pollRetries = 0;
  try {
    while (!controller.signal.aborted) {
      const progress = new AbortController();
      const abortProgress = () => progress.abort();
      controller.signal.addEventListener('abort', abortProgress, { once: true });
      let requestExpired = false;
      const timeout = setTimeout(() => { requestExpired = true; progress.abort(); }, 30_000);
      let result: FolderPackageResult;
      try {
        result = await abortable(captureSessionPackage(datasetId, session,
          { ...(taskId ? { taskId } : { retry: true }), signal: progress.signal }), progress.signal);
      } catch (error) {
        if (taskId && !controller.signal.aborted && (requestExpired || transient(error)) && pollRetries < 3) {
          pollRetries++;
          await pause(2000 * 2 ** (pollRetries - 1), controller.signal);
          continue;
        }
        if (requestExpired && !controller.signal.aborted) throw new Error('查询打包进度超时，后台任务可能仍在继续，请稍后重试');
        throw error;
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener('abort', abortProgress);
      }
      if (controller.signal.aborted) throw cancelled();
      pollRetries = 0;
      if (result?.state === 'processing') {
        if (!taskPattern.test(result.taskId) || (taskId && result.taskId !== taskId))
          throw new Error('压缩包任务标识无效或已改变，请重新点击下载');
        taskId = result.taskId;
        const delay = typeof result.retryAfterMs === 'number' && Number.isFinite(result.retryAfterMs) ? result.retryAfterMs : 2000;
        await pause(Math.min(30_000, Math.max(2000, delay)), controller.signal);
      } else if (result?.state === 'ready' && typeof result.url === 'string' && typeof result.fileName === 'string') {
        if (!result.taskId || !taskPattern.test(result.taskId) || (taskId && result.taskId !== taskId))
          throw new Error('压缩包任务标识无效或已改变，请重新点击下载');
        return { url: result.url, fileName: result.fileName };
      } else throw new Error('压缩包状态无效，请刷新后重试');
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
