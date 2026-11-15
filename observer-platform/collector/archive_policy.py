"""Explicit archive selection shared by recording, sealing and file transfer.

This policy changes future storage selection only. It never removes old files,
acknowledges excluded segments, or starts a sensor.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import re

CAMERA_ROS_SENSORS = ('Berxel彩色', 'Berxel深度')
CAMERA_SENSOR_NAMES = frozenset((*CAMERA_ROS_SENSORS,
    '原相机前', '原相机后', '原相机左', '原相机右',
    # Explicit sensor labels used by the original native-camera deployment.
    '原始相机_前', '原始相机_后', '原始相机_左', '原始相机_右'))
CAMERA_MESSAGE_TYPES = frozenset(('sensor_msgs/msg/Image', 'sensor_msgs/msg/CameraInfo'))
MAX_POLICY_MANIFEST_BYTES = 256 * 1024
TRAINING_CAMERA_SENSORS = CAMERA_SENSOR_NAMES | {'鱼眼相机'}
TRAINING_BAG_SENSORS = frozenset(('雷达MID360', '二维雷达scan', '二维雷达蓝海', '定位与TF'))
SYNC_SENSOR = '同步配对'


def sync_manifest_allowed(manifest):
    """Select the explicit camera-derived time index, never a renamed raw cache."""
    if not isinstance(manifest, dict) or manifest.get('sensor') != SYNC_SENSOR:
        return False
    metadata = manifest.get('metadata')
    if not isinstance(metadata, dict) or manifest.get('files') != ['sync-index.jsonl', 'sync-summary.json']:
        return False
    return (metadata.get('scope') == 'garage' and metadata.get('source') == 'native-time-sync'
        and metadata.get('syncKind') == 'software_host_timestamp'
        and metadata.get('hardwareSynchronized') is False
        and metadata.get('clockSource') == 'CLOCK_REALTIME'
        and isinstance(metadata.get('clockDomain'), str)
        and re.fullmatch(r'linux-boot:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', metadata['clockDomain']) is not None
        and isinstance(metadata.get('captureSessionId'), str)
        and re.fullmatch(r'[0-9a-f]{32}', metadata['captureSessionId']) is not None)


def training_only_enabled(environ=None):
    value = (os.environ if environ is None else environ).get('ARCHIVE_TRAINING_ONLY', '0')
    if value not in ('0', '1'):
        raise ValueError('ARCHIVE_TRAINING_ONLY must be 0 or 1')
    return value == '1'


def training_manifest_allowed(manifest):
    """Allow final video or compressed lidar/pose, never raw camera caches."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get('metadata'), dict):
        return False
    metadata, files, sensor = manifest['metadata'], manifest.get('files'), manifest.get('sensor')
    if sensor == SYNC_SENSOR:
        return sync_manifest_allowed(manifest)
    if not isinstance(sensor, str):
        return False
    if sensor in TRAINING_CAMERA_SENSORS:
        if metadata.get('videoReady') is True and files == ['video.mkv']:
            return True
        topics = metadata.get('topics')
        calibration_only = (metadata.get('cameraMetadataOnly') is True
            and isinstance(topics, dict) and bool(topics)
            and all(isinstance(row, dict) and row.get('type') == 'sensor_msgs/msg/CameraInfo'
                    for row in topics.values()))
        if not calibration_only:
            return False
    elif sensor not in TRAINING_BAG_SENSORS:
        return False
    if metadata.get('bagCompressionReady') is not True:
        return False
    return (isinstance(files, list) and len(files) >= 2
        and all(isinstance(name, str) for name in files)
        and 'compressed-bag/metadata.yaml' in files and len(set(files)) == len(files)
        and all(isinstance(name, str) and re.fullmatch(
            r'compressed-bag/(?:metadata\.yaml|[A-Za-z0-9_-]+\.db3\.zst)', name) for name in files))


def camera_only_enabled(environ=None):
    """An absent flag preserves the previous all-source deployment behavior."""
    value = (os.environ if environ is None else environ).get('ARCHIVE_CAMERA_ONLY')
    if value is None or value == '0':
        return False
    if value == '1':
        return True
    raise ValueError('ARCHIVE_CAMERA_ONLY must be 0 or 1')


def allowed_archive_manifest(manifest, camera_only=False, training_only=None):
    if isinstance(manifest, dict) and manifest.get('sensor') == SYNC_SENSOR:
        return sync_manifest_allowed(manifest)
    if training_only is None:
        training_only = training_only_enabled()
    if training_only:
        # Pending video/bag manifests must remain eligible for conversion.
        # File transfer and upload separately enforce finalized outputs.
        if not isinstance(manifest, dict) or not isinstance(manifest.get('sensor'), str):
            return False
        if manifest['sensor'] not in TRAINING_CAMERA_SENSORS | TRAINING_BAG_SENSORS:
            return False
    if not camera_only:
        return True
    if not isinstance(manifest, dict):
        return False
    sensor = manifest.get('sensor')
    if not isinstance(sensor, str) or sensor not in CAMERA_SENSOR_NAMES:
        return False
    metadata = manifest.get('metadata', {})
    if not isinstance(metadata, dict):
        return False
    # Native USB video does not have ROS topics. When topics are present, their
    # types must all be image/calibration data, even for a camera-labelled folder.
    if 'topics' not in metadata:
        return sensor not in CAMERA_ROS_SENSORS
    topics = metadata['topics']
    return isinstance(topics, dict) and bool(topics) and all(
        isinstance(row, dict) and isinstance(row.get('type'), str) and row['type'] in CAMERA_MESSAGE_TYPES
        for row in topics.values())


def allowed_manifest_path(path, camera_only=False):
    """Read only a bounded, regular manifest; malformed selections fail closed."""
    training_only = training_only_enabled()
    if not camera_only and not training_only:
        return True
    descriptor = None
    try:
        path = Path(path)
        if any(parent.is_symlink() for parent in path.parents):
            return False
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_POLICY_MANIFEST_BYTES:
            return False
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(MAX_POLICY_MANIFEST_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) > MAX_POLICY_MANIFEST_BYTES or
                (before.st_size, before.st_mtime_ns, before.st_ino) !=
                (after.st_size, after.st_mtime_ns, after.st_ino)):
            return False
        return allowed_archive_manifest(json.loads(raw), camera_only, training_only)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def parse_ros_sensors(raw, camera_only=False):
    """Use explicit 'none' for an IPC with only source-file upload duties.

    A blank value keeps legacy full selection unless camera-only is enabled.
    Known non-camera names are filtered in camera-only mode; misspellings fail.
    """
    from .archive_ros import sensor_specs
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if camera_only:
            return CAMERA_ROS_SENSORS
        if training_only_enabled():
            return tuple(name for name in sensor_specs() if name in TRAINING_CAMERA_SENSORS | TRAINING_BAG_SENSORS)
        return None
    if not isinstance(raw, str):
        raise ValueError('ARCHIVE_ROS_SENSORS must be a comma-separated string')
    if raw.strip() == 'none':
        return ()
    names = tuple(dict.fromkeys(part.strip() for part in raw.split(',')))
    if not names or any(not name or name not in sensor_specs() for name in names):
        raise ValueError('ARCHIVE_ROS_SENSORS contains an unknown sensor')
    return tuple(name for name in names
        if (not camera_only or name in CAMERA_ROS_SENSORS)
        and (not training_only_enabled() or name in TRAINING_CAMERA_SENSORS | TRAINING_BAG_SENSORS))
