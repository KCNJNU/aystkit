"""Pixel-free receipt evidence from the existing raw-image preview subscriber.

This standalone helper runs beside the Jetson preview encoder. It does not use
ROS, inspect hardware, change the archive gate, or claim that frames were saved.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

MAX_REPORT_BYTES = 16 * 1024
MAX_SAFE_INTEGER = 2 ** 53 - 1
DEFAULT_PATH = Path.home() / '.local/state/robot-cloud/garage-control/berxel-capture.json'
TOPICS = {'color': '/berxel/color/image_raw', 'depth': '/berxel/depth/image_raw'}
_BOOT_ID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')


def _number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('Invalid raw capture age or monotonic time')
    return value


def _boot_id():
    with open('/proc/sys/kernel/random/boot_id', 'r', encoding='ascii') as source:
        value = source.read(65).strip()
    if not _BOOT_ID.fullmatch(value):
        raise ValueError('Invalid kernel boot identity')
    return value


def pack_capture_health(report, *, pid=None, boot_id=None, monotonic=None):
    """Filter existing encoder health down to original-image receipt evidence."""
    if not isinstance(report, dict):
        raise ValueError('Missing raw capture health report')
    stamp = _number(time.monotonic() if monotonic is None else monotonic)
    channels = {}
    for key, topic in TOPICS.items():
        row = report.get(key)
        if not isinstance(row, dict) or row.get('sourceTopic') != topic:
            raise ValueError('Raw capture topic does not match the camera')
        count = row.get('receivedFrames')
        if type(count) is not int or not 0 <= count <= MAX_SAFE_INTEGER:
            raise ValueError('Invalid original-image receipt count')
        if 'sourceAgeS' not in row:
            raise ValueError('Missing original-image receipt age')
        age = row['sourceAgeS']
        sampled_at = _number(row.get('reportedMonotonic', stamp))
        if sampled_at > stamp:
            raise ValueError('Original-image receipt report is future dated')
        if age is not None:
            # ROS health publication can block before this diagnostic write.
            # Carry that elapsed time forward instead of refreshing an old age.
            age = _number(_number(age) + (stamp - sampled_at))
        channels[key] = {'topic': topic, 'receivedFrames': count, 'sourceAgeS': age}
    process = os.getpid() if pid is None else pid
    if type(process) is not int or not 1 <= process <= MAX_SAFE_INTEGER:
        raise ValueError('Invalid capture reporter process')
    boot = _boot_id() if boot_id is None else boot_id
    if not isinstance(boot, str) or not _BOOT_ID.fullmatch(boot):
        raise ValueError('Invalid kernel boot identity')
    return {'schemaVersion': 1, 'pid': process, 'bootId': boot,
            'reportedMonotonic': stamp, 'channels': channels}


def _directory(path):
    """Pin each ancestor with no-follow descriptors; create private missing dirs."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or not path.name:
        raise ValueError('Raw capture report path must be absolute')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    protected = False
    try:
        for part in path.parent.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            # A verified owner-only ancestor already prevents other accounts
            # from reaching a same-owner intermediate directory. Preserve its
            # existing mode instead of changing unrelated source directories.
            protected_owner = protected and info.st_uid == os.getuid()
            if (info.st_uid not in (0, os.getuid())
                    or (info.st_mode & 0o022 and not sticky_root and not protected_owner)):
                raise ValueError('Untrusted raw capture report directory')
            protected = protected or (info.st_uid == os.getuid()
                                      and stat.S_IMODE(info.st_mode) == 0o700)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Raw capture report directory must be private (0700)')
        return descriptor, path.name
    except BaseException:
        os.close(descriptor)
        raise


def _file_identity(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError('Raw capture report must be an owned private regular file')


def write_capture_health(report, path=None):
    """Atomically publish fresh raw-frame counts; never write pixels or preview data."""
    payload = pack_capture_health(report)
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
    if len(encoded) > MAX_REPORT_BYTES:
        raise ValueError('Raw capture report exceeds size limit')
    directory, name = _directory(DEFAULT_PATH if path is None else path)
    temporary = '.berxel-capture-' + uuid.uuid4().hex + '.tmp'
    try:
        try:
            _file_identity(os.stat(name, dir_fd=directory, follow_symlinks=False))
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    return payload
