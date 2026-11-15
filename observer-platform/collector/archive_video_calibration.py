"""Describe sealed-video intrinsics from frozen capture evidence only.

No registry, ROS, camera or filesystem access occurs here.  A matrix is exposed
for the video only when the finalizer proves an identity or pure-resize path.
"""
from copy import deepcopy
import json
import math


def _size(width, height):
    if any(type(value) is not int or value <= 0 for value in (width, height)):
        return None
    return dict(width=width, height=height)


def _image_info(value, size):
    if (not isinstance(value, dict) or size is None
            or _size(value.get('width'), value.get('height')) != size
            or value.get('distortionModel') != 'plumb_bob'):
        return False
    try:
        for key, count in (('K', 9), ('D', 5), ('R', 9), ('P', 12)):
            values = value.get(key)
            if (not isinstance(values, list) or len(values) != count
                    or any(type(x) not in (int, float) or not math.isfinite(x) for x in values)):
                return False
    except (OverflowError, TypeError, ValueError):
        return False
    return (value['K'][0] > 0 and value['K'][4] > 0
            and value['P'][0] > 0 and value['P'][5] > 0
            and value['K'][6:] == [0, 0, 1] and value['P'][8:] == [0, 0, 1, 0])


def calibration_metadata(metadata, source_details, video_details):
    """Return independent source/video snapshots without changing input objects.

``videoImageTransform`` is proof supplied by the known codec path, rather than
inferred from aspect ratio or the requested preset.  Actual output dimensions
are required even when an encoder claims to have resized the image.
"""
    if 'cameraCalibration' not in metadata:
        return {}
    original = metadata['cameraCalibration']
    try:
        encoded = json.dumps(original, ensure_ascii=False, allow_nan=False)
        if not isinstance(original, dict) or len(encoded.encode('utf-8')) > 16 * 1024:
            raise ValueError('invalid frozen calibration')
        source = json.loads(encoded)
    except (ValueError, TypeError, OverflowError, RecursionError):
        source = dict(state='invalid', errorCode='invalid_saved_calibration')
    source['imageSpace'] = 'capture_image_pixels'
    video = dict(schemaVersion=1, imageSpace='video_image_pixels',
                 sourceImageSpace='capture_image_pixels', sourceCalibrationState=source.get('state'),
                 state='unavailable')
    for key in ('cameraId', 'robotId', 'direction', 'device', 'sourceResultId',
                'sourceArchiveSha256', 'sourceYamlSha256', 'quality'):
        if key in source:
            video[key] = deepcopy(source[key])
    result = dict(cameraCalibration=source, videoCalibration=video)
    source_size = _size(source_details.get('width'), source_details.get('height'))
    target_size = _size(video_details.get('width'), video_details.get('height'))
    video.update(sourceSize=source_size, size=target_size)
    if source.get('state') not in ('matched', 'resized'):
        video.update(state=source.get('state') if source.get('state') in
                     ('invalid', 'resolution_mismatch') else 'unavailable',
                     reason='source_calibration_unavailable')
        return result
    if source_size is None or target_size is None:
        video.update(reason='image_dimensions_unavailable')
        return result
    image_info = source.get('currentImageInfo')
    if source.get('size') != source_size or not _image_info(image_info, source_size):
        video.update(state='resolution_mismatch', reason='capture_calibration_dimensions_mismatch')
        return result
    proof = video_details.get('videoImageTransform')
    identity_proof = (proof is None or (isinstance(proof, dict)
        and proof.get('kind') == 'identity' and proof.get('crop') is False
        and proof.get('letterbox') is False and proof.get('sourceSize') == source_size
        and proof.get('size') == target_size))
    identity = (source_size == target_size and video_details.get('resized') is False
                and identity_proof)
    if identity:
        transform = dict(kind='identity', crop=False, letterbox=False)
    elif (video_details.get('resized') is True and isinstance(proof, dict)
            and proof.get('kind') == 'resize' and proof.get('crop') is False
            and proof.get('letterbox') is False and proof.get('sourceSize') == source_size
            and proof.get('size') == target_size):
        transform = dict(kind='resize', crop=False, letterbox=False,
                         scaleX=target_size['width'] / source_size['width'],
                         scaleY=target_size['height'] / source_size['height'])
    else:
        video.update(reason='image_transform_unverified')
        return result
    current = deepcopy(image_info)
    current.update(target_size)
    if transform['kind'] == 'resize':
        sx, sy = transform['scaleX'], transform['scaleY']
        for key, columns in (('K', 3), ('P', 4)):
            current[key] = [number * (sx if i < columns else sy if i < 2 * columns else 1)
                            for i, number in enumerate(current[key])]
    video.update(state='matched' if identity else 'resized', transform=transform,
                 currentImageInfo=current)
    return result
