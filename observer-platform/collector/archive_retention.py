"""Release only verified warehouse copies from the local transfer spool."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .archive_core import atomic_json
from .archive_upload import ArchiveUploadError


def _source_files(manifest):
    """Only explicitly finalized media/bag inputs are eligible for cache cleanup."""
    metadata = manifest.get('metadata', {})
    if not isinstance(metadata, dict):
        return []
    video = metadata.get('videoReady') is True
    bag = metadata.get('bagCompressionReady') is True
    if not video and not bag:
        return []
    rows = metadata.get('sourceFiles')
    outputs = manifest.get('files')
    valid_outputs = outputs == ['video.mkv'] if video and not bag else (
        bag and not video and isinstance(outputs, list) and len(outputs) >= 2
        and 'compressed-bag/metadata.yaml' in outputs
        and len(set(outputs)) == len(outputs)
        and all(isinstance(name, str) and re.fullmatch(
            r'compressed-bag/(?:metadata\.yaml|[A-Za-z0-9_-]+\.db3\.zst)', name) for name in outputs))
    if not valid_outputs or not isinstance(rows, list) or not 1 <= len(rows) <= 4096:
        raise ArchiveUploadError('ARCHIVE_RETENTION_SOURCE_LIST_INVALID')
    result, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'relativePath', 'sha256', 'size'}
                or not isinstance(row.get('relativePath'), str)
                or not re.fullmatch(r'(?:frames\.mjpg|index\.jsonl|bag/(?:metadata\.yaml|[A-Za-z0-9_-]+\.db3))'
                    if video else r'bag/(?:metadata\.yaml|[A-Za-z0-9_-]+\.db3)', row['relativePath'])
                or row['relativePath'] in seen or not isinstance(row.get('sha256'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])
                or type(row.get('size')) is not int or row['size'] < 0):
            raise ArchiveUploadError('ARCHIVE_RETENTION_SOURCE_LIST_INVALID')
        seen.add(row['relativePath'])
        result.append(dict(row))
    return result


def _parent(segment, name):
    relative = Path(name)
    if (not isinstance(name, str) or not relative.parts or str(relative) != name or relative.is_absolute()
            or any(part in ('.', '..') or any(ord(c) < 32 for c in part) for part in relative.parts)
            or '\\' in name):
        raise ArchiveUploadError('ARCHIVE_RETENTION_PATH_INVALID')
    descriptor = None
    try:
        descriptor = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in relative.parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor, relative.name
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _verified_file(segment, row, *, missing_ok=False):
    """Read regular owned files without following a leaf or directory link."""
    parent = descriptor = None
    try:
        parent, name = _parent(segment, row['relativePath'])
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_nlink != 1:
            raise ArchiveUploadError('ARCHIVE_RETENTION_PATH_INVALID')
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            for data in iter(lambda: stream.read(131072), b''):
                digest.update(data); size += len(data)
            after = os.fstat(stream.fileno())
        if (digest.hexdigest(), size) != (row.get('sha256'), row.get('size')) or _fingerprint(before) != _fingerprint(after):
            raise ArchiveUploadError('ARCHIVE_RETENTION_FILE_CHANGED')
        return _fingerprint(after)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as error:
        raise ArchiveUploadError('ARCHIVE_RETENTION_PATH_INVALID') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def _unlink_verified(segment, name, fingerprint):
    if fingerprint is None:
        return
    descriptor = None
    try:
        descriptor, leaf = _parent(segment, name)
        current = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or _fingerprint(current) != fingerprint:
            raise ArchiveUploadError('ARCHIVE_RETENTION_FILE_CHANGED')
        os.unlink(leaf, dir_fd=descriptor)
        os.fsync(descriptor)
    except OSError as error:
        raise ArchiveUploadError('ARCHIVE_RETENTION_PATH_INVALID') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def prune_confirmed_copy(segment, client, *, retain_seconds=300, now=None):
    segment = Path(segment)
    marker = segment / 'local-copy-pruned.json'
    resume = False
    if marker.exists():
        if marker.is_symlink():
            raise ArchiveUploadError('ARCHIVE_RETENTION_MARKER_INVALID')
        saved = json.loads(marker.read_text())
        if saved.get('state') == 'pruned':
            return False
        if saved.get('state') != 'pruning':
            raise ArchiveUploadError('ARCHIVE_RETENTION_MARKER_INVALID')
        resume = True
    ack_path = segment / 'uploaded.json'
    if ack_path.is_symlink() or not ack_path.is_file():
        return False
    ack = json.loads(ack_path.read_text())
    now = now or datetime.now(timezone.utc)
    if ack.get('state') != 'complete':
        return False
    manifest_path = segment / 'archive.json'
    if manifest_path.is_symlink() or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != ack.get('manifestSha256'):
        raise ArchiveUploadError('ARCHIVE_RETENTION_MANIFEST_MISMATCH')
    manifest = json.loads(manifest_path.read_text())
    metadata = manifest.get('metadata', {})
    # New compact captures already have a verified durable warehouse copy.
    # Release their duplicate spool immediately; older captures keep the
    # configured retention period. No warehouse object is deleted here.
    compact = isinstance(metadata, dict) and metadata.get('archiveProfile') == 'compact-v1' and (
        metadata.get('videoReady') is True or metadata.get('bagCompressionReady') is True)
    delay = 0 if compact else retain_seconds
    if (now - datetime.fromisoformat(ack['completedAt'])).total_seconds() < delay:
        return False
    if not resume:
        _, manifest, _ = client._manifest(segment)
    # Final videos may upload while their session is still recording. Their
    # small timestamp attachment must outlive both the video and sourceFiles.
    from .archive_frame_index_cache import preserve_frame_time_index, FrameIndexCacheError
    try:
        preserve_frame_time_index(segment, manifest)
    except FrameIndexCacheError as error:
        raise ArchiveUploadError(error.code) from error
    sources = _source_files(manifest)
    if resume and sources and (saved.get('sourceFiles') != sources or saved.get('files') != ack['files']):
        raise ArchiveUploadError('ARCHIVE_RETENTION_MARKER_INVALID')
    confirmed = {row['relativePath']: row for row in ack['files']}
    if set(confirmed) != set(manifest['files']):
        raise ArchiveUploadError('ARCHIVE_RETENTION_ACK_INCOMPLETE')
    # Recheck every local digest and current server record/access before unlinking
    # any file. Unconfirmed/pending/interrupted captures are never candidates.
    checked = {}
    for name in manifest['files']:
        row = confirmed[name]
        checked[name] = _verified_file(segment, row, missing_ok=resume)
        remote_folder = '/'.join((ack['folderPath'], *Path(name).parts[:-1]))
        client._confirmed_file(row['robotFileId'], row, ack['datasetId'], remote_folder, Path(name).name)
    for row in sources:
        # Pulled IPC copies contain only the final video or compressed bag. Inputs
        # may all be absent; existing inputs must still exactly match the list.
        checked[row['relativePath']] = _verified_file(segment, row, missing_ok=True)
    # Marker precedes deletion so an interrupted pruning pass can be recognized;
    # it contains only file IDs/digests, not credentials or presigned object URLs.
    checkpoint = dict(schemaVersion=1, state='pruning', timestamp=now.isoformat(), files=ack['files'])
    if sources:
        checkpoint['sourceFiles'] = sources
    atomic_json(marker, checkpoint)
    for name, fingerprint in checked.items():
        _unlink_verified(segment, name, fingerprint)
    checkpoint['state'] = 'pruned'
    atomic_json(marker, checkpoint)
    return True
