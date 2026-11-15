import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, refreshCsrfToken, request, resolveBrowserBase, setCsrfToken, type RobotFile } from './api';
import { streamChat } from './warehouse';

describe('LAN API addressing', () => {
  it('replaces a loopback API host when the page is opened through a LAN address', () => {
    expect(resolveBrowserBase('http://localhost:3100', 3100, { protocol: 'http:', hostname: '192.168.1.25' }))
      .toBe('http://192.168.1.25:3100');
  });

  it('preserves an explicitly configured non-loopback API origin', () => {
    expect(resolveBrowserBase('https://api.example.com', 3100, { protocol: 'http:', hostname: '192.168.1.25' }))
      .toBe('https://api.example.com');
  });
});

describe('API deployment prefix', () => {
  it('keeps a same-origin path prefix and removes trailing slashes', () => {
    expect(resolveBrowserBase('/dbprog/', 3000)).toBe(window.location.origin + '/dbprog');
  });

  it('keeps an explicit remote origin together with its deployment path', () => {
    expect(resolveBrowserBase('https://api.example.com/dbprog/', 3000, { protocol: 'https:', hostname: 'aystkit.cn' }))
      .toBe('https://api.example.com/dbprog');
  });

  it('keeps the deployment path when replacing a loopback host for a remote browser', () => {
    expect(resolveBrowserBase('http://localhost:3000/dbprog/', 3000, { protocol: 'http:', hostname: '192.168.1.25' }))
      .toBe('http://192.168.1.25:3000/dbprog');
  });

  it('keeps an empty configured base on the current origin', () => {
    expect(resolveBrowserBase('', 3000)).toBe(window.location.origin);
  });
});

describe('Observer session routing for a mounted warehouse', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    setCsrfToken();
  });

  function fixture(code: string, status = 401, base = '/dbprog/') {
    vi.stubEnv('BASE_URL', base);
    const replace = vi.fn();
    const dispatchEvent = vi.fn();
    vi.stubGlobal('window', { location: { replace }, dispatchEvent });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: code, message: 'Authentication required' }), {
      status, headers: { 'Content-Type': 'application/json' },
    })));
    return { replace, dispatchEvent };
  }

  it('returns an expired observer session to the observer login with the warehouse return path', async () => {
    const { replace, dispatchEvent } = fixture('observer_auth_required');
    await expect(request('/v1/datasets')).rejects.toThrow();
    expect(replace).toHaveBeenCalledWith('/login?next=%2Fdbprog%2F');
    expect(dispatchEvent).not.toHaveBeenCalled();
  });

  it('returns an unapproved observer account to the existing pending page', async () => {
    const { replace, dispatchEvent } = fixture('observer_access_required', 403);
    await expect(request('/system/status')).rejects.toThrow();
    expect(replace).toHaveBeenCalledWith('/pending');
    expect(dispatchEvent).not.toHaveBeenCalled();
  });

  it('keeps an expired DBProg session on the normal DBProg authentication path', async () => {
    const { replace, dispatchEvent } = fixture('DBPROG_AUTH_REQUIRED');
    await expect(request('/v1/datasets')).rejects.toThrow();
    expect(replace).not.toHaveBeenCalled();
    expect(dispatchEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'auth-expired' }));
  });

  it('preserves the standalone root deployment authentication behavior', async () => {
    const { replace, dispatchEvent } = fixture('observer_auth_required', 401, '/');
    await expect(request('/v1/datasets')).rejects.toThrow();
    expect(replace).not.toHaveBeenCalled();
    expect(dispatchEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'auth-expired' }));
  });

  it('also restores the observer session when CSRF renewal encounters its expiry', async () => {
    const { replace } = fixture('observer_auth_required');
    await expect(refreshCsrfToken()).rejects.toThrow();
    expect(replace).toHaveBeenCalledWith('/login?next=%2Fdbprog%2F');
  });

  it('handles the same expired observer session before an AI stream starts', async () => {
    const { replace, dispatchEvent } = fixture('observer_auth_required');
    await expect(streamChat('fixture question', undefined, new AbortController().signal, vi.fn())).rejects.toThrow();
    expect(replace).toHaveBeenCalledWith('/login?next=%2Fdbprog%2F');
    expect(dispatchEvent).not.toHaveBeenCalled();
  });
});

describe('API CSRF recovery', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCsrfToken();
  });

  it('refreshes the CSRF token once and retries a rejected upload part', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ code: 'CSRF_INVALID', message: 'Invalid CSRF token' }), { status: 403, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ user: { id: 'user' }, csrfToken: 'fresh-token' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ partNumber: 1 }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetch);
    setCsrfToken('stale-token');

    await expect(request('/v1/uploads/session/parts/1', {
      method: 'PUT', body: new ArrayBuffer(1), headers: { 'Content-Range': 'bytes 0-0/1', 'X-Part-SHA256': 'a'.repeat(64) },
    })).resolves.toEqual({ partNumber: 1 });

    expect(fetch).toHaveBeenCalledTimes(3);
    expect(new Headers(fetch.mock.calls[2][1].headers).get('X-CSRF-Token')).toBe('fresh-token');
  });
});


describe('video preview metadata', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('recognizes archived binary MKV without fetching the entire video for preview', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ url: '/dbprog/objects/video-file' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetch);
    const file = { id: 'video-file', originalName: 'video.mkv', fileType: 'binary', mimeType: 'application/octet-stream', fileSize: '113000000' } as RobotFile;
    await expect(api.preview(file)).resolves.toEqual({ kind: 'video', url: '/dbprog/objects/video-file' });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(String(fetch.mock.calls[0][0])).toContain('/api/v1/files/video-file/download');
  });
});

describe('Complete archive recording API', () => {
  afterEach(() => { vi.unstubAllGlobals(); setCsrfToken(); });

  it('requests all recording groups for the exact dataset and sensor path', async () => {
    const result = { datasetId: 'dataset-one', path: '原相机前', recordings: [] };
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(result), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    await expect(api.datasetRecordings('dataset-one', '原相机前')).resolves.toEqual(result);
    const url = new URL(String(fetch.mock.calls[0][0]));
    expect(url.pathname).toMatch(/\/api\/v1\/datasets\/dataset-one\/recordings$/);
    expect(url.searchParams.get('path')).toBe('原相机前');
    expect(fetch.mock.calls[0][1].credentials).toBe('include');
  });

  it('uses an authenticated CSRF-protected POST to retry one recording', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ jobId: 'job-one' }), { status: 202 }));
    vi.stubGlobal('fetch', fetch); setCsrfToken('csrf-one');
    await expect(api.assembleDatasetRecording('dataset-one', 'capture/one')).resolves.toEqual({ jobId: 'job-one' });
    expect(String(fetch.mock.calls[0][0])).toContain('/datasets/dataset-one/recordings/capture%2Fone/assemble');
    expect(fetch.mock.calls[0][1].method).toBe('POST');
    expect(new Headers(fetch.mock.calls[0][1].headers).get('X-CSRF-Token')).toBe('csrf-one');
  });
});

describe('Recursive folder package API', () => {
  afterEach(() => { vi.unstubAllGlobals(); setCsrfToken(); });

  it('requests the precise nested folder with authenticated GET and preserves the server filename', async () => {
    const result = { url: '/dbprog/folder-packages/ready', fileName: '原相机前_20261102_220100.tar.zst' };
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(result), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    const path = '原相机前/时段 + 样本/子目录';
    await expect(api.folderPackage('dataset/one', path)).resolves.toEqual(result);
    const url = new URL(String(fetch.mock.calls[0][0]));
    expect(url.pathname).toMatch(/\/api\/v1\/datasets\/dataset%2Fone\/folder-package$/);
    expect(url.searchParams.get('path')).toBe(path);
    expect([...url.searchParams.keys()]).toEqual(['path']);
    expect(fetch.mock.calls[0][1].credentials).toBe('include');
    expect(fetch.mock.calls[0][1].method ?? 'GET').toBe('GET');
  });

  it('uses an abortable short async request and retries a failed task only when explicitly requested', async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ state: 'processing', taskId: 'a'.repeat(64), retryAfterMs: 2000 }), { status: 202 }));
    vi.stubGlobal('fetch', fetch);
    const controller = new AbortController();
    await expect(api.folderPackage('dataset/one', '原相机前', { async: true, retry: true, signal: controller.signal })).resolves.toEqual({ state: 'processing', taskId: 'a'.repeat(64), retryAfterMs: 2000 });
    expect(new URL(String(fetch.mock.calls[0][0])).searchParams.get('async')).toBe('true');
    expect(new URL(String(fetch.mock.calls[0][0])).searchParams.get('retry')).toBe('true');
    expect(fetch.mock.calls[0][1].signal).toBe(controller.signal);
    await api.folderPackage('dataset/one', '原相机前', { async: true, taskId: 'a'.repeat(64), signal: controller.signal });
    expect(new URL(String(fetch.mock.calls[1][0])).searchParams.get('async')).toBe('true');
    expect(new URL(String(fetch.mock.calls[1][0])).searchParams.has('retry')).toBe(false);
    expect(new URL(String(fetch.mock.calls[1][0])).searchParams.get('taskId')).toBe('a'.repeat(64));
  });
});
