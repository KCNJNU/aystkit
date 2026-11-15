import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RequireRole } from '../auth/RouteGuards';
import { auditRoles, readRoles } from '../auth/permissions';
import { api, type UserRole } from '../services/api';
import { TasksAndAudit } from './TasksAndAudit';

const access = vi.hoisted(() => ({ role: 'SUPER_ADMIN' as UserRole }));
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({
  hasRole: (...roles: UserRole[]) => roles.includes(access.role),
}) }));
vi.mock('../services/api', () => ({
  apiBase: '/nest',
  api: { jobs: vi.fn(), auditLogs: vi.fn() },
}));

function Location() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}
function mount(entry = '/jobs') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[entry]}>
    <Location />
    <Routes>
      <Route element={<RequireRole roles={readRoles} />}><Route path="/jobs" element={<TasksAndAudit />} /></Route>
      <Route element={<RequireRole roles={auditRoles} />}><Route path="/audit-logs" element={<TasksAndAudit />} /></Route>
    </Routes>
  </MemoryRouter></QueryClientProvider>);
}

describe('Tasks and audit workspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    access.role = 'SUPER_ADMIN';
    vi.mocked(api.jobs).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20 });
    vi.mocked(api.auditLogs).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 100 });
  });

  it('keeps job filters in existing links and only loads the active content', async () => {
    mount('/jobs?page=2&status=FAILED');
    await waitFor(() => expect(api.jobs).toHaveBeenCalledWith({ page: 2, pageSize: 20, status: 'FAILED' }));
    expect(api.auditLogs).not.toHaveBeenCalled();
    expect(screen.getByRole('tab', { name: /数据处理任务/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: /审计日志/ })).toBeInTheDocument();
  });

  it('switches between existing task and audit URLs with working audit export', async () => {
    mount();
    await screen.findByText('暂无后台任务');
    fireEvent.click(screen.getByRole('tab', { name: /审计日志/ }));
    await waitFor(() => expect(api.auditLogs).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId('location')).toHaveTextContent('/audit-logs');
    expect(screen.getByRole('link', { name: '导出 CSV' })).toHaveAttribute('href', '/nest/api/v1/audit-logs/export');
    expect(screen.queryByRole('heading', { name: '数据处理任务' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: /数据处理任务/ }));
    expect(screen.getByTestId('location')).toHaveTextContent('/jobs');
    expect(screen.queryByRole('link', { name: '导出 CSV' })).not.toBeInTheDocument();
  });

  it('opens an existing audit link without fetching jobs', async () => {
    mount('/audit-logs');
    await screen.findByText('暂无审计记录');
    expect(api.jobs).not.toHaveBeenCalled();
    expect(screen.getByRole('tab', { name: /审计日志/ })).toHaveAttribute('aria-selected', 'true');
  });

  it.each<UserRole>(['PROJECT_ADMIN', 'OPERATOR', 'VIEWER', 'MAINTENANCE_ENGINEER'])('keeps jobs accessible to %s without fetching audit records', async (role) => {
    access.role = role;
    mount();
    await screen.findByText('暂无后台任务');
    expect(screen.queryByRole('tab', { name: /审计日志/ })).not.toBeInTheDocument();
    expect(api.auditLogs).not.toHaveBeenCalled();
  });

  it('continues rejecting unauthorized audit deep links', () => {
    access.role = 'VIEWER';
    mount('/audit-logs');
    expect(screen.getByText('权限不足')).toBeInTheDocument();
    expect(api.auditLogs).not.toHaveBeenCalled();
    expect(api.jobs).not.toHaveBeenCalled();
  });

  it('does not give BASIC users task access through the merged workspace', () => {
    access.role = 'BASIC';
    mount('/jobs');
    expect(screen.getByText('权限不足')).toBeInTheDocument();
    expect(api.jobs).not.toHaveBeenCalled();
    expect(api.auditLogs).not.toHaveBeenCalled();
  });
});
