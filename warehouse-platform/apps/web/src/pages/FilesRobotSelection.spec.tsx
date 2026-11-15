import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, waitFor } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../services/api';
import { selectRobot } from '../services/robot-selection';
import { Files } from './Files';
vi.mock('../components/SavedViews', () => ({ SavedViews: () => null }));
vi.mock('../services/api', () => ({ api: {
  files: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20 }),
  robots: vi.fn().mockResolvedValue([]), tags: vi.fn().mockResolvedValue([]),
  savedViews: vi.fn().mockResolvedValue([]),
} }));
const clients: QueryClient[] = [];
afterEach(() => { vi.clearAllMocks(); clients.splice(0).forEach(client => client.clear()); selectRobot(''); });
describe('File fleet filter', () => {
  it('never sends the all-robots URL sentinel as a robot UUID', async () => {
    selectRobot('');
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });clients.push(client);
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/files?robotId=all']}><ConfigProvider theme={{ token: { motion: false } }}><Files /></ConfigProvider></MemoryRouter></QueryClientProvider>);
    await waitFor(() => expect(api.files).toHaveBeenCalled());
    expect(vi.mocked(api.files).mock.calls[0][0].robotId).toBeUndefined();
  });
});
