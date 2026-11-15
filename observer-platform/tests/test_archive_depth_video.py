"""Explicit visual-only depth archive checks using generated pixels, never devices."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from collector.archive_depth_video import pseudocolor_depth16
from collector.archive_video import finalize_pending, VideoFinalizeError

CODECS = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                           reason='real ffmpeg and ffprobe required')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fixture(path, *, mode='pseudocolor', count=16, topic='/berxel/depth/image_raw',
            sensor='Berxel深度', encoding='16UC1'):
    path.mkdir(); (path/'bag').mkdir()
    original = b'ISOLATED RAW DEPTH SOURCE FIXTURE'
    (path/'bag/raw.db3').write_bytes(original)
    width, height = 160, 120
    rng = np.random.default_rng(314)
    rows, canonical, raw_payloads, stamps = [], [], [], []
    for index in range(count):
        values = rng.integers(1, 5001, (height, width), dtype=np.uint16)
        values[0, :4] = [0, 1, 5000, 65535]
        canonical.append(values.astype('<u2').tobytes())
        big = bool(index % 2)
        source = values.astype('>u2' if big else '<u2').tobytes()
        source = b''.join(source[row*width*2:(row+1)*width*2] + b'\x12\x34'
                          for row in range(height))
        raw_payloads.append(source)
        message = SimpleNamespace(data=source, width=width, height=height,
            encoding=encoding, step=width*2+2, is_bigendian=big,
            header=SimpleNamespace(frame_id='synthetic_depth_frame',
                                   stamp=SimpleNamespace(sec=123, nanosec=index)))
        stamp = 1_000_000_000 + index*40_000_000 + index % 3*1_000_000
        stamps.append(stamp)
        rows.append((topic, 'sensor_msgs/msg/Image', stamp, message))
    rows.insert(0, ('/berxel/depth/camera_info', 'sensor_msgs/msg/CameraInfo',
                    999_999_999, b'synthetic-camera-info-cdr'))
    meta = {'scope': 'garage', 'source': 'native-ros-cdr', 'videoRequired': True,
            'archiveProfile': 'compact-v1', 'videoArchivePreset': '720p-balanced-v1',
            'receivedFrames': count, 'receivedMessages': count+1,
            'videoImageTopics': [topic], 'topics': {
                topic: {'type': 'sensor_msgs/msg/Image', 'count': count},
                '/berxel/depth/camera_info': {'type': 'sensor_msgs/msg/CameraInfo', 'count': 1}}}
    if mode is not None:
        meta['depthVideoMode'] = mode
    manifest = {'schemaVersion': 1, 'id': path.name, 'day': '2026-10-31', 'sensor': sensor,
                'label': 'synthetic-depth', 'robotId': 'isolated-robot',
                'startedAt': '2026-10-31T00:00:00Z', 'endAt': '2026-10-31T00:00:01Z',
                'files': ['bag/raw.db3'], 'metadata': meta}
    (path/'video-pending.json').write_text(json.dumps(manifest))
    return rows, canonical, raw_payloads, stamps, original


def inspect(path):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_packets', '-show_streams', '-of', 'json', str(path)], text=True))


def attachment(video, output, number=0):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-dump_attachment:t:'+str(number),
        str(output), '-i', str(video), '-map', '0:v:0', '-c:v', 'copy', '-f', 'null', '-'],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return output.read_bytes()


def test_depth_visualization_uses_existing_inverse_turbo_range_and_black_invalid_pixels():
    values = np.array([[0, 1, 500, 2500, 5000, 65535]], dtype='<u2')
    expected_scaled = np.array([[0, 0, 25, 127, 255, 255]], dtype=np.uint8)
    expected = cv2.applyColorMap(255-expected_scaled, cv2.COLORMAP_TURBO)
    expected[0, 0] = 0
    assert pseudocolor_depth16(values.tobytes(), 6, 1) == expected.tobytes()


@pytest.mark.parametrize('pixels,width,height', [(b'', 1, 1), (b'12', True, 1),
    (b'12', 1, 0), (b'12', 8193, 1), (bytearray(b'12'), 1, 1)])
def test_invalid_canonical_depth_is_rejected(pixels, width, height):
    with pytest.raises(ValueError):
        pseudocolor_depth16(pixels, width, height)


@CODECS
def test_new_visual_only_depth_is_balanced_hevc_with_all_times_and_provenance(tmp_path):
    path = tmp_path/'visual'
    rows, canonical, original, stamps, bag = fixture(path)
    pending = (path/'video-pending.json').read_bytes()
    result = finalize_pending(path, reserve_bytes=0, colour_backend='software',
                              ros_reader=lambda *_: iter(rows))
    meta = result['metadata']; actual = inspect(path/'video.mkv')
    assert meta['videoCodec'] == 'hevc' and meta['videoArchivePresetApplied'] is True
    assert meta['videoArchivePreset'] == '720p-balanced-v1' and meta['videoBitrateBps'] == 2_000_000
    assert meta['losslessPixelValues'] is False and meta['depthMeasurementAvailable'] is False
    assert meta['depthRepresentation'] == 'pseudocolor' and meta['depthVideoMode'] == 'pseudocolor'
    assert meta['depthVisualizationRangeMm'] == [0, 5000] and meta['depthVisualizationPalette'] == 'TURBO'
    assert meta['originalDepthPixelFormat'] == meta['sourcePixelFormat'] == 'gray16le'
    assert meta['videoInputPixelFormat'] == 'bgr24'
    assert meta['receivedFrames'] == len(canonical) == len(actual['packets'])
    assert [round(float(p['pts_time'])*1_000_000) for p in actual['packets']] == [
        (stamp-stamps[0])//1000 for stamp in stamps]
    indexes = [json.loads(line) for line in attachment(path/'video.mkv', tmp_path/'index').splitlines()]
    assert len(indexes) == len(canonical)
    for index, row in enumerate(indexes):
        assert row['encoding'] == '16UC1' and row['pixelFormat'] == 'bgr24'
        assert row['originalDepthPixelFormat'] == 'gray16le'
        assert row['originalDepthPixelSha256'] == digest(canonical[index])
        assert row['sourcePayloadSha256'] == digest(original[index])
        assert row['pixelSha256'] == digest(pseudocolor_depth16(canonical[index], 160, 120))
        assert row['pixelBytes'] == 160*120*3 and 'rowPaddingBase64' not in row
        assert row['depthMeasurementAvailable'] is False
        assert row['sourceHeaderStamp'] == {'sec': 123, 'nanosec': index}
    info = json.loads(attachment(path/'video.mkv', tmp_path/'info', 1))
    assert base64.b64decode(info['cdrBase64']) == b'synthetic-camera-info-cdr'
    assert (path/'bag/raw.db3').read_bytes() == bag and (path/'video-pending.json').read_bytes() == pending


@CODECS
def test_visual_fallback_never_claims_raw_depth_is_lossless_or_measurable(tmp_path, monkeypatch):
    from collector import archive_hevc
    path = tmp_path/'fallback'
    rows, canonical, _, _, bag = fixture(path, count=2)
    def unavailable(*args, **kwargs):
        raise archive_hevc.HevcCompressionError('synthetic-unavailable')
    monkeypatch.setattr(archive_hevc, 'compress_rgb_mkv', unavailable)
    result = finalize_pending(path, reserve_bytes=0, ros_reader=lambda *_: iter(rows))
    meta = result['metadata']
    assert meta['videoCodec'] == 'ffv1' and meta['videoCompression'] == 'pseudocolor-fallback'
    assert meta['losslessPixelValues'] is False and meta['depthMeasurementAvailable'] is False
    decoded = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path/'video.mkv'),
        '-map', '0:v:0', '-vsync', '0', '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'])
    assert decoded == b''.join(pseudocolor_depth16(frame, 160, 120) for frame in canonical)
    assert (path/'bag/raw.db3').read_bytes() == bag


@CODECS
def test_absent_depth_policy_preserves_original_exact_16bit_behavior(tmp_path):
    path = tmp_path/'legacy'
    rows, canonical, _, _, bag = fixture(path, mode=None, count=2)
    result = finalize_pending(path, reserve_bytes=0, ros_reader=lambda *_: iter(rows))
    meta = result['metadata']
    assert meta['videoCodec'] == 'ffv1' and meta['losslessPixelValues'] is True
    assert 'depthRepresentation' not in meta and 'depthMeasurementAvailable' not in meta
    decoded = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path/'video.mkv'),
        '-map', '0:v:0', '-vsync', '0', '-f', 'rawvideo', '-pix_fmt', 'gray16le', 'pipe:1'])
    assert decoded == b''.join(canonical) and (path/'bag/raw.db3').read_bytes() == bag


@CODECS
@pytest.mark.parametrize('mode,topic,sensor,error', [
    ('pseudocolor', '/other/depth', 'Berxel深度', 'source_mismatch'),
    ('pseudocolor', '/berxel/depth/image_raw', 'Berxel彩色', 'sensor_mismatch'),
    ('unexpected', '/berxel/depth/image_raw', 'Berxel深度', 'mode_invalid'),
])
def test_explicit_depth_policy_rejects_wrong_sources_without_publishing(tmp_path, mode, topic, sensor, error):
    path = tmp_path/'bad'
    rows, _, _, _, bag = fixture(path, mode=mode, count=1, topic=topic, sensor=sensor)
    with pytest.raises(VideoFinalizeError, match=error):
        finalize_pending(path, reserve_bytes=0, ros_reader=lambda *_: iter(rows))
    assert not (path/'archive.json').exists() and (path/'bag/raw.db3').read_bytes() == bag
