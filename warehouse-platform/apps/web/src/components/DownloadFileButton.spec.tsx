import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { message } from 'antd';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DownloadFileButton } from './DownloadFileButton';
import { api } from '../services/api';

vi.mock('../services/api', () => ({ api: { download: vi.fn() } }));

describe('warehouse file download', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); });

  it('requests the authenticated link and streams it through a browser download', async () => {
    vi.mocked(api.download).mockResolvedValue({ url: '/dbprog/objects/file-one' });
    const links: Array<{ href: string; download: string; connected: boolean }> = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      links.push({ href: this.href, download: this.download, connected: this.isConnected });
    });
    render(<DownloadFileButton file={{ id: 'file-one', originalName: 'camera/video.mkv' }} />);
    fireEvent.click(screen.getByRole('button', { name: '下载文件 camera/video.mkv' }));
    await waitFor(() => expect(links).toHaveLength(1));
    expect(api.download).toHaveBeenCalledWith('file-one');
    expect(links[0]).toEqual({ href: window.location.origin + '/dbprog/objects/file-one', download: 'video.mkv', connected: true });
    expect(document.querySelector('a[download]')).toBeNull();
  });

  it('reports an expired or failed download link without opening an empty page', async () => {
    vi.mocked(api.download).mockRejectedValue(new Error('文件暂不可下载'));
    const error = vi.spyOn(message, 'error').mockImplementation(() => undefined as never);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    render(<DownloadFileButton file={{ id: 'file-one', originalName: 'video.mkv' }} />);
    fireEvent.click(screen.getByRole('button', { name: '下载文件 video.mkv' }));
    await waitFor(() => expect(error).toHaveBeenCalledWith('文件暂不可下载'));
    expect(click).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '下载文件 video.mkv' })).not.toHaveClass('ant-btn-loading');
  });

  it.each(['javascript:alert(1)', '', undefined, 'https://user:password@example.com/file'])('rejects an invalid download response (%s)', async (url) => {
    vi.mocked(api.download).mockResolvedValue({ url } as { url: string });
    const error = vi.spyOn(message, 'error').mockImplementation(() => undefined as never);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    render(<DownloadFileButton file={{ id: 'file-one', originalName: 'video.mkv' }} />);
    fireEvent.click(screen.getByRole('button', { name: '下载文件 video.mkv' }));
    await waitFor(() => expect(error).toHaveBeenCalledWith('下载地址无效，请刷新后重试'));
    expect(click).not.toHaveBeenCalled();
  });
});
