"""Calibration evidence follows its original segment without changing JPEGs."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import cv2
import numpy as np
import pytest

from collector import archive_camera
from collector.archive_camera import NativeCameraSink, NativeCameraStore


ROBOT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
WALL = 1793073600.0


def jpeg(width=19, height=13, value=100):
    ok, data = cv2.imencode('.jpg', np.full((height, width, 3), value, dtype=np.uint8))
    assert ok
    return data.tobytes()


def write_config(path, *, focal=50, width=19, height=13, result='result-original'):
    path.write_text(json.dumps(dict(focal=focal, width=width, height=height, result=result)))


def snapshot(config, camera, width, height):
    native = dict(width=config['width'], height=config['height'], distortionModel='plumb_bob',
        K=[config['focal'], 0, 9, 0, config['focal'], 6, 0, 0, 1], D=[0.] * 5,
        R=[1, 0, 0, 0, 1, 0, 0, 0, 1], P=[config['focal'], 0, 9, 0, 0, config['focal'], 6, 0, 0, 0, 1, 0])
    matched = (width, height) == (config['width'], config['height'])
    value = dict(state='matched' if matched else 'resolution_mismatch', cameraId=camera,
        direction='front', device='/FIXTURE/usb-camera', robotId=ROBOT,
        nativeSize=dict(width=config['width'], height=config['height']),
        sourceSize=dict(width=config['width'], height=config['height']),
        size=dict(width=width, height=height), nativeImageInfo=native,
        sourceResultId=config['result'], sourceArchiveSha256='a' * 64,
        sourceYamlSha256='b' * 64, transform=dict(kind='none', reason='exact' if matched else 'resolution_mismatch'))
    if matched:
        value['currentImageInfo'] = deepcopy(native)
    return value


@pytest.fixture
def loader(monkeypatch):
    """Stub only the separately tested registry decoder, using explicit files."""
    calls, results = [], []

    def get(camera, width, height, *, robot_id=None, path=None, registry=None, resize=False):
        calls.append((camera, width, height, robot_id, path, resize, threading.current_thread()))
        assert path is not None  # This suite must not read the user's registry.
        if not Path(path).exists():
            return None
        value = snapshot(json.loads(Path(path).read_text()), camera, width, height)
        results.append(value)
        return value

    module = ModuleType('collector.camera_calibration')
    module.get_camera_calibration = get
    monkeypatch.setitem(sys.modules, 'collector.camera_calibration', module)
    return SimpleNamespace(calls=calls, results=results, module=module)


def store(tmp_path, config, **options):
    return NativeCameraStore(tmp_path / 'spool', ROBOT, gate=lambda: True,
        calibration_path=config, min_free_bytes=0, max_spool_bytes=10**8,
        disk_usage=lambda _: SimpleNamespace(free=10**10),
        clock=lambda: WALL, video_format=options.pop('video_format', 'mjpeg'), **options)


def record(target, raw, sequence=1, offset=0):
    return target.record_accepted('camera1', raw, sequence=sequence, timestamp_ns=1_000_000 + sequence,
        wall_time=WALL + offset)


def manifests(tmp_path, filename='archive.json'):
    return [(path, json.loads(path.read_text())) for path in (tmp_path / 'spool' / 'days').rglob(filename)]


def interrupt(target):
    value = target.active['camera1']
    for key in ('framesFile', 'indexFile'):
        value[key].close()
    target.active.clear()
    return value['path']


def test_segment_freezes_original_result_and_rotation_reads_new_result(tmp_path, loader):
    config = tmp_path / 'registry.json'
    write_config(config)
    target = store(tmp_path, config, segment_seconds=5)
    raw = jpeg()
    assert record(target, raw)
    original = deepcopy(target.active['camera1']['cameraCalibration'])
    session_path = target.active['camera1']['path'] / 'session.json'
    assert json.loads(session_path.read_text())['cameraCalibration'] == original
    # A caller reusing its output object or replacing the active registry cannot
    # alter evidence already associated with written frames.
    loader.results[0]['nativeImageInfo']['K'][0] = 9999
    write_config(config, focal=75, result='result-next')
    assert record(target, raw, sequence=2, offset=1)
    assert len(loader.calls) == 1
    assert target.active['camera1']['cameraCalibration'] == original
    assert record(target, raw, sequence=3, offset=6)
    assert len(loader.calls) == 2
    target.close()
    rows = sorted(manifests(tmp_path), key=lambda row: row[1]['startedAt'])
    assert rows[0][1]['metadata']['cameraCalibration'] == original
    assert rows[1][1]['metadata']['cameraCalibration']['sourceResultId'] == 'result-next'
    assert rows[1][1]['metadata']['cameraCalibration']['currentImageInfo']['K'][0] == 75
    assert [(path.parent / 'frames.mjpg').read_bytes() for path, _ in rows] == [raw + raw, raw]
    assert all(row['files'] == ['frames.mjpg', 'index.jsonl'] for _, row in rows)


def test_resolution_mismatch_preserves_raw_jpeg_without_a_current_matrix(tmp_path, loader):
    config = tmp_path / 'registry.json'
    write_config(config, width=38, height=26)
    target = store(tmp_path, config)
    raw = jpeg()
    assert record(target, raw)
    target.close()
    path, manifest = manifests(tmp_path)[0]
    calibration = manifest['metadata']['cameraCalibration']
    assert calibration['state'] == 'resolution_mismatch'
    assert 'currentImageInfo' not in calibration
    assert calibration['nativeImageInfo']['width'] == 38
    assert (path.parent / 'frames.mjpg').read_bytes() == raw
    assert manifest['metadata']['receivedFrames'] == 1


def test_recovery_keeps_frozen_snapshot_instead_of_current_registry(tmp_path, loader):
    config = tmp_path / 'registry.json'
    write_config(config)
    target = store(tmp_path, config)
    raw = jpeg()
    assert record(target, raw)
    original = deepcopy(target.active['camera1']['cameraCalibration'])
    # A valid snapshot can exceed the old generic 4 KiB session JSON limit.
    original['quality'] = dict(note='fixture provenance ' * 300)
    value = target.active['camera1']
    session = json.loads((value['path'] / 'session.json').read_text())
    session['cameraCalibration'] = original
    archive_camera._atomic_json(value['path'] / 'session.json', session)
    source = interrupt(target)
    source_session = (source / 'session.json').read_bytes()
    write_config(config, focal=1000, result='wrong-current-result')
    recovered = store(tmp_path, config)
    assert len(loader.calls) == 1
    path, manifest = manifests(tmp_path)[0]
    assert manifest['metadata']['cameraCalibration'] == original
    assert (path.parent / 'frames.mjpg').read_bytes() == raw
    archived_source = tmp_path / 'spool' / 'recovery' / manifest['metadata']['recoveryOriginal']
    assert (archived_source / 'session.json').read_bytes() == source_session
    assert 'recovery' not in recovered.errors
    # Reopening does not duplicate a recovery copy or consult a new registry.
    again = store(tmp_path, config)
    assert len(manifests(tmp_path)) == 1 and len(loader.calls) == 1
    assert 'recovery' not in again.errors


def test_missing_registry_preserves_legacy_metadata_and_is_not_applied_on_recovery(tmp_path, loader):
    config = tmp_path / 'missing-registry.json'
    target = store(tmp_path, config)
    raw = jpeg()
    assert record(target, raw)
    source = interrupt(target)
    assert 'cameraCalibration' not in json.loads((source / 'session.json').read_text())
    write_config(config)
    recovered = store(tmp_path, config)
    path, manifest = manifests(tmp_path)[0]
    assert 'cameraCalibration' not in manifest['metadata']
    assert (path.parent / 'frames.mjpg').read_bytes() == raw
    assert len(loader.calls) == 1 and 'recovery' not in recovered.errors


@pytest.mark.parametrize('invalid', ['raises', 'wrong_camera', 'oversize', 'nonfinite'])
def test_bad_calibration_cannot_prevent_native_frame_recording(tmp_path, loader, invalid):
    config = tmp_path / 'registry.json'
    write_config(config)

    def bad(*args, **kwargs):
        if invalid == 'raises':
            raise ValueError('fixture malformed registry')
        value = snapshot(json.loads(config.read_text()), 'camera1', 19, 13)
        if invalid == 'wrong_camera':
            value['cameraId'] = 'camera2'
        elif invalid == 'oversize':
            value['quality'] = 'x' * 20000
        else:
            value['nativeImageInfo']['K'][0] = float('nan')
        return value

    loader.module.get_camera_calibration = bad
    target = store(tmp_path, config)
    raw = jpeg()
    assert record(target, raw)
    target.close()
    path, manifest = manifests(tmp_path)[0]
    assert manifest['metadata']['cameraCalibration'] == dict(state='invalid', cameraId='camera1',
        errorCode='calibration_load_failed')
    assert (path.parent / 'frames.mjpg').read_bytes() == raw


@pytest.mark.parametrize('persist_fails', [False, True])
def test_size_change_removes_current_matrix_and_recovery_rechecks_sizes(tmp_path, loader, monkeypatch, persist_fails):
    config = tmp_path / 'registry.json'
    write_config(config)
    target = store(tmp_path, config)
    originals = [jpeg(), jpeg(20, 14)]
    assert record(target, originals[0])
    original_atomic = archive_camera._atomic_json

    def atomic(path, value):
        if persist_fails and path.name == 'session.json':
            raise OSError('fixture session metadata unavailable')
        return original_atomic(path, value)

    with monkeypatch.context() as context:
        context.setattr(archive_camera, '_atomic_json', atomic)
        assert record(target, originals[1], sequence=2, offset=1)
    assert len(loader.calls) == 1
    assert target.active['camera1']['cameraCalibration']['state'] == 'resolution_mismatch'
    assert 'currentImageInfo' not in target.active['camera1']['cameraCalibration']
    interrupt(target)
    recovered = store(tmp_path, config)
    path, manifest = manifests(tmp_path)[0]
    calibration = manifest['metadata']['cameraCalibration']
    assert calibration['state'] == 'resolution_mismatch' and 'currentImageInfo' not in calibration
    assert calibration['nativeImageInfo']['K'][0] == 50
    assert (path.parent / 'frames.mjpg').read_bytes() == b''.join(originals)
    assert 'recovery' not in recovered.errors and len(loader.calls) == 1


def test_native_sink_loads_metadata_in_writer_without_touching_submit_callback(tmp_path, loader):
    config = tmp_path / 'registry.json'
    write_config(config)
    target = store(tmp_path, config)
    sink = NativeCameraSink(target)
    raw = jpeg()
    sink.reconcile_gate()
    assert sink.submit('camera1', raw, sequence=1, timestamp_ns=1_000_000, wall_time=WALL)
    assert loader.calls == []
    sink.start()
    try:
        assert sink.flush(timeout=3)
    finally:
        assert sink.close(timeout=3)
    assert len(loader.calls) == 1 and loader.calls[0][-1] is not threading.current_thread()
    assert (manifests(tmp_path)[0][0].parent / 'frames.mjpg').read_bytes() == raw


def test_mkv_pending_keeps_original_file_contract_and_calibration(tmp_path, loader):
    config = tmp_path / 'registry.json'
    write_config(config)
    target = store(tmp_path, config, video_format='mkv')
    assert record(target, jpeg())
    target.close()
    _, manifest = manifests(tmp_path, 'video-pending.json')[0]
    assert manifest['files'] == ['frames.mjpg', 'index.jsonl']
    assert manifest['metadata']['cameraCalibration']['state'] == 'matched'
    assert manifest['metadata']['videoRequired'] is True


def test_real_registry_loader_and_writer_preserve_each_camera_identity(tmp_path):
    """Exercise the actual decoder/writer boundary with no host configuration."""
    config = tmp_path / 'private-registry.json'
    directions = dict(camera1='front', camera2='rear', camera3='right', camera4='left')
    registry = dict(schemaVersion=1, robotId=ROBOT,
        quality='candidate_no_independent_validation', sourceResultId='result-interface-fixture',
        sourceArchiveSha256='a' * 64, cameras={})
    originals = {}
    for index, (camera, direction) in enumerate(directions.items(), 1):
        width, height = (19, 13) if index < 3 else (20, 14)
        native = snapshot(dict(width=width, height=height, focal=50 + index,
            result='unused'), camera, width, height)['nativeImageInfo']
        registry['cameras'][camera] = dict(id=camera, direction=direction,
            device=f'/dev/v4l/by-path/platform-fixture-usb-0:{index}:1.0-video-index0',
            imageWidth=width, imageHeight=height, distortionModel='plumb_bob',
            sourceYamlSha256=str(index) * 64, **{key: native[key] for key in ('K', 'D', 'R', 'P')})
        originals[camera] = jpeg(width, height, value=20 * index)
    config.write_text(json.dumps(registry))
    config.chmod(0o600)
    target = store(tmp_path, config)
    for index, (camera, raw) in enumerate(originals.items(), 1):
        assert target.record_accepted(camera, raw, sequence=1, timestamp_ns=1000 + index, wall_time=WALL)
    target.close()
    rows = manifests(tmp_path)
    assert len(rows) == 4
    for path, manifest in rows:
        camera = manifest['metadata']['cameraId']
        calibration = manifest['metadata']['cameraCalibration']
        assert calibration['state'] == 'matched'
        assert calibration['cameraId'] == camera and calibration['direction'] == directions[camera]
        assert calibration['robotId'] == ROBOT
        assert calibration['sourceResultId'] == registry['sourceResultId']
        assert calibration['currentImageInfo']['K'] == registry['cameras'][camera]['K']
        assert (path.parent / 'frames.mjpg').read_bytes() == originals[camera]


def test_host_clock_domain_is_read_once_and_frozen_for_each_segment(tmp_path, loader, monkeypatch):
    boot = tmp_path / 'boot-id'
    boot.write_text('11111111-2222-4333-8444-555555555555\n')
    expected = 'linux-boot:11111111-2222-4333-8444-555555555555'
    assert archive_camera._linux_boot_clock_domain(boot) == expected
    reads = []

    def clock_domain():
        reads.append(True)
        return expected

    monkeypatch.setattr(archive_camera, '_linux_boot_clock_domain', clock_domain)
    target = store(tmp_path, tmp_path / 'missing.json', segment_seconds=5)
    assert len(reads) == 1
    raw = jpeg()
    assert record(target, raw)
    session = json.loads((target.active['camera1']['path'] / 'session.json').read_text())
    assert session['clockDomain'] == expected
    assert session['clockSource'] == 'CLOCK_REALTIME'
    assert session['timestampSemantics'] == 'host_read_complete'
    assert record(target, raw, sequence=2, offset=6)
    target.close()
    assert len(reads) == 1
    rows = manifests(tmp_path)
    assert len(rows) == 2
    for _, manifest in rows:
        assert manifest['metadata']['clockDomain'] == expected
        assert manifest['metadata']['clockSource'] == 'CLOCK_REALTIME'
        assert manifest['metadata']['timestampSemantics'] == 'host_read_complete'
        assert manifest['metadata']['timestampBasis'] == 'source timestampNs and original wallTime in index.jsonl'


def test_missing_or_invalid_boot_id_never_emits_a_shared_clock_domain(tmp_path, loader, monkeypatch):
    boot = tmp_path / 'boot-id'
    for invalid in ('', 'invalid', '00000000-0000-0000-0000-000000000000',
                    '11111111-2222-4333-8444-555555555555 garbage', 'x' * 100):
        boot.write_text(invalid)
        assert archive_camera._linux_boot_clock_domain(boot) is None
    boot.unlink()
    assert archive_camera._linux_boot_clock_domain(boot) is None
    monkeypatch.setattr(archive_camera, '_linux_boot_clock_domain', lambda: None)
    target = store(tmp_path, tmp_path / 'missing.json')
    assert record(target, jpeg())
    session = json.loads((target.active['camera1']['path'] / 'session.json').read_text())
    assert 'clockDomain' not in session
    target.close()
    metadata = manifests(tmp_path)[0][1]['metadata']
    assert 'clockDomain' not in metadata
    assert metadata['clockSource'] == 'CLOCK_REALTIME'
    assert metadata['timestampSemantics'] == 'host_read_complete'


def test_recovery_preserves_old_boot_domain_and_never_labels_legacy_as_current(tmp_path, loader, monkeypatch):
    old = 'linux-boot:11111111-2222-4333-8444-555555555555'
    new = 'linux-boot:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    monkeypatch.setattr(archive_camera, '_linux_boot_clock_domain', lambda: old)
    target = store(tmp_path, tmp_path / 'missing.json')
    assert record(target, jpeg())
    source = interrupt(target)
    old_session = (source / 'session.json').read_bytes()
    monkeypatch.setattr(archive_camera, '_linux_boot_clock_domain', lambda: new)
    recovered = store(tmp_path, tmp_path / 'missing.json')
    metadata = manifests(tmp_path)[0][1]['metadata']
    assert recovered.clock_domain == new
    assert metadata['clockDomain'] == old
    assert metadata['clockSource'] == 'CLOCK_REALTIME'
    assert metadata['timestampSemantics'] == 'host_read_complete'
    recovery_source = tmp_path / 'spool' / 'recovery' / metadata['recoveryOriginal']
    assert (recovery_source / 'session.json').read_bytes() == old_session

    legacy_root = tmp_path / 'legacy'
    legacy_root.mkdir()
    target = store(legacy_root, tmp_path / 'missing.json')
    assert record(target, jpeg())
    session_path = target.active['camera1']['path'] / 'session.json'
    session = json.loads(session_path.read_text())
    for key in ('clockDomain', 'clockSource', 'timestampSemantics'):
        session.pop(key)
    archive_camera._atomic_json(session_path, session)
    interrupt(target)
    store(legacy_root, tmp_path / 'missing.json')
    legacy = manifests(legacy_root)[0][1]['metadata']
    assert not set(('clockDomain', 'clockSource', 'timestampSemantics')) & legacy.keys()
