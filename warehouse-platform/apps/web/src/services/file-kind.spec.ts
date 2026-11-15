import { describe, expect, it } from 'vitest';
import { fileKindLabel, isVideoFile } from './file-kind';

describe('stored video display', () => {
  it('recognizes existing MKV records classified as binary', () => {
    const file = { originalName: 'capture/VIDEO.MKV', fileType: 'binary', mimeType: 'application/octet-stream' };
    expect(isVideoFile(file)).toBe(true);
    expect(fileKindLabel(file)).toBe('视频');
  });

  it('recognizes a video MIME while retaining other file classifications', () => {
    expect(isVideoFile({ originalName: 'capture', mimeType: 'video/x-matroska' })).toBe(true);
    expect(fileKindLabel({ originalName: 'metadata.json', fileType: 'json' })).toBe('json');
    expect(isVideoFile({ originalName: 'video.mkv.json', fileType: 'json' })).toBe(false);
  });
});
