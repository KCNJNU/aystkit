"""Native camera tap integrity, bounded callback, gate/quota and durable segments."""
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from collector.archive_camera import NativeCameraSink, NativeCameraStore
from collector.archive_upload import ArchiveUploadClient, ArchiveUploadConfig, ArchiveUploadError

ROBOT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


def jpeg(value=127):
    ok, raw = cv2.imencode('.jpg', np.full((13, 19, 3), value, dtype=np.uint8),
                           [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert ok
    return raw.tobytes()


def stamp(value):
    return datetime.fromisoformat(value+'+08:00').timestamp()


def store(root, **options):
    return NativeCameraStore(root, ROBOT, gate=options.pop('gate', lambda: True),
        min_free_bytes=10, disk_usage=options.pop('disk_usage', lambda _: SimpleNamespace(free=10**10)),
        video_format=options.pop('video_format', 'mjpeg'), **options)


def record(target, raw, sequence=1, wall=None, **options):
    return target.record('camera1', raw, sequence=sequence, timestamp_ns=1000+sequence,
                         wall_time=stamp('2026-10-27T12:00:00') if wall is None else wall,
                         width=19, height=13, **options)


def test_exact_jpeg_bytes_index_and_manifest_are_upload_client_compatible(tmp_path):
    target = store(tmp_path, camera_names={'camera1': '已确认原相机identity'})
    originals = [jpeg(20), jpeg(220)]
    assert record(target, originals[0], 1)
    assert record(target, originals[1], 2)
    target.close()
    manifest_path = next(tmp_path.rglob('archive.json'))
    manifest = json.loads(manifest_path.read_text())
    raw = (manifest_path.parent/'frames.mjpg').read_bytes()
    assert raw == b''.join(originals)
    index = [json.loads(line) for line in (manifest_path.parent/'index.jsonl').read_text().splitlines()]
    assert [row['offset'] for row in index] == [0, len(originals[0])]
    for row, original in zip(index, originals):
        assert raw[row['offset']:row['offset']+row['length']] == original
        assert row['sha256'] == hashlib.sha256(original).hexdigest()
        assert (row['width'], row['height']) == (19, 13)
    assert manifest['sensor'] == '已确认原相机identity'
    assert manifest['metadata']['source'] == 'native-camera-jpeg'
    assert manifest['metadata']['resized'] is manifest['metadata']['reencoded'] is False
    client = ArchiveUploadClient(ArchiveUploadConfig('operator@fixture.test', 'fixture-password', robot_id=ROBOT))
    _, accepted, _ = client._manifest(manifest_path.parent)
    assert accepted['files'] == ['frames.mjpg', 'index.jsonl']
    assert all((manifest_path.parent/name).stat().st_mode & 0o077 == 0 for name in accepted['files'])


def test_gate_fails_closed_and_off_finalizes_original_segment(tmp_path):
    enabled = [False]
    target = store(tmp_path, gate=lambda: enabled[0])
    assert not record(target, jpeg())
    assert not (tmp_path/'.camera-active').exists()
    enabled[0] = True
    assert record(target, jpeg())
    enabled[0] = False
    target.tick()
    manifest = json.loads(next(tmp_path.rglob('archive.json')).read_text())
    assert manifest['metadata']['closeReason'] == 'garage_disabled'
    assert not target.active
    assert not record(target, jpeg(), 2)


def test_real_camera_store_drains_sink_tail_after_capture_gate_closes(tmp_path):
    enabled = [True]
    target = store(tmp_path, gate=lambda: enabled[0])
    entered, release = threading.Event(), threading.Event()
    original_record = target.record_accepted

    def slow_record(camera, raw, **metadata):
        if metadata['sequence'] == 1:
            entered.set()
            assert release.wait(2)
        return original_record(camera, raw, **metadata)

    target.record_accepted = slow_record
    sink = NativeCameraSink(target)
    sink.start()
    originals = [jpeg(20), jpeg(40), jpeg(60)]
    try:
        assert sink.submit('camera1', originals[0], sequence=1, timestamp_ns=1, wall_time=time.time())
        assert entered.wait(1)
        for sequence, raw in enumerate(originals[1:], 2):
            assert sink.submit('camera1', raw, sequence=sequence, timestamp_ns=sequence, wall_time=time.time())
        enabled[0] = False
        sink.reconcile_gate()
        assert sink.snapshot()['queueFrames'] == 2
        assert not sink.submit('camera1', jpeg(), sequence=4, timestamp_ns=4, wall_time=time.time())
        release.set()
        assert sink.flush()
        assert sink.snapshot()['counters']['written'] == 3
        manifests = list(tmp_path.rglob('archive.json'))
        assert len(manifests) == 1
        assert (manifests[0].parent / 'frames.mjpg').read_bytes() == b''.join(originals)
        assert json.loads(manifests[0].read_text())['metadata']['closeReason'] == 'garage_disabled'
        assert not record(target, jpeg(), 5)
    finally:
        release.set()
        assert sink.close()


def test_corrupt_packets_gaps_duplicate_and_source_reset_are_reported(tmp_path):
    target = store(tmp_path)
    assert record(target, jpeg(), 1)
    assert record(target, jpeg(), 3)
    assert not record(target, jpeg()[:-2], 4)
    assert record(target, jpeg(), 5)
    assert not record(target, jpeg(), 5)
    assert record(target, jpeg(), 0)
    target.close()
    value = json.loads(next(tmp_path.rglob('archive.json')).read_text())['metadata']
    assert value['receivedFrames'] == 4 and value['sequenceGaps'] == 2
    assert value['cameraCountersAtClose']['corruptPackets'] == 1
    assert value['cameraCountersAtClose']['duplicatePackets'] == 1
    assert value['cameraCountersAtClose']['sequenceResets'] == 1


def test_midnight_hour_size_rotation_preserves_source_frames(tmp_path):
    raw = jpeg()
    target = store(tmp_path, segment_bytes=len(raw)*2)
    first = stamp('2026-10-27T23:59:50')
    for sequence, wall in enumerate([first, first+1, first+2, first+20, first+3620], 1):
        assert record(target, raw, sequence, wall)
    target.close()
    manifests = [json.loads(path.read_text()) for path in tmp_path.rglob('archive.json')]
    assert len(manifests) == 4
    assert {value['day'] for value in manifests} == {'2026-10-27', '2026-10-28'}
    assert sum(value['metadata']['receivedFrames'] for value in manifests) == 5
    assert sum(p.stat().st_size for p in tmp_path.rglob('frames.mjpg')) == len(raw)*5


def test_quota_and_low_disk_pause_without_deleting_any_recording(tmp_path):
    capacity = [True]
    target = store(tmp_path, capacity=lambda: capacity[0])
    raw = jpeg()
    assert record(target, raw)
    capacity[0] = False
    assert not record(target, raw, 2)
    assert next(tmp_path.rglob('frames.mjpg')).read_bytes() == raw
    assert 'storage' in target.snapshot()['errors']
    capacity[0] = True
    assert record(target, raw, 3)
    target.close()
    limited = store(tmp_path/'low-disk', disk_usage=lambda _: SimpleNamespace(free=9))
    assert not record(limited, raw)
    assert not list(limited.root.rglob('frames.mjpg'))


def test_interrupted_native_files_are_preserved_and_verified_prefix_is_ready_to_upload(tmp_path):
    target = store(tmp_path)
    raw = jpeg()
    assert record(target, raw)
    for value in target.active.values():
        value['framesFile'].close()
        value['indexFile'].close()
    recovered = store(tmp_path)
    assert next((tmp_path/'recovery').rglob('frames.mjpg')).read_bytes() == raw
    manifest = json.loads(next((tmp_path/'days').rglob('archive.json')).read_text())
    assert manifest['metadata']['captureInterrupted'] is True
    assert manifest['metadata']['recoveredFrames'] == 1
    assert manifest['metadata']['unrecoveredTailBytes'] == 0
    assert not list(tmp_path.rglob('uploaded.json'))
    assert recovered.snapshot()['finalizedSegments'] == 1


def test_dimension_and_path_identity_errors_never_create_wrong_recordings(tmp_path):
    target = store(tmp_path, camera_names={'camera1': '原相机'})
    assert not target.record('camera1', jpeg(), sequence=1, timestamp_ns=1,
                             wall_time=time.time(), width=999, height=13)
    with pytest.raises(ValueError):
        target.record('../escape', jpeg(), sequence=1, timestamp_ns=1)
    with pytest.raises(ValueError):
        target.record('unknown', jpeg(), sequence=1, timestamp_ns=1)
    assert not list(tmp_path.rglob('frames.mjpg'))


def test_submit_is_bounded_and_cannot_wait_on_slow_disk_or_gate(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowStore:
        camera_names = {'camera1': 'camera1'}
        def __init__(self):
            self.rows, self.losses = [], []
        def enabled(self):
            return True
        def record(self, camera, raw, **metadata):
            entered.set()
            assert release.wait(2)
            self.rows.append((camera, raw, metadata))
            return True
        def tick(self):
            pass
        def note_loss(self, *args):
            self.losses.append(args)
        def close(self):
            pass

    target = SlowStore()
    sink = NativeCameraSink(target, queue_capacity=1, queue_bytes=2*len(jpeg()))
    raw = jpeg()
    sink.start()
    assert sink.submit('camera1', raw, sequence=1, timestamp_ns=1, wall_time=time.time())
    assert entered.wait(1)
    started = time.monotonic()
    assert sink.submit('camera1', raw, sequence=2, timestamp_ns=2, wall_time=time.time())
    assert not sink.submit('camera1', raw, sequence=3, timestamp_ns=3, wall_time=time.time())
    assert time.monotonic()-started < .1
    assert sink.snapshot()['queueFrames'] == 1
    release.set()
    assert sink.flush()
    assert sink.close()
    assert [row[2]['sequence'] for row in target.rows] == [1, 2]
    assert all(row[1] is raw for row in target.rows)
    assert ('camera1', 'queueDropped', 1) in target.losses


def test_module_and_callback_have_no_device_codec_or_network_calls():
    path = Path(__file__).parents[1]/'collector/archive_camera.py'
    tree = ast.parse(path.read_text())
    imports = {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports |= {name.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names}
    assert not imports & {'cv2', 'numpy', 'requests', 'aiohttp', 'socket', 'serial', 'rclpy'}
    submit = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'submit')
    calls = {getattr(node.func, 'attr', getattr(node.func, 'id', '')) for node in ast.walk(submit) if isinstance(node, ast.Call)}
    assert not calls & {'open', 'write', 'imdecode', 'imencode', 'resize', 'enabled', 'gate', 'record', 'fsync', 'sha256'}


def test_gate_closure_drains_accepted_frames_and_rejects_new_frames_until_reopening():
    enabled = [True]
    entered, release = threading.Event(), threading.Event()

    class GateStore:
        camera_names = {'camera1': 'camera1'}
        def __init__(self):
            self.rows, self.losses = [], []
        def enabled(self):
            return enabled[0]
        def record(self, camera, raw, **metadata):
            if metadata['sequence'] == 1:
                entered.set()
                assert release.wait(2)
            self.rows.append(metadata['sequence'])
            return True
        def note_loss(self, *args):
            self.losses.append(args)
        def tick(self):
            pass
        def close(self):
            pass

    target = GateStore()
    sink = NativeCameraSink(target)
    sink.start()
    raw = jpeg()
    assert sink.submit('camera1', raw, sequence=1, timestamp_ns=1, wall_time=time.time())
    assert entered.wait(1)
    assert sink.submit('camera1', raw, sequence=2, timestamp_ns=2, wall_time=time.time())
    enabled[0] = False
    sink.reconcile_gate()
    assert not sink.submit('camera1', raw, sequence=99, timestamp_ns=99, wall_time=time.time())
    enabled[0] = True
    sink.reconcile_gate()
    assert sink.submit('camera1', raw, sequence=3, timestamp_ns=3, wall_time=time.time())
    release.set()
    assert sink.flush() and sink.close()
    assert target.rows == [1, 2, 3]
    assert not target.losses


def test_writer_error_is_visible_and_cannot_kill_capture_queue_worker():
    class RecoveringStore:
        camera_names = {'camera1': 'camera1'}
        def __init__(self):
            self.rows = []
        def enabled(self):
            return True
        def record(self, camera, raw, **metadata):
            if metadata['sequence'] == 1:
                raise OSError('fixture disk error')
            self.rows.append(metadata['sequence'])
            return True
        def note_loss(self, *args):
            pass
        def tick(self):
            pass
        def close(self):
            pass

    target = RecoveringStore()
    sink = NativeCameraSink(target)
    sink.start()
    raw = jpeg()
    assert sink.submit('camera1', raw, sequence=1, timestamp_ns=1, wall_time=time.time())
    assert sink.submit('camera1', raw, sequence=2, timestamp_ns=2, wall_time=time.time())
    assert sink.flush()
    status = sink.snapshot()
    assert status['counters']['writeErrors'] == 1 and status['writerRunning']
    assert status['errors']['writer'] == 'OSError'
    assert target.rows == [2]
    assert sink.close()


def interrupted(root, walls=None):
    """Emulate a process exit after native writes, without camera hardware."""
    target = store(root, camera_names={'camera1': '原始相机_前'})
    walls = walls or [stamp('2026-10-27T12:00:00'), stamp('2026-10-27T12:00:01')]
    originals = [jpeg(30 + index) for index in range(len(walls))]
    for index, (raw, wall) in enumerate(zip(originals, walls), 1):
        assert record(target, raw, index, wall)
    value = target.active['camera1']
    for key in ('framesFile', 'indexFile'):
        value[key].close()
    target.active.clear()
    return value['path'], originals


def recovered_manifest(root):
    paths = list((root/'days').rglob('archive.json'))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text())


def test_recovery_works_with_gate_closed_and_is_idempotent(tmp_path):
    entry, original = interrupted(tmp_path)
    original_index = (entry/'index.jsonl').read_bytes()
    archive = store(tmp_path, gate=lambda: False, camera_names={'camera1': '原始相机_前'})
    path, manifest = recovered_manifest(tmp_path)
    assert (path.parent/'frames.mjpg').read_bytes() == b''.join(original)
    assert (path.parent/'index.jsonl').read_bytes() == original_index
    assert manifest['metadata']['recoveredFrames'] == 2
    assert manifest['day'] == '2026-10-27' and manifest['label'] == '原始相机_前_12:00-12:00'
    assert archive.active == {} and archive.snapshot()['enabled'] is False
    source = next((tmp_path/'recovery').glob('camera-*'))
    assert (source/'frames.mjpg').read_bytes() == b''.join(original)
    assert json.loads((source/'camera-recovery.json').read_text())['state'] == 'recovered'
    again = store(tmp_path, gate=lambda: False, camera_names={'camera1': '原始相机_前'})
    assert recovered_manifest(tmp_path)[0] == path
    assert again.finalized == 0 and 'recovery' not in again.errors
    client = ArchiveUploadClient(ArchiveUploadConfig('operator@fixture.test', 'fixture-password', robot_id=ROBOT))
    assert client._manifest(path.parent)[1]['files'] == ['frames.mjpg', 'index.jsonl']


@pytest.mark.parametrize('damage', ['bad_sha', 'partial_frame', 'partial_index', 'deep_json', 'oversized_line', 'bad_offset'])
def test_recovery_only_copies_complete_verified_prefix_and_retains_bad_tail(tmp_path, damage):
    entry, original = interrupted(tmp_path)
    rows = (entry/'index.jsonl').read_bytes().splitlines(keepends=True)
    if damage in ('bad_sha', 'bad_offset'):
        row = json.loads(rows[1])
        row['sha256' if damage == 'bad_sha' else 'offset'] = '0'*64 if damage == 'bad_sha' else 1
        rows[1] = (json.dumps(row)+'\n').encode()
    elif damage == 'partial_frame':
        (entry/'frames.mjpg').write_bytes(b''.join(original)[:-2])
    elif damage == 'partial_index':
        rows[1] = rows[1][:-4]
    elif damage == 'deep_json':
        rows[1] = b'['*1100+b'0'+b']'*1100+b'\n'
    elif damage == 'oversized_line':
        rows[1] = b'x'*5000+b'\n'
    (entry/'index.jsonl').write_bytes(b''.join(rows))
    preserved_raw = (entry/'frames.mjpg').read_bytes()
    preserved_index = (entry/'index.jsonl').read_bytes()
    archive = store(tmp_path, camera_names={'camera1': '原始相机_前'})
    path, manifest = recovered_manifest(tmp_path)
    assert (path.parent/'frames.mjpg').read_bytes() == original[0]
    assert manifest['metadata']['recoveredFrames'] == 1
    assert manifest['metadata']['unrecoveredTailBytes'] == len(preserved_raw)-len(original[0])
    source = next((tmp_path/'recovery').glob('camera-*'))
    assert (source/'frames.mjpg').read_bytes() == preserved_raw
    assert (source/'index.jsonl').read_bytes() == preserved_index
    assert 'recovery' in archive.errors
    assert not list(tmp_path.rglob('uploaded.json'))


@pytest.mark.parametrize('field,value', [('scope', 'outside'), ('robotId', 'different'), ('sensor', '../outside')])
def test_untrusted_session_never_publishes_outside_garage_or_robot_scope(tmp_path, field, value):
    entry, original = interrupted(tmp_path)
    session = json.loads((entry/'session.json').read_text())
    session[field] = value
    (entry/'session.json').write_text(json.dumps(session))
    archive = store(tmp_path, camera_names={'camera1': '原始相机_前'})
    assert not list((tmp_path/'days').rglob('archive.json'))
    assert next((tmp_path/'recovery').rglob('frames.mjpg')).read_bytes() == b''.join(original)
    assert 'recovery' in archive.errors


@pytest.mark.parametrize('name', ['frames.mjpg', 'index.jsonl', 'session.json'])
def test_recovery_rejects_payload_symlinks_and_preserves_external_target(tmp_path, name):
    entry, _ = interrupted(tmp_path)
    outside = tmp_path/'outside'
    outside.write_bytes((entry/name).read_bytes())
    previous = outside.read_bytes()
    (entry/name).unlink()
    (entry/name).symlink_to(outside)
    archive = store(tmp_path)
    assert outside.read_bytes() == previous
    assert not list((tmp_path/'days').rglob('archive.json'))
    assert 'recovery' in archive.errors


def test_recovery_cannot_write_through_a_days_symlink(tmp_path):
    interrupted(tmp_path)
    outside = tmp_path/'outside'
    outside.mkdir()
    (tmp_path/'days').symlink_to(outside, target_is_directory=True)
    archive = store(tmp_path)
    assert not list(outside.iterdir())
    assert list((tmp_path/'recovery').rglob('frames.mjpg'))
    assert 'recovery' in archive.errors


def test_clock_correction_inside_segment_preserves_every_native_frame(tmp_path):
    first = stamp('2026-10-27T12:00:00')
    _, original = interrupted(tmp_path, [first, first+10, first+5])
    store(tmp_path, camera_names={'camera1': '原始相机_前'})
    path, manifest = recovered_manifest(tmp_path)
    assert manifest['metadata']['recoveredFrames'] == 3
    assert (path.parent/'frames.mjpg').read_bytes() == b''.join(original)
    assert datetime.fromisoformat(manifest['endAt']).timestamp() == first+10
    index = [json.loads(line) for line in (path.parent/'index.jsonl').read_text().splitlines()]
    assert [row['wallTime'] for row in index] == [first, first+10, first+5]


def test_interrupted_recovery_reuses_reserved_stage_without_exhausting_quota(tmp_path):
    entry, original = interrupted(tmp_path)
    identity = 'f'*32
    (entry/'camera-recovery.json').write_text(json.dumps(dict(schemaVersion=1,
        robotId=ROBOT, cameraId='camera1', segmentId=identity, state='recovering')))
    stage = tmp_path/'.camera-recovering'/identity
    stage.mkdir(parents=True)
    (stage/'frames.mjpg').write_bytes(b'old-derivative-copy'*2000)
    store(tmp_path, max_spool_bytes=12000, camera_names={'camera1': '原始相机_前'})
    path, manifest = recovered_manifest(tmp_path)
    assert manifest['id'] == identity
    assert (path.parent/'frames.mjpg').read_bytes() == b''.join(original)
    assert not stage.exists()
    assert next((tmp_path/'recovery').rglob('frames.mjpg')).read_bytes() == b''.join(original)


def test_upload_pruning_does_not_republish_or_flag_recovery_as_corrupt(tmp_path):
    interrupted(tmp_path)
    store(tmp_path, camera_names={'camera1': '原始相机_前'})
    path, _ = recovered_manifest(tmp_path)
    files = [dict(relativePath=name, robotFileId=str(index), sha256=hashlib.sha256((path.parent/name).read_bytes()).hexdigest(),
                  size=(path.parent/name).stat().st_size) for index, name in enumerate(('frames.mjpg', 'index.jsonl'), 1)]
    (path.parent/'uploaded.json').write_text(json.dumps(dict(state='complete',
        manifestSha256=hashlib.sha256(path.read_bytes()).hexdigest(), files=files)))
    (path.parent/'local-copy-pruned.json').write_text(json.dumps(dict(state='pruned', files=files)))
    for name in ('frames.mjpg', 'index.jsonl'):
        (path.parent/name).unlink()
    again = store(tmp_path, camera_names={'camera1': '原始相机_前'})
    assert recovered_manifest(tmp_path)[0] == path
    assert 'recovery' not in again.errors and again.finalized == 0


def test_zero_verified_frames_stay_quarantined_and_deep_session_json_does_not_abort_start(tmp_path):
    entry, original = interrupted(tmp_path)
    (entry/'session.json').write_bytes(b'['*1100+b'0'+b']'*1100)
    archive = store(tmp_path)
    assert 'recovery' in archive.errors
    assert not list((tmp_path/'days').rglob('archive.json'))
    assert next((tmp_path/'recovery').rglob('frames.mjpg')).read_bytes() == b''.join(original)


def test_default_video_archive_keeps_one_frame_pending_and_not_uploadable(tmp_path):
    target = NativeCameraStore(tmp_path, ROBOT, gate=lambda: True, min_free_bytes=0,
        disk_usage=lambda _: SimpleNamespace(free=10**10))
    assert target.segment_bytes == 512*1024**2
    original = jpeg()
    assert record(target, original)
    target.close()
    assert not list(tmp_path.rglob('archive.json'))
    paths = list((tmp_path/'days').rglob('video-pending.json'))
    assert len(paths) == 1
    pending = json.loads(paths[0].read_text())
    metadata = pending['metadata']
    assert pending['files'] == ['frames.mjpg', 'index.jsonl']
    assert metadata['videoRequired'] is True and metadata['format'] == 'matroska/mjpeg'
    assert metadata['receivedFrames'] == 1
    assert (paths[0].parent/'frames.mjpg').read_bytes() == original
    for row in metadata['sourceFiles']:
        raw = (paths[0].parent/row['relativePath']).read_bytes()
        assert row['size'] == len(raw) and row['sha256'] == hashlib.sha256(raw).hexdigest()
    client = ArchiveUploadClient(ArchiveUploadConfig('operator@fixture.test', 'fixture-password', robot_id=ROBOT))
    with pytest.raises(ArchiveUploadError, match='ARCHIVE_MANIFEST_INVALID'):
        client._manifest(paths[0].parent)


def test_video_short_gate_close_and_rotation_never_publish_raw_uploads(tmp_path):
    enabled = [True]
    original = jpeg()
    target = store(tmp_path, video_format='mkv', gate=lambda: enabled[0], segment_bytes=len(original))
    assert record(target, original, 1)
    assert record(target, original, 2)
    enabled[0] = False
    target.tick()
    pending = [json.loads(path.read_text()) for path in (tmp_path/'days').rglob('video-pending.json')]
    assert len(pending) == 2
    assert {row['metadata']['closeReason'] for row in pending} == {'rotation', 'garage_disabled'}
    assert all(row['metadata']['receivedFrames'] == 1 for row in pending)
    assert not list(tmp_path.rglob('archive.json')) and not target.active


def pending_recovery(root):
    interrupted(root)
    target = store(root, video_format='mkv', gate=lambda: False,
                   camera_names={'camera1': '原始相机_前'})
    paths = list((root/'days').rglob('video-pending.json'))
    assert len(paths) == 1 and target.finalized == 1
    return paths[0], json.loads(paths[0].read_text())


@pytest.mark.parametrize('restart_format', ['mkv', 'mjpeg'])
def test_recovered_video_pending_is_a_durable_idempotent_checkpoint(tmp_path, restart_format):
    path, manifest = pending_recovery(tmp_path)
    assert manifest['metadata']['captureInterrupted'] is True
    assert manifest['metadata']['videoRequired'] is True
    assert manifest['metadata']['recoveredFrames'] == 2
    before = {name: (path.parent/name).read_bytes() for name in manifest['files']}
    again = store(tmp_path, video_format=restart_format, gate=lambda: False,
                  camera_names={'camera1': '原始相机_前'})
    assert list((tmp_path/'days').rglob('video-pending.json')) == [path]
    assert not list(tmp_path.rglob('archive.json'))
    assert again.finalized == 0 and 'recovery' not in again.errors
    assert all((path.parent/name).read_bytes() == raw for name, raw in before.items())


def test_video_resolution_change_starts_another_track_instead_of_mixing_dimensions(tmp_path):
    target = store(tmp_path, video_format='mkv')
    assert record(target, jpeg())
    ok, changed = cv2.imencode('.jpg', np.full((14, 20, 3), 80, dtype=np.uint8))
    assert ok
    assert target.record('camera1', changed.tobytes(), sequence=2, timestamp_ns=2000,
                         wall_time=stamp('2026-10-27T12:00:01'), width=20, height=14)
    target.close()
    pending = [json.loads(path.read_text()) for path in (tmp_path/'days').rglob('video-pending.json')]
    assert len(pending) == 2
    assert {tuple((d['width'], d['height']) for d in item['metadata']['dimensions']) for item in pending} == {
        ((19, 13),), ((20, 14),)}


def publish_video_checkpoint(path, pending, payload=b'isolated finalized video fixture'):
    """Model the independent finalizer's atomic publication contract."""
    video = path.parent/'video.mkv'
    video.write_bytes(payload)
    manifest = dict(pending, files=['video.mkv'], metadata=dict(pending['metadata'],
        videoReady=True, videoSha256=hashlib.sha256(payload).hexdigest(), videoBytes=len(payload)))
    archive = path.parent/'archive.json'
    archive.write_text(json.dumps(manifest))
    return archive, video


def test_recovery_does_not_republish_an_already_finalized_video(tmp_path):
    path, pending = pending_recovery(tmp_path)
    archive, video = publish_video_checkpoint(path, pending)
    before = video.read_bytes()
    again = store(tmp_path, video_format='mkv', camera_names={'camera1': '原始相机_前'})
    assert list((tmp_path/'days').rglob('archive.json')) == [archive]
    assert video.read_bytes() == before and again.finalized == 0
    assert 'recovery' not in again.errors


def test_recovery_accepts_video_pruned_only_after_matching_upload_ack(tmp_path):
    path, pending = pending_recovery(tmp_path)
    archive, video = publish_video_checkpoint(path, pending)
    payload = video.read_bytes()
    rows = [dict(relativePath='video.mkv', robotFileId='fixture-video-file',
                 sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))]
    (path.parent/'uploaded.json').write_text(json.dumps(dict(state='complete',
        manifestSha256=hashlib.sha256(archive.read_bytes()).hexdigest(), files=rows)))
    (path.parent/'local-copy-pruned.json').write_text(json.dumps(dict(state='pruned', files=rows)))
    for name in ('video.mkv', 'frames.mjpg', 'index.jsonl'):
        (path.parent/name).unlink()
    again = store(tmp_path, video_format='mkv', camera_names={'camera1': '原始相机_前'})
    assert list((tmp_path/'days').rglob('archive.json')) == [archive]
    assert not video.exists() and again.finalized == 0 and 'recovery' not in again.errors


@pytest.mark.parametrize('damage', ['video', 'source-checksum', 'ack'])
def test_recovery_rejects_changed_video_or_unverified_pruning_without_overwrite(tmp_path, damage):
    path, pending = pending_recovery(tmp_path)
    archive, video = publish_video_checkpoint(path, pending)
    if damage == 'video':
        video.write_bytes(b'changed video')
    elif damage == 'source-checksum':
        manifest = json.loads(archive.read_text())
        manifest['metadata']['sourceFiles'][0]['sha256'] = '0'*64
        archive.write_text(json.dumps(manifest))
    else:
        video.unlink()
    before = archive.read_bytes()
    again = store(tmp_path, video_format='mkv', camera_names={'camera1': '原始相机_前'})
    assert archive.read_bytes() == before and again.finalized == 0
    assert 'recovery' in again.errors
    assert len(list((tmp_path/'days').rglob('video-pending.json'))) == 1


def test_new_camera_session_requests_compact_video_without_rewriting_sources(tmp_path):
    target=store(tmp_path,video_format='mkv')
    raw=jpeg();assert record(target,raw)
    active=target.active['camera1']['path']
    assert json.loads((active/'session.json').read_text())['archiveProfile']=='compact-v1'
    target.close()
    pending=next((tmp_path/'days').rglob('video-pending.json'))
    manifest=json.loads(pending.read_text())
    assert manifest['metadata']['archiveProfile']=='compact-v1'
    assert (pending.parent/'frames.mjpg').read_bytes()==raw
    assert not list(tmp_path.rglob('archive.json'))


@pytest.mark.parametrize('compact',[False,True])
def test_camera_recovery_only_inherits_original_session_compact_choice(tmp_path,compact):
    entry,raw=interrupted(tmp_path)
    session=json.loads((entry/'session.json').read_text())
    if not compact:session.pop('archiveProfile')
    (entry/'session.json').write_text(json.dumps(session))
    store(tmp_path,video_format='mkv',gate=lambda:False,camera_names={'camera1':'原始相机_前'})
    pending=next((tmp_path/'days').rglob('video-pending.json'))
    metadata=json.loads(pending.read_text())['metadata']
    assert (metadata.get('archiveProfile')=='compact-v1') is compact
    assert (pending.parent/'frames.mjpg').read_bytes()==b''.join(raw)


@pytest.mark.parametrize('source_session', [None, 'a' * 32, 'not-a-session', 42])
def test_recovery_keeps_original_capture_identity_when_another_gate_is_current(tmp_path, source_session):
    entry, originals = interrupted(tmp_path)
    session = json.loads((entry / 'session.json').read_text())
    if source_session is not None:
        session['captureSessionId'] = source_session
    (entry / 'session.json').write_text(json.dumps(session))
    original_session = (entry / 'session.json').read_bytes()
    original_index = (entry / 'index.jsonl').read_bytes()
    current_gate = SimpleNamespace(enabled=lambda: False,
        snapshot=lambda: {'desktopSession': 'b' * 32})

    store(tmp_path, video_format='mkv', gate=current_gate,
        camera_names={'camera1': '原始相机_前'})

    pending_path = next((tmp_path / 'days').rglob('video-pending.json'))
    metadata = json.loads(pending_path.read_text())['metadata']
    assert metadata.get('captureSessionId') == ('a' * 32 if source_session == 'a' * 32 else None)
    assert metadata.get('captureSessionId') != 'b' * 32
    assert metadata['captureInterrupted'] is True
    source = tmp_path / 'recovery' / metadata['recoveryOriginal']
    assert (source / 'session.json').read_bytes() == original_session
    assert (source / 'index.jsonl').read_bytes() == original_index
    assert (source / 'frames.mjpg').read_bytes() == b''.join(originals)


def test_recovery_preserves_legacy_published_video_without_adding_session_identity(tmp_path):
    pending_path, pending = pending_recovery(tmp_path)
    source = next((tmp_path / 'recovery').glob('camera-*'))
    session = json.loads((source / 'session.json').read_text())
    session['captureSessionId'] = 'a' * 32
    (source / 'session.json').write_text(json.dumps(session))
    # Older releases omitted the durable source identity in published metadata.
    archive, video = publish_video_checkpoint(pending_path, pending)
    before_manifest, before_video = archive.read_bytes(), video.read_bytes()

    restarted = store(tmp_path, video_format='mkv', gate=lambda: False,
        camera_names={'camera1': '原始相机_前'})

    assert restarted.finalized == 0 and 'recovery' not in restarted.errors
    assert list((tmp_path / 'days').rglob('archive.json')) == [archive]
    assert archive.read_bytes() == before_manifest
    assert video.read_bytes() == before_video
    assert 'captureSessionId' not in json.loads(archive.read_text())['metadata']


@pytest.mark.parametrize('observe_disabled', ['tick', 'record'])
def test_disabled_capture_resets_only_sequence_baseline_not_previous_loss_counts(tmp_path,observe_disabled):
    enabled=[True]
    target=store(tmp_path,gate=lambda:enabled[0]);raw=jpeg()
    assert record(target,raw,100)
    assert record(target,raw,102)
    assert target.counters['camera1']['sequenceGaps']==1
    enabled[0]=False
    if observe_disabled=='tick':target.tick()
    else:assert not record(target,raw,120)
    assert not target.active and target.last_sequences=={}
    enabled[0]=True
    assert record(target,raw,10000)
    assert record(target,raw,10003)
    target.close()
    manifests=[json.loads(p.read_text()) for p in tmp_path.rglob('archive.json')]
    old=next(m for m in manifests if m['metadata']['closeReason']=='garage_disabled')
    new=next(m for m in manifests if m['metadata']['closeReason']=='shutdown')
    assert old['metadata']['sequenceGaps']==1
    assert new['metadata']['sequenceGaps']==2
    assert new['metadata']['cameraCountersAtClose']['sequenceGaps']==3
    segment=next(p.parent for p in tmp_path.rglob('archive.json') if json.loads(p.read_text())['id']==new['id'])
    rows=[json.loads(line) for line in (segment/'index.jsonl').read_text().splitlines()]
    assert rows[0]['gapBefore']==0 and rows[0]['sequenceReset'] is False
    assert rows[1]['gapBefore']==2


def test_rotation_and_storage_pause_preserve_true_missing_sequence_detection(tmp_path):
    raw=jpeg();capacity=[True]
    target=store(tmp_path,segment_bytes=len(raw),capacity=lambda:capacity[0])
    assert record(target,raw,10)
    assert record(target,raw,13)
    capacity[0]=False;assert not record(target,raw,14)
    capacity[0]=True;assert record(target,raw,18)
    target.close()
    rows=[json.loads(line) for p in tmp_path.rglob('index.jsonl') for line in p.read_text().splitlines()]
    assert {r['sequence']:r['gapBefore'] for r in rows}=={10:0,13:2,18:4}
    assert target.counters['camera1']['sequenceGaps']==6
    assert target.counters['camera1']['storageDropped']==1


def test_recorder_restart_starts_new_sequence_baseline_without_rewriting_old_archive(tmp_path):
    first=store(tmp_path);raw=jpeg();assert record(first,raw,900);first.close()
    old=next(tmp_path.rglob('archive.json'));before=old.read_bytes()
    restarted=store(tmp_path);assert record(restarted,raw,1);restarted.close()
    assert old.read_bytes()==before
    new=next(p for p in tmp_path.rglob('archive.json') if p!=old)
    row=json.loads((new.parent/'index.jsonl').read_text())
    assert row['gapBefore']==0 and row['sequenceReset'] is False
    assert json.loads(new.read_text())['metadata']['cameraCountersAtClose']['sequenceResets']==0



def test_new_usb_capture_persists_720p_policy_without_resizing_native_jpeg(tmp_path):
    target = store(tmp_path, video_format='mkv')
    raw = jpeg(173)
    assert record(target, raw)
    session = json.loads(next((tmp_path/'.camera-active').rglob('session.json')).read_text())
    assert session['videoArchivePreset'] == '720p-balanced-v1'
    target.close()
    pending_path = next(tmp_path.rglob('video-pending.json'))
    pending = json.loads(pending_path.read_text())
    assert pending['metadata']['archiveProfile'] == 'compact-v1'
    assert pending['metadata']['videoArchivePreset'] == '720p-balanced-v1'
    assert pending['metadata']['resized'] is False
    assert (pending_path.parent/'frames.mjpg').read_bytes() == raw


@pytest.mark.parametrize('preset', [None, '720p-economy-v1', '720p-balanced-v1'])
def test_native_recovery_never_applies_default_720p_to_unmarked_history(tmp_path, preset):
    entry, originals = interrupted(tmp_path)
    session = json.loads((entry/'session.json').read_text())
    session['archiveProfile'] = 'compact-v1'
    session.pop('videoArchivePreset', None)
    if preset is not None:
        session['videoArchivePreset'] = preset
    (entry/'session.json').write_text(json.dumps(session))
    store(tmp_path, video_format='mkv', camera_names={'camera1': '原始相机_前'})
    pending_path = next(tmp_path.rglob('video-pending.json'))
    pending = json.loads(pending_path.read_text())
    assert pending['metadata'].get('videoArchivePreset') == preset
    assert (pending_path.parent/'frames.mjpg').read_bytes() == b''.join(originals)
