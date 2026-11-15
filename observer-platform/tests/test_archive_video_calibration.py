"""Frozen native and derived-video pixel coordinates stay distinct, offline."""
from copy import deepcopy
import hashlib
import json

import pytest

from collector import archive_hevc, archive_video
from collector.archive_video_calibration import calibration_metadata


def frozen(width=1920, height=1080):
    info = dict(width=width, height=height, distortionModel='plumb_bob',
        K=[900, 1, width / 2, 0, 902, height / 2, 0, 0, 1],
        D=[.1, -.2, .003, .004, .05], R=[1, 0, 0, 0, 1, 0, 0, 0, 1],
        P=[890, 1, width / 2, 4, 0, 892, height / 2, 8, 0, 0, 1, 0])
    return dict(state='matched', cameraId='camera1', direction='front', robotId='fixture-robot',
        device='/FIXTURE/usb-camera', sourceResultId='result-original',
        sourceArchiveSha256='a' * 64, sourceYamlSha256='b' * 64,
        quality='candidate_no_independent_validation',
        size=dict(width=width, height=height), nativeSize=dict(width=width, height=height),
        nativeImageInfo=deepcopy(info), currentImageInfo=info, transform=dict(kind='identity'))


def details(width, height, *, resized=False, source=None, proof=True):
    value = dict(width=width, height=height, resized=resized)
    if source and proof:
        value['videoImageTransform'] = dict(kind='resize' if resized else 'identity',
            sourceSize=dict(width=source[0], height=source[1]),
            size=dict(width=width, height=height), crop=False, letterbox=False)
    return value


@pytest.mark.parametrize('original,target', [((1296, 972), (960, 720)),
    ((1920, 1080), (1280, 720)), ((2592, 1944), (960, 720)),
    ((1922, 1082), (1278, 720))])
def test_resize_scales_k_p_rows_and_retains_raw_provenance(original, target):
    metadata = dict(cameraCalibration=frozen(*original))
    before = deepcopy(metadata)
    result = calibration_metadata(metadata, details(*original),
        details(*target, resized=True, source=original))
    assert metadata == before
    assert result['cameraCalibration']['imageSpace'] == 'capture_image_pixels'
    assert result['cameraCalibration']['currentImageInfo'] == before['cameraCalibration']['currentImageInfo']
    video = result['videoCalibration']
    assert video['imageSpace'] == 'video_image_pixels' and video['state'] == 'resized'
    assert video['sourceResultId'] == 'result-original'
    source = before['cameraCalibration']['currentImageInfo']
    actual = video['currentImageInfo']
    sx, sy = target[0] / original[0], target[1] / original[1]
    assert actual['K'] == pytest.approx([x * (sx if i < 3 else sy if i < 6 else 1)
                                       for i, x in enumerate(source['K'])])
    assert actual['P'] == pytest.approx([x * (sx if i < 4 else sy if i < 8 else 1)
                                       for i, x in enumerate(source['P'])])
    assert actual['D'] == source['D'] and actual['R'] == source['R']
    assert (actual['width'], actual['height']) == target
    assert video['transform'] == dict(kind='resize', scaleX=sx, scaleY=sy, crop=False, letterbox=False)
    actual['D'][0] = 123
    assert source['D'][0] == .1 and metadata == before


def test_unchanged_video_keeps_original_matrix_with_independent_coordinate_labels():
    native = frozen()
    result = calibration_metadata(dict(cameraCalibration=native), details(1920, 1080), details(1920, 1080))
    assert result['videoCalibration']['state'] == 'matched'
    assert result['videoCalibration']['currentImageInfo'] == native['currentImageInfo']
    assert result['videoCalibration']['transform']['kind'] == 'identity'
    assert result['cameraCalibration']['imageSpace'] != result['videoCalibration']['imageSpace']


@pytest.mark.parametrize('invalid', ['crop', 'letterbox', 'dimensions', 'kind'])
def test_same_size_does_not_override_conflicting_transform_evidence(invalid):
    target = details(1920, 1080, source=(1920, 1080))
    proof = target['videoImageTransform']
    if invalid in ('crop', 'letterbox'):
        proof[invalid] = True
    elif invalid == 'dimensions':
        proof['sourceSize']['width'] = 2000
    else:
        proof['kind'] = 'rotation'
    result = calibration_metadata(dict(cameraCalibration=frozen()), details(1920, 1080), target)
    assert result['videoCalibration']['state'] == 'unavailable'
    assert 'currentImageInfo' not in result['videoCalibration']


@pytest.mark.parametrize('problem', ['missing_width', 'bool_width', 'unknown_transform', 'crop',
    'letterbox', 'proof_source_mismatch', 'matrix_source_mismatch', 'invalid',
    'resolution_mismatch', 'missing_matrix', 'nonfinite', 'huge_integer', 'bad_shape'])
def test_unverified_or_missing_evidence_never_exposes_video_matrices(problem):
    metadata = dict(cameraCalibration=frozen())
    source, target = details(1920, 1080), details(1280, 720, resized=True, source=(1920, 1080))
    if problem == 'missing_width':
        target.pop('width')
    elif problem == 'bool_width':
        target['width'] = True
    elif problem == 'unknown_transform':
        target.pop('videoImageTransform')
    elif problem in ('crop', 'letterbox'):
        target['videoImageTransform'][problem] = True
    elif problem == 'proof_source_mismatch':
        target['videoImageTransform']['sourceSize']['width'] = 2000
    elif problem == 'matrix_source_mismatch':
        metadata['cameraCalibration']['size']['width'] = 2000
    elif problem in ('invalid', 'resolution_mismatch'):
        metadata['cameraCalibration']['state'] = problem
    elif problem == 'missing_matrix':
        metadata['cameraCalibration'].pop('currentImageInfo')
    elif problem == 'nonfinite':
        metadata['cameraCalibration']['currentImageInfo']['K'][0] = float('nan')
    elif problem == 'huge_integer':
        metadata['cameraCalibration']['currentImageInfo']['K'][0] = 10 ** 1000
    else:
        metadata['cameraCalibration']['currentImageInfo']['K'] = [1]
    result = calibration_metadata(metadata, source, target)
    assert result['videoCalibration']['state'] not in ('matched', 'resized')
    assert 'currentImageInfo' not in result['videoCalibration']
    assert not any(key in result['videoCalibration'] for key in ('K', 'D', 'R', 'P'))


def test_legacy_without_snapshot_remains_unchanged():
    metadata = dict(source='native-camera-jpeg')
    assert calibration_metadata(metadata, details(1920, 1080), details(1280, 720, resized=True)) == {}
    assert metadata == dict(source='native-camera-jpeg')


def test_finalizer_uses_sealed_snapshot_and_actual_codec_dimensions_without_touching_sources(tmp_path, monkeypatch):
    segment = tmp_path / 'segment'
    segment.mkdir()
    files = dict(**{'frames.mjpg': b'FIXTURE NATIVE BYTES', 'index.jsonl': b'FIXTURE NATIVE INDEX'})
    for name, value in files.items():
        (segment / name).write_bytes(value)
    metadata = dict(scope='garage', source='native-camera-jpeg', videoRequired=True,
        archiveProfile='compact-v1', videoArchivePreset='720p-balanced-v1', receivedFrames=2,
        cameraCalibration=frozen(1296, 972))
    pending = dict(schemaVersion=1, id='fixture-segment', files=list(files), metadata=metadata)
    pending_path = segment / 'video-pending.json'
    pending_path.write_text(json.dumps(pending))
    before = {name: (segment / name).read_bytes() for name in [*files, 'video-pending.json']}

    def native(_segment, _manifest, _work, output, *_args):
        output.write_bytes(b'FIXTURE VERIFIED ORIGINAL MKV')
        return dict(videoCodec='mjpeg', receivedFrames=2, width=1296, height=972,
                    resized=False, reencoded=False)

    def compact(source, output, **options):
        assert source.read_bytes() == b'FIXTURE VERIFIED ORIGINAL MKV'
        assert options['max_width'] == 1280 and options['max_height'] == 720
        output.write_bytes(b'FIXTURE VERIFIED COMPACT MKV')
        return dict(videoCodec='hevc', receivedFrames=2, width=960, height=720,
                    sourceWidth=1296, sourceHeight=972, targetWidth=960, targetHeight=720,
                    resized=True, reencoded=True)

    def forbidden_registry(*_args, **_kwargs):
        raise AssertionError('sealed video must not consult the current calibration registry')

    from collector import camera_calibration
    monkeypatch.setattr(camera_calibration, 'load_registry', forbidden_registry)
    monkeypatch.setattr(camera_calibration, 'get_camera_calibration', forbidden_registry)
    monkeypatch.setattr(archive_video, '_native_video', native)
    monkeypatch.setattr(archive_hevc, 'compress_rgb_mkv', compact)
    monkeypatch.delenv('ARCHIVE_DISCARD_RAW_AFTER_DERIVATIVE', raising=False)
    result = archive_video.finalize_pending(segment, reserve_bytes=0)
    assert result['files'] == ['video.mkv']
    assert (segment / 'video.mkv').read_bytes() == b'FIXTURE VERIFIED COMPACT MKV'
    assert result['metadata']['cameraCalibration']['currentImageInfo']['width'] == 1296
    assert result['metadata']['videoCalibration']['currentImageInfo']['width'] == 960
    assert result['metadata']['videoCalibration']['sourceResultId'] == 'result-original'
    assert result['metadata']['videoCalibration']['currentImageInfo']['K'][0] == pytest.approx(900 * 960 / 1296)
    assert {name: (segment / name).read_bytes() for name in before} == before
    assert result['metadata']['sourceFiles'] == [dict(relativePath=name, size=len(value),
        sha256=hashlib.sha256(value).hexdigest()) for name, value in files.items()]
    assert archive_video.finalize_pending(segment, reserve_bytes=0) == result
