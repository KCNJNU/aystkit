import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AuthProvider, useAuth } from './AuthContext';
import { RequireAuth } from './RouteGuards';
import { getCsrfToken, request, setCsrfToken, type AuthUser } from '../services/api';

const user: AuthUser = { id: 'admin', email: 'admin@example.test', displayName: 'Admin', role: 'SUPER_ADMIN', mustChangePassword: false };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

function Controls() {
  const auth = useAuth();
  return <><p>{auth.loading ? 'Loading session' : auth.user?.displayName ?? 'Signed out'}</p><button onClick={() => void auth.logout()}>Logout</button><button onClick={() => void auth.login('admin@example.test', 'test-password')}>Login</button></>;
}

function mount(protectedRoute = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  client.setQueryData(['private-data'], { secret: 'previous-user' });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/dashboard']}><AuthProvider>{protectedRoute ? <Routes><Route element={<RequireAuth />}><Route path="/dashboard" element={<Controls />} /></Route><Route path="/login" element={<p>Login page</p>} /></Routes> : <Controls />}</AuthProvider></MemoryRouter></QueryClientProvider>);
  return client;
}

afterEach(() => { vi.unstubAllGlobals(); setCsrfToken(); });

describe('Authentication session lifecycle', () => {
  it('leaves the loading screen and redirects a first-time visitor after session 401', async () => {
    const fetch = vi.fn().mockResolvedValue(json({ message: 'Unauthorized' }, 401));
    vi.stubGlobal('fetch', fetch);
    mount(true);
    expect(await screen.findByText('Login page')).toBeVisible();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('publishes an anonymous session on 403 without an undefined query result', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ message: 'Forbidden' }, 403)));
    const client = mount();
    expect(await screen.findByText('Signed out')).toBeVisible();
    expect(client.getQueryState(['auth', 'session'])?.status).toBe('success');
    expect(client.getQueryData(['auth', 'session'])).toBeNull();
  });

  it('updates mounted observers on expiry and clears private data and CSRF', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ user, csrfToken: 'session-token' })).mockResolvedValueOnce(json({ message: 'Unauthorized' }, 401));
    vi.stubGlobal('fetch', fetch);
    const client = mount();
    await screen.findByText('Admin');
    await expect(request('/v1/files')).rejects.toThrow('身份验证失败，请重新登录');
    expect(await screen.findByText('Signed out')).toBeVisible();
    expect(client.getQueryData(['private-data'])).toBeUndefined();
    expect(getCsrfToken()).toBeUndefined();
  });

  it('can log out and sign in again without leaving observers on a removed query', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(json({ user, csrfToken: 'old-token' }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(json({ user: { ...user, displayName: 'New session' }, csrfToken: 'new-token' })));
    const client = mount();
    await screen.findByText('Admin');
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));
    await screen.findByText('Signed out');
    expect(client.getQueryData(['private-data'])).toBeUndefined();
    fireEvent.click(screen.getByRole('button', { name: 'Login' }));
    expect(await screen.findByText('New session')).toBeVisible();
    await waitFor(() => expect(getCsrfToken()).toBe('new-token'));
  });
});
