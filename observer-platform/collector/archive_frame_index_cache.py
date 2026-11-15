"""Retain small original frame indexes before verified video copies are pruned.

These private local files are not warehouse upload files. They preserve the
exact Matroska attachment bytes, even while a capture session is still open.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

MAX_INDEX_BYTES = 64 * 1024**2


class FrameIndexCacheError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def frame_index_cache_required(manifest):
    metadata = manifest.get('metadata') if isinstance(manifest, dict) else None
    if not isinstance(metadata, dict):
        return False
    source = metadata.get('source')
    camera = source == 'native-camera-jpeg' or (source == 'native-ros-cdr'
        and manifest.get('sensor') in ('Berxel深度', 'Berxel彩色'))
    return (camera and manifest.get('files') == ['video.mkv']
        and metadata.get('videoReady') is True
        and isinstance(metadata.get('captureSessionId'), str)
        and re.fullmatch(r'[a-f0-9]{32}', metadata['captureSessionId']) is not None
        and isinstance(metadata.get('clockDomain'), str)
        and re.fullmatch(r'linux-boot:[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',
                        metadata['clockDomain']) is not None
        and metadata.get('clockSource') == 'CLOCK_REALTIME'
        and metadata.get('timestampSemantics') == (
            'host_read_complete' if source == 'native-camera-jpeg' else 'host_subscription_receive'))


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read(path, size, checksum):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_nlink != 1 or before.st_size != size or size > MAX_INDEX_BYTES):
                raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_CACHE_INVALID')
            raw = stream.read(MAX_INDEX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) != size or hashlib.sha256(raw).hexdigest() != checksum
                or _fingerprint(before) != _fingerprint(after)):
            raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_CHECKSUM_MISMATCH')
        return raw
    except OSError as error:
        raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_CACHE_UNAVAILABLE') from error


def _directory(segment, *parts, create=False):
    current = segment
    for part in (None, *parts):
        if part is not None:
            current /= part
            if create:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_CACHE_PATH_INVALID')
    return current


def preserve_frame_time_index(segment, manifest, *, extractor=None):
    """Persist an exact index or refuse destructive cleanup; never decode frames."""
    if not frame_index_cache_required(manifest):
        return dict(state='not_required')
    metadata = manifest['metadata']
    name = 'index.jsonl' if metadata['source'] == 'native-camera-jpeg' else 'frames.index.jsonl'
    attachments = metadata.get('videoAttachments')
    matches = [(index, row) for index, row in enumerate(attachments or [])
               if isinstance(row, dict) and row.get('name') == name] if isinstance(attachments, list) else []
    if len(matches) != 1:
        raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_ATTACHMENT_INVALID')
    position, expected = matches[0]
    checksum, size = expected.get('sha256'), expected.get('bytes')
    if (position > 8 or not isinstance(checksum, str)
            or re.fullmatch(r'[a-f0-9]{64}', checksum) is None
            or type(size) is not int or not 0 < size <= MAX_INDEX_BYTES):
        raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_ATTACHMENT_INVALID')
    segment = Path(segment)
    _directory(segment)
    parent = segment / '.sync-frame-indexes' / checksum
    destination = parent / name
    if (segment / '.sync-frame-indexes').exists() or (segment / '.sync-frame-indexes').is_symlink():
        _directory(segment, '.sync-frame-indexes')
        if parent.exists() or parent.is_symlink():
            _directory(segment, '.sync-frame-indexes', checksum)
            if destination.exists() or destination.is_symlink():
                _read(destination, size, checksum)
                return dict(state='preserved', path=str(destination.relative_to(segment)),
                    bytes=size, sha256=checksum)
    source = segment / name
    if source.exists() or source.is_symlink():
        raw = _read(source, size, checksum)
    else:
        video = segment / 'video.mkv'
        try:
            before = video.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_nlink != 1 or before.st_size <= 0):
                raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_VIDEO_UNAVAILABLE')
            if extractor is None:
                from .archive_sync_index import _extract_attachment
                extractor = _extract_attachment
            raw = extractor(video, position)
            if _fingerprint(before) != _fingerprint(video.lstat()):
                raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_VIDEO_CHANGED')
        except FrameIndexCacheError:
            raise
        except Exception as error:
            raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_EXTRACTION_FAILED') from error
        if (not isinstance(raw, bytes) or len(raw) != size or len(raw) > MAX_INDEX_BYTES
                or hashlib.sha256(raw).hexdigest() != checksum):
            raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_CHECKSUM_MISMATCH')
    parent = _directory(segment, '.sync-frame-indexes', checksum, create=True)
    temporary = parent / (name + '.tmp-' + uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            if stream.write(raw) != len(raw):
                raise FrameIndexCacheError('ARCHIVE_FRAME_TIME_INDEX_CACHE_WRITE_FAILED')
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists() or destination.is_symlink():
            _read(destination, size, checksum)
        else:
            os.replace(temporary, destination)
        for directory in (parent, parent.parent, segment):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(state='preserved', path=str(destination.relative_to(segment)), bytes=size, sha256=checksum)
