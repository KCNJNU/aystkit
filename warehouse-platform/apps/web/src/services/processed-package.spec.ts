import { describe, expect, it } from 'vitest';
import { isProcessedPackageDestination, PROCESSED_PACKAGE_EXAMPLE, PROCESSED_PACKAGE_REQUIREMENT, requireProcessedPackage } from './processed-package';

const folder = '处理好的数据_20261112_153045';

describe('processed package destination rules', () => {
  it('renders the package example as four lines without literal escape text', () => {
    expect(PROCESSED_PACKAGE_EXAMPLE.split('\n')).toEqual([
      '结果.zip', '├─ 处理前/原图.jpg', '├─ 处理后/结果.jpg', '└─ result.json',
    ]);
    expect(PROCESSED_PACKAGE_EXAMPLE).not.toContain(String.fromCharCode(92, 110));
  });
  it.each([folder, `${folder}/review`, `results/${folder}/review`, '处理好的数据_人工整理'])('recognizes a selected target ancestor: %s', path => {
    expect(isProcessedPackageDestination(path)).toBe(true);
  });
  it.each(['video.mkv', 'scan.json', 'frames.jpg', 'result.rar', 'result.zip.exe'])('rejects loose or non-ZIP uploads before network transfer: %s', name => {
    expect(() => requireProcessedPackage('dataset', folder, name)).toThrow(PROCESSED_PACKAGE_REQUIREMENT);
  });
  it('recognizes folder-selection relative paths, including Windows separators', () => {
    expect(() => requireProcessedPackage('dataset', '', `batch/${folder}/frame.jpg`)).toThrow(PROCESSED_PACKAGE_REQUIREMENT);
    expect(() => requireProcessedPackage('dataset', 'review', `batch\\${folder}\\frame.jpg`)).toThrow(PROCESSED_PACKAGE_REQUIREMENT);
    expect(requireProcessedPackage('dataset', '', `batch/${folder}/result.ZIP`)).toBe(true);
  });
  it.each(['原相机前', '雷达MID360', '处理好的数据', '处理好的数据_', '旧处理好的数据_20261112_153045'])('preserves ordinary source targets: %s', path => {
    expect(requireProcessedPackage('dataset', path, 'video.mkv')).toBe(false);
  });
  it('does not confuse a filename or a similarly named sibling with a target ancestor', () => {
    expect(requireProcessedPackage('dataset', '', `${folder}.jpg`)).toBe(false);
    expect(requireProcessedPackage('dataset', '原相机前', `${folder}.mkv`)).toBe(false);
    expect(requireProcessedPackage('', folder, 'loose.jpg')).toBe(false);
  });
  it('accepts ZIP independently of reported MIME while the server verifies its actual contents', () => {
    expect(requireProcessedPackage('dataset', folder, '结果.zip')).toBe(true);
    expect(requireProcessedPackage('dataset', `${folder}/child`, 'result.ZIP')).toBe(true);
  });
  it.each([8 * 1024 ** 3, 8 * 1024 ** 3 + 1, 64 * 1024 ** 3])('keeps format validation independent of byte size: %s', size => {
    expect(requireProcessedPackage('dataset', folder, 'batch.zip', size)).toBe(true);
    expect(requireProcessedPackage('dataset', '原相机前', 'video.mkv', size)).toBe(false);
  });
});
