"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.isProcessedFolder = isProcessedFolder;
exports.assertProcessedPackageName = assertProcessedPackageName;
exports.validateProcessedPackage = validateProcessedPackage;
const common_1 = require("@nestjs/common");
const node_child_process_1 = require("node:child_process");
const node_fs_1 = require("node:fs");
const promises_1 = require("node:fs/promises");
const node_os_1 = require("node:os");
const node_path_1 = require("node:path");
const node_stream_1 = require("node:stream");
const promises_2 = require("node:stream/promises");
const VALIDATION_IDLE_MS = 120_000;
const TEMPORARY_RESERVE_BYTES = 512n * 1024n ** 2n;
const SPACE_CHECK_BYTES = 64 * 1024 ** 2;
const PREFIX = '处理好的数据_';
function isProcessedFolder(folderPath) {
    return folderPath.split('/').some(part => part.startsWith(PREFIX) && part.length > PREFIX.length);
}
function assertProcessedPackageName(folderPath, name) {
    if (isProcessedFolder(folderPath) && (0, node_path_1.extname)(name).toLowerCase() !== '.zip') {
        throw new common_1.ConflictException('处理好的数据目录仅支持上传 ZIP 压缩包；包内须包含处理前图像、处理后图像和有效的 .json 文件');
    }
}
const JSON_VALIDATOR = String.raw `
const fs = require('node:fs');
const { Writable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const Parser = require(process.argv[1]);
let depth = 0, last = 0;
const pulse = () => {
  const now = Date.now();
  if (now - last >= 1000) { process.stderr.write('.'); last = now; }
};
pulse();
const source = fs.createReadStream(process.argv[2], { highWaterMark: 65536 });
source.on('data', pulse);
const parser = Parser.make({ packValues: false, packKeys: false, packStrings: false, packNumbers: false });
const drain = new Writable({ objectMode: true, write(token, _encoding, done) {
  if (token.name === 'startObject' || token.name === 'startArray') {
    if (++depth > 1024) { done(new Error('JSON nesting')); return; }
  } else if (token.name === 'endObject' || token.name === 'endArray') depth--;
  done();
} });
pipeline(source, parser, drain).then(() => process.stdout.write('valid'), () => {
  process.stdout.write('invalid'); process.exitCode = 2;
});
`;
const VALIDATOR = String.raw `
import codecs, json, os, re, stat, struct, subprocess, sys, time, unicodedata, zipfile

MAX_FILES = 20000
MAX_DIRECTORY = 32 * 1024**2
MAX_RATIO = 1000
RESERVE = 512 * 1024**2
SPACE_INTERVAL = 64 * 1024**2
IMAGES = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.pgm', '.ppm'}
ARCHIVES = {'.zip', '.rar', '.7z', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.zst'}
BEFORE = {'处理前', '处理前图像', '处理前的图像', '原图', '原始图像', 'before', 'original', 'raw', 'input', 'images_before', 'images_original', 'images_raw'}
AFTER = {'处理后', '处理后图像', '处理后的图像', '处理好的图像', 'processed', 'after', 'output', 'images_after', 'images_processed'}

class Rejected(Exception):
    pass

last_progress = 0
def progress():
    global last_progress
    now = time.monotonic()
    if now - last_progress >= 1:
        sys.stderr.write('.')
        sys.stderr.flush()
        last_progress = now

def available_space(directory, needed=0):
    volume = os.statvfs(directory)
    if volume.f_bavail * volume.f_frsize < needed + RESERVE:
        reject('服务器临时空间不足，无法完整校验该压缩包；请清理空间、扩容或分批上传')

def reject(message):
    raise Rejected(message)

def label(name):
    return ''.join(c if ord(c) >= 32 and ord(c) != 127 else '?' for c in name)[:180]

def preflight(path):
    # Bound central-directory allocation before ZipFile builds its in-memory index.
    length = os.path.getsize(path)
    with open(path, 'rb') as stream:
        head = stream.read(4)
        if head not in (b'PK\x03\x04', b'PK\x05\x06'):
            reject('文件内容不是有效的 ZIP 压缩包，请勿只修改扩展名')
        stream.seek(max(0, length - 65557))
        tail = stream.read(65557)
        cursor = len(tail)
        end = None
        while cursor:
            offset = tail.rfind(b'PK\x05\x06', 0, cursor)
            if offset < 0:
                break
            if offset + 22 <= len(tail):
                comment = struct.unpack_from('<H', tail, offset + 20)[0]
                if offset + 22 + comment == len(tail):
                    end = length - len(tail) + offset
                    record = tail[offset:offset + 22]
                    break
            cursor = offset
        if end is None:
            reject('ZIP 压缩包不完整或已损坏，请重新压缩后上传')
        _, disk, directory_disk, disk_count, count, size, start, _ = struct.unpack('<4s4H2IH', record)
        if disk or directory_disk or disk_count != count:
            reject('不支持分卷 ZIP，请合并为一个完整压缩包后上传')
        boundary = end
        locator = b''
        if end >= 20:
            stream.seek(end - 20)
            locator = stream.read(20)
        if locator[:4] == b'PK\x06\x07':
            signature, disk64, offset64, disks = struct.unpack('<4sIQI', locator)
            if signature != b'PK\x06\x07' or disk64 or disks != 1:
                reject('不支持分卷或不完整的 ZIP64 压缩包')
            if offset64 + 56 > end - 20:
                reject('ZIP64 目录不完整，请重新压缩后上传')
            stream.seek(offset64)
            record64 = stream.read(56)
            sig, record_size, _, _, disk, directory_disk, disk_count, count, size, start = struct.unpack('<4sQ2H2I4Q', record64)
            if sig != b'PK\x06\x06' or record_size < 44 or record_size > 1024**2 or offset64 + 12 + record_size != end - 20:
                reject('ZIP64 目录无效，请重新压缩后上传')
            if disk or directory_disk or disk_count != count:
                reject('不支持分卷 ZIP，请合并为一个完整压缩包后上传')
            boundary = offset64
        elif count == 65535 or size == 0xffffffff or start == 0xffffffff:
            reject('ZIP64 目录不完整，请重新压缩后上传')
        if count > MAX_FILES:
            reject('ZIP 内文件和目录数量超过 20000 个，请分批打包上传')
        if size > MAX_DIRECTORY or start + size != boundary:
            reject('ZIP 目录过大或结构不完整，请重新分包压缩后上传')
        stream.seek(start)
        consumed = 0
        actual_count = 0
        while consumed < size:
            progress()
            header = stream.read(46)
            if len(header) != 46 or header[:4] != b'PK\x01\x02':
                reject('ZIP 中央目录已损坏，请重新压缩后上传')
            name_size, extra_size, comment_size, entry_disk = struct.unpack_from('<4H', header, 28)
            if entry_disk:
                reject('不支持分卷 ZIP，请合并为一个完整压缩包后上传')
            entry_size = 46 + name_size + extra_size + comment_size
            if not name_size or name_size > 4096 or consumed + entry_size > size:
                reject('ZIP 目录中的文件名过长或结构无效')
            actual_count += 1
            if actual_count > MAX_FILES:
                reject('ZIP 内文件和目录数量超过 20000 个，请分批打包上传')
            stream.seek(entry_size - 46, 1)
            consumed += entry_size
        if consumed != size or actual_count != count:
            reject('ZIP 目录记录不一致，请重新压缩后上传')

def safe_path(info, seen):
    name = unicodedata.normalize('NFC', info.orig_filename)
    if (not name or len(name.encode('utf-8')) > 4096 or name.startswith('/') or '\\' in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        reject('ZIP 中存在不安全的文件路径：' + label(name))
    parts = name[:-1].split('/') if name.endswith('/') else name.split('/')
    if any(not part or part in ('.', '..') or re.match(r'^[A-Za-z]:', part) or part.endswith((' ', '.')) for part in parts):
        reject('ZIP 中存在不安全的文件路径：' + label(name))
    normalized = '/'.join(parts).casefold()
    if normalized in seen:
        reject('ZIP 内存在重复的文件或目录路径：' + label(name))
    seen.add(normalized)
    mode = (info.external_attr >> 16) & 65535
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not info.is_dir()) or (kind == stat.S_IFREG and info.is_dir()):
        reject('ZIP 内不允许符号链接或特殊文件：' + label(name))
    return parts

def image_role(parts):
    dirs = {part.casefold() for part in parts[:-1]}
    before, after = bool(dirs & BEFORE), bool(dirs & AFTER)
    # Optional explicit prefixes make small flat packages usable as well.
    name = parts[-1].casefold()
    before = before or any(name.startswith(role + separator) for role in BEFORE for separator in ('_', '-'))
    after = after or any(name.startswith(role + separator) for role in AFTER for separator in ('_', '-'))
    if before and after:
        reject('图像同时被标为处理前和处理后，请分开目录：' + label('/'.join(parts)))
    return before, after

def image_magic(extension, head, tail, length):
    if extension in ('.jpg', '.jpeg'):
        return length > 4 and head.startswith(b'\xff\xd8\xff') and tail.endswith(b'\xff\xd9')
    if extension == '.png':
        return (length >= 45 and head.startswith(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR')
                and len(head) >= 24 and all(struct.unpack('>II', head[16:24]))
                and tail.endswith(b'\x00\x00\x00\x00IEND\xaeB\x60\x82'))
    if extension == '.webp':
        return (length >= 20 and head[:4] == b'RIFF' and head[8:12] == b'WEBP'
                and head[12:16] in (b'VP8 ', b'VP8L', b'VP8X') and struct.unpack('<I', head[4:8])[0] + 8 == length)
    if extension == '.bmp':
        return length >= 26 and head.startswith(b'BM')
    if extension in ('.tif', '.tiff'):
        return length >= 8 and head[:4] in (b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+')
    if extension in ('.pgm', '.ppm'):
        allowed = (b'P2', b'P5') if extension == '.pgm' else (b'P3', b'P6')
        if head[:2] not in allowed or head[2:3] not in b' \t\r\n\v\f':
            return False
        tokens = re.sub(rb'#[^\r\n]*', b' ', head).split()
        try:
            return len(tokens) >= 4 and int(tokens[1]) > 0 and int(tokens[2]) > 0 and 0 < int(tokens[3]) <= 65535
        except ValueError:
            return False
    return False

class JsonEncoding:
    def __init__(self, name):
        self.decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
        self.name, self.quoted, self.escaped = name, False, False
    def feed(self, content, final=False):
        try:
            text = self.decoder.decode(content, final)
        except UnicodeError:
            reject('JSON 不是有效的 UTF-8 JSON 文件：' + label(self.name))
        for char in text:
            if self.quoted:
                if self.escaped:
                    self.escaped = False
                elif char == '\\':
                    self.escaped = True
                elif char == '"':
                    self.quoted = False
            elif char == '"':
                self.quoted = True
            elif (char.isspace() or char == '\ufeff') and char not in ' \t\r\n':
                reject('JSON 不是有效的 UTF-8 JSON 文件：' + label(self.name))

def validate_json(path, name):
    progress()
    checked = subprocess.run([sys.argv[3], '-e', sys.argv[5], sys.argv[4], path],
                             stdout=subprocess.PIPE, stderr=sys.stderr)
    if checked.returncode or checked.stdout != b'valid':
        reject('JSON 不是有效的 UTF-8 JSON 文件，或嵌套层级超过 1024：' + label(name))

def validate(path):
    preflight(path)
    before = after = documents = 0
    seen = set()
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > MAX_FILES:
            reject('ZIP 内文件和目录数量超过 20000 个，请分批打包上传')
        for info in archive.infolist():
            progress()
            parts = safe_path(info, seen)
            name = '/'.join(parts)
            if info.flag_bits & 0x41:
                reject('不支持加密 ZIP，请移除压缩包密码后上传：' + label(name))
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                reject('ZIP 仅支持标准存储或 Deflate 压缩方式：' + label(name))
            if info.file_size > max(1, info.compress_size) * MAX_RATIO:
                reject('ZIP 内文件压缩倍率超过 1000，请分包或更换压缩方式：' + label(name))
            extension = os.path.splitext(parts[-1])[1].casefold()
            if not info.is_dir() and extension in ARCHIVES:
                reject('ZIP 内不支持嵌套压缩包，请将图像和 JSON 直接放入包内目录：' + label(name))
            size = 0
            head = bytearray()
            tail = b''
            is_document = extension == '.json' and not info.is_dir()
            document = encoding = document_path = None
            next_space_check = SPACE_INTERVAL
            try:
                if is_document:
                    available_space(sys.argv[2], info.file_size)
                    document_path = os.path.join(sys.argv[2], 'json-' + str(documents) + '.tmp')
                    descriptor = os.open(document_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    document = os.fdopen(descriptor, 'wb')
                    encoding = JsonEncoding(name)
                with archive.open(info) as member:
                    while True:
                        block = member.read(65536)
                        if not block:
                            break
                        progress()
                        size += len(block)
                        if size > info.file_size:
                            reject('ZIP 内文件实际大小超出声明：' + label(name))
                        if len(head) < 65536:
                            head.extend(block[:65536 - len(head)])
                        tail = (tail + block)[-16:]
                        if document is not None:
                            if size >= next_space_check:
                                available_space(sys.argv[2], info.file_size - size + len(block))
                                next_space_check = size + SPACE_INTERVAL
                            encoding.feed(block)
                            document.write(block)
                if is_document:
                    encoding.feed(b'', final=True)
                    document.close()
                    document = None
                    available_space(sys.argv[2])
                    validate_json(document_path, name)
            except (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError) as error:
                reject('ZIP 内文件损坏、校验失败或无法读取：' + label(name))
            except OSError as error:
                if error.errno in (28, 122):
                    reject('服务器临时空间不足，无法完整校验该压缩包；请清理空间、扩容或分批上传')
                raise
            finally:
                if document is not None:
                    document.close()
                if document_path is not None:
                    try:
                        os.unlink(document_path)
                    except FileNotFoundError:
                        pass
            if size != info.file_size:
                reject('ZIP 内文件不完整：' + label(name))
            if info.is_dir():
                if size:
                    reject('ZIP 中的目录包含异常文件内容：' + label(name))
                continue
            if extension in IMAGES:
                if not image_magic(extension, bytes(head), tail, size):
                    reject('图像内容与格式不符、为空或不完整：' + label(name))
                is_before, is_after = image_role(parts)
                before += int(is_before)
                after += int(is_after)
            elif is_document:
                documents += 1
    missing = []
    if not before:
        missing.append('处理前图像（放在“处理前/”或“before/”目录）')
    if not after:
        missing.append('处理后图像（放在“处理后/”或“after/”目录）')
    if not documents:
        missing.append('有效的 .json 文件')
    if missing:
        reject('处理结果 ZIP 缺少：' + '、'.join(missing))
    return {'ok': True, 'beforeImages': before, 'afterImages': after, 'jsonFiles': documents}

try:
    result = validate(sys.argv[1])
except Rejected as error:
    print(json.dumps({'ok': False, 'message': str(error)}, ensure_ascii=False))
    sys.exit(2)
except Exception:
    print(json.dumps({'ok': False, 'message': 'ZIP 压缩包已损坏或结构不受支持，请重新压缩后上传'}, ensure_ascii=False))
    sys.exit(2)
else:
    print(json.dumps(result, ensure_ascii=False))
`;
function resultFromOutput(output) {
    let value;
    try {
        value = JSON.parse(output);
    }
    catch {
        throw new common_1.ConflictException('处理结果压缩包校验未完成，请重新上传或联系管理员');
    }
    if (!value || typeof value !== 'object')
        throw new common_1.ConflictException('处理结果压缩包校验未完成，请重新上传或联系管理员');
    const result = value;
    if (result.ok !== true)
        throw new common_1.ConflictException(typeof result.message === 'string' ? result.message.slice(0, 1000) : '处理结果压缩包校验失败');
    const counts = [result.beforeImages, result.afterImages, result.jsonFiles];
    if (counts.some(count => !Number.isSafeInteger(count) || Number(count) < 1))
        throw new common_1.ConflictException('处理结果压缩包校验记录不完整，请重新上传');
    return { beforeImages: Number(result.beforeImages), afterImages: Number(result.afterImages), jsonFiles: Number(result.jsonFiles) };
}
async function requireTemporarySpace(directory, needed = 0n) {
    const volume = await (0, promises_1.statfs)(directory, { bigint: true });
    if (volume.bavail * volume.bsize < needed + TEMPORARY_RESERVE_BYTES) {
        throw new common_1.ConflictException('服务器临时空间不足，无法完整校验该压缩包；请清理空间、扩容或分批上传');
    }
}
function runValidator(archivePath, temporary, signal, progress) {
    return new Promise((resolve, reject) => {
        const grouped = process.platform !== 'win32';
        const child = (0, node_child_process_1.spawn)('python3', ['-I', '-B', '-c', VALIDATOR, archivePath, temporary,
            process.execPath, require.resolve('stream-json/Parser'), JSON_VALIDATOR], {
            stdio: ['ignore', 'pipe', 'pipe'], detached: grouped, windowsHide: true,
        });
        const output = [];
        let outputBytes = 0;
        let failure;
        let termination;
        const kill = (force = false) => {
            try {
                if (grouped && child.pid)
                    process.kill(-child.pid, force ? 'SIGKILL' : 'SIGTERM');
                else
                    child.kill(force ? 'SIGKILL' : 'SIGTERM');
            }
            catch { }
        };
        const stop = (error) => {
            if (failure)
                return;
            failure = error;
            kill();
            termination = setTimeout(() => kill(true), 2000);
            termination.unref();
        };
        const abort = () => stop(new common_1.ConflictException('处理结果压缩包校验长时间没有进度，请检查服务后重新上传'));
        signal.addEventListener('abort', abort, { once: true });
        child.stdout.on('data', (chunk) => {
            outputBytes += chunk.length;
            if (outputBytes > 64 * 1024)
                stop(new common_1.ConflictException('处理结果压缩包校验返回异常，请联系管理员'));
            else
                output.push(chunk);
        });
        child.stderr.on('data', () => progress());
        child.once('error', () => stop(new common_1.ConflictException('处理结果压缩包校验服务不可用，请联系管理员')));
        child.once('close', () => {
            signal.removeEventListener('abort', abort);
            if (termination)
                clearTimeout(termination);
            if (failure) {
                reject(failure);
                return;
            }
            try {
                resolve(resultFromOutput(Buffer.concat(output).toString('utf8')));
            }
            catch (error) {
                reject(error);
            }
        });
        if (signal.aborted)
            abort();
    });
}
async function validateProcessedPackage(storage, objectKey, compressedBytes) {
    if (!Number.isSafeInteger(compressedBytes) || compressedBytes < 1) {
        throw new common_1.ConflictException('处理结果 ZIP 压缩包大小须为大于 0 的安全整数');
    }
    const controller = new AbortController();
    let timer;
    const progress = () => {
        if (controller.signal.aborted)
            return;
        clearTimeout(timer);
        timer = setTimeout(() => controller.abort(), VALIDATION_IDLE_MS);
        timer.unref();
    };
    progress();
    let temporary;
    try {
        await requireTemporarySpace((0, node_os_1.tmpdir)(), BigInt(compressedBytes));
        temporary = await (0, promises_1.mkdtemp)((0, node_path_1.join)((0, node_os_1.tmpdir)(), 'warehouse-processed-'));
        const archivePath = (0, node_path_1.join)(temporary, 'package.zip');
        let bytes = 0;
        let nextSpaceCheck = SPACE_CHECK_BYTES;
        const bounded = new node_stream_1.Transform({
            transform(chunk, _encoding, callback) {
                bytes += chunk.length;
                progress();
                if (bytes > compressedBytes) {
                    callback(new common_1.ConflictException('处理结果 ZIP 实际大小超过上传声明'));
                    return;
                }
                if (bytes >= nextSpaceCheck) {
                    nextSpaceCheck = bytes + SPACE_CHECK_BYTES;
                    void requireTemporarySpace(temporary, BigInt(compressedBytes - bytes + chunk.length))
                        .then(() => callback(null, chunk), error => callback(error));
                }
                else
                    callback(null, chunk);
            },
            flush(callback) { callback(bytes !== compressedBytes ? new common_1.ConflictException('处理结果 ZIP 不完整，实际大小与上传声明不一致') : undefined); },
        });
        const source = await new Promise((resolve, reject) => {
            const abort = () => reject(new common_1.ConflictException('处理结果压缩包校验长时间没有进度，请检查服务后重新上传'));
            controller.signal.addEventListener('abort', abort, { once: true });
            void storage.getObject(objectKey).then(value => {
                controller.signal.removeEventListener('abort', abort);
                if (controller.signal.aborted)
                    value.destroy();
                else
                    resolve(value);
            }, error => {
                controller.signal.removeEventListener('abort', abort);
                reject(error);
            });
            if (controller.signal.aborted)
                abort();
        });
        await (0, promises_2.pipeline)(source, bounded, (0, node_fs_1.createWriteStream)(archivePath, { flags: 'wx', mode: 0o600 }), { signal: controller.signal });
        await requireTemporarySpace(temporary);
        progress();
        return await runValidator(archivePath, temporary, controller.signal, progress);
    }
    catch (error) {
        if (error instanceof common_1.ConflictException)
            throw error;
        if (controller.signal.aborted)
            throw new common_1.ConflictException('处理结果压缩包校验长时间没有进度，请检查服务后重新上传');
        if (error && typeof error === 'object' && 'code' in error && ['ENOSPC', 'EDQUOT'].includes(String(error.code))) {
            throw new common_1.ConflictException('服务器临时空间不足，无法完整校验该压缩包；请清理空间、扩容或分批上传');
        }
        throw new common_1.ConflictException('处理结果压缩包读取失败，请稍后重新上传');
    }
    finally {
        clearTimeout(timer);
        if (temporary)
            await (0, promises_1.rm)(temporary, { recursive: true, force: true }).catch(() => undefined);
    }
}
//# sourceMappingURL=processed-package.js.map