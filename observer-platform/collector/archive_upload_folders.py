"""Optional empty result directories for explicitly requested foreground uploads.

This module reads bounded archive metadata only. It never changes media,
manifests, upload checkpoints or acknowledgements, and has no automatic worker.
"""
from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID
from zoneinfo import ZoneInfo


MAX_MANIFEST_BYTES = 4 * 1024**2
FOLDER_ERROR = 'ARCHIVE_PROCESSED_FOLDER_UNAVAILABLE'
MANIFEST_ERROR = 'ARCHIVE_PROCESSED_FOLDER_MANIFEST_INVALID'
IDENTITY_ERROR = 'ARCHIVE_PROCESSED_FOLDER_ROBOT_INVALID'
NAME_ERROR = 'ARCHIVE_PROCESSED_FOLDER_NAME_INVALID'


def processed_folder_name(now=None):
    """Use one Beijing instant supplied by the foreground upload invocation."""
    if now is None:
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('ARCHIVE_PROCESSED_FOLDER_TIME_INVALID')
    local = now.astimezone(ZoneInfo('Asia/Shanghai'))
    return (f'处理好的数据_{local.year:04d}{local.month:02d}{local.day:02d}_'
            f'{local.hour:02d}{local.minute:02d}{local.second:02d}')


def _valid_folder_name(value):
    if not isinstance(value, str) or re.fullmatch(r'处理好的数据_[0-9]{8}_[0-9]{6}', value) is None:
        return False
    try:
        datetime.strptime(value, '处理好的数据_%Y%m%d_%H%M%S')
    except ValueError:
        return False
    return True


def _robot_id(value):
    if not isinstance(value, str) or not value or str(UUID(value)) != value:
        raise ValueError(IDENTITY_ERROR)
    return value


def _manifest_day(path, robot_id):
    """Read a regular metadata file without opening any referenced media file."""
    path = Path(path)
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError(MANIFEST_ERROR)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_MANIFEST_BYTES:
            raise ValueError(MANIFEST_ERROR)
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) != before.st_size
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
        raise ValueError(MANIFEST_ERROR)
    value = json.loads(raw)
    if (not isinstance(value, dict) or type(value.get('schemaVersion')) is not int
            or value['schemaVersion'] != 1 or _robot_id(value.get('robotId')) != robot_id):
        raise ValueError(MANIFEST_ERROR)
    day = value.get('day')
    if not isinstance(day, str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', day) is None:
        raise ValueError(MANIFEST_ERROR)
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError(MANIFEST_ERROR)
    return day


def ensure_processed_folders(client, manifests, folder_name):
    """Ensure one empty result directory per valid day, independently of media.

    Counts cover unique valid dataset days; malformed metadata adds an unresolved
    entry because its dataset cannot be identified safely. Errors are fixed codes
    and never include paths, API responses or credential-bearing exception text.
    BaseException subclasses, including the upload deadline, remain interruptible.
    """
    result = dict(name=folder_name, createdDatasets=0, pendingDatasets=0, lastError=None)
    if not _valid_folder_name(folder_name):
        result.update(pendingDatasets=1, lastError=NAME_ERROR)
        return result
    try:
        robot_id = _robot_id(client.config.robot_id)
    except Exception:
        result.update(pendingDatasets=1, lastError=IDENTITY_ERROR)
        return result
    days = set()
    seen_paths = set()
    for manifest in manifests:
        try:
            path = Path(manifest)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            days.add(_manifest_day(path, robot_id))
        except Exception:
            result['pendingDatasets'] += 1
            result['lastError'] = MANIFEST_ERROR
    for day in sorted(days):
        try:
            # ensure_day validates dataset ownership, robot scope and 409 races.
            # The existing folders API persists an empty directory; no fake file.
            client.ensure_day(day, (folder_name,))
            result['createdDatasets'] += 1
        except Exception:
            result['pendingDatasets'] += 1
            result['lastError'] = FOLDER_ERROR
    return result
