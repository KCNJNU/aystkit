"""Read existing USB intrinsics without opening cameras or changing image pixels."""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

DEFAULT_PATH = Path.home() / '.config/robot-cloud/camera-calibration.json'
MAX_BYTES = 65536
DIRECTIONS = dict(camera1='front', camera2='rear', camera3='right', camera4='left')


class CalibrationError(ValueError):
    """The configured registry cannot safely describe these camera intrinsics."""


def _check(condition, message):
    if not condition:
        raise CalibrationError(message)


def _integer(value, name):
    _check(type(value) is int and 0 < value <= 0xffffffff, f'{name} must be a positive uint32 integer')
    return value


def _matrix(value, count, name):
    _check(isinstance(value, list) and len(value) == count, f'{name} has invalid shape')
    try:
        finite = all(type(x) in (int, float) and math.isfinite(x) for x in value)
    except OverflowError:
        finite = False
    _check(finite, f'{name} must contain finite numbers')
    return [float(x) for x in value]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def _validate(value, robot_id):
    _check(isinstance(value, dict), 'registry must be an object')
    _check(type(value.get('schemaVersion')) is int and value['schemaVersion'] == 1,
           'unsupported schemaVersion')
    configured = value.get('robotId')
    _check(isinstance(configured, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', configured),
           'invalid robotId')
    _check(robot_id is None or robot_id == configured, 'robotId does not match registry')
    _check(value.get('quality') == 'candidate_no_independent_validation', 'invalid quality')
    result_id = value.get('sourceResultId')
    _check(isinstance(result_id, str) and 0 < len(result_id) <= 160
           and all(ord(c) >= 32 for c in result_id), 'invalid sourceResultId')
    _check(isinstance(value.get('sourceArchiveSha256'), str)
           and re.fullmatch('[0-9a-f]{64}', value['sourceArchiveSha256']), 'invalid source archive hash')
    cameras = value.get('cameras')
    _check(isinstance(cameras, dict) and set(cameras) == set(DIRECTIONS), 'four USB cameras are required')
    devices = set()
    for camera_id, camera in cameras.items():
        _check(isinstance(camera, dict) and camera.get('id') == camera_id, 'camera id does not match key')
        _check(camera.get('direction') == DIRECTIONS[camera_id], 'camera direction does not match id')
        device = camera.get('device')
        _check(isinstance(device, str) and len(device) <= 255 and re.fullmatch(
            r'/dev/v4l/by-path/[A-Za-z0-9_.:+-]*usb[A-Za-z0-9_.:+-]*-video-index[0-9]+', device),
            'camera device must be a USB by-path name')
        _check(device not in devices, 'camera devices must be distinct')
        devices.add(device)
        for key in ('imageWidth', 'imageHeight'):
            _integer(camera.get(key), key)
        _check(camera.get('distortionModel') == 'plumb_bob', 'unsupported distortion model')
        k, d, r, p = (_matrix(camera.get(key), size, key)
                       for key, size in (('K', 9), ('D', 5), ('R', 9), ('P', 12)))
        _check(k[0] > 0 and k[4] > 0 and p[0] > 0 and p[5] > 0, 'focal lengths must be positive')
        _check(k[6:] == [0, 0, 1] and p[8:] == [0, 0, 1, 0], 'invalid homogeneous matrix row')
        _check(all(abs(sum(r[3*i+t] * r[3*j+t] for t in range(3)) - (i == j)) < 1e-5
                   for i in range(3) for j in range(3)), 'R must be orthonormal')
        determinant = (r[0]*(r[4]*r[8]-r[5]*r[7]) - r[1]*(r[3]*r[8]-r[5]*r[6])
                       + r[2]*(r[3]*r[7]-r[4]*r[6]))
        _check(abs(determinant - 1) < 1e-5, 'R must be a proper rotation')
        _check(isinstance(camera.get('sourceYamlSha256'), str)
               and re.fullmatch('[0-9a-f]{64}', camera['sourceYamlSha256']), 'invalid source YAML hash')
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, OverflowError) as error:
        raise CalibrationError('registry must contain finite JSON values') from error


def load_registry(path=None, *, robot_id=None):
    """Read a private, owned regular JSON file; an absent optional file returns None."""
    try:
        location = Path(path) if path is not None else DEFAULT_PATH
        fd = os.open(location, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _check(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                   and info.st_mode & 0o077 == 0, 'registry must be a private owned regular file')
            _check(info.st_size <= MAX_BYTES, 'registry is too large')
            content = stream.read(MAX_BYTES + 1)
            _check(len(content) <= MAX_BYTES, 'registry is too large')
        value = json.loads(content, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(CalibrationError('nonfinite JSON')))
        return _validate(value, robot_id)
    except FileNotFoundError:
        return None
    except CalibrationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise CalibrationError('cannot read calibration registry') from error


def get_camera_calibration(camera_id, width, height, *, robot_id=None, path=None,
                           registry=None, resize=False):
    """Snapshot native intrinsics; resize=True asserts a pure resize, never a crop."""
    _check(isinstance(camera_id, str), 'camera id must be a string')
    width, height = _integer(width, 'width'), _integer(height, 'height')
    _check(type(resize) is bool, 'resize must be boolean')
    data = load_registry(path, robot_id=robot_id) if registry is None else _validate(registry, robot_id)
    if data is None or camera_id not in data['cameras']:
        return None
    camera = data['cameras'][camera_id]
    native_size = dict(width=camera['imageWidth'], height=camera['imageHeight'])
    native = dict(native_size, distortionModel=camera['distortionModel'],
                  **{key: copy.deepcopy(camera[key]) for key in ('K', 'D', 'R', 'P')})
    snapshot = {key: data[key] for key in ('schemaVersion', 'robotId', 'sourceResultId', 'sourceArchiveSha256', 'quality')}
    snapshot.update(cameraId=camera_id, direction=camera['direction'], device=camera['device'],
                    sourceYamlSha256=camera['sourceYamlSha256'], nativeSize=dict(native_size),
                    sourceSize=dict(native_size), size=dict(width=width, height=height), nativeImageInfo=native)
    matched = width == native_size['width'] and height == native_size['height']
    snapshot['state'] = 'matched' if matched else ('resized' if resize else 'resolution_mismatch')
    snapshot['transform'] = dict(kind='identity' if matched else 'none')
    if not matched and not resize:
        snapshot['transform']['reason'] = 'resolution_mismatch'
    if matched or resize:
        current = copy.deepcopy(native)
        current.update(width=width, height=height)
        if not matched:
            sx, sy = width/native_size['width'], height/native_size['height']
            for key, cols in (('K', 3), ('P', 4)):
                current[key] = [v * (sx if i < cols else sy if i < cols*2 else 1)
                                for i, v in enumerate(current[key])]
            snapshot['transform'] = dict(kind='resize', scaleX=sx, scaleY=sy, crop=False)
        snapshot['currentImageInfo'] = current
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--robot-id')
    parser.add_argument('--camera', required=True, choices=tuple(DIRECTIONS))
    parser.add_argument('--width', type=int)
    parser.add_argument('--height', type=int)
    parser.add_argument('--resize', action='store_true', help='assert input is a pure resize, without cropping')
    args = parser.parse_args(argv)
    if (args.width is None) != (args.height is None):
        parser.error('--width and --height must be supplied together')
    try:
        registry = load_registry(args.config, robot_id=args.robot_id)
        camera = registry['cameras'][args.camera] if registry else None
        value = get_camera_calibration(args.camera,
            args.width if args.width is not None else camera['imageWidth'],
            args.height if args.height is not None else camera['imageHeight'],
            robot_id=args.robot_id, registry=registry, resize=args.resize) if camera else None
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except CalibrationError as error:
        print(json.dumps(dict(state='invalid', error=str(error))), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
