import { afterEach, describe, expect, it, vi } from 'vitest';
import { startDownloadURL } from './file-download';

describe('shared browser download handoff', () => {
  afterEach(() => vi.restoreAllMocks());

  it('keeps same-origin URLs and a safe timestamped basename without buffering the archive', () => {
    const links: Array<{ href: string; name: string; rel: string; attached: boolean }> = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      links.push({ href: this.href, name: this.download, rel: this.rel, attached: this.isConnected });
    });
    startDownloadURL('/dbprog/folder-packages/ready', '../private\\原相机前_20261102_220100\n.tar.zst');
    expect(links).toEqual([{ href: window.location.origin + '/dbprog/folder-packages/ready',
      name: '原相机前_20261102_220100_.tar.zst', rel: 'noopener noreferrer', attached: true }]);
    expect(document.querySelector('a[download]')).toBeNull();
  });
});
