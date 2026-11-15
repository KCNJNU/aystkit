"""Recording-only sampling preserves timestamped selected bytes and real loss accounting."""
import json
import time
from types import SimpleNamespace

import pytest

from collector.archive_sampling import FrameSampler
from collector.archive_camera import NativeCameraSink, NativeCameraStore, _inspect_prefix
from collector.archive_ros import ArchiveRosAdapter
from tests.test_archive_camera import jpeg


def test_clock_slots_keep_ten_of_thirty_without_jitter_drift_and_cameras_are_independent():
    sampler = FrameSampler(10)
    result = {key: [] for key in ('front', 'depth')}
    for frame in range(300):
        stamp = 1_000_000_000 + round(frame*1_000_000_000/30)
        for key in result:
            if sampler.accept(key, stamp):
                result[key].append(frame)
    assert result['front'] == result['depth'] == list(range(0, 300, 3))
    assert sampler.accept('front', 1_000_000_000)  # clock reset starts a fresh schedule
    sampler.clear()
    assert sampler.accept('front', 1_000_000_001)


@pytest.mark.parametrize('fps', [True, -1, 121, float('nan'), float('inf'), '10'])
def test_invalid_rates_fail_closed(fps):
    with pytest.raises(ValueError):
        FrameSampler(fps)


def test_native_sampling_is_before_queue_and_keeps_original_bytes_and_true_gap_count(tmp_path):
    raw = jpeg(100)
    store = NativeCameraStore(tmp_path, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', gate=lambda: True,
        video_format='mjpeg', video_fps=10, min_free_bytes=0,
        disk_usage=lambda _: SimpleNamespace(free=10**10))
    sink = NativeCameraSink(store, target_fps=10, queue_capacity=64)
    sink.start()
    try:
        for frame in range(30):
            stamp = 1_000_000_000+round(frame*1_000_000_000/30)
            result = sink.submit('front', raw, sequence=frame+1,
                timestamp_ns=stamp, wall_time=1_800_000_000+frame/30)
            assert result is (True if frame % 3 == 0 else None)
        assert sink.flush(timeout=5)
    finally:
        assert sink.close(timeout=5)
    manifest = json.loads(next(tmp_path.rglob('archive.json')).read_text())
    rows = [json.loads(line) for line in next(tmp_path.rglob('index.jsonl')).read_text().splitlines()]
    assert [r['sequence'] for r in rows] == list(range(1, 31, 3))
    assert all(r['gapBefore'] == 0 for r in rows)
    assert manifest['metadata']['sequenceGaps'] == 0
    assert manifest['metadata']['videoTargetFps'] == 10
    assert next(tmp_path.rglob('frames.mjpg')).read_bytes() == raw*10
    prefix = _inspect_prefix(next(tmp_path.rglob('archive.json')).parent)
    assert prefix['count'] == 10 and prefix['gaps'] == 0 and prefix['reason'] is None
    counts = sink.snapshot()['counters']
    assert counts['written'] == 10 and counts['sampledFrames'] == 20
    assert not counts.get('queueDropped') and not counts.get('writerDropped')


def test_queue_rejection_is_not_hidden_by_later_sampling(tmp_path):
    raw = jpeg(100)
    store = NativeCameraStore(tmp_path, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', gate=lambda: True,
        video_format='mjpeg', video_fps=10, min_free_bytes=0,
        disk_usage=lambda _: SimpleNamespace(free=10**10))
    sink = NativeCameraSink(store, target_fps=10, queue_capacity=1)
    sink.reconcile_gate()
    assert sink.submit('front', raw, sequence=1, timestamp_ns=1_000_000_000, wall_time=time.time())
    assert sink.submit('front', raw, sequence=2, timestamp_ns=1_033_333_333, wall_time=time.time()) is None
    assert sink.submit('front', raw, sequence=3, timestamp_ns=1_066_666_667, wall_time=time.time()) is None
    assert sink.submit('front', raw, sequence=4, timestamp_ns=1_100_000_000, wall_time=time.time()) is False
    sink.start()
    assert sink.flush()
    assert sink.submit('front', raw, sequence=5, timestamp_ns=1_133_333_333, wall_time=time.time()) is None
    assert sink.submit('front', raw, sequence=6, timestamp_ns=1_166_666_667, wall_time=time.time()) is None
    assert sink.submit('front', raw, sequence=7, timestamp_ns=1_200_000_000, wall_time=time.time()) is True
    assert sink.flush() and sink.close()
    rows = [json.loads(line) for line in next(tmp_path.rglob('index.jsonl')).read_text().splitlines()]
    assert rows[-1]['sampledOutBefore'] == 4 and rows[-1]['gapBefore'] == 1


def test_ros_only_samples_images_never_calibration_or_lidar():
    clock = [1_800_000_000.0]
    adapter = ArchiveRosAdapter(SimpleNamespace(), lambda: True,
        sensors=('Berxel彩色','雷达MID360'), image_fps=10, wall_clock=lambda: clock[0],
        queue_capacity=1000)
    adapter.active = True
    image = next(s for s in adapter.specs if s.type_name == 'sensor_msgs/msg/Image')
    info = next(s for s in adapter.specs if s.type_name == 'sensor_msgs/msg/CameraInfo')
    lidar = next(s for s in adapter.specs if s.topic == '/livox/lidar')
    for frame in range(30):
        clock[0] = 1_800_000_000+frame/30
        for spec in (image,info,lidar):
            adapter.receive(spec, b'unchanged-cdr', adapter.epoch)
    assert adapter.stats[image.topic]['enqueued'] == 10
    assert adapter.stats[image.topic]['sampled'] == 20
    assert adapter.stats[image.topic]['dropped'] == 0
    assert adapter.stats[info.topic]['enqueued'] == 30
    assert adapter.stats[lidar.topic]['enqueued'] == 30
    assert all(packet.data == b'unchanged-cdr' for packet in adapter.queue)
