import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SystemSettings } from './SystemSettings';
import { api } from '../services/api';

vi.mock('../services/api', () => ({ api: { systemConfiguration: vi.fn(), updateSettings: vi.fn() } }));
const settings = {
  'upload.jsonMaxBytes': 0, 'upload.imageMaxBytes': 0, 'upload.pcdMaxBytes': 0,
  'upload.mapMaxBytes': 0, 'upload.yamlMaxBytes': 0, 'upload.logMaxBytes': 0,
  'download.presignedTtlSeconds': 900, 'jobs.workerConcurrency': 2,
  'notifications.jobSuccessEnabled': true,
};
function mount() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SystemSettings /></QueryClientProvider>);
}
describe('Optional per-file upload policies', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.systemConfiguration).mockResolvedValue({ settings } as never);
    vi.mocked(api.updateSettings).mockResolvedValue(settings);
  });
  it('saves zero policies without imposing the former 2 GiB ceiling', async () => {
    mount();
    const field = await screen.findByRole('spinbutton', { name: '点云文件' });
    expect(field).toHaveValue('0');
    fireEvent.click(screen.getByRole('button', { name: /保存设置/ }));
    await waitFor(() => expect(api.updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      'upload.pcdMaxBytes': 0, 'upload.logMaxBytes': 0,
    }), expect.anything()));
  });
  it('can save an explicit policy greater than 2 GiB', async () => {
    mount();
    const field = await screen.findByRole('spinbutton', { name: '日志与文本文件' });
    fireEvent.change(field, { target: { value: '5368709120' } });
    fireEvent.blur(field);
    fireEvent.click(screen.getByRole('button', { name: /保存设置/ }));
    await waitFor(() => expect(api.updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      'upload.logMaxBytes': 5368709120,
    }), expect.anything()));
  });
});
