import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { UserRole } from './services/api';
import { rememberObserverRobot, selectRobot } from './services/robot-selection';
import { App } from './App';

const session = vi.hoisted(() => ({ role: 'ADMIN' as UserRole }));

vi.mock('./auth/AuthContext', () => ({
  useAuth: () => ({
    authenticated: true,
    loading: false,
    user: { id: 'route-user', role: session.role, mustChangePassword: false },
    hasRole: (...roles: UserRole[]) => roles.includes(session.role),
  }),
}));
vi.mock('./components/AppLayout', async () => {
  const { Outlet } = await import('react-router-dom');
  const { useRobotSelection } = await import('./components/useRobotSelection');
  return { AppLayout: () => {
    const scope = useRobotSelection();
    return <main aria-label="数仓布局">{!scope.transitioning && <Outlet key={scope.robotId} />}</main>;
  } };
});
vi.mock('./pages/UploadCenter', () => ({ UploadCenter: () => <h1>上传入口</h1> }));
vi.mock('./pages/Datasets', () => ({ Datasets: () => <h1>数据集入口</h1> }));
vi.mock('./pages/AIAssistant', () => ({ AIAssistant: () => <h1>AI入口</h1> }));

const robotId = '00000000-0000-4000-8000-000000000001';
const rememberedRobotId = '00000000-0000-4000-8000-000000000002';
const legacyLink = `/garage-archive/recordings/old-session?robotId=${robotId}`;
const clients: QueryClient[] = [];

function RouteProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output aria-label="路由位置">{location.pathname}{location.search}</output><button onClick={() => navigate(-1)}>返回上一页</button></>;
}

function mount(entries = [legacyLink]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}><RouteProbe /><App /></MemoryRouter></QueryClientProvider>);
}

beforeEach(() => {
  session.role = 'ADMIN';
  sessionStorage.clear();
  selectRobot(rememberedRobotId);
});

afterEach(() => clients.splice(0).forEach(client => client.clear()));

it.each([
  { role: 'ADMIN', path: '/upload', page: '上传入口' },
  { role: 'OPERATOR', path: '/upload', page: '上传入口' },
  { role: 'VIEWER', path: '/datasets', page: '数据集入口' },
  { role: 'BASIC', path: '/ai', page: 'AI入口' },
] as const)('redirects the retired garage link for $role to $path and retains its explicit robot', async ({ role, path, page }) => {
  session.role = role;
  mount();

  expect(await screen.findByRole('heading', { name: page })).toBeVisible();
  expect(screen.getByLabelText('路由位置')).toHaveTextContent(`${path}?robotId=${robotId}`);
  expect(screen.getByLabelText('路由位置')).not.toHaveTextContent(rememberedRobotId);
});

it('replaces the retired link so browser back returns to the preceding page', async () => {
  mount(['/ai?robotId=all', legacyLink]);
  await screen.findByRole('heading', { name: '上传入口' });

  fireEvent.click(screen.getByRole('button', { name: '返回上一页' }));

  expect(await screen.findByRole('heading', { name: 'AI入口' })).toBeVisible();
  await waitFor(() => expect(screen.getByLabelText('路由位置')).toHaveTextContent('/ai?robotId=all'));
  expect(screen.queryByRole('heading', { name: '上传入口' })).not.toBeInTheDocument();
});

it.each([
  { legacyPath: '/garage-archive', robot: rememberedRobotId },
  { legacyPath: '/garage-archive?robotId=all', robot: 'all' },
])('uses the correct robot scope for $legacyPath', async ({ legacyPath, robot }) => {
  mount([legacyPath]);

  expect(await screen.findByRole('heading', { name: '上传入口' })).toBeVisible();
  expect(screen.getByLabelText('路由位置')).toHaveTextContent(`/upload?robotId=${robot}`);
});

it('completes the redirect when the layout canonicalizes an observer alias from session storage', async () => {
  // The old link lacks observerRobot, while the remembered current robot already
  // has an alias. Nesting this redirect inside AppLayout races its Navigate
  // effect against useRobotSelection replacing the old URL with a canonical one.
  selectRobot(robotId);
  rememberObserverRobot(robotId, 'garage-robot-one');
  expect(new URL(legacyLink, 'https://warehouse.test').searchParams.has('observerRobot')).toBe(false);
  mount();

  expect(await screen.findByRole('heading', { name: '上传入口' })).toBeVisible();
  const destination = new URL(screen.getByLabelText('路由位置').textContent!, 'https://warehouse.test');
  expect(destination.pathname).toBe('/upload');
  expect(destination.searchParams.get('robotId')).toBe(robotId);
  expect(destination.searchParams.get('observerRobot')).toBe('garage-robot-one');
  expect(screen.getByRole('main', { name: '数仓布局' })).toContainElement(screen.getByRole('heading', { name: '上传入口' }));
});
