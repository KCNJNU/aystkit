import { ConflictException } from '@nestjs/common';
import { execFileSync } from 'node:child_process';
import * as fsPromises from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { PassThrough, Readable } from 'node:stream';
import type { ObjectStorage } from '../storage/object-storage';
import { assertProcessedPackageName, isProcessedFolder, validateProcessedPackage } from './processed-package';

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=', 'base64');
type GeneratedText = { prefix: string; repeat: string; count: number; suffix: string };
type Entry = { name: string; content?: Buffer | string | GeneratedText; symlink?: boolean };
const before = (name = '处理前/one.png'): Entry => ({ name, content: PNG });
const after = (name = '处理后/one.png'): Entry => ({ name, content: PNG });
const metadata = (content: Buffer | string | GeneratedText = '{"scene":"test"}', name = 'result.json'): Entry => ({ name, content });
const fixtureProgram = String.raw`
import base64, io, json, stat, sys, zipfile
value = json.loads(sys.argv[1])
if value.get('zip64'):
    zipfile.ZIP64_LIMIT = 0
result = io.BytesIO()
with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED if value.get('deflate') else zipfile.ZIP_STORED) as archive:
    for entry in value['entries']:
        if 'generated' in entry:
            text = entry['generated']
            data = (text['prefix'] + text['repeat'] * text['count'] + text['suffix']).encode('utf-8')
        else:
            data = base64.b64decode(entry['content'])
        info = zipfile.ZipInfo(entry['name'])
        info.compress_type = zipfile.ZIP_DEFLATED if value.get('deflate') else zipfile.ZIP_STORED
        if entry.get('symlink'):
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, data)
sys.stdout.buffer.write(result.getvalue())
`;

function zip(entries: Entry[] = [before(), after(), metadata()], options: { deflate?: boolean; zip64?: boolean } = {}): Buffer {
  const input = { ...options, entries: entries.map(entry => ({ ...entry, ...(typeof entry.content === 'object' && !Buffer.isBuffer(entry.content)
    ? { content: undefined, generated: entry.content } : { content: Buffer.from(entry.content ?? '').toString('base64') }) })) };
  return execFileSync('python3', ['-I', '-B', '-c', fixtureProgram, JSON.stringify(input)], { maxBuffer: 64 * 1024 ** 2, timeout: 15_000, stdio: ['ignore', 'pipe', 'pipe'] });
}

function storageFor(content: Buffer) {
  const chunkSize = Math.max(17, Math.min(65536, Math.ceil(content.length / 100)));
  return { getObject: jest.fn(async () => Readable.from((function* () { for (let offset = 0; offset < content.length; offset += chunkSize) yield content.subarray(offset, offset + chunkSize); })())) };
}

async function verify(content: Buffer, size = content.length) {
  return validateProcessedPackage(storageFor(content) as unknown as ObjectStorage, 'private-object-key', size);
}

describe('processed package directory policy', () => {
  it.each(['处理好的数据_20261112_153045', 'session/处理好的数据_20261112_153045/nested', '处理好的数据_自定义批次'])('recognizes directory %s and its descendants', path => {
    expect(isProcessedFolder(path)).toBe(true);
  });

  it.each(['', '原相机前/20261112', 'other_处理好的数据_20261112_153045', '处理好的数据_'])('does not restrict ordinary directory %s', path => {
    expect(isProcessedFolder(path)).toBe(false);
    expect(() => assertProcessedPackageName(path, 'camera.mkv')).not.toThrow();
  });

  it('checks extensions only in processing directories and accepts uppercase ZIP', () => {
    const path = '处理好的数据_20261112_153045';
    expect(() => assertProcessedPackageName(path, 'result.ZIP')).not.toThrow();
    expect(() => assertProcessedPackageName(path, 'result.zip.exe')).toThrow('仅支持上传 ZIP');
    expect(() => assertProcessedPackageName('', '处理好的数据_20261112_153045.mkv')).not.toThrow();
  });
});

describe('processed ZIP content verification', () => {
  it.each([false, true])('reads a real ZIP with Chinese directories, JSON and deflate=%s', async deflate => {
    await expect(verify(zip([before(), after(), metadata()], { deflate }))).resolves.toEqual({ beforeImages: 1, afterImages: 1, jsonFiles: 1 });
  });

  it('accepts ZIP64 directories even when the small EOCD fields still fit', async () => {
    const content = zip(undefined, { zip64: true });
    expect(content.includes(Buffer.from([0x50, 0x4b, 0x06, 0x06]))).toBe(true);
    await expect(verify(content)).resolves.toEqual({ beforeImages: 1, afterImages: 1, jsonFiles: 1 });
  });

  it('supports public top-level folders, original/processed roles and JSON without a schema', async () => {
    await expect(verify(zip([before('test-001/original/a.png'), after('test-001/processed/b.png'), metadata('[1,true,null]', 'test-001/metadata.json')]))).resolves.toEqual({ beforeImages: 1, afterImages: 1, jsonFiles: 1 });
  });

  it('allows explicit before/after file prefixes for a small flat package', async () => {
    await expect(verify(zip([before('before_a.png'), after('after_a.png'), metadata()]))).resolves.toEqual({ beforeImages: 1, afterImages: 1, jsonFiles: 1 });
  });

  it.each([
    [[after(), metadata()], '处理前图像'],
    [[before(), metadata()], '处理后图像'],
    [[before(), after()], '.json 文件'],
    [[before('image_a.png'), after('image_b.png'), metadata()], '处理前图像'],
    [[], '处理前图像'],
  ] as [Entry[], string][])('reports missing package components', async (entries, missing) => {
    await expect(verify(zip(entries))).rejects.toThrow(missing);
  });

  it.each(['{bad json', '{"x":NaN}', '{"x":Infinity}', ''])('rejects invalid JSON %s', async text => {
    await expect(verify(zip([before(), after(), metadata(text)]))).rejects.toThrow('有效的 UTF-8 JSON');
  });

  it('rejects invalid UTF-8 JSON and identifies the member', async () => {
    await expect(verify(zip([before(), after(), metadata(Buffer.from([0xff]), 'wrong.json')]))).rejects.toThrow('wrong.json');
  });

  it('accepts a UTF-8 BOM produced by desktop JSON tools', async () => {
    await expect(verify(zip([before(), after(), metadata('\ufeff{"valid":true}')]))).resolves.toMatchObject({ jsonFiles: 1 });
  });

  it.each([Buffer.from('a photograph'), Buffer.alloc(0), PNG.subarray(0, 24)])('rejects text, empty or truncated files renamed as PNG', async content => {
    await expect(verify(zip([{ ...before(), content }, after(), metadata()]))).rejects.toThrow('图像内容与格式不符');
  });

  it('rejects an image with conflicting before/after roles', async () => {
    await expect(verify(zip([before('before/after/a.png'), after(), metadata()]))).rejects.toThrow('同时被标为处理前和处理后');
  });

  it.each(['../before/a.png', '/before/a.png', 'C:/before/a.png', 'folder\\before\\a.png', 'before/../a.png', 'before//a.png'])('rejects unsafe archive path %s', async name => {
    await expect(verify(zip([before(name), after(), metadata()]))).rejects.toThrow('不安全的文件路径');
  });

  it('rejects a NUL byte embedded in a real archive member name', async () => {
    const name = 'before/a.pngXignored';
    const content = zip([before(name), after(), metadata()]);
    let offset = content.indexOf(Buffer.from(name));
    while (offset >= 0) {
      content[offset + 'before/a.png'.length] = 0;
      offset = content.indexOf(Buffer.from(name), offset + 1);
    }
    await expect(verify(content)).rejects.toThrow('不安全的文件路径');
  });

  it('rejects duplicate normalized paths including case-only differences', async () => {
    await expect(verify(zip([before('before/a.png'), before('BEFORE/A.PNG'), after(), metadata()]))).rejects.toThrow('重复的文件或目录路径');
  });

  it('rejects Unicode-normalized duplicate paths', async () => {
    await expect(verify(zip([before('before/é.png'), before('before/e\u0301.png'), after(), metadata()]))).rejects.toThrow('重复的文件或目录路径');
  });

  it('rejects symlinks rather than reading them as images', async () => {
    await expect(verify(zip([{ name: 'before/link.png', content: 'target', symlink: true }, after(), metadata()]))).rejects.toThrow('符号链接或特殊文件');
  });

  it('does not count or recursively unpack nested archives', async () => {
    await expect(verify(zip([before(), after(), metadata(), { name: 'nested.zip', content: zip() }]))).rejects.toThrow('不支持嵌套压缩包');
  });

  it('rejects CRC damage before accepting the file', async () => {
    const content = zip();
    const location = content.indexOf(PNG);
    expect(location).toBeGreaterThan(0);
    content[location + 25] ^= 1;
    await expect(verify(content)).rejects.toThrow('校验失败');
  });

  it('rejects encrypted entries before trying to read them', async () => {
    const content = zip();
    content.writeUInt16LE(content.readUInt16LE(6) | 1, 6);
    const central = content.indexOf(Buffer.from([0x50, 0x4b, 0x01, 0x02]));
    content.writeUInt16LE(content.readUInt16LE(central + 8) | 1, central + 8);
    await expect(verify(content)).rejects.toThrow('不支持加密 ZIP');
  });

  it('rejects unsupported compression without attempting decompression', async () => {
    const content = zip();
    const central = content.indexOf(Buffer.from([0x50, 0x4b, 0x01, 0x02]));
    content.writeUInt16LE(99, central + 10);
    await expect(verify(content)).rejects.toThrow('仅支持标准存储或 Deflate');
  });

  it('checks actual archive integrity instead of imposing the former 1 GiB member limit', async () => {
    const content = zip();
    const central = content.indexOf(Buffer.from([0x50, 0x4b, 0x01, 0x02]));
    content.writeUInt32LE(1024 ** 3 + 1, central + 24);
    content.writeUInt32LE(1024 ** 3, central + 20);
    await expect(verify(content)).rejects.toThrow('校验失败');
  });

  it('streams JSON with a single string exceeding 16 MiB without a whole-value accumulator', async () => {
    const content = zip([before(), after(), metadata({ prefix: '{"large":"', repeat: 'x', count: 17 * 1024 ** 2, suffix: '"}' })]);
    await expect(verify(content)).resolves.toEqual({ beforeImages: 1, afterImages: 1, jsonFiles: 1 });
  }, 30_000);

  it('rejects invalid JSON after a large streamed value has already passed', async () => {
    const content = zip([before(), after(), metadata({ prefix: '{"large":"', repeat: 'x', count: 17 * 1024 ** 2, suffix: '"} trailing' })]);
    await expect(verify(content)).rejects.toThrow('有效的 UTF-8 JSON');
  }, 30_000);

  it.each(['\u00a0{"valid":true}', '{"valid":true}\ufeff', '\f{}', '{}\u2028'])('rejects non-RFC whitespace accepted by a permissive streaming parser', async text => {
    await expect(verify(zip([before(), after(), metadata(text)]))).rejects.toThrow('有效的 UTF-8 JSON');
  });

  it('accepts Unicode whitespace inside JSON strings and escaped quotes', async () => {
    await expect(verify(zip([before(), after(), metadata('{"text":"\u00a0\\"\u2028\ufeff"}')]))).resolves.toMatchObject({ jsonFiles: 1 });
  });

  it('rejects unsafe JSON nesting depth rather than limiting JSON byte size', async () => {
    await expect(verify(zip([before(), after(), metadata('['.repeat(1025) + '0' + ']'.repeat(1025))]))).rejects.toThrow('嵌套层级超过 1024');
  });

  it('rejects excessive file counts before loading the ZIP index', async () => {
    const content = zip();
    const end = content.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
    content.writeUInt16LE(20001, end + 8);
    content.writeUInt16LE(20001, end + 10);
    await expect(verify(content)).rejects.toThrow('数量超过 20000');
  });

  it('rejects archives with an excessive declared expansion ratio', async () => {
    const content = zip();
    const central = content.indexOf(Buffer.from([0x50, 0x4b, 0x01, 0x02]));
    content.writeUInt32LE(1024 * 1024, central + 24);
    content.writeUInt32LE(1, central + 20);
    await expect(verify(content)).rejects.toThrow('压缩倍率超过 1000');
  });

  it('rejects a split-volume archive', async () => {
    const content = zip();
    const end = content.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
    content.writeUInt16LE(1, end + 4);
    await expect(verify(content)).rejects.toThrow('不支持分卷 ZIP');
  });

  it.each([Buffer.from('not a zip'), zip().subarray(0, -8)])('rejects a renamed or truncated archive', async content => {
    await expect(verify(content)).rejects.toBeInstanceOf(ConflictException);
  });

  it.each([0, -1, Number.NaN, 1.5, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])('rejects invalid compressed size %s before fetching the object', async size => {
    const storage = storageFor(zip());
    await expect(validateProcessedPackage(storage as unknown as ObjectStorage, 'object', size)).rejects.toThrow('安全整数');
    expect(storage.getObject).not.toHaveBeenCalled();
  });

  it('does not reject an above-8-GiB declaration before reading its actual stream', async () => {
    const space = jest.spyOn(fsPromises, 'statfs')
      .mockResolvedValue({ bavail: 1024n ** 4n, bsize: 4096n } as never);
    const content = zip();
    const storage = storageFor(content);
    try {
      await expect(validateProcessedPackage(storage as unknown as ObjectStorage, 'object', 8 * 1024 ** 3 + 1)).rejects.toThrow('实际大小与上传声明不一致');
      expect(storage.getObject).toHaveBeenCalledTimes(1);
    } finally { space.mockRestore(); }
  });

  it('checks physical temporary capacity before downloading, with no per-file size policy', async () => {
    const space = jest.spyOn(fsPromises, 'statfs')
      .mockResolvedValue({ bavail: 512n * 1024n ** 2n, bsize: 1n } as never);
    const storage = storageFor(zip());
    try {
      await expect(validateProcessedPackage(storage as unknown as ObjectStorage, 'object', 1024)).rejects.toThrow('服务器临时空间不足');
      expect(storage.getObject).not.toHaveBeenCalled();
    } finally { space.mockRestore(); }
  });

  it('allows an active transfer to exceed 120 seconds of total elapsed time', async () => {
    const content = zip();
    const source = new PassThrough();
    let ready!: () => void;
    const opened = new Promise<void>(resolve => { ready = resolve; });
    const storage = { getObject: async () => { ready(); return source; } };
    jest.useFakeTimers({ doNotFake: ['nextTick', 'setImmediate'] });
    const work = validateProcessedPackage(storage as unknown as ObjectStorage, 'object', content.length)
      .then(result => ({ result }), error => ({ error }));
    try {
      await opened;
      await new Promise<void>(resolve => setImmediate(resolve));
      jest.advanceTimersByTime(119_000);
      source.write(content.subarray(0, 32));
      await new Promise<void>(resolve => setImmediate(resolve));
      jest.advanceTimersByTime(119_000);
      source.end(content.subarray(32));
      const completed = await work;
      expect(completed).toEqual({ result: { beforeImages: 1, afterImages: 1, jsonFiles: 1 } });
    } finally {
      source.destroy();
      jest.useRealTimers();
      await work;
    }
  });

  it('times out an object request only when it makes no progress', async () => {
    let ready!: () => void;
    const opened = new Promise<void>(resolve => { ready = resolve; });
    const storage = { getObject: () => { ready(); return new Promise<Readable>(() => undefined); } };
    jest.useFakeTimers({ doNotFake: ['nextTick', 'setImmediate'] });
    const work = validateProcessedPackage(storage as unknown as ObjectStorage, 'object', 100)
      .then(result => ({ result }), error => ({ error }));
    try {
      await opened;
      jest.advanceTimersByTime(120_000);
      const completed = await work;
      expect(completed).toEqual({ error: expect.objectContaining({ message: expect.stringContaining('长时间没有进度') }) });
    } finally { jest.useRealTimers(); }
  });

  it('detects a short stream and a stream larger than the declared size', async () => {
    const content = zip();
    await expect(verify(content, content.length + 1)).rejects.toThrow('实际大小与上传声明不一致');
    await expect(verify(content, content.length - 1)).rejects.toThrow('实际大小超过上传声明');
  });

  it('removes private spool directories after successful and rejected validation', async () => {
    const existing = (await fsPromises.readdir(tmpdir())).filter(name => name.startsWith('warehouse-processed-')).sort();
    await verify(zip());
    await expect(verify(zip([before(), after()]))).rejects.toThrow('.json 文件');
    const remaining = (await fsPromises.readdir(tmpdir())).filter(name => name.startsWith('warehouse-processed-')).sort();
    expect(remaining).toEqual(existing);
  });
});
