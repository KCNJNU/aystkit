import { robotQuery, selectedRobot } from './robot-selection';
import { errorText } from './labels';
import { isVideoFile } from './file-kind';

export type Robot = { id: string; name: string; externalId?: string | null; metadata?: unknown; createdAt: string; updatedAt: string; };
export type RobotSummary = Pick<Robot, 'id' | 'name' | 'externalId'> & { projectId?: string | null };
export type RobotFile = { datasetId?: string | null; folderPath?: string; id: string; robotId: string; objectKey: string; originalName: string; fileType: string; mimeType: string; fileSize: string; checksum: string; recognitionType: string; schemaVersion: string; capturedAt: string; metadata?: unknown; createdAt: string; updatedAt: string; robot?: RobotSummary; tags?: Array<{ tag: Tag }>; datasetFiles?: Array<{ datasetVersionId: string; datasetVersion: DatasetVersion & { dataset: Pick<Dataset, 'id' | 'name'> } }> };
export type FileImpact = FileReferences & { objectKey: string; folderPath: string; datasetId: string | null; datasetName: string | null; fileSize: string; canRecycle: boolean; blockers: string[]; canRelocate: boolean; recycleReviewDays: number };
export type RecycledFile = { id: string; datasetId: string | null; originalName: string; objectKey: string; fileSize: string; folderPath: string; deletedAt: string; reviewAt: string; purgingAt: string | null };
export type FileReferences = { fileId: string; originalName: string; total: number; restrictedCount: number; references: Array<{ versionId: string; datasetId: string; datasetName: string; version: number; status: DatasetVersion['status']; referencedAt: string; canDetach: boolean; reason: string | null }> };
export type UserRole = 'ADMIN' | 'SUPER_ADMIN' | 'SYSTEM_ADMIN' | 'BASIC' | 'TENANT_ADMIN' | 'PROJECT_ADMIN' | 'OPERATOR' | 'MAINTENANCE_ENGINEER' | 'VIEWER';
export type AuthUser = { id: string; email: string; displayName: string; role: UserRole; tenantId?: string; projectIds?: string[]; mustChangePassword: boolean };
export type Tag = { id: string; name: string; color: string; _count?: { robotFiles: number; datasets: number } };
export type FilePage = { items: RobotFile[]; total: number; page: number; pageSize: number };
export type FileQuery = Record<string, string | number | undefined>;
export type DatasetFolder = { id: string; datasetId: string; name: string; path: string; parentPath: string; objectKey: string; createdAt: string; captureDateRange?: { from: string; to: string } };
export type DatasetContents = { dataset: { id: string; name: string; metadata?: unknown }; path: string; folders: DatasetFolder[]; files: RobotFile[]; total: number; page: number; pageSize: number; view?: { kind: 'archive-time-slot'; collapsedSegments: number } };
export type FolderPackageResult = { state?: 'ready'; url: string; fileName: string; taskId?: string } | { state: 'processing'; retryAfterMs?: number; taskId: string };
export type FolderPackageOptions = { async?: boolean; retry?: boolean; taskId?: string; signal?: AbortSignal };
export type PreparedFolderPackage = { path: string; state: 'QUEUED' | 'PROCESSING' | 'READY' | 'FAILED';
  taskId?: string | null; fileName?: string | null; readyAt?: string | null; errorMessage?: string | null };
export type PreparedFolderPackages = { items: PreparedFolderPackage[]; total: number; ready: number; active: number };
export type SavedArchiveVideo = { id: string; originalName: string; fileSize: string; startedAt: string; endAt: string };
export type ArchiveRecording = { id: string; sensor: string; robotId: string; startedAt: string; endAt: string; partCount: number; sourceBytes: string; frameCount: number; durationSeconds: number; state: 'collecting' | 'waiting_end' | 'pending' | 'processing' | 'ready' | 'failed'; closed: boolean; captureIncomplete?: boolean; closureSessionId?: string | null; closureReason?: 'desktop_stopped' | 'recovered_interruption' | null; captureSessionId?: string | null; controlSession?: { startedAt: string; endedAt: string; nativeTimeOutsideSession: boolean } | null; gapBeforeSeconds: number | null; file: RobotFile | null; downloadableVideos?: SavedArchiveVideo[]; jobId: string | null; error: string | null };
export type DatasetRecordings = { datasetId: string; path: string; recordings: ArchiveRecording[] };
export type ArchivePackage = { id: string; sensor: string; startedAt: string; endAt: string; bytes: string; fileCount: number; state: 'ready' };
export type ArchivePackages = { datasetId: string; path: string; packages: ArchivePackage[] };
export type DatasetVersion = { id: string; datasetId: string; version: number; status: 'DRAFT' | 'FROZEN' | 'PUBLISHED' | 'ARCHIVED'; manifestKey?: string | null; manifestChecksum?: string | null; fileCount: number; totalSize: string; gitCommit?: string | null; gitTag?: string | null; dvcPath?: string | null; dvcHash?: string | null; createdAt: string; updatedAt: string; frozenAt?: string | null; files?: Array<{ robotFileId: string; robotFile: RobotFile }> };
export type Dataset = { id: string; name: string; description?: string | null; metadata?: unknown; createdAt: string; updatedAt: string; versions: DatasetVersion[] };
export type DatasetBlockingUpload = { id: string; originalName: string; status: string; partCount: number; createdAt: string; updatedAt: string; expiresAt: string; canAbortEmpty: boolean };
export type DatasetUploadsPending = { code: 'DATASET_UPLOADS_PENDING'; blockingUploads: DatasetBlockingUpload[] };
export type DatasetPageItem = Dataset & { latestVersion: DatasetVersion | null; versionCount: number };
export type DatasetPage = { items: DatasetPageItem[]; total: number; page: number; pageSize: number };
export type VersionPageItem = DatasetVersion & { dataset: Pick<Dataset, 'id' | 'name'> };
export type VersionPage = { items: VersionPageItem[]; total: number; page: number; pageSize: number };
export type ValidationIssue = { code: string; severity: 'error' | 'warning'; message: string; fileId?: string; objectKey?: string };
export type ValidationReport = { versionId: string; status: DatasetVersion['status']; valid: boolean; validatedAt: string; summary: { files: number; totalSize: string; checkedObjects: number; errors: number; warnings: number }; issues: ValidationIssue[] };
export type CompareFile = Pick<RobotFile, 'id' | 'originalName' | 'checksum' | 'recognitionType' | 'schemaVersion' | 'capturedAt' | 'fileSize'> & { robot: RobotSummary };
export type VersionComparison = { dataset: Pick<Dataset, 'id' | 'name'>; base: Pick<DatasetVersion, 'id' | 'version' | 'status' | 'fileCount' | 'totalSize'>; target: Pick<DatasetVersion, 'id' | 'version' | 'status' | 'fileCount' | 'totalSize'>; summary: { added: number; removed: number; unchanged: number; totalSizeDelta: string }; files: { added: CompareFile[]; removed: CompareFile[]; unchanged: CompareFile[] } };
export type HealthStatus = 'healthy' | 'degraded' | 'unhealthy' | 'not_checked';
export type JobStatus = 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED';
export type Job = { id: string; type: 'DATASET_DELETE' | 'DATASET_PUBLISH' | 'RECOVERY_VERIFY' | 'STORAGE_SCAN' | 'STORAGE_ORPHAN_DELETE' | 'UPLOAD_FINALIZE' | 'RETENTION_CLEANUP' | 'ARCHIVE_VIDEO_ASSEMBLE'; status: JobStatus; progress: number; resourceType?: string; resourceId?: string; errorMessage?: string; cancelRequested: boolean; createdAt: string; updatedAt: string; startedAt?: string; finishedAt?: string; steps: Array<{ id: string; name: string; stepOrder: number; status: string; progress: number; errorMessage?: string; startedAt?: string; finishedAt?: string }> };
export type Page<T> = { items: T[]; total: number; page: number; pageSize: number };
export type ResumableUpload = { id: string; status: string; originalName: string; fileSize: string; checksum: string; chunkSize: number; totalParts: number; robotFileId?: string; expiresAt: string; parts: Array<{ partNumber: number; size: number; checksum: string }> };
export type SearchItem = { type: 'file' | 'dataset' | 'version'; id: string; title: string; subtitle: string; datasetId?: string };
export type SearchResult = { query: string; groups: { files: SearchItem[]; datasets: SearchItem[]; versions: SearchItem[] } };
export function resolveBrowserBase(configured: string | undefined, fallbackPort: number, location: Pick<Location, 'protocol' | 'hostname'> = window.location) {
  if (configured === '') return window.location.origin;
  const url = new URL(configured ?? `${location.protocol}//${location.hostname}:${fallbackPort}`, window.location.origin);
  const configuredIsLoopback = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
  const browserIsRemote = location.hostname !== 'localhost' && location.hostname !== '127.0.0.1';
  if (configuredIsLoopback && browserIsRemote) url.hostname = location.hostname;
  return url.origin + url.pathname.replace(/\/+$/, '');
}
export const apiBase = resolveBrowserBase(import.meta.env.VITE_API_BASE_URL, 3000);
let csrfToken: string | undefined;
let csrfRefresh: Promise<void> | undefined;
export function getCsrfToken() { return csrfToken; }
export function setCsrfToken(value?: string) { csrfToken = value; }
export class ApiError extends Error { constructor(message: string, readonly status: number, readonly payload?: unknown) { super(errorText(message, status)); } }
export function redirectObserverAuth(status: number, body: { error?: unknown }) {
  if (import.meta.env.BASE_URL === '/') return false;
  if (status === 401 && body.error === 'observer_auth_required') {
    window.location.replace('/login?next=' + encodeURIComponent(import.meta.env.BASE_URL));
    return true;
  }
  if (status === 403 && body.error === 'observer_access_required') {
    window.location.replace('/pending');
    return true;
  }
  return false;
}
export async function request<T>(path: string, init?: RequestInit, allowCsrfRetry = true): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase();
  const body = init?.body;
  const needsJsonContentType = body != null && !(body instanceof FormData || body instanceof Blob || body instanceof ArrayBuffer);
  const response = await fetch(`${apiBase}/api${robotQuery(path)}`, { ...init, credentials: 'include', headers: { ...(needsJsonContentType ? { 'Content-Type': 'application/json' } : {}), ...(!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken ? { 'X-CSRF-Token': csrfToken } : {}), ...init?.headers } });
  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({ message: response.statusText }));
    const observerRedirect = redirectObserverAuth(response.status, errorBody);
    if (!observerRedirect && response.status === 401 && path !== '/v1/auth/login') window.dispatchEvent(new Event('auth-expired'));
    if (!observerRedirect && response.status === 403 && allowCsrfRetry && !['GET', 'HEAD', 'OPTIONS'].includes(method) && isCsrfFailure(errorBody)) {
      await refreshCsrfToken();
      return request<T>(path, init, false);
    }
    throw new ApiError(Array.isArray(errorBody.message) ? errorBody.message.join(', ') : errorBody.message ?? response.statusText, response.status, errorBody);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}
function isCsrfFailure(body: { code?: string; message?: string | string[] }) {
  const message = Array.isArray(body.message) ? body.message.join(' ') : body.message ?? '';
  return body.code === 'CSRF_INVALID' || /csrf/i.test(message);
}
export async function refreshCsrfToken() {
  csrfRefresh ??= (async () => {
    const response = await fetch(`${apiBase}/api/v1/auth/session`, { credentials: 'include' });
    const body = await response.json().catch(() => ({ message: response.statusText }));
    if (!response.ok) redirectObserverAuth(response.status, body);
    if (!response.ok) throw new ApiError(Array.isArray(body.message) ? body.message.join(', ') : body.message ?? response.statusText, response.status, body);
    setCsrfToken((body as { csrfToken?: string }).csrfToken);
  })().finally(() => { csrfRefresh = undefined; });
  return csrfRefresh;
}
const queryString = (query: FileQuery) => new URLSearchParams(Object.entries(query).filter(([key, value]) => value !== undefined && (value !== '' || key === 'folderPath')).map(([key, value]) => [key, String(value)])).toString();

async function getAllFiles() {
  const first = await request<FilePage>('/v1/files?page=1&pageSize=100');
  if (first.items.length >= first.total) return first.items;
  const remainingPages = Math.ceil(first.total / 100) - 1;
  const pages = await Promise.all(Array.from({ length: remainingPages }, (_, index) => request<FilePage>(`/v1/files?page=${index + 2}&pageSize=100`)));
  return [first, ...pages].flatMap((page) => page.items);
}

export type UploadInput = { file: File; robotId: string; recognitionType: string; schemaVersion: string; capturedAt: string; metadata?: Record<string, unknown> };
export const api = {
  warehouseProjects: () => request<Array<{ id: string; name: string; tenantId: string; tenant: { name: string } }>>('/warehouse/projects'),
  uploadDefaults: (datasetId: string) => request<{ robotId: string; recognitionType: string; schemaVersion: string; capturedAtStrategy: 'LAST_MODIFIED' }>('/v1/uploads/defaults', { method: 'POST', body: JSON.stringify({ datasetId }) }),
  login: (email: string, password: string) => request<{ user: AuthUser; csrfToken: string; expiresAt: string }>('/v1/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  session: (signal?: AbortSignal) => request<{ user: AuthUser; csrfToken: string }>('/v1/auth/session', { signal }),
  logout: () => request<{ success: boolean }>('/v1/auth/logout', { method: 'POST' }),
  changePassword: (currentPassword: string, newPassword: string) => request<{ success: boolean; reauthenticate: boolean }>('/v1/auth/change-password', { method: 'POST', body: JSON.stringify({ currentPassword, newPassword }) }),
  search: (q: string, limit = 5) => request<SearchResult>(`/v1/search?${queryString({ q, limit })}`),
  fleetRobots: () => request<Array<RobotSummary & { onlineStatus?: string; observerId?: string }>>('/warehouse/robots'),
  robots: () => request<Robot[]>('/warehouse/sources'),
  files: (query: FileQuery) => request<FilePage>(`/v1/files?${queryString(query)}`),
  allFiles: getAllFiles,
  file: (id: string) => request<RobotFile>(`/v1/files/${id}`),
  download: (id: string) => request<{ url: string }>(`/v1/files/${id}/download`),
  updateFile: (id: string, data: { recognitionType: string; metadata: Record<string, unknown> }) => request<RobotFile>(`/v1/files/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  fileImpact: (id: string) => request<FileImpact>(`/v1/files/${id}/delete-impact`),
  relocateFile: (id: string, originalName: string, folderPath: string) => request<{ id: string; objectKey: string; cleanupPending: boolean }>(`/v1/files/${id}/location`, { method: 'PATCH', body: JSON.stringify({ originalName, folderPath, confirmed: true }) }),
  recycledFiles: (query: FileQuery) => request<Page<RecycledFile>>(`/v1/files/recycle-bin?${new URLSearchParams(Object.entries(query).filter(([, value]) => value !== undefined).map(([key, value]) => [key, String(value)]))}`),
  restoreFile: (id: string) => request<{ restored: boolean }>(`/v1/files/recycle-bin/${id}/restore`, { method: 'POST', body: JSON.stringify({ confirmed: true }) }),
  purgeFile: (id: string) => request<{ purged: boolean }>(`/v1/files/recycle-bin/${id}/purge`, { method: 'POST', body: JSON.stringify({ confirmed: true }) }),
  fileReferences: (id: string) => request<FileReferences>(`/v1/files/${id}/references`),
  detachFileReferences: (id: string, versionIds: string[]) => request<{ detachedCount: number; remainingCount: number }>(`/v1/files/${id}/references/detach`, { method: 'POST', body: JSON.stringify({ versionIds, confirmed: true }) }),
  deleteFile: (id: string) => request<void>(`/v1/files/${id}`, { method: 'DELETE' }),
  datasets: () => request<Dataset[]>('/v1/datasets'),
  datasetPage: (query: FileQuery) => request<DatasetPage>(`/v1/datasets/page?${queryString(query)}`),
  dataset: (id: string) => request<Dataset>(`/v1/datasets/${id}`),
  datasetContents: (id: string, path = '', page = 1) => request<DatasetContents>(`/v1/datasets/${id}/contents?${new URLSearchParams({ path, page: String(page), pageSize: '50' })}`),
  datasetRecordings: (id: string, path: string) => request<DatasetRecordings>(`/v1/datasets/${encodeURIComponent(id)}/recordings?${new URLSearchParams({ path })}`),
  assembleDatasetRecording: (id: string, recordingId: string) => request<{ jobId: string }>(`/v1/datasets/${encodeURIComponent(id)}/recordings/${encodeURIComponent(recordingId)}/assemble`, { method: 'POST' }),
  archiveRecordingPackage: (id: string, recordingId: string) => request<{ url: string; fileName: string }>(`/v1/datasets/${encodeURIComponent(id)}/recordings/${encodeURIComponent(recordingId)}/package`),
  folderPackage: (id: string, path: string, options: FolderPackageOptions = {}) => request<FolderPackageResult>(`/v1/datasets/${encodeURIComponent(id)}/folder-package?${new URLSearchParams({ path, ...(options.async ? { async: 'true' } : {}), ...(options.retry ? { retry: 'true' } : {}), ...(options.taskId ? { taskId: options.taskId } : {}) })}`, { signal: options.signal }),
  preparedFolderPackages: (id: string, signal?: AbortSignal) => request<PreparedFolderPackages>(`/v1/datasets/${encodeURIComponent(id)}/prepared-folder-packages`, { signal }),
  prepareDatasetFolderPackages: (id: string) => request<PreparedFolderPackages>(`/v1/datasets/${encodeURIComponent(id)}/prepared-folder-packages`, { method: 'POST', body: JSON.stringify({}) }),
  archivePackages: (id: string, path: string) => request<ArchivePackages>(`/v1/datasets/${encodeURIComponent(id)}/archive-packages?${new URLSearchParams({ path })}`),
  datasetFolders: (id: string) => request<DatasetFolder[]>(`/v1/datasets/${id}/folders`),
  createDatasetFolder: (id: string, parentPath: string, name: string) => request<DatasetFolder>(`/v1/datasets/${id}/folders`, { method: 'POST', body: JSON.stringify({ parentPath, name }) }),
  deleteDataset: (id: string, confirmationName: string) => request<Job>(`/v1/datasets/${id}`, { method: 'DELETE', body: JSON.stringify({ confirmationName }) }),
  createDataset: (data: { name: string; description?: string; projectId?: string; robotId?: string }) => request<Dataset>('/v1/datasets', { method: 'POST', body: JSON.stringify({ ...data, robotId: (data.robotId ?? selectedRobot()) || undefined }) }),
  createVersion: (id: string, input: string[] | { fileIds?: string[]; sourceVersionId?: string }) => request<DatasetVersion>(`/v1/datasets/${id}/versions`, { method: 'POST', body: JSON.stringify(Array.isArray(input) ? { fileIds: input } : input) }),
  versionPage: (query: FileQuery) => request<VersionPage>(`/v1/datasets/versions/page?${queryString(query)}`),
  version: (id: string) => request<DatasetVersion>(`/v1/datasets/versions/${id}`),
  versionFiles: (id: string, query: FileQuery) => request<FilePage>(`/v1/datasets/versions/${id}/files?${queryString(query)}`),
  versionCandidates: (id: string, query: FileQuery) => request<FilePage>(`/v1/datasets/versions/${id}/candidates?${queryString(query)}`),
  addFiles: (id: string, fileIds: string[]) => request<DatasetVersion>(`/v1/datasets/versions/${id}/files`, { method: 'POST', body: JSON.stringify({ fileIds }) }),
  replaceVersionFiles: (id: string, fileIds: string[]) => request<DatasetVersion>(`/v1/datasets/versions/${id}/files`, { method: 'PUT', body: JSON.stringify({ fileIds }) }),
  removeFile: (versionId: string, fileId: string) => request<void>(`/v1/datasets/versions/${versionId}/files/${fileId}`, { method: 'DELETE' }),
  validateVersion: (id: string) => request<ValidationReport>(`/v1/datasets/versions/${id}/validate`, { method: 'POST' }),
  compareVersions: (datasetId: string, baseVersionId: string, targetVersionId: string) => request<VersionComparison>(`/v1/datasets/${datasetId}/versions/compare?${queryString({ baseVersionId, targetVersionId })}`),
  freeze: (id: string) => request<DatasetVersion>(`/v1/datasets/versions/${id}/freeze`, { method: 'POST' }),
  publish: (id: string) => request<DatasetVersion>(`/v1/datasets/versions/${id}/publish`, { method: 'POST' }),
  publishJob: (id: string) => request<Job>(`/v1/datasets/versions/${id}/publish-jobs`, { method: 'POST' }),
  recoveryJob: (id: string) => request<Job>(`/v1/datasets/versions/${id}/recovery-jobs`, { method: 'POST' }),
  jobs: (query: FileQuery) => request<Page<Job>>(`/v1/jobs/page?${queryString(query)}`),
  job: (id: string) => request<Job>(`/v1/jobs/${id}`),
  retryJob: (id: string) => request<Job>(`/v1/jobs/${id}/retry`, { method: 'POST' }),
  cancelJob: (id: string) => request<Job>(`/v1/jobs/${id}/cancel`, { method: 'POST' }),
  activity: (query: FileQuery) => request<Page<{ id: string; source: string; type: string; status: string; timestamp: string; actor?: { displayName: string }; resourceType?: string; resourceId?: string; jobId?: string }>>(`/v1/activity/page?${queryString(query)}`),
  notifications: (query: FileQuery) => request<Page<{ id: string; type: string; title: string; message: string; resourceUrl?: string; readAt?: string; createdAt: string }> & { unread: number }>(`/v1/notifications?${queryString(query)}`),
  readNotification: (id: string) => request<{ success: boolean }>(`/v1/notifications/${id}/read`, { method: 'PATCH' }),
  readAllNotifications: () => request<{ success: boolean }>('/v1/notifications/read-all', { method: 'POST' }),
  tags: () => request<Tag[]>('/v1/tags'),
  createTag: (data: { name: string; color: string }) => request<Tag>('/v1/tags', { method: 'POST', body: JSON.stringify(data) }),
  bulkTagFiles: (fileIds: string[], tagIds: string[]) => request<{ attached: number }>('/v1/tags/files/bulk', { method: 'POST', body: JSON.stringify({ fileIds, tagIds }) }),
  savedViews: (resource: string) => request<Array<{ id: string; name: string; resource: string; query: Record<string, string | number | boolean>; isShared: boolean; owner: { displayName: string } }>>(`/v1/saved-views?resource=${resource}`),
  createSavedView: (data: { resource: string; name: string; query: Record<string, string | number | boolean>; isShared?: boolean }) => request<{ id: string }>('/v1/saved-views', { method: 'POST', body: JSON.stringify(data) }),
  deleteSavedView: (id: string) => request<void>(`/v1/saved-views/${id}`, { method: 'DELETE' }),
  storageObjects: (query: FileQuery) => request<{ items: Array<{ objectKey: string; size: number; etag?: string; lastModified?: string; mapping: unknown }>; nextCursor?: string }>(`/v1/storage/objects?${queryString(query)}`),
  reconciliationRuns: (query: FileQuery) => request<Page<{ id: string; status: string; scannedObjects: number; indexedObjects: number; issueCount: number; createdAt: string }>>(`/v1/storage/reconciliation-runs/page?${queryString(query)}`),
  reconciliationRun: (id: string) => request<{ id: string; status: string; issues: Array<{ id: string; type: string; status: string; objectKey: string; expectedSize?: string; actualSize?: string }> }>(`/v1/storage/reconciliation-runs/${id}`),
  startReconciliation: () => request<{ job: Job }>('/v1/storage/reconciliation-runs', { method: 'POST' }),
  deleteOrphans: (issueIds: string[]) => request<Job>('/v1/storage/orphan-deletion-jobs', { method: 'POST', body: JSON.stringify({ issueIds, confirmation: 'DELETE_ORPHAN_OBJECTS' }) }),
  systemConfiguration: () => request<{ settings: Record<string, number | boolean>; infrastructure: Record<string, unknown>; worker: Record<string, unknown> }>('/v1/system/configuration'),
  updateSettings: (values: Record<string, unknown>) => request<Record<string, unknown>>('/v1/system/settings', { method: 'PATCH', body: JSON.stringify({ values }) }),
  users: (query: FileQuery) => request<Page<AuthUser & { isActive: boolean; lastLoginAt?: string; createdAt: string }>>(`/v1/users/page?${queryString(query)}`),
  createUser: (data: { email: string; displayName: string; role: UserRole; tenantId?: string; projectIds?: string[]; temporaryPassword: string }) => request<AuthUser>('/v1/users', { method: 'POST', body: JSON.stringify(data) }),
  auditLogs: (query: FileQuery) => request<Page<{ id: string; action: string; route?: string; statusCode?: number; actor?: { email: string; displayName: string }; createdAt: string }>>(`/v1/audit-logs/page?${queryString(query)}`),
  createUpload: (data: { folderPath?: string; datasetId: string; robotId: string; originalName: string; mimeType: string; fileSize: number; checksum: string; recognitionType: string; schemaVersion: string; capturedAt: string; metadata?: Record<string, unknown>; lastModified?: number }) => request<ResumableUpload>('/v1/uploads', { method: 'POST', body: JSON.stringify(data) }),
  uploadSession: (id: string) => request<ResumableUpload>(`/v1/uploads/${id}`),
  uploadPart: (id: string, partNumber: number, bytes: ArrayBuffer, start: number, end: number, total: number, checksum: string, signal?: AbortSignal) => request<{ partNumber: number }>(`/v1/uploads/${id}/parts/${partNumber}`, { method: 'PUT', body: bytes, signal, headers: { 'Content-Type': 'application/octet-stream', 'Content-Range': `bytes ${start}-${end}/${total}`, 'X-Part-SHA256': checksum } }),
  completeUpload: (id: string) => request<Job | { completed: true; robotFileId: string }>(`/v1/uploads/${id}/complete`, { method: 'POST' }),
  abortUpload: (id: string) => request<void>(`/v1/uploads/${id}`, { method: 'DELETE' }),
  preview: async (file: RobotFile) => {
    const { url } = await request<{ url: string }>(`/v1/files/${file.id}/download`);
    if (isVideoFile(file)) return { kind: 'video' as const, url };
    if (file.mimeType.startsWith('image/')) return { kind: 'image' as const, url };
    if (Number(file.fileSize) > 1024 * 1024) return { kind: 'large' as const, url };
    const response = await fetch(url); if (!response.ok) throw new Error(`Preview failed (${response.status})`);
    if (file.mimeType.includes('json')) return { kind: 'json' as const, value: await response.json(), url };
    if (file.mimeType.startsWith('text/')) return { kind: 'text' as const, value: await response.text(), url };
    return { kind: 'unsupported' as const, url };
  },
};
