from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from collector.archive_core import ArchiveStore, GarageGate


class Writer:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir()
        self.rows = []
    def write(self, topic, kind, data, stamp):
        self.rows.append((topic, kind, data, stamp))
        with (self.path / 'bag_0.db3').open('ab') as out:
            out.write(data)
    def close(self):
        (self.path / 'metadata.yaml').write_text('rosbag2_bagfile_information: {}\n')


def store(tmp_path, **kw):
    kw.setdefault('compression', False)
    return ArchiveStore(tmp_path, 'fixture-robot', writer_factory=Writer,
        min_free_bytes=10, disk_usage=lambda _: SimpleNamespace(free=1000), **kw)


def stamp(value):
    return datetime.fromisoformat(value + '+08:00').timestamp()


def manifests(root):
    return [json.loads(p.read_text()) for p in root.rglob('archive.json')]


def test_gate_disabled_is_default_and_persists_explicit_site_choice(tmp_path):
    gate = GarageGate(tmp_path / 'control' / 'garage.json')
    archive = store(tmp_path / 'data', gate=gate)
    assert not archive.record('雷达', '/raw', 'type', b'raw', 1)
    assert not (archive.root / '.active').exists()
    gate.set(True)
    assert GarageGate(gate.path).enabled()  # subsequent boots read the same choice
    assert archive.record('雷达', '/raw', 'type', b'\x00native\xff', 2)
    gate.set(False)
    archive.tick()
    assert not archive.active
    assert not archive.record('雷达', '/raw', 'type', b'outside-garage', 3)
    assert manifests(archive.root)[0]['metadata']['closeReason'] == 'garage_disabled'
    gate.path.write_text('{bad')
    assert not gate.enabled()


def test_admitted_ros_tail_drains_after_stop_without_allowing_new_direct_writes(tmp_path):
    gate = GarageGate(tmp_path / 'control' / 'garage.json')
    archive = store(tmp_path / 'data', gate=gate)
    gate.set(True)
    assert archive.record('雷达', '/raw', 'type', b'first', 1)
    gate.set(False)
    assert archive.record_accepted('雷达', '/raw', 'type', b'accepted-tail', 2)
    assert not archive.record('雷达', '/raw', 'type', b'after-stop', 3)
    archive.tick()
    assert not archive.active
    values = manifests(archive.root)
    assert len(values) == 1
    assert values[0]['metadata']['closeReason'] == 'garage_disabled'
    assert next(archive.root.rglob('bag_0.db3')).read_bytes() == b'firstaccepted-tail'
    archive.close()
    assert not archive.record_accepted('雷达', '/raw', 'type', b'after-close', 4)


def test_desktop_lease_never_overwrites_web_choice_and_expires_after_terminal_dies(tmp_path, monkeypatch):
    from collector import archive_core
    clock = [100.0]
    monkeypatch.setattr(archive_core.time, 'time', lambda: clock[0])
    gate = GarageGate(tmp_path/'garage.json')
    gate.set(False)
    original = gate.path.read_bytes()
    lease = tmp_path/'desktop-lease.json'
    archive_core.atomic_json(lease, dict(schemaVersion=1, scope='garage', enabled=True,
        desktopSession='a'*32, updatedAt='desktop', expiresAt=120))
    assert gate.enabled() and gate.snapshot()['expiresAt'] == 120
    assert gate.path.read_bytes() == original
    clock[0] = 121
    assert not gate.enabled()
    gate.set(True)  # A later web owner remains active after the desktop expires.
    assert gate.enabled() and gate.snapshot().get('desktopSession') is None


@pytest.mark.parametrize('expiry', [None, float('inf'), float('nan'), '120', 10000, 90])
def test_desktop_lease_without_bounded_numeric_lifetime_is_not_recording(tmp_path, monkeypatch, expiry):
    from collector import archive_core
    monkeypatch.setattr(archive_core.time, 'time', lambda: 100)
    archive_core.atomic_json(tmp_path/'desktop-lease.json', dict(schemaVersion=1, scope='garage',
        enabled=True, desktopSession='a'*32, expiresAt=expiry if not isinstance(expiry, float) else str(expiry)))
    assert not GarageGate(tmp_path/'garage.json').enabled()


def test_midnight_and_hour_rotation_keep_shanghai_day_sensor_and_real_bytes(tmp_path):
    archive = store(tmp_path, idle_seconds=99999)
    first = stamp('2026-10-27T23:50:00')
    archive.record('雷达', '/lidar', 'CustomMsg', b'\x00unmodified\xff', 1, first)
    archive.record('雷达', '/lidar', 'CustomMsg', b'next-day', 2, first + 601)
    archive.record('雷达', '/lidar', 'CustomMsg', b'new-hour', 3, first + 4201)
    archive.close()
    values = manifests(tmp_path)
    assert len(values) == 3
    assert {x['day'] for x in values} == {'2026-10-27', '2026-10-28'}
    assert any(x['label'] == '雷达_23:50-23:50' for x in values)
    bags = [p.read_bytes() for p in tmp_path.rglob('bag_0.db3')]
    assert b'\x00unmodified\xff' in bags
    assert all(x['metadata']['source'] == 'native-ros-cdr' for x in values)


def test_size_split_has_unique_ids_and_preserves_all_messages(tmp_path):
    archive = store(tmp_path, segment_bytes=4)
    archive.record('相机', '/image_raw', 'Image', b'abcd', 1, 100)
    archive.record('相机', '/image_raw', 'Image', b'ef', 2, 101)
    archive.close()
    values = manifests(tmp_path)
    assert len(values) == 2 and values[0]['id'] != values[1]['id']
    assert sum(x['metadata']['receivedMessages'] for x in values) == 2


def test_disk_full_and_spool_limit_pause_without_deleting_originals(tmp_path):
    archive = store(tmp_path, max_spool_bytes=2)
    assert archive.record('雷达', '/raw', 'type', b'ab', 1)
    assert not archive.record('雷达', '/raw', 'type', b'cd', 2)
    assert 'storage' in archive.snapshot()['errors']
    assert next(tmp_path.rglob('bag_0.db3')).read_bytes() == b'ab'
    archive.disk_usage = lambda _: SimpleNamespace(free=9)
    assert not archive.record('相机', '/raw', 'type', b'a', 3)


def test_sensor_failure_is_quarantined_other_sources_keep_recording(tmp_path):
    class BadWriter(Writer):
        def write(self, topic, *args):
            if topic == '/broken':
                raise OSError('fixture disk failure')
            super().write(topic, *args)
    archive = ArchiveStore(tmp_path, 'robot', writer_factory=BadWriter, min_free_bytes=0, compression=False)
    with pytest.raises(OSError):
        archive.record('故障传感器', '/broken', 'type', b'bytes', 1)
    assert list((tmp_path / 'recovery').rglob('recovery-required.json'))
    assert archive.record('正常传感器', '/good', 'type', b'good', 2)
    archive.close()
    assert [x['sensor'] for x in manifests(tmp_path)] == ['正常传感器']


def test_incomplete_crash_remnant_is_retained_unless_validated(tmp_path):
    partial = tmp_path / '.active' / 'interrupted'
    partial.mkdir(parents=True)
    (partial / 'important.raw').write_bytes(b'unconfirmed-source')
    archive = store(tmp_path)
    assert (tmp_path / 'recovery/interrupted/important.raw').read_bytes() == b'unconfirmed-source'
    assert not manifests(tmp_path)
    assert 'recovery' in archive.snapshot()['errors']


@pytest.mark.parametrize('active_exists', [False, True])
def test_empty_recovery_directory_does_not_fail_an_idle_recorder_shutdown(tmp_path, active_exists):
    from collector.archive_main import finish_archive
    recovery = tmp_path / 'recovery'
    recovery.mkdir()
    if active_exists:
        (tmp_path / '.active').mkdir()
    archive = store(tmp_path)
    report = finish_archive(None, archive)
    assert report['state'] == 'stopped'
    assert report['errors'] == {}
    assert 'recovery' not in archive.snapshot()['errors']
    assert recovery.is_dir() and list(recovery.iterdir()) == []


def test_existing_recovery_remnant_is_reported_without_an_active_directory(tmp_path):
    from collector.archive_main import finish_archive
    original = tmp_path / 'recovery' / 'interrupted' / 'important.raw'
    original.parent.mkdir(parents=True)
    original.write_bytes(b'unverified historical original')
    archive = store(tmp_path)
    assert original.read_bytes() == b'unverified historical original'
    assert not manifests(tmp_path)
    assert 'recovery' in archive.snapshot()['errors']
    report = finish_archive(None, archive)
    assert report['state'] == 'stopped'
    assert report['warnings']['recovery']
    assert 'store' not in report['errors']


@pytest.mark.parametrize('kind', ['directory_symlink', 'broken_symlink', 'regular_file'])
def test_abnormal_recovery_path_is_not_treated_as_a_verified_empty_directory(tmp_path, kind):
    recovery = tmp_path / 'recovery'
    outside = tmp_path / 'outside'
    if kind == 'directory_symlink':
        outside.mkdir()
        recovery.symlink_to(outside, target_is_directory=True)
    elif kind == 'broken_symlink':
        recovery.symlink_to(outside, target_is_directory=True)
    else:
        recovery.write_bytes(b'unverified recovery record')
    archive = store(tmp_path)
    assert 'recovery' in archive.snapshot()['errors']
    if kind == 'regular_file':
        assert recovery.read_bytes() == b'unverified recovery record'
    else:
        assert recovery.is_symlink()
        if kind == 'directory_symlink':
            assert list(outside.iterdir()) == []


@pytest.mark.parametrize('failure', ['open', 'scan'])
def test_unreadable_recovery_directory_keeps_the_warning(tmp_path, monkeypatch, failure):
    from collector import archive_core
    recovery = tmp_path / 'recovery'
    recovery.mkdir()
    if failure == 'open':
        original_open = archive_core.os.open
        def inaccessible(path, *args, **kwargs):
            if Path(path) == recovery:
                raise PermissionError('isolated inaccessible recovery directory')
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(archive_core.os, 'open', inaccessible)
    else:
        original_scan = archive_core.os.scandir
        def inaccessible(path):
            if isinstance(path, int):
                raise OSError('isolated recovery listing failure')
            return original_scan(path)
        monkeypatch.setattr(archive_core.os, 'scandir', inaccessible)
    archive = store(tmp_path)
    assert 'recovery' in archive.snapshot()['errors']
    assert recovery.is_dir()


def test_rejects_unsafe_names_and_overlarge_messages(tmp_path):
    archive = store(tmp_path)
    with pytest.raises(ValueError):
        archive.record('../outside', '/raw', 'type', b'bytes', 1)
    with pytest.raises(ValueError):
        archive.record('雷达', '/raw', 'type', b'', 1)


def test_short_image_capture_waits_for_one_mkv_without_exposing_raw_bag(tmp_path):
    archive = store(tmp_path)
    start = stamp('2026-10-27T12:00:00')
    original = b'\x00native-image-CDR\xff'
    assert archive.record('Berxel深度', '/berxel/depth/image_raw',
        'sensor_msgs/msg/Image', original, int(start*1e9), start)
    assert archive.record('Berxel深度', '/berxel/depth/camera_info',
        'sensor_msgs/msg/CameraInfo', b'original calibration', int(start*1e9)+1, start+.001)
    archive.close()
    assert not manifests(tmp_path)
    paths = list((tmp_path/'days').rglob('video-pending.json'))
    assert len(paths) == 1
    pending = json.loads(paths[0].read_text())
    metadata = pending['metadata']
    assert metadata['videoRequired'] is True and metadata['format'] == 'matroska/ffv1'
    assert metadata['receivedFrames'] == 1 and metadata['receivedMessages'] == 2
    assert metadata['videoImageTopics'] == ['/berxel/depth/image_raw']
    assert (paths[0].parent/'bag/bag_0.db3').read_bytes() == original+b'original calibration'
    assert set(pending['files']) == {'bag/bag_0.db3', 'bag/metadata.yaml'}


def test_camera_info_without_an_image_stays_metadata_only_not_an_empty_video(tmp_path):
    archive = store(tmp_path)
    assert archive.record('Berxel彩色', '/berxel/color/camera_info',
        'sensor_msgs/msg/CameraInfo', b'original intrinsics', 1)
    archive.close()
    values = manifests(tmp_path)
    assert len(values) == 1 and values[0]['metadata']['cameraMetadataOnly'] is True
    assert values[0]['metadata']['format'] == 'rosbag2/sqlite3'
    assert 'videoRequired' not in values[0]['metadata']
    assert not list(tmp_path.rglob('video-pending.json'))


def test_pointcloud_and_explicit_legacy_image_archives_remain_original_bags(tmp_path):
    cloud = store(tmp_path/'cloud')
    assert cloud.record('Berxel点云', '/berxel/points', 'sensor_msgs/msg/PointCloud2', b'original points', 1)
    cloud.close()
    legacy = store(tmp_path/'legacy', video_format='rosbag')
    assert legacy.record('Berxel彩色', '/berxel/color/image_raw', 'sensor_msgs/msg/Image', b'legacy image CDR', 1)
    legacy.close()
    assert len(manifests(tmp_path)) == 2
    assert all(row['metadata']['format'] == 'rosbag2/sqlite3' for row in manifests(tmp_path))
    assert not list(tmp_path.rglob('video-pending.json'))


def interrupted_bag(root, *, image=True):
    entry = root/'.active'/'interrupted-bag'
    bag = entry/'bag';bag.mkdir(parents=True)
    (entry/'session.json').write_text(json.dumps(dict(scope='garage', source='native-ros-cdr',
        sensor='Berxel深度', robotId='fixture-robot')))
    start = int(stamp('2026-10-27T12:00:00')*1e9)
    with sqlite3.connect(bag/'data_0.db3') as connection:
        connection.execute('CREATE TABLE topics (id INTEGER PRIMARY KEY,name TEXT,type TEXT)')
        connection.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY,topic_id INTEGER,timestamp INTEGER,data BLOB)')
        connection.execute('INSERT INTO topics VALUES (1,?,?)',('/berxel/depth/camera_info','sensor_msgs/msg/CameraInfo'))
        connection.execute('INSERT INTO messages VALUES (1,1,?,?)',(start,b'native intrinsics'))
        if image:
            connection.execute('INSERT INTO topics VALUES (2,?,?)',('/berxel/depth/image_raw','sensor_msgs/msg/Image'))
            connection.execute('INSERT INTO messages VALUES (2,2,?,?)',(start+1000,b'native image CDR'))
    return entry


def install_reindexer(monkeypatch, *, fail=False):
    calls = []
    class Reindexer:
        def reindex(self, options):
            calls.append(options.uri)
            if fail:
                raise OSError('isolated reindex failure')
            (Path(options.uri)/'metadata.yaml').write_text('reindexed original bag')
    monkeypatch.setitem(sys.modules,'rosbag2_py',SimpleNamespace(Reindexer=Reindexer,
        StorageOptions=lambda **values: SimpleNamespace(**values)))
    return calls


def test_interrupted_image_bag_is_reindexed_once_then_waits_for_video(tmp_path, monkeypatch):
    entry = interrupted_bag(tmp_path)
    original = (entry/'bag/data_0.db3').read_bytes()
    calls = install_reindexer(monkeypatch)
    archive = store(tmp_path)
    paths = list((tmp_path/'days').rglob('video-pending.json'))
    assert len(paths) == 1 and len(calls) == 1 and not manifests(tmp_path)
    pending = json.loads(paths[0].read_text())
    assert pending['metadata']['captureInterrupted'] is True
    assert pending['metadata']['videoRequired'] is True and pending['metadata']['receivedFrames'] == 1
    assert (paths[0].parent/'bag/data_0.db3').read_bytes() == original
    assert archive.finalized == 1
    again = store(tmp_path)
    assert len(calls) == 1 and list((tmp_path/'days').rglob('video-pending.json')) == paths
    assert again.finalized == 0


def test_interrupted_camera_info_only_bag_cannot_claim_video_frames(tmp_path, monkeypatch):
    interrupted_bag(tmp_path, image=False)
    install_reindexer(monkeypatch)
    store(tmp_path)
    values = manifests(tmp_path)
    assert len(values) == 1 and values[0]['metadata']['captureInterrupted'] is True
    assert values[0]['metadata']['cameraMetadataOnly'] is True
    assert not list(tmp_path.rglob('video-pending.json'))


def test_failed_reindex_preserves_original_image_bag_without_publishing_pending(tmp_path, monkeypatch):
    entry = interrupted_bag(tmp_path)
    original = (entry/'bag/data_0.db3').read_bytes()
    install_reindexer(monkeypatch, fail=True)
    archive = store(tmp_path)
    assert not manifests(tmp_path) and not list(tmp_path.rglob('video-pending.json'))
    assert (tmp_path/'recovery/interrupted-bag/bag/data_0.db3').read_bytes() == original
    assert 'recovery' in archive.errors


def test_legacy_mixed_depth_bag_recovery_keeps_original_pointcloud_visible(tmp_path, monkeypatch):
    entry = interrupted_bag(tmp_path)
    with sqlite3.connect(entry/'bag/data_0.db3') as connection:
        connection.execute('INSERT INTO topics VALUES (3,?,?)',('/berxel/points','sensor_msgs/msg/PointCloud2'))
        connection.execute('INSERT INTO messages VALUES (3,3,?,?)',
            (int(stamp('2026-10-27T12:00:01')*1e9),b'original depth pointcloud'))
    original = (entry/'bag/data_0.db3').read_bytes()
    install_reindexer(monkeypatch)
    store(tmp_path)
    values = manifests(tmp_path)
    assert len(values) == 1 and values[0]['metadata']['format'] == 'rosbag2/sqlite3'
    metadata = values[0]['metadata']
    assert metadata['videoDeferredReason'] == 'mixed_non_image_topics'
    assert metadata['topics']['/berxel/points']['count'] == 1
    assert metadata['receivedMessages'] == 3
    assert 'cameraMetadataOnly' not in metadata and 'videoRequired' not in metadata
    assert not list(tmp_path.rglob('video-pending.json'))
    assert next((tmp_path/'days').rglob('data_0.db3')).read_bytes() == original


def test_camera_uses_larger_segment_limit_even_when_camera_info_arrives_first(tmp_path):
    archive = store(tmp_path/'camera', segment_bytes=4, camera_segment_bytes=12)
    first = stamp('2026-10-27T12:00:00')
    assert archive.record('Berxel彩色','/berxel/color/camera_info','sensor_msgs/msg/CameraInfo',b'in',1,first)
    for index in range(3):
        assert archive.record('Berxel彩色','/berxel/color/image_raw','sensor_msgs/msg/Image',
                              b'RGB',index+2,first+index+.01)
    archive.close()
    pending = list((archive.root/'days').rglob('video-pending.json'))
    assert len(pending) == 1
    metadata = json.loads(pending[0].read_text())['metadata']
    assert metadata['receivedFrames'] == 3 and metadata['rawBytes'] == 11
    cloud = store(tmp_path/'points', segment_bytes=4, camera_segment_bytes=12)
    for index in range(3):
        assert cloud.record('Berxel点云','/berxel/points','sensor_msgs/msg/PointCloud2',
                            b'xyz',index+1,first+index)
    cloud.close()
    assert len(manifests(cloud.root)) == 3


def test_camera_larger_limit_still_splits_at_its_own_byte_boundary(tmp_path):
    archive = store(tmp_path, segment_bytes=2, camera_segment_bytes=6)
    first = stamp('2026-10-27T12:00:00')
    for index in range(3):
        assert archive.record('Berxel深度','/berxel/depth/image_raw','sensor_msgs/msg/Image',
                              b'16b',index+1,first+index)
    archive.close()
    pending = [json.loads(path.read_text()) for path in (tmp_path/'days').rglob('video-pending.json')]
    assert len(pending) == 2
    assert sorted(row['metadata']['receivedFrames'] for row in pending) == [1,2]


def test_camera_limit_is_bounded_and_legacy_camera_bags_keep_regular_limit(tmp_path):
    for limit in (0,512*1024**2+1):
        with pytest.raises(ValueError,match='camera segment limit'):
            store(tmp_path/'invalid',camera_segment_bytes=limit)
    archive = store(tmp_path/'legacy',segment_bytes=2,camera_segment_bytes=6,video_format='rosbag')
    for index in range(2):
        assert archive.record('Berxel深度','/berxel/depth/image_raw','sensor_msgs/msg/Image',b'16',index+1,100+index)
    archive.close()
    assert len(manifests(archive.root)) == 2 and not list(archive.root.rglob('video-pending.json'))


@pytest.mark.parametrize('sensor,kind', [
    ('Berxel点云', 'sensor_msgs/msg/PointCloud2'),
    ('MID360雷达', 'livox_ros_driver2/msg/CustomMsg'),
    ('TF', 'tf2_msgs/msg/TFMessage'),
    ('系统诊断', 'diagnostic_msgs/msg/DiagnosticArray'),
    ('Berxel彩色', 'sensor_msgs/msg/CameraInfo'),
])
def test_new_native_non_image_bags_wait_for_verified_lossless_compression_by_default(tmp_path,sensor,kind):
    archive = ArchiveStore(tmp_path, 'fixture-robot', writer_factory=Writer, min_free_bytes=0,video_fps=10)
    raw = b'\x00native-CDR-all-values\xff'
    assert archive.record(sensor, '/native', kind, raw, 1, 100)
    session = next((tmp_path/'.active').rglob('session.json'))
    assert json.loads(session.read_text())['archiveProfile'] == 'compact-v1'
    archive.close()
    pending = list((tmp_path/'days').rglob('bag-pending.json'))
    assert len(pending) == 1 and not manifests(tmp_path)
    metadata = json.loads(pending[0].read_text())['metadata']
    assert metadata['archiveProfile'] == 'compact-v1' and metadata['bagCompressionRequired'] is True
    assert metadata['receivedMessages'] == 1
    assert 'videoTargetFps' not in metadata
    assert next(tmp_path.rglob('bag_0.db3')).read_bytes() == raw
    assert not list(tmp_path.rglob('video-pending.json'))


def test_new_image_video_has_compact_profile_without_changing_original_cdr(tmp_path):
    archive = store(tmp_path,compression=True,video_fps=10)
    raw = b'original image CDR'
    assert archive.record('Berxel彩色','/berxel/color/image_raw','sensor_msgs/msg/Image',raw,1,100)
    archive.close()
    pending = next(tmp_path.rglob('video-pending.json'))
    metadata = json.loads(pending.read_text())['metadata']
    assert metadata['archiveProfile'] == 'compact-v1' and metadata['videoRequired'] is True
    assert metadata['videoTargetFps'] == 10
    assert 'bagCompressionRequired' not in metadata
    assert next(tmp_path.rglob('bag_0.db3')).read_bytes() == raw


def test_desktop_session_identity_survives_rotation_and_accepted_tail_after_stop(tmp_path, monkeypatch):
    from collector import archive_core
    monkeypatch.setattr(archive_core.time, 'time', lambda: 100)
    control = tmp_path/'control'
    token = 'c'*32
    archive_core.atomic_json(control/'desktop-lease.json', dict(schemaVersion=1,scope='garage',
        enabled=True,desktopSession=token,expiresAt=120))
    archive = store(tmp_path/'data',gate=GarageGate(control/'garage.json'),segment_bytes=4,video_fps=10)
    assert archive.record('雷达','/lidar','livox_ros_driver2/msg/CustomMsg',b'abcd',1,100)
    # The last admitted packet can open a segment after the lease has closed.
    (control/'desktop-lease.json').unlink()
    assert archive.record_accepted('雷达','/lidar','livox_ros_driver2/msg/CustomMsg',b'tail',2,101)
    archive.close()
    values = manifests(archive.root)
    assert len(values) == 2
    assert {value['metadata']['captureSessionId'] for value in values} == {token}
    assert all('videoTargetFps' not in value['metadata'] for value in values)


@pytest.mark.parametrize('new_profile', [False, True])
def test_recovery_uses_saved_profile_and_never_applies_new_default_to_legacy_capture(tmp_path,monkeypatch,new_profile):
    entry = interrupted_bag(tmp_path,image=False)
    session = json.loads((entry/'session.json').read_text())
    if new_profile:
        session['archiveProfile'] = 'compact-v1'
    (entry/'session.json').write_text(json.dumps(session))
    original = (entry/'bag/data_0.db3').read_bytes()
    install_reindexer(monkeypatch)
    # An upgrade to the new default cannot opt an old active segment in.
    ArchiveStore(tmp_path,'fixture-robot',writer_factory=Writer,min_free_bytes=0)
    pending = list(tmp_path.rglob('bag-pending.json'))
    ready = list(tmp_path.rglob('archive.json'))
    assert (len(pending),len(ready)) == ((1,0) if new_profile else (0,1))
    metadata = json.loads((pending or ready)[0].read_text())['metadata']
    assert metadata['captureInterrupted'] is True
    assert ('archiveProfile' in metadata) is new_profile
    assert ('bagCompressionRequired' in metadata) is new_profile
    assert next((tmp_path/'days').rglob('data_0.db3')).read_bytes() == original


def test_existing_published_history_is_not_rewritten_by_new_default(tmp_path):
    old = store(tmp_path)
    old.record('点云','/raw','sensor_msgs/msg/PointCloud2',b'old',1,100)
    old.close()
    path = next(tmp_path.rglob('archive.json'));before = path.read_bytes()
    ArchiveStore(tmp_path,'fixture-robot',writer_factory=Writer,min_free_bytes=0)
    assert path.read_bytes() == before and not list(tmp_path.rglob('bag-pending.json'))


def test_compression_switch_must_be_explicit_boolean(tmp_path):
    with pytest.raises(ValueError,match='compression switch'):
        store(tmp_path,compression='false')



def test_new_rgb_capture_persists_balanced_preset_before_first_frame_is_sealed(tmp_path):
    archive = store(tmp_path, compression=True)
    raw = b'preserved-original-rgb-cdr'
    assert archive.record('Berxel彩色', '/berxel/color/image_raw', 'sensor_msgs/msg/Image', raw, 1, 100)
    session = json.loads(next((tmp_path/'.active').rglob('session.json')).read_text())
    assert session['archiveProfile'] == 'compact-v1'
    assert session['videoArchivePreset'] == '720p-balanced-v1'
    archive.close()
    pending = json.loads(next(tmp_path.rglob('video-pending.json')).read_text())
    assert pending['metadata']['videoArchivePreset'] == '720p-balanced-v1'
    assert next(tmp_path.rglob('bag_0.db3')).read_bytes() == raw


@pytest.mark.parametrize('preset', [None, '720p-economy-v1', '720p-balanced-v1'])
def test_image_recovery_preserves_only_explicit_session_video_preset(tmp_path, monkeypatch, preset):
    entry = interrupted_bag(tmp_path)
    session = json.loads((entry/'session.json').read_text())
    session['archiveProfile'] = 'compact-v1'
    if preset is not None:
        session['videoArchivePreset'] = preset
    (entry/'session.json').write_text(json.dumps(session))
    original = (entry/'bag/data_0.db3').read_bytes()
    install_reindexer(monkeypatch)
    archive = store(tmp_path, compression=True)
    pending = json.loads(next(tmp_path.rglob('video-pending.json')).read_text())
    assert pending['metadata'].get('videoArchivePreset') == preset
    assert next(tmp_path.rglob('data_0.db3')).read_bytes() == original
    assert archive.finalized == 1


def test_non_image_archive_does_not_claim_a_colour_video_preset(tmp_path):
    archive = store(tmp_path, compression=True)
    assert archive.record('MID360', '/livox/lidar', 'livox_ros_driver2/msg/CustomMsg', b'lidar-cdr', 1, 100)
    archive.close()
    pending = json.loads(next(tmp_path.rglob('bag-pending.json')).read_text())
    assert pending['metadata']['archiveProfile'] == 'compact-v1'
    assert 'videoArchivePreset' not in pending['metadata']



@pytest.mark.parametrize('mode', ['lossless', 'pseudocolor'])
def test_depth_video_mode_is_sealed_with_capture_and_not_applied_to_color(tmp_path, mode):
    archive = store(tmp_path, compression=True, depth_video_mode=mode)
    assert archive.record('Berxel深度', '/berxel/depth/image_raw', 'sensor_msgs/msg/Image', b'raw-depth', 1)
    active = archive.active['Berxel深度']
    session = json.loads((active['path'] / 'session.json').read_text())
    assert session['depthVideoMode'] == mode
    assert session['videoArchivePreset'] == '720p-balanced-v1'
    assert archive.record('Berxel彩色', '/berxel/color/image_raw', 'sensor_msgs/msg/Image', b'raw-color', 2)
    archive.close()
    pending = [json.loads(p.read_text()) for p in tmp_path.rglob('video-pending.json')]
    assert len(pending) == 2
    depth = next(x for x in pending if x['sensor'] == 'Berxel深度')
    color = next(x for x in pending if x['sensor'] == 'Berxel彩色')
    assert depth['metadata']['depthVideoMode'] == mode
    assert 'depthVideoMode' not in color['metadata']


@pytest.mark.parametrize('value', ['', 'PSEUDOCOLOR', 'unknown', None, True])
def test_invalid_depth_mode_never_starts_a_store(tmp_path, value):
    with pytest.raises(ValueError, match='depth video mode'):
        store(tmp_path, depth_video_mode=value)


def test_pseudocolor_requires_explicit_compressed_capture(tmp_path):
    with pytest.raises(ValueError, match='requires compression'):
        store(tmp_path, compression=False, depth_video_mode='pseudocolor')
