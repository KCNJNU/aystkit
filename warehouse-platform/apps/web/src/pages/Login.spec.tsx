import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Login } from './Login';
const { login } = vi.hoisted(() => ({ login: vi.fn() }));
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ login }) }));

describe('Login account input', () => {
  beforeEach(() => { login.mockReset().mockResolvedValue({ mustChangePassword: false, role: 'ADMIN' }); });
  it.each(['admin', 'user@example.com'])('submits %s and opens datasets', async account => {
    render(<MemoryRouter initialEntries={['/login']}><Routes><Route path="/login" element={<Login />} /><Route path="/datasets" element={<p>Datasets</p>} /></Routes></MemoryRouter>);
    fireEvent.change(screen.getByLabelText('账号或邮箱'), { target: { value: account } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'test-password' } });
    fireEvent.click(screen.getByRole('button', { name: '进入数仓平台' }));
    await waitFor(() => expect(login).toHaveBeenCalledWith(account, 'test-password'));
    expect(await screen.findByText('Datasets')).toBeVisible();
  });
  it('opens the AI assistant for a basic account without dataset access', async () => {
    login.mockResolvedValue({ mustChangePassword: false, role: 'BASIC' });
    render(<MemoryRouter initialEntries={['/login']}><Routes><Route path="/login" element={<Login />} /><Route path="/ai" element={<p>AI assistant</p>} /></Routes></MemoryRouter>);
    fireEvent.change(screen.getByLabelText('账号或邮箱'), { target: { value: 'basic' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'test-password' } });
    fireEvent.click(screen.getByRole('button', { name: '进入数仓平台' }));
    expect(await screen.findByText('AI assistant')).toBeVisible();
  });
});
