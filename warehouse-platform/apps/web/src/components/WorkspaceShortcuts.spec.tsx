import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { MemoryRouter, Outlet, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { selectRobot } from '../services/robot-selection';
import { createWorkspaceShortcutsStore, makeWorkspaceShortcut, workspaceShortcutsKey } from '../services/workspace-shortcuts';
import { useRobotSelection } from './useRobotSelection';
import { WorkspaceFavoriteButton, WorkspaceShortcutsProvider, WorkspaceShortcutsTrigger, useRecordWorkspaceVisit } from './WorkspaceShortcuts';

const datasetId = '00000000-0000-4000-8000-000000000001';
const robotA = '00000000-0000-4000-8000-000000000002';
const robotB = '00000000-0000-4000-8000-000000000003';
const clients: QueryClient[] = [];
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); selectRobot(''); });
afterEach(() => clients.splice(0).forEach(client => client.clear()));

function Workspace() {
  const { datasetId: id } = useParams();
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  const ready = params.get('fail') !== 'true';
  useRecordWorkspaceVisit({ ready, datasetId: id, datasetName: '地库数据', path: params.get('path') ?? '', captureSession: params.get('captureSession') ?? undefined, sessionLabel: params.has('captureSession') ? '16:00 – 16:02' : undefined });
  return <WorkspaceFavoriteButton />;
}
function Layout() {
  const [account, setAccount] = useState('alice');
  const selection = useRobotSelection();
  const navigate = useNavigate();
  const location = useLocation();
  return <WorkspaceShortcutsProvider userId={account} robots={[{ id: robotA, name: '一号车' }, { id: robotB, name: '二号车' }]}>
    <WorkspaceShortcutsTrigger compact />
    <button onClick={() => navigate(`/datasets/${datasetId}?robotId=${robotB}&fail=true`)}>打开失败目录</button>
    <button onClick={() => setAccount(account === 'alice' ? 'bob' : 'alice')}>切换账号</button>
    <output data-testid="route">{location.pathname + location.search}</output><output data-testid="scope">{selection.robotId || 'all'}</output>
    {!selection.transitioning && <Outlet key={selection.robotId} />}
  </WorkspaceShortcutsProvider>;
}
function show(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[entry]}><Routes><Route element={<Layout />}><Route path="/datasets/:datasetId" element={<Workspace />} /></Route></Routes></MemoryRouter></QueryClientProvider>);
}

it('records successful visits only, saves a folder, and restores its robot from a failed route', async () => {
  show(`/datasets/${datasetId}?robotId=${robotA}&path=${encodeURIComponent('前相机/视频')}`);
  const favorite = await screen.findByRole('button', { name: '收藏当前位置' });
  await waitFor(() => expect(favorite).toBeEnabled());
  fireEvent.click(favorite);
  expect(await screen.findByRole('button', { name: '取消收藏当前位置' })).toHaveAttribute('aria-pressed', 'true');
  fireEvent.click(screen.getByRole('button', { name: '打开失败目录' }));
  await waitFor(() => expect(screen.getByTestId('scope')).toHaveTextContent(robotB));
  expect(createWorkspaceShortcutsStore('alice').getSnapshot().recent).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '收藏与最近' }));
  const drawer = await screen.findByRole('dialog');
  expect(within(drawer).getByText('一号车')).toBeInTheDocument();
  fireEvent.click(within(drawer).getByText('视频').closest('button')!);
  await waitFor(() => expect(screen.getByTestId('scope')).toHaveTextContent(robotA));
  expect(screen.getByTestId('route')).toHaveTextContent('path=%E5%89%8D%E7%9B%B8%E6%9C%BA%2F%E8%A7%86%E9%A2%91');
  expect(screen.getByTestId('route')).not.toHaveTextContent('fail');
});

it('restores explicit all and capture session, supports recent removal, and hides other account favorites', async () => {
  const saved = makeWorkspaceShortcut({ datasetId, datasetName: '地库数据', captureSession: 'a'.repeat(32), sessionLabel: '16:00 – 16:02' }, 'all')!;
  createWorkspaceShortcutsStore('alice').toggleFavorite(saved);
  show(`/datasets/${datasetId}?robotId=${robotB}&fail=true`);
  fireEvent.click(screen.getByRole('button', { name: '收藏与最近' }));
  fireEvent.click((await screen.findByText('16:00 – 16:02')).closest('button')!);
  await waitFor(() => expect(screen.getByTestId('scope')).toHaveTextContent('all'));
  expect(screen.getByTestId('route')).toHaveTextContent(`captureSession=${'a'.repeat(32)}`);
  await waitFor(() => expect(createWorkspaceShortcutsStore('alice').getSnapshot().recent).toHaveLength(1));
  fireEvent.click(screen.getByRole('button', { name: '收藏与最近' }));
  const drawer = await screen.findByRole('dialog');
  fireEvent.click(within(drawer).getByRole('tab', { name: /最近/ }));
  fireEvent.click(within(drawer).getByRole('button', { name: /移除记录/ }));
  expect(await within(drawer).findByText('暂无访问记录')).toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole('button', { name: 'Close' }));
  fireEvent.click(screen.getByRole('button', { name: '切换账号' }));
  fireEvent.click(screen.getByRole('button', { name: '收藏与最近' }));
  const otherDrawer = await screen.findByRole('dialog');
  fireEvent.click(within(otherDrawer).getByRole('tab', { name: /收藏/ }));
  expect(await within(otherDrawer).findByText('暂无收藏')).toBeInTheDocument();
  expect(JSON.parse(localStorage.getItem(workspaceShortcutsKey('alice'))!).favorites).toHaveLength(1);
});
