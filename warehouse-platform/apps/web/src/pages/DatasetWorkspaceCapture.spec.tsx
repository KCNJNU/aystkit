import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DatasetWorkspace } from './DatasetWorkspace';
import { listCaptureSessions, type CaptureSessions } from '../services/capture-sessions';
import { selectRobot } from '../services/robot-selection';
import { useRecordWorkspaceVisit } from '../components/WorkspaceShortcuts';

const mocks = vi.hoisted(() => ({ dataset: vi.fn(), datasetContents: vi.fn() }));
vi.mock('../services/api', async original => ({ ...await original<typeof import('../services/api')>(), api: mocks }));
vi.mock('../services/capture-sessions', () => ({ listCaptureSessions: vi.fn(), prepareCaptureSessionDownload: vi.fn() }));
vi.mock('../components/WorkspaceShortcuts', () => ({ useRecordWorkspaceVisit: vi.fn(), WorkspaceFavoriteButton: () => <button>收藏</button> }));
vi.mock('../components/PreparedFolderPackagesPanel', () => ({ PreparedFolderPackagesPanel: () => null }));
vi.mock('../components/ContinuousRecordingsPanel', () => ({ ContinuousRecordingsPanel: () => null, recordingRange: () => '2026-11-10 08:10 — 08:11' }));
vi.mock('../components/ArchivePackagesPanel', () => ({ ArchivePackagesPanel: () => null }));
vi.mock('../components/FilePreview', () => ({ FilePreview: () => null }));
vi.mock('../upload/UploadQueueContext', () => ({ useUploadQueue: () => ({ items: [] }) }));
vi.mock('../auth/AuthContext', () => ({ useOptionalAuth: () => undefined }));
const datasetId = '465c77c6-0029-4406-a504-739014d0a672', robotId = '365c77c6-0029-4406-a504-739014d0a672';
const sessionId = 'b'.repeat(32);
const dataset = { id: datasetId, name: '地库_2026-11-10', metadata: { type: 'garage-native-archive' } };
const sessions: CaptureSessions = { datasetId, robotId, snapshotAt: '2026-11-10T00:11:00Z', unassigned: { fileCount: 0, totalBytes: '0' }, excludedDerivedFileCount: 0,
  sessions: [{ sessionId, robotId, startedAt: '2026-11-10T00:10:00Z', endAt: '2026-11-10T00:11:00Z', fileCount: 1, totalBytes: '1024', sensorCount: 1, sensors: [{ name: '前相机', fileCount: 1, totalBytes: '1024' }], snapshot: true }] };
function mount(search: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  function Location() { return <output data-testid="location">{useLocation().pathname + useLocation().search}</output>; }
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[`/datasets/${datasetId}?${search}`]}><Routes><Route path="/datasets/:datasetId" element={<><DatasetWorkspace /><Location /></>} /></Routes></MemoryRouter></QueryClientProvider>);
}
describe('dataset workspace capture integration', () => {
  beforeEach(() => {
    vi.clearAllMocks(); selectRobot(robotId);
    mocks.dataset.mockResolvedValue(dataset);
    mocks.datasetContents.mockResolvedValue({ dataset, files: [], folders: [], total: 0, page: 1, pageSize: 50 });
    vi.mocked(listCaptureSessions).mockResolvedValue(sessions);
  });

  it('keeps the robot and existing directory query when selecting a shareable session', async () => {
    mount(`robotId=${robotId}&path=&page=2&sort=oldest`);
    fireEvent.click(await screen.findByRole('button', { name: /选择采集/ }));
    const query = new URL(screen.getByTestId('location').textContent!, 'https://warehouse.invalid').searchParams;
    expect(query.get('captureSession')).toBe(sessionId);
    expect(query.get('robotId')).toBe(robotId);
    expect(query.get('path')).toBe('');
    expect(query.get('page')).toBe('2');
    expect(query.get('sort')).toBe('oldest');
  });

  it('records a session visit only after that robot session is successfully identified', async () => {
    let complete!: (value: CaptureSessions) => void;
    vi.mocked(listCaptureSessions).mockReturnValue(new Promise(resolve => { complete = resolve; }));
    mount(`robotId=${robotId}&captureSession=${sessionId}`);
    await screen.findByRole('heading', { name: /地库_2026-11-10/ });
    expect(vi.mocked(useRecordWorkspaceVisit).mock.calls.at(-1)?.[0].ready).toBe(false);
    await act(async () => complete(sessions));
    await waitFor(() => expect(vi.mocked(useRecordWorkspaceVisit).mock.calls.at(-1)?.[0]).toMatchObject({ ready: true, captureSession: sessionId, datasetId, sessionLabel: '2026-11-10 08:10 — 08:11' }));
  });

  it('does not record an unknown deep link as a successful session', async () => {
    mount(`robotId=${robotId}&captureSession=${'e'.repeat(32)}`);
    await screen.findByText('当前机器人未找到这次采集，请选择其他记录。');
    expect(vi.mocked(useRecordWorkspaceVisit).mock.calls.at(-1)?.[0].ready).toBe(false);
  });

  it('keeps directory navigation before helper panels and clears selection at the root', async () => {
    mount(`robotId=${robotId}&captureSession=${sessionId}`);
    const panel = await screen.findByRole('region', { name: '整次采集' });
    const nav = screen.getByRole('navigation', { name: '数据集目录导航' });
    expect(nav.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText('下载本数据集内本次采集的已入仓数据；跨日数据请前往对应日期的数据集。')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /地库_2026-11-10/ }));
    const query = new URL(screen.getByTestId('location').textContent!, 'https://warehouse.invalid').searchParams;
    expect(query.get('captureSession')).toBeNull();
    expect(query.get('robotId')).toBe(robotId);
  });
});
