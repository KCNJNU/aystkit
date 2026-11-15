"""Private temporary-file fixtures only; no ROS, services, network or pixels."""
import copy
import json
import os
from pathlib import Path
import stat

import pytest

from shared import preview_health as health


def channels():
    return {'color': {'frameSeq': 7, 'status': 'AVAILABLE', 'ageS': .1},
            'depth': {'frameSeq': 0, 'status': 'WAITING_UPSTREAM', 'ageS': None}}


@pytest.fixture
def target(tmp_path):
    return tmp_path / 'state' / 'robot-cloud' / 'preview-receiver.json'


def write_report(target, monkeypatch):
    monkeypatch.setattr(health.time, 'monotonic', lambda: 100.0)
    return health.PreviewHealthWriter(target).write(channels())


def replace_report(target, report):
    target.write_text(json.dumps(report))
    target.chmod(0o600)


def test_roundtrip_accepts_live_and_waiting_metadata_without_mutating_input(target, monkeypatch):
    source = channels()
    before = copy.deepcopy(source)
    monkeypatch.setattr(health.time, 'monotonic', lambda: 100.0)
    result = health.PreviewHealthWriter(target).write(source)
    assert source == before
    assert result == health.read_preview_health(target, os.getpid(), now=101)
    assert result == {'schemaVersion': 1, 'pid': os.getpid(), 'monotonic': 100.0,
                      'channels': before}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.parent.parent.stat().st_mode) == 0o700
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_atomic_replacement_leaves_prior_open_inode_complete(target, monkeypatch):
    old = write_report(target, monkeypatch)
    with target.open('rb') as old_handle:
        second = channels()
        second['color']['frameSeq'] = 8
        new = health.PreviewHealthWriter(target).write(second)
        assert json.load(old_handle) == old
    assert health.read_preview_health(target, os.getpid(), now=100) == new


@pytest.mark.parametrize('change', [
    lambda row: row.update(frameSeq=True), lambda row: row.update(frameSeq=-1),
    lambda row: row.update(frameSeq=1.5), lambda row: row.update(ageS=True),
    lambda row: row.update(ageS=-1), lambda row: row.update(ageS=float('nan')),
    lambda row: row.update(ageS=float('inf')), lambda row: row.update(ageS=10 ** 400),
    lambda row: row.update(status=''),
    lambda row: row.update(status=123), lambda row: row.update(status='BAD\nSTATUS'),
    lambda row: row.update(data='private-pixels'), lambda row: row.pop('status'),
])
def test_invalid_input_never_creates_or_changes_report(target, monkeypatch, change):
    original = write_report(target, monkeypatch)
    raw = target.read_bytes()
    bad = channels()
    change(bad['color'])
    with pytest.raises(ValueError):
        health.PreviewHealthWriter(target).write(bad)
    assert target.read_bytes() == raw
    assert health.read_preview_health(target, os.getpid(), now=100) == original


@pytest.mark.parametrize('mutation', [
    lambda report: report.update(schemaVersion=True),
    lambda report: report.update(schemaVersion=2),
    lambda report: report.update(pid=True),
    lambda report: report.update(pid=os.getpid() + 1),
    lambda report: report.update(monotonic=True),
    lambda report: report.update(monotonic=-1),
    lambda report: report.update(monotonic=float('nan')),
    lambda report: report.update(monotonic=float('inf')),
    lambda report: report.update(monotonic=10 ** 400),
    lambda report: report.update(monotonic=96.99),
    lambda report: report.update(monotonic=101),
    lambda report: report.update(url='https://private.example'),
    lambda report: report['channels'].pop('depth'),
    lambda report: report['channels'].update(extra={}),
    lambda report: report['channels']['depth'].update(ageS=False),
    lambda report: report['channels']['color'].update(frameSeq=-1),
    lambda report: report['channels']['color'].update(data='private-pixels'),
])
def test_reader_rejects_wrong_process_stale_future_and_unexpected_fields(target, monkeypatch, mutation):
    report = write_report(target, monkeypatch)
    mutation(report)
    replace_report(target, report)
    before = target.stat().st_mtime_ns
    with pytest.raises(ValueError):
        health.read_preview_health(target, os.getpid(), now=100)
    assert target.stat().st_mtime_ns == before


@pytest.mark.parametrize('kwargs', [
    {'pid': True}, {'pid': 0}, {'pid': -1}, {'pid': 1.5},
    {'now': True}, {'now': float('nan')}, {'now': float('inf')},
    {'max_age': True}, {'max_age': 0}, {'max_age': float('nan')},
])
def test_reader_validates_caller_numbers(target, monkeypatch, kwargs):
    write_report(target, monkeypatch)
    arguments = {'pid': os.getpid(), 'now': 100, **kwargs}
    with pytest.raises(ValueError):
        health.read_preview_health(target, **arguments)


def test_reader_is_bounded_and_nonblocking_for_special_files(target, monkeypatch):
    write_report(target, monkeypatch)
    for raw in (b'', b'{broken', b'[]', b' ' * (health.MAX_REPORT_BYTES + 1)):
        target.write_bytes(raw)
        with pytest.raises(ValueError):
            health.read_preview_health(target, os.getpid(), now=100)
    target.unlink()
    os.mkfifo(target, mode=0o600)
    with pytest.raises(ValueError):
        health.read_preview_health(target, os.getpid(), now=100)


def test_file_symlink_and_hardlink_are_rejected_by_writer_and_reader(target, monkeypatch, tmp_path):
    write_report(target, monkeypatch)
    other = tmp_path / 'other.json'
    target.rename(other)
    target.symlink_to(other)
    for operation in (lambda: health.PreviewHealthWriter(target).write(channels()),
                      lambda: health.read_preview_health(target, os.getpid(), now=100)):
        with pytest.raises((ValueError, OSError)):
            operation()
    target.unlink()
    os.link(other, target)
    for operation in (lambda: health.PreviewHealthWriter(target).write(channels()),
                      lambda: health.read_preview_health(target, os.getpid(), now=100)):
        with pytest.raises(ValueError):
            operation()


@pytest.mark.parametrize('kind', ['directory-symlink', 'directory-mode', 'file-mode'])
def test_directory_components_and_private_modes_are_enforced(target, monkeypatch, tmp_path, kind):
    write_report(target, monkeypatch)
    if kind == 'directory-symlink':
        moved = tmp_path / 'moved'
        target.parent.rename(moved)
        target.parent.symlink_to(moved, target_is_directory=True)
    elif kind == 'directory-mode':
        target.parent.chmod(0o755)
    else:
        target.chmod(0o644)
    for operation in (lambda: health.PreviewHealthWriter(target).write(channels()),
                      lambda: health.read_preview_health(target, os.getpid(), now=100)):
        with pytest.raises((ValueError, OSError)):
            operation()


def test_relative_paths_rejected_and_missing_read_does_not_create_dirs(tmp_path):
    with pytest.raises(ValueError):
        health.PreviewHealthWriter(Path('relative/report.json')).write(channels())
    absent = tmp_path / 'absent' / 'report.json'
    with pytest.raises(OSError):
        health.read_preview_health(absent, os.getpid())
    assert not absent.parent.exists()
