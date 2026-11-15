import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { describe, expect, it, vi } from 'vitest';
import { FilePreview } from './FilePreview';
import { api } from '../services/api';

vi.mock('./RelocateFileButton', () => ({ RelocateFileButton: () => null }));
vi.mock('./FileReferencesButton', () => ({ FileReferencesButton: () => null }));
vi.mock('./DeleteFilesButton', () => ({ DeleteFilesButton: () => null }));
vi.mock('../services/api', () => ({ api: { file: vi.fn(), preview: vi.fn(), download: vi.fn() } }));

describe('archived video file details', () => {
  it('places download actions in the header and the video preview panel', async () => {
    vi.mocked(api.file).mockResolvedValue({ id: 'video-one', originalName: 'video.mkv', fileType: 'binary',
      mimeType: 'video/x-matroska', fileSize: '113000000', metadata: {} } as never);
    vi.mocked(api.preview).mockResolvedValue({ kind: 'video', url: '/dbprog/objects/video-one' });
    render(<ConfigProvider theme={{ token: { motion: false } }}><QueryClientProvider client={new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })}><FilePreview fileId="video-one" onClose={() => undefined} /></QueryClientProvider></ConfigProvider>);
    expect(await screen.findByText('视频文件，可直接下载')).toBeVisible();
    expect(screen.getAllByRole('button', { name: '下载文件 video.mkv' })).toHaveLength(2);
    expect(api.download).not.toHaveBeenCalled();
  });
});
