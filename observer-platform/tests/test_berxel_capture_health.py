"""Synthetic metadata and private temporary files only; no ROS or device access."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest

PATH = Path(__file__).resolve().parents[1] / 'deploy/jetson/berxel_preview/capture_health.py'
SPEC = importlib.util.spec_from_file_location('berxel_capture_health_test_module', PATH)
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)
BOOT = '12345678-1234-1234-1234-123456789abc'


def source_report():
    return {'color': {'sourceTopic': '/berxel/color/image_raw', 'receivedFrames': 42,
                      'sourceAgeS': .1, 'publishedFrames': 12, 'previewAgeS': .2,
                      'data': 'pixels must not be persisted'},
            'depth': {'sourceTopic': '/berxel/depth/image_raw', 'receivedFrames': 0,
                      'sourceAgeS': None, 'publishedFrames': 99},
            'fisheye': {'data': 'also excluded'}}


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setattr(health, '_boot_id', lambda: BOOT)
    monkeypatch.setattr(health.time, 'monotonic', lambda: 100.0)
    return tmp_path / 'state' / 'robot-cloud' / 'garage-control' / 'berxel-capture.json'


def test_payload_has_only_original_image_evidence_and_does_not_mutate_input():
    value = source_report()
    before = copy.deepcopy(value)
    actual = health.pack_capture_health(value, pid=123, boot_id=BOOT, monotonic=100)
    assert value == before
    assert actual == {'schemaVersion': 1, 'pid': 123, 'bootId': BOOT,
                      'reportedMonotonic': 100, 'channels': {
                          'color': {'topic': '/berxel/color/image_raw', 'receivedFrames': 42, 'sourceAgeS': .1},
                          'depth': {'topic': '/berxel/depth/image_raw', 'receivedFrames': 0, 'sourceAgeS': None}}}
    assert 'pixels' not in json.dumps(actual)
    assert 'publishedFrames' not in json.dumps(actual)
    assert 'previewAgeS' not in json.dumps(actual)


def test_atomic_private_write_and_complete_old_inode(target):
    old = health.write_capture_health(source_report(), target)
    assert old['pid'] == os.getpid()
    assert old['bootId'] == BOOT
    assert old['reportedMonotonic'] == 100
    assert json.loads(target.read_bytes()) == old
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    with target.open('rb') as prior:
        update = source_report()
        update['color']['receivedFrames'] += 1
        new = health.write_capture_health(update, target)
        assert json.load(prior) == old
    assert json.loads(target.read_bytes()) == new
    assert [p.name for p in target.parent.iterdir()] == [target.name]
    assert target.stat().st_size < health.MAX_REPORT_BYTES


@pytest.mark.parametrize('mutation', [
    lambda value: value.pop('color'),
    lambda value: value.update(depth=[]),
    lambda value: value['color'].update(sourceTopic='/observer_camera/color/compressed'),
    lambda value: value['color'].update(sourceTopic='/berxel/depth/image_raw'),
    lambda value: value['color'].pop('receivedFrames'),
    lambda value: value['color'].update(receivedFrames=True),
    lambda value: value['color'].update(receivedFrames=-1),
    lambda value: value['color'].update(receivedFrames=2.0),
    lambda value: value['color'].update(receivedFrames=2 ** 53),
    lambda value: value['depth'].pop('sourceAgeS'),
    lambda value: value['depth'].update(sourceAgeS=True),
    lambda value: value['depth'].update(sourceAgeS=-1),
    lambda value: value['depth'].update(sourceAgeS=float('nan')),
    lambda value: value['depth'].update(sourceAgeS=float('inf')),
    lambda value: value['depth'].update(sourceAgeS=10 ** 400),
    lambda value: value['depth'].update(sourceAgeS='0.1'),
])
def test_invalid_raw_evidence_never_replaces_existing_report(target, mutation):
    original = health.write_capture_health(source_report(), target)
    before = target.read_bytes()
    value = source_report()
    mutation(value)
    with pytest.raises(ValueError):
        health.write_capture_health(value, target)
    assert target.read_bytes() == before
    assert json.loads(before) == original
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


@pytest.mark.parametrize('kwargs', [
    {'pid': True}, {'pid': 0}, {'pid': -1}, {'pid': 2.0}, {'pid': 2 ** 53},
    {'boot_id': ''}, {'boot_id': 'not-a-boot-id'}, {'boot_id': BOOT + '\n'},
    {'boot_id': 123}, {'monotonic': True}, {'monotonic': -1},
    {'monotonic': float('nan')}, {'monotonic': float('inf')}, {'monotonic': 10 ** 400},
])
def test_invalid_report_identity_or_time_is_rejected(kwargs):
    options = {'pid': 123, 'boot_id': BOOT, 'monotonic': 100}
    options.update(kwargs)
    with pytest.raises(ValueError):
        health.pack_capture_health(source_report(), **options)


def test_safe_integer_boundary_is_preserved():
    value = source_report()
    value['color']['receivedFrames'] = 2 ** 53 - 1
    result = health.pack_capture_health(value, pid=123, boot_id=BOOT, monotonic=0)
    assert result['channels']['color']['receivedFrames'] == 2 ** 53 - 1
    assert result['reportedMonotonic'] == 0


def test_symlink_report_is_rejected_without_touching_destination(target):
    target.parent.mkdir(parents=True, mode=0o700)
    other = target.parent / 'unrelated'
    other.write_text('original')
    target.symlink_to(other)
    with pytest.raises((OSError, ValueError)):
        health.write_capture_health(source_report(), target)
    assert target.is_symlink() and other.read_text() == 'original'


def test_symlink_ancestor_is_rejected_without_creating_report(tmp_path, monkeypatch):
    monkeypatch.setattr(health, '_boot_id', lambda: BOOT)
    real = tmp_path / 'real'
    real.mkdir(mode=0o700)
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        health.write_capture_health(source_report(), link / 'report.json')
    assert list(real.iterdir()) == []


@pytest.mark.parametrize('mode', [0o644, 0o660, 0o700])
def test_existing_report_permissions_are_not_silently_repaired(target, mode):
    health.write_capture_health(source_report(), target)
    target.chmod(mode)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        health.write_capture_health(source_report(), target)
    assert target.read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == mode


def test_existing_nonprivate_directory_is_not_changed(target):
    target.parent.mkdir(parents=True, mode=0o755)
    target.parent.chmod(0o755)
    with pytest.raises(ValueError):
        health.write_capture_health(source_report(), target)
    assert not target.exists()
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o755


def test_hardlinked_report_is_rejected(target):
    health.write_capture_health(source_report(), target)
    alias = target.parent / 'another-name'
    os.link(target, alias)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        health.write_capture_health(source_report(), target)
    assert target.read_bytes() == alias.read_bytes() == before


def test_failed_replace_preserves_report_and_removes_temporary(target, monkeypatch):
    health.write_capture_health(source_report(), target)
    before = target.read_bytes()
    def failed(*args, **kwargs):
        raise OSError('synthetic rename failure')
    monkeypatch.setattr(health.os, 'replace', failed)
    with pytest.raises(OSError):
        health.write_capture_health(source_report(), target)
    assert target.read_bytes() == before
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_relative_path_rejected(target, monkeypatch):
    with pytest.raises(ValueError):
        health.write_capture_health(source_report(), Path('relative-report.json'))


def test_unavailable_boot_id_does_not_touch_files(target, monkeypatch):
    health.write_capture_health(source_report(), target)
    before = target.read_bytes()
    def missing():
        raise OSError('synthetic unavailable kernel boot identity')
    monkeypatch.setattr(health, '_boot_id', missing)
    with pytest.raises(OSError):
        health.write_capture_health(source_report(), target)
    assert target.read_bytes() == before



def test_private_ancestor_protects_same_owner_writable_intermediate(target):
    target.parent.mkdir(parents=True, mode=0o700)
    protected = target.parents[2]
    protected.chmod(0o700)
    intermediate = target.parent.parent
    intermediate.chmod(0o775)
    result = health.write_capture_health(source_report(), target)
    assert json.loads(target.read_bytes()) == result
    assert stat.S_IMODE(intermediate.stat().st_mode) == 0o775
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_unprotected_writable_intermediate_is_rejected(monkeypatch):
    import tempfile
    monkeypatch.setattr(health, '_boot_id', lambda: BOOT)
    with tempfile.TemporaryDirectory(prefix='berxel-unprotected-', dir='/tmp') as directory:
        root = Path(directory)
        root.chmod(0o755)
        intermediate = root / 'shared-state'
        intermediate.mkdir(mode=0o775)
        intermediate.chmod(0o775)
        private = intermediate / 'private'
        private.mkdir(mode=0o700)
        target = private / 'berxel-capture.json'
        with pytest.raises(ValueError, match='Untrusted'):
            health.write_capture_health(source_report(), target)
        assert not target.exists()
        assert stat.S_IMODE(intermediate.stat().st_mode) == 0o775



def test_blocked_ros_publish_time_is_carried_into_original_frame_age():
    value = source_report()
    for row in (value['color'], value['depth']):
        row['reportedMonotonic'] = 95.0
    result = health.pack_capture_health(value, pid=123, boot_id=BOOT, monotonic=100)
    assert result['reportedMonotonic'] == 100
    assert result['channels']['color']['sourceAgeS'] == 5.1
    assert result['channels']['depth']['sourceAgeS'] is None
    assert value['color']['sourceAgeS'] == .1


@pytest.mark.parametrize('stamp', [101, True, -1, float('nan'), float('inf'), 10 ** 400, '95'])
def test_future_or_malformed_original_sampling_time_does_not_refresh_report(target, stamp):
    health.write_capture_health(source_report(), target)
    before = target.read_bytes()
    value = source_report()
    value['color']['reportedMonotonic'] = stamp
    with pytest.raises(ValueError):
        health.write_capture_health(value, target)
    assert target.read_bytes() == before
