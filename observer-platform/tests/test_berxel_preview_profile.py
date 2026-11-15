"""Hardware-free codec and ROS-boundary checks for the optional preview profile."""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import cv2
import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'deploy/jetson/berxel_preview'
LEGACY = ROOT / 'collector/legacy'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    class Node:
        def __init__(self, *args, **kwargs):
            self.timers = []

        def create_publisher(self, *args):
            published = []
            return SimpleNamespace(published=published, publish=published.append)

        def create_subscription(self, *args):
            return args

        def create_timer(self, interval, callback):
            self.timers.append((interval, callback))

    for name in ('rclpy', 'rclpy.node', 'rclpy.qos', 'sensor_msgs', 'sensor_msgs.msg',
                 'std_msgs', 'std_msgs.msg'):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules['rclpy.node'].Node = Node
    sys.modules['rclpy.qos'].QoSProfile = lambda **kwargs: kwargs
    sys.modules['rclpy.qos'].ReliabilityPolicy = SimpleNamespace(BEST_EFFORT=1, RELIABLE=2)
    for name in ('Image', 'CompressedImage', 'PointCloud2'):
        setattr(sys.modules['sensor_msgs.msg'], name, SimpleNamespace)
    sys.modules['std_msgs.msg'].String = SimpleNamespace
    codec = load_module('preview_camera_codec', SOURCE / 'camera_codec.py')
    points = load_module('preview_point_codec', SOURCE / 'point_codec.py')
    monkeypatch.setitem(sys.modules, 'camera_codec', codec)
    monkeypatch.setitem(sys.modules, 'point_codec', points)
    capture = ModuleType('capture_health')
    capture.writes = []
    capture.write_capture_health = capture.writes.append
    monkeypatch.setitem(sys.modules, 'capture_health', capture)
    encoder = load_module('preview_encoder', SOURCE / 'jetson_camera_encoder.py')
    for key in ('MAX_EDGE', 'JPEG_QUALITY', 'IMAGE_FPS', 'CLOUD_POINTS', 'CLOUD_FPS'):
        monkeypatch.delenv('BERXEL_PREVIEW_' + key, raising=False)
    return codec, points, encoder


def image_message(width=640, height=400, depth=False):
    if depth:
        image = (np.arange(width * height, dtype=np.uint32) % 5000).astype('<u2')
        step, encoding = width * 2, '16UC1'
    else:
        image = (np.arange(width * height * 3, dtype=np.uint32) % 251).astype('u1')
        step, encoding = width * 3, 'rgb8'
    return SimpleNamespace(width=width, height=height, step=step, encoding=encoding,
                           is_bigendian=False, data=bytearray(image.tobytes()),
                           header=SimpleNamespace(frame_id='camera_original',
                                                  stamp=SimpleNamespace(sec=1234, nanosec=567)))


def cloud_message(points):
    array = np.asarray(points, dtype='<f4')
    return SimpleNamespace(width=len(array), height=1, row_step=len(array) * 12,
                           point_step=12, data=bytearray(array.tobytes()), is_bigendian=False,
                           fields=[SimpleNamespace(name=name, offset=i * 4, datatype=7)
                                   for i, name in enumerate(('x', 'y', 'z'))],
                           header=SimpleNamespace(frame_id='berxel_original'))


@pytest.mark.parametrize('depth', [False, True])
@pytest.mark.parametrize('width,height,edge,expected',
                         [(640, 400, 480, (480, 300)), (400, 640, 480, (300, 480)),
                          (1280, 960, 320, (320, 240)), (160, 120, 480, (160, 120))])
def test_preview_bounds_and_source_unchanged(modules, depth, width, height, edge, expected):
    codec = modules[0]
    message = image_message(width, height, depth)
    original = bytes(message.data)
    jpeg, out_width, out_height = codec.image_to_jpeg(
        message, depth=depth, max_edge=edge, jpeg_quality=45)
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert (out_width, out_height) == expected
    assert decoded.shape[:2] == expected[::-1]
    assert bytes(message.data) == original
    assert message.header.stamp.sec == 1234


@pytest.mark.parametrize('depth', [False, True])
def test_default_codec_is_byte_identical_to_legacy(modules, depth):
    legacy = load_module('legacy_camera_codec', LEGACY / 'camera_codec.py')
    message = image_message(800, 1000, depth)
    actual = modules[0].image_to_jpeg(message, depth=depth)
    assert actual == legacy.image_to_jpeg(message, depth=depth)
    assert actual[1:] == (576, 720)


@pytest.mark.parametrize('options', [
    {'max_edge': 159}, {'max_edge': 961}, {'max_edge': 480.0}, {'max_edge': True},
    {'max_edge': float('nan')}, {'jpeg_quality': 19}, {'jpeg_quality': 86},
    {'jpeg_quality': 45.0}, {'jpeg_quality': float('inf')},
])
def test_codec_rejects_unbounded_parameters(modules, options):
    with pytest.raises(ValueError):
        modules[0].image_to_jpeg(image_message(), **options)


def test_profile_defaults_and_boundaries(modules):
    load = modules[2].load_preview_profile
    assert load({}) == dict(max_edge=None, jpeg_quality=70, image_fps=5.0,
                           cloud_points=3000, cloud_fps=2.0)
    for edge, quality, image_fps, points, cloud_fps in [
        ('160', '20', '0.5', '100', '0.5'), ('960', '85', '10', '3000', '2'),
    ]:
        values = dict(MAX_EDGE=edge, JPEG_QUALITY=quality, IMAGE_FPS=image_fps,
                      CLOUD_POINTS=points, CLOUD_FPS=cloud_fps)
        parsed = load({'BERXEL_PREVIEW_' + key: value for key, value in values.items()})
        assert parsed['max_edge'] == int(edge)
        assert parsed['image_fps'] == float(image_fps)


@pytest.mark.parametrize('key,values', [
    ('MAX_EDGE', ['159', '961', '480.0', '', 'nan', 'inf']),
    ('JPEG_QUALITY', ['19', '86', '45.1', 'nan', 'inf']),
    ('IMAGE_FPS', ['0', '0.49', '10.01', 'nan', 'inf', '-inf']),
    ('CLOUD_POINTS', ['99', '3001', '900.1', 'nan', 'inf']),
    ('CLOUD_FPS', ['0', '0.49', '2.01', 'nan', 'inf', '-inf']),
])
def test_profile_rejects_invalid_values(modules, key, values):
    for value in values:
        with pytest.raises(ValueError, match='BERXEL_PREVIEW_' + key):
            modules[2].load_preview_profile({'BERXEL_PREVIEW_' + key: value})


def configure(monkeypatch):
    for key, value in dict(MAX_EDGE='480', JPEG_QUALITY='45', IMAGE_FPS='10',
                           CLOUD_POINTS='900', CLOUD_FPS='2').items():
        monkeypatch.setenv('BERXEL_PREVIEW_' + key, value)


def test_encoder_keeps_fisheye_stamp_and_consumes_frames_once(modules, monkeypatch):
    configure(monkeypatch)
    node = modules[2].CameraEncoder()
    assert [interval for interval, callback in node.timers] == [0.2, 0.1, 1.0, 0.5]
    sources = {key: image_message(depth=(key == 'depth'))
               for key in ('fisheye', 'color', 'depth')}
    for key, message in sources.items():
        node.receive(key, message)
    node.timers[0][1]()
    assert len(node.outputs['fisheye'].published) == 1
    assert not node.outputs['color'].published
    node.timers[1][1]()
    for key, message in sources.items():
        output = node.outputs[key].published[0]
        assert output.header is message.header
        assert output.header.stamp.sec == 1234
        expected = ((640, 400) if key == 'fisheye' else (480, 386 if key == 'depth' else 300))
        assert (node.received[key]['previewWidth'], node.received[key]['previewHeight']) == expected
    assert node.outputs['fisheye'].published[0].data == modules[0].image_to_jpeg(sources['fisheye'])[0]
    node.encode_latest()
    assert all(len(output.published) == 1 for output in node.outputs.values())
    broken = image_message()
    broken.data = b''
    node.receive('color', broken)
    node.encode_latest()
    node.encode_latest()
    assert len(node.outputs['color'].published) == 1
    assert 'color' in node.errors
    node.receive('color', image_message())
    node.encode_latest()
    assert len(node.outputs['color'].published) == 2
    assert 'color' not in node.errors


def test_cloud_finite_precision_real_identity_and_no_repeat(modules, monkeypatch):
    configure(monkeypatch)
    node = modules[2].CameraEncoder()
    values = np.arange(6000 * 3, dtype=np.float32).reshape(-1, 3) / 999
    values[0, 0], values[7, 1] = np.nan, np.inf
    message = cloud_message(values)
    original = bytes(message.data)
    node.receive_cloud(message)
    node.publish_cloud()
    output = node.cloud_output.published[0].data
    payload = json.loads(output)
    assert payload['totalPoints'] == 6000
    assert payload['frameId'] == 'berxel_original'
    # ceil(6000/900) = 7; only true source samples with all coordinates finite.
    expected = [[round(float(value), 3) for value in point] for point in values[::7]
                if np.isfinite(point).all()]
    assert payload['points'] == expected
    assert len(expected) <= 900
    assert ': ' not in output and ', ' not in output
    assert bytes(message.data) == original
    node.publish_cloud()
    message.data = b''
    node.receive_cloud(message)
    node.publish_cloud()
    node.publish_cloud()
    assert len(node.cloud_output.published) == 1


def test_cloud_small_input_does_not_invent_points(modules):
    message = cloud_message([[0.12345, -1.23456, 2.34567], [np.inf, 1, 1], [0, 0, 0]])
    assert modules[1].cloud_points(message, limit=900) == [[0.123, -1.235, 2.346], [0.0, 0.0, 0.0]]



def depth_patch_message(patch, encoding='mono16', bigendian=False, padding=0):
    width, height = 320, 200
    scalar = 'f4' if encoding == '32FC1' else 'u2'
    dtype = np.dtype(('>' if bigendian else '<') + scalar)
    step = width * dtype.itemsize + padding
    buffer = bytearray([238]) * (step * height)
    pixels = np.ndarray((height, width), dtype=dtype, buffer=buffer,
                        strides=(step, dtype.itemsize))
    pixels[:] = 2.0 if scalar == 'f4' else 2000
    pixels[height // 2 - 2:height // 2 + 3, width // 2 - 2:width // 2 + 3] = patch
    return SimpleNamespace(width=width, height=height, step=step, encoding=encoding,
        is_bigendian=bigendian, data=buffer, header=SimpleNamespace(frame_id='depth_original'))


@pytest.mark.parametrize('encoding', ['mono16', '16UC1', '32FC1'])
@pytest.mark.parametrize('bigendian,padding', [(False, 0), (True, 12)])
def test_same_frame_center_depth_uses_raw_units_stride_and_scaled_pixels(modules, monkeypatch, encoding, bigendian, padding):
    codec = modules[0]
    patch = np.full((5, 5), 1.234 if encoding == '32FC1' else 1234)
    message = depth_patch_message(patch, encoding, bigendian, padding)
    original = bytes(message.data)
    labels, rectangles = [], []
    put_text, rectangle = cv2.putText, cv2.rectangle
    def capture_text(image, text, *args, **kwargs):
        labels.append((image.shape, text))
        return put_text(image, text, *args, **kwargs)
    def capture_rectangle(image, p1, p2, *args, **kwargs):
        rectangles.append((image.shape, p1, p2))
        return rectangle(image, p1, p2, *args, **kwargs)
    monkeypatch.setattr(cv2, 'putText', capture_text)
    monkeypatch.setattr(cv2, 'rectangle', capture_rectangle)
    jpeg, width, height = codec.image_to_jpeg(message, depth=True, max_edge=160,
                                             jpeg_quality=45, annotate_depth=True)
    assert (width, height) == (160, 186)
    assert any(shape == (100, 160, 3) and text == 'Z: 123.4 cm' for shape, text in labels)
    assert any(text == 'Depth color (m)' for _, text in labels)
    assert any(text == '>=5m' for _, text in labels)
    assert rectangles[0] == ((100, 160, 3), (79, 49), (81, 51))
    assert bytes(message.data) == original
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == (186, 160, 3)


@pytest.mark.parametrize('values,scalar,expected', [
    ([1234] * 25, 'u2', 123.4),
    ([0] * 25, 'u2', None),
    ([4095] * 25, 'u2', None),
    ([8191] * 25, 'u2', None),
    ([65535] * 25, 'u2', None),
    ([1000] * 12 + [0] * 13, 'u2', None),
    ([1000] * 13 + [0] * 12, 'u2', 100.0),
    ([1000] * 12 + [3000] * 12 + [0], 'u2', None),
    ([1000] * 13 + [3000] * 12, 'u2', 100.0),
    ([float('nan'), float('inf'), -1.0, 0.0, 9.0] * 5, 'f4', None),
    ([1.234] * 13 + [float('nan')] * 12, 'f4', 123.4),
])
def test_center_depth_rejects_invalid_and_mixed_edges_without_inventing_range(modules, values, scalar, expected):
    dtype = np.float32 if scalar == 'f4' else np.uint16
    distance, roi = modules[0].center_depth_cm(np.asarray(values, dtype=dtype).reshape(5, 5), scalar)
    assert roi == (0, 0, 5, 5)
    if expected is None:
        assert distance is None
    else:
        assert distance == pytest.approx(expected, abs=.001)


def test_depth_invalid_label_and_color_opt_in_does_not_modify_color(modules, monkeypatch):
    codec = modules[0]
    labels = []
    put_text = cv2.putText
    def capture(image, text, *args, **kwargs):
        labels.append(text)
        return put_text(image, text, *args, **kwargs)
    monkeypatch.setattr(cv2, 'putText', capture)
    message = depth_patch_message(np.zeros((5, 5), dtype=np.uint16))
    codec.image_to_jpeg(message, depth=True, annotate_depth=True)
    assert 'Z: -- cm' in labels
    assert 'Depth color (m)' in labels
    color = image_message()
    assert codec.image_to_jpeg(color, annotate_depth=True) == codec.image_to_jpeg(color)
    assert labels.count('Z: -- cm') == 1


def test_encoder_only_enables_same_frame_distance_on_depth(modules, monkeypatch):
    configure(monkeypatch)
    node = modules[2].CameraEncoder()
    calls = []
    original = modules[2].image_to_jpeg
    def capture(message, depth=False, **kwargs):
        calls.append((depth, dict(kwargs)))
        return original(message, depth=depth, **kwargs)
    monkeypatch.setattr(modules[2], 'image_to_jpeg', capture)
    for key in ('fisheye', 'color', 'depth'):
        node.receive(key, image_message(depth=key == 'depth'))
    node.encode_latest()
    assert calls[0] == (False, {})
    assert calls[1][0] is False and calls[1][1]['annotate_depth'] is False
    assert calls[2][0] is True and calls[2][1]['annotate_depth'] is True
    assert all(len(output.published) == 1 for output in node.outputs.values())


def frame_report(at, sequence, *, session='encoder-session', received=None, published=None):
    return {key: dict(sessionId=session, reportSequence=sequence, reportedMonotonic=at,
        receivedFrames=received if received is not None else 30 * sequence,
        publishedFrames=published if published is not None else 5 * sequence,
        sourceAgeS=.01, previewAgeS=.1, error=None, width=640, height=400,
        previewWidth=480, previewHeight=386 if key == 'depth' else 300,
        sourcePublisherCount=0) for key in ('color', 'depth')}


def test_frame_health_counts_valid_output_and_retains_output_dimensions_between_raw_frames(modules, monkeypatch):
    encoder = modules[2]
    now = [100.0]
    monkeypatch.setattr(encoder.time, 'monotonic', lambda: now[0])
    node = encoder.CameraEncoder()
    node.count_publishers = lambda topic: 0
    node.publish_health()
    empty = json.loads(node.health.published[-1].data)
    assert empty['depth']['receivedFrames'] == empty['depth']['publishedFrames'] == 0
    assert empty['depth']['sourceAgeS'] is None and empty['depth']['previewAgeS'] is None
    for key in ('color', 'depth'):
        node.receive(key, image_message(depth=key == 'depth'))
    node.encode_latest(('color', 'depth'))
    now[0] += .2
    node.receive('depth', image_message(depth=True))
    now[0] += .1
    node.publish_health()
    report = json.loads(node.health.published[-1].data)
    assert report['color']['receivedFrames'] == report['color']['publishedFrames'] == 1
    assert report['depth']['receivedFrames'] == 2 and report['depth']['publishedFrames'] == 1
    assert report['depth']['previewWidth'] > 0
    assert report['depth']['sourceAgeS'] == pytest.approx(.1)
    assert report['depth']['previewAgeS'] == pytest.approx(.3)
    assert report['color']['sessionId'] == report['depth']['sessionId']
    assert report['depth']['reportSequence'] == 2
    now[0] += 2
    node.publish_health()
    stale = json.loads(node.health.published[-1].data)
    assert stale['depth']['sourceAgeS'] > 2
    assert stale['depth']['publishedFrames'] == 1


def test_frame_health_never_counts_failed_encoding_as_an_output(modules, monkeypatch):
    encoder = modules[2]
    monkeypatch.setattr(encoder.time, 'monotonic', lambda: 100.0)
    node = encoder.CameraEncoder()
    node.count_publishers = lambda topic: 1
    broken = image_message(depth=True)
    broken.data = b''
    node.receive('depth', broken)
    node.encode_latest(('depth',))
    node.publish_health()
    report = json.loads(node.health.published[-1].data)['depth']
    assert report['receivedFrames'] == 1 and report['publishedFrames'] == 0
    assert report['previewAgeS'] is None and report['error']


def test_readiness_requires_three_seconds_of_both_actual_inputs_and_outputs_without_fisheye(modules):
    verdict = modules[2].FrameReadiness()
    for index in range(3):
        assert verdict.observe(frame_report(100 + index, index + 1), now=100 + index) is None
    result = verdict.observe(frame_report(103, 4), now=103)
    assert result['ok'] and result['dataVerified']
    assert result['sampleWindowSeconds'] == 3 and result['reports'] == 4
    assert result['channels']['depth']['receivedDelta'] == 90
    assert result['channels']['color']['publishedDelta'] == 15
    # Real frames are evidence even when the DDS graph query has no publisher yet.
    assert frame_report(100, 1)['depth']['sourcePublisherCount'] == 0


def test_four_fast_reports_are_not_a_three_second_observation(modules):
    verdict = modules[2].FrameReadiness()
    for index in range(4):
        at = 100 + index / 10
        assert verdict.observe(frame_report(at, index + 1), now=at) is None


@pytest.mark.parametrize('field,value,reason', [
    ('sourceAgeS', 1.01, '原始帧已过期'), ('previewAgeS', 1.01, '预览帧已过期'),
    ('sourceAgeS', None, '原始帧已过期'), ('previewAgeS', float('nan'), '预览帧已过期'),
    ('receivedFrames', True, '尚未同时收到'), ('publishedFrames', 0, '尚未同时收到'),
    ('error', 'codec failed', '编码错误'), ('previewWidth', 0, '有效尺寸'),
])
def test_readiness_rejects_stale_or_invalid_channel_even_with_live_process_counts(modules, field, value, reason):
    verdict = modules[2].FrameReadiness()
    assert verdict.observe(frame_report(100, 1), now=100) is None
    report = frame_report(101, 2)
    report['depth'][field] = value
    assert verdict.observe(report, now=101) is None
    assert reason in verdict.last_reason
    assert not verdict.samples


def test_readiness_accrues_status_delivery_age_and_rejects_future_or_old_reports(modules):
    verdict = modules[2].FrameReadiness()
    report = frame_report(100, 1)
    report['depth']['sourceAgeS'] = .9
    assert verdict.observe(report, now=100.2) is None
    assert '原始帧已过期' in verdict.last_reason
    assert verdict.observe(frame_report(100, 1), now=103) is None
    assert '状态已过期' in verdict.last_reason
    assert verdict.observe(frame_report(104, 2), now=103) is None
    assert '状态已过期' in verdict.last_reason


@pytest.mark.parametrize('field', ['receivedFrames', 'publishedFrames', 'reportSequence'])
def test_readiness_stalled_counters_or_replayed_reports_reset_the_entire_window(modules, field):
    verdict = modules[2].FrameReadiness()
    for index in range(3):
        verdict.observe(frame_report(100 + index, index + 1), now=100 + index)
    frozen = frame_report(103, 4)
    if field == 'reportSequence':
        for row in frozen.values(): row[field] = 3
    else:
        frozen['depth'][field] = frame_report(102, 3)['depth'][field]
    assert verdict.observe(frozen, now=103) is None
    assert len(verdict.samples) == 1
    for index in range(2):
        at = 104 + index
        assert verdict.observe(frame_report(at, index + 5), now=at) is None
    assert verdict.observe(frame_report(106, 7), now=106)['ok']


def test_readiness_requires_a_new_full_window_after_process_session_changes(modules):
    verdict = modules[2].FrameReadiness()
    for index in range(3):
        verdict.observe(frame_report(100 + index, index + 1), now=100 + index)
    for index in range(3):
        assert verdict.observe(frame_report(103 + index, index + 1, session='new-process'), now=103 + index) is None
    assert verdict.observe(frame_report(106, 4, session='new-process'), now=106)['ok']


def test_readiness_report_gap_and_mixed_sessions_cannot_be_claimed_as_continuity(modules):
    verdict = modules[2].FrameReadiness()
    verdict.observe(frame_report(100, 1), now=100)
    verdict.observe(frame_report(101, 2), now=101)
    assert verdict.observe(frame_report(105, 3), now=105) is None
    assert len(verdict.samples) == 1
    mixed = frame_report(106, 4)
    mixed['depth']['sessionId'] = 'other-process'
    assert verdict.observe(mixed, now=106) is None
    assert not verdict.samples and '不同会话' in verdict.last_reason


def test_readiness_rejects_legacy_status_instead_of_believing_old_dimensions(modules):
    verdict = modules[2].FrameReadiness()
    old = {key: dict(width=640, height=400, sourceHz=30, sourcePublisherCount=1)
           for key in ('color', 'depth')}
    assert verdict.observe(old, now=100) is None
    assert '请更新预览服务' in verdict.last_reason


@pytest.mark.parametrize('mode', ['ready', 'timeout', 'cancelled', 'exception', 'cleanup-error'])
def test_probe_subscribes_only_to_lightweight_status_and_always_cleans_up(modules, monkeypatch, mode):
    import os
    encoder = modules[2]
    now, calls, callback, profiles = [100.0], [], [None], []
    monkeypatch.setenv('RMW_IMPLEMENTATION', 'original-rmw')
    monkeypatch.setenv('FASTRTPS_DEFAULT_PROFILES_FILE', '/fixture/original-profile.xml')
    monkeypatch.delenv('FASTDDS_DEFAULT_PROFILES_FILE', raising=False)
    before = {key: os.environ.get(key) for key in encoder.PROBE_PROFILE_ENV}
    class ProbeNode:
        def __init__(self, *args, **kwargs):
            calls.append('node')
            assert os.environ['RMW_IMPLEMENTATION'] == 'rmw_fastrtps_cpp'
            profile = Path(os.environ['FASTRTPS_DEFAULT_PROFILES_FILE'])
            assert str(profile) == os.environ['FASTDDS_DEFAULT_PROFILES_FILE']
            assert profile.is_file()
            profiles.append(profile)
        def create_subscription(self, kind, topic, receive, qos):
            calls.append(('subscription', topic, qos))
            callback[0] = receive
            return object()
        def destroy_node(self):
            calls.append('destroy')
            assert profiles[-1].is_file()
            if mode == 'cleanup-error': raise RuntimeError('isolated cleanup failure')
    monkeypatch.setattr(encoder, 'Node', ProbeNode)
    monkeypatch.setattr(encoder.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(encoder.rclpy, 'init', lambda **kwargs: calls.append('init'), raising=False)
    monkeypatch.setattr(encoder.rclpy, 'ok', lambda: True, raising=False)
    monkeypatch.setattr(encoder.rclpy, 'shutdown', lambda: calls.append('shutdown'), raising=False)
    def spin_once(node, timeout_sec):
        assert 0 <= timeout_sec <= .2
        if mode == 'cancelled': raise KeyboardInterrupt()
        if mode == 'exception': raise RuntimeError('isolated executor failure')
        now[0] += 1
        if mode in ('ready', 'cleanup-error'):
            callback[0](SimpleNamespace(data=json.dumps(frame_report(now[0], int(now[0] - 100)))))
    monkeypatch.setattr(encoder.rclpy, 'spin_once', spin_once, raising=False)
    result = encoder.probe_frame_health(timeout=5)
    assert result['state'] == ('failed' if mode in ('exception', 'cleanup-error') else mode)
    assert result['ok'] is (mode == 'ready')
    assert result['probeTransport'] == 'udp-only'
    subscriptions = [call for call in calls if isinstance(call, tuple)]
    assert len(subscriptions) == 1 and subscriptions[0][1] == '/observer_camera/status'
    assert subscriptions[0][2]['reliability'] == encoder.ReliabilityPolicy.RELIABLE
    assert calls[-2:] == ['destroy', 'shutdown']
    assert {key: os.environ.get(key) for key in encoder.PROBE_PROFILE_ENV} == before
    assert profiles and all(not path.exists() and not path.parent.exists() for path in profiles)


@pytest.mark.parametrize('interrupt', [False, True])
def test_probe_udp_profile_is_private_disables_builtin_transports_and_restores_explicit_settings(modules, monkeypatch, interrupt):
    import os
    from xml.etree import ElementTree
    encoder = modules[2]
    monkeypatch.setenv('RMW_IMPLEMENTATION', 'fixture-rmw')
    monkeypatch.delenv('FASTRTPS_DEFAULT_PROFILES_FILE', raising=False)
    monkeypatch.setenv('FASTDDS_DEFAULT_PROFILES_FILE', '/fixture/existing.xml')
    monkeypatch.setenv('ROS_DOMAIN_ID', '27')
    monkeypatch.setenv('ROS_DISCOVERY_SERVER', '127.0.0.1:11811')
    monkeypatch.setenv('RMW_FASTRTPS_USE_QOS_FROM_XML', '1')
    before = dict(os.environ)
    profile = None
    try:
        with encoder.probe_udp_transport():
            profile = Path(os.environ['FASTRTPS_DEFAULT_PROFILES_FILE'])
            assert profile.stat().st_mode & 0o777 == 0o600
            assert profile.parent.stat().st_mode & 0o777 == 0o700
            assert os.environ['FASTDDS_DEFAULT_PROFILES_FILE'] == str(profile)
            namespace = {'p': 'http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles'}
            xml = ElementTree.fromstring(profile.read_text())
            transports = xml.findall('p:transport_descriptors/p:transport_descriptor', namespace)
            assert len(transports) == 1 and transports[0].find('p:type', namespace).text == 'UDPv4'
            participant = xml.find('p:participant', namespace)
            assert participant.get('is_default_profile') == 'true'
            assert participant.find('p:rtps/p:useBuiltinTransports', namespace).text == 'false'
            assert (participant.find('p:rtps/p:userTransports/p:transport_id', namespace).text
                    == transports[0].find('p:transport_id', namespace).text)
            assert os.environ['ROS_DOMAIN_ID'] == '27'
            assert os.environ['ROS_DISCOVERY_SERVER'] == '127.0.0.1:11811'
            assert os.environ['RMW_FASTRTPS_USE_QOS_FROM_XML'] == '1'
            if interrupt: raise KeyboardInterrupt()
    except KeyboardInterrupt:
        assert interrupt
    assert dict(os.environ) == before
    assert profile is not None and not profile.exists() and not profile.parent.exists()


def test_udp_probe_restores_environment_and_profile_if_ros_initialization_fails(modules, monkeypatch):
    import os
    encoder = modules[2]
    before, profiles = dict(os.environ), []
    def fail_init(**kwargs):
        profile = Path(os.environ['FASTRTPS_DEFAULT_PROFILES_FILE'])
        assert profile.is_file()
        profiles.append(profile)
        raise RuntimeError('isolated init error')
    monkeypatch.setattr(encoder.rclpy, 'init', fail_init, raising=False)
    result = encoder.probe_frame_health(timeout=5)
    assert result['state'] == 'failed' and result['probeTransport'] == 'udp-only'
    assert dict(os.environ) == before
    assert profiles and not profiles[0].exists() and not profiles[0].parent.exists()


def test_normal_encoder_never_changes_transport_or_creates_a_diagnostic_profile(modules, monkeypatch):
    import os
    encoder = modules[2]
    monkeypatch.setenv('RMW_IMPLEMENTATION', 'resident-rmw')
    monkeypatch.setenv('FASTRTPS_DEFAULT_PROFILES_FILE', '/fixture/resident.xml')
    monkeypatch.setenv('FASTDDS_DEFAULT_PROFILES_FILE', '/fixture/resident-new.xml')
    before = dict(os.environ)
    def forbidden(**kwargs): pytest.fail('resident encoder must not create a diagnostic transport')
    monkeypatch.setattr(encoder.tempfile, 'TemporaryDirectory', forbidden)
    encoder.CameraEncoder()
    assert dict(os.environ) == before


@pytest.mark.parametrize('outcome', ['ready', 'timeout', 'cancelled', 'invalid'])
def test_stdlib_parent_prepares_worker_environment_and_cleans_profile_for_every_exit(modules, monkeypatch, outcome):
    import os
    encoder = modules[2]
    monkeypatch.setenv('RMW_IMPLEMENTATION', 'original-rmw')
    monkeypatch.setenv('FASTRTPS_DEFAULT_PROFILES_FILE', '/fixture/resident.xml')
    before, profiles, commands = dict(os.environ), [], []
    def run(command, **kwargs):
        commands.append(command)
        assert kwargs['timeout'] == 18
        environment = kwargs['env']
        assert environment['BERXEL_HEALTH_PROBE_WORKER'] == '1'
        assert environment['RMW_IMPLEMENTATION'] == 'rmw_fastrtps_cpp'
        profile = Path(environment['FASTRTPS_DEFAULT_PROFILES_FILE'])
        assert str(profile) == environment['FASTDDS_DEFAULT_PROFILES_FILE']
        assert profile.exists() and profile.stat().st_mode & 0o777 == 0o600
        profiles.append(profile)
        if outcome == 'timeout': raise encoder.subprocess.TimeoutExpired(command, kwargs['timeout'])
        if outcome == 'cancelled': raise KeyboardInterrupt()
        if outcome == 'invalid': return SimpleNamespace(returncode=1, stdout='not a worker result', stderr='')
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(ok=True, dataVerified=True, state='ready')),
                               stderr='')
    monkeypatch.setattr(encoder.subprocess, 'run', run)
    result = encoder.launch_health_probe(timeout=15)
    assert result['ok'] is (outcome == 'ready')
    assert result['state'] == ('failed' if outcome == 'invalid' else outcome)
    assert result['probeIsolation'] == 'fresh-worker' and result['probeTransport'] == 'udp-only'
    assert commands[0][-3:] == ['--health-probe-worker', '--health-timeout', '15']
    if outcome == 'timeout':
        assert result['timeoutSeconds'] == 18
        assert result['errorCode'] == 'BERXEL_FRAME_PROBE_WORKER_TIMEOUT'
    assert dict(os.environ) == before
    assert profiles and not profiles[0].exists() and not profiles[0].parent.exists()


def test_real_health_cli_parent_never_imports_ros_and_worker_has_private_profile_before_rclpy(tmp_path):
    import os
    import subprocess
    fake = tmp_path / 'import-guard'
    fake.mkdir()
    events = tmp_path / 'imports.jsonl'
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    (fake / 'sitecustomize.py').write_text('''import builtins,json,os,sys,types
original_import=builtins.__import__
def guarded_import(name,*args,**kwargs):
    if name in ('cv2','rclpy'):
        if os.environ.get('BERXEL_HEALTH_PROBE_WORKER')!='1':
            raise RuntimeError('parent imported ROS/OpenCV before preparing worker')
        if name=='cv2':
            module=types.ModuleType('cv2');sys.modules['cv2']=module;return module
        path=os.environ.get('FASTRTPS_DEFAULT_PROFILES_FILE','')
        valid=(os.path.isfile(path) and path==os.environ.get('FASTDDS_DEFAULT_PROFILES_FILE')
            and os.environ.get('RMW_IMPLEMENTATION')=='rmw_fastrtps_cpp')
        with open(os.environ['FIXTURE_IMPORT_EVENTS'],'a') as output:
            output.write(json.dumps({'name':name,'pid':os.getpid(),'profile':path,'validBeforeImport':valid})+'\\n')
        raise RuntimeError('intentional fixture stop before loading any ROS library')
    return original_import(name,*args,**kwargs)
builtins.__import__=guarded_import
''')
    environment = dict(os.environ, PYTHONPATH=str(fake), TMPDIR=str(temporary),
                       FIXTURE_IMPORT_EVENTS=str(events))
    environment.pop('BERXEL_HEALTH_PROBE_WORKER', None)
    completed = subprocess.run([sys.executable, '-B', str(SOURCE/'jetson_camera_encoder.py'),
        '--health-probe', '--health-timeout', '4'], env=environment,
        capture_output=True, text=True, timeout=6)
    # The intentionally stopped worker is reported by the stdlib parent as a
    # bounded JSON failure, while the import log proves correct initialization order.
    result = json.loads(completed.stdout)
    assert completed.returncode == 1 and result['errorCode'] == 'BERXEL_FRAME_PROBE_WORKER_FAILED'
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['name'] == 'rclpy' and rows[0]['validBeforeImport']
    assert not Path(rows[0]['profile']).exists()
    assert not list(temporary.iterdir())


def test_internal_worker_without_import_time_profile_fails_before_loading_ros(tmp_path):
    import os
    import subprocess
    environment = dict(os.environ)
    for key in ('BERXEL_HEALTH_PROBE_WORKER', 'FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_DEFAULT_PROFILES_FILE'):
        environment.pop(key, None)
    completed = subprocess.run([sys.executable, '-B', str(SOURCE/'jetson_camera_encoder.py'),
        '--health-probe-worker', '--health-timeout', '4'], env=environment,
        capture_output=True, text=True, timeout=3)
    result = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert result['errorCode'] == 'BERXEL_FRAME_PROBE_ENVIRONMENT'



def test_garage_health_uses_real_raw_receipts_and_diagnostic_failure_cannot_stop_preview(modules, monkeypatch):
    encoder = modules[2]
    monkeypatch.setattr(encoder.CameraEncoder, 'count_publishers', lambda self, topic: 1, raising=False)
    node = encoder.CameraEncoder()
    for key in ('color', 'depth'):
        node.receive(key, image_message(depth=key == 'depth'))
    node.publish_health()
    capture = sys.modules['capture_health']
    report = capture.writes[-1]
    for key in ('color', 'depth'):
        assert report[key]['sourceTopic'] == '/berxel/' + key + '/image_raw'
        assert report[key]['receivedFrames'] == 1
        assert report[key]['publishedFrames'] == 0  # Capture evidence independent of preview success.
    def fail(report):
        raise OSError('synthetic unavailable disk')
    monkeypatch.setattr(capture, 'write_capture_health', fail)
    node.publish_health()
    assert len(node.health.published) == 2
