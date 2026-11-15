"""Offline regressions for source completeness, robot ACKs and foreground budgets."""
import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from collector import archive_upload_only as foreground
from collector.archive_pull import FilePuller, REMOTE_FILES, source_descriptor
from collector.archive_upload import ArchiveUploadClient, ArchiveUploadConfig, ArchiveUploadError
from tests.test_archive_upload import FakeApi, PASSWORD


@pytest.fixture
def archive(tmp_path):
    robot = str(uuid4())
    root = tmp_path / 'ipc'
    root.mkdir()
    home = tmp_path / 'source'
    values = dict(ARCHIVE_ROBOT_ID=robot, ARCHIVE_DBPROG_EMAIL='fixture@example.invalid',
        ARCHIVE_DBPROG_PASSWORD=PASSWORD, ARCHIVE_UPLOAD_BYTES_PER_SECOND='0',
        ARCHIVE_CONTROL_DIR=str(tmp_path / 'control'))
    config = foreground.upload_config(values)
    api = FakeApi()

    def popen(_command, **kwargs):
        return subprocess.Popen([sys.executable, '-B', '-c', REMOTE_FILES],
            env={'HOME': str(home), 'PATH': '/usr/bin:/bin'}, **kwargs)

    puller = FilePuller([], 'local-fixture-only', root, config, popen=popen,
        camera_only=False, training_only=False, min_free_bytes=0,
        max_spool_bytes=1024**2, bytes_per_second=0)
    value = SimpleNamespace(root=root, home=home, values=values, robot=robot,
        config=config, api=api, puller=puller)
    value.client = lambda configuration: ArchiveUploadClient(configuration, opener=api)
    return value


def write_segment(root, robot, identity='capture', *, source=False, ack=False, **changes):
    relative = '2026-11-04/原相机前/原相机前_16:00-16:01/' + identity
    directory = root / ('.local/share/robot-native-archive/days' if source else 'days') / relative
    directory.mkdir(parents=True)
    (directory / 'video.mkv').write_bytes(b'fixture-video-' + identity.encode())
    manifest = dict(schemaVersion=1, id=identity, day='2026-11-04', sensor='原相机前',
        label='原相机前_16:00-16:01', robotId=robot, startedAt='2026-11-04T08:00:00Z',
        endAt='2026-11-04T08:01:00Z', metadata=dict(scope='garage'), files=['video.mkv'])
    manifest.update(changes)
    raw = json.dumps(manifest).encode()
    (directory / 'archive.json').write_bytes(raw)
    if ack:
        (directory / 'uploaded.json').write_text(json.dumps(dict(schemaVersion=1,
            state='complete', manifestSha256=hashlib.sha256(raw).hexdigest())))
    descriptor = dict(path=relative, manifest=base64.b64encode(raw).decode(),
        sha256=hashlib.sha256(raw).hexdigest())
    return directory, descriptor


def source_upload(archive, **options):
    with patch.object(foreground.time, 'sleep', side_effect=lambda _: archive.api.finish()):
        return foreground.run_upload(archive.root, archive.values, timeout=3, sync_source=True,
            source_factory=lambda _: archive.puller, client_factory=archive.client, **options)


@pytest.mark.parametrize('corruption', ['{truncated-json', '{}', '[]', '{"metadata": []}'])
def test_invalid_source_manifests_cannot_disappear_into_empty_success(archive, corruption):
    source, _ = write_segment(archive.home, archive.robot, source=True)
    (source / 'archive.json').write_text(corruption)
    before = {path.name: path.read_bytes() for path in source.iterdir()}
    report, status = source_upload(archive)
    assert status == 3 and report['state'] == 'incomplete'
    assert report['remoteSync'] == 'incomplete'
    assert report['sourceInvalidSegments'] == report['invalidSegments'] == 1
    assert report['lastError'] == 'ARCHIVE_SOURCE_MANIFEST_INVALID'
    assert not archive.api.calls
    assert {path.name: path.read_bytes() for path in source.iterdir()} == before


def test_invalid_source_manifest_does_not_block_healthy_media(archive):
    broken, _ = write_segment(archive.home, archive.robot, 'broken', source=True)
    (broken / 'archive.json').write_text('{incomplete-json')
    healthy, descriptor = write_segment(archive.home, archive.robot, 'healthy', source=True)
    report, status = source_upload(archive)
    assert status == 3 and report['remoteSync'] == 'incomplete'
    assert report['sourceInvalidSegments'] == report['invalidSegments'] == 1
    assert report['completedSegments'] == report['uploadedThisRun'] == 1
    assert report['pendingSegments'] == 0
    local = archive.root / 'days' / descriptor['path']
    assert foreground.acknowledged(local / 'archive.json', archive.robot)
    assert (healthy / 'video.mkv').exists() and not (healthy / 'uploaded.json').exists()
    assert (broken / 'video.mkv').exists()


@pytest.mark.parametrize('ack', [False, True])
def test_same_path_source_manifest_change_is_a_collision_and_preserves_both_copies(archive, ack):
    source, descriptor = write_segment(archive.home, archive.robot, source=True)
    local, _ = write_segment(archive.root, archive.robot, ack=ack,
        endAt='2026-11-04T08:00:30Z')
    (local / 'video.mkv').write_bytes(b'previous video at same path')
    before = {path.name: path.read_bytes() for path in local.iterdir()}
    report, status = source_upload(archive)
    assert status == 3 and report['state'] == report['remoteSync'] == 'incomplete'
    assert report['completedSegments'] == report['alreadyCompletedSegments'] == 0
    assert report['pendingSegments'] == report['sourcePendingSegments'] == 1
    assert report['invalidSegments'] == 1
    assert report['lastError'] == 'ARCHIVE_SOURCE_MANIFEST_CHANGED'
    assert not archive.api.calls
    assert {path.name: path.read_bytes() for path in local.iterdir()} == before
    assert (source / 'video.mkv').read_bytes() != (local / 'video.mkv').read_bytes()
    assert hashlib.sha256((source / 'archive.json').read_bytes()).hexdigest() == descriptor['sha256']


def test_matching_source_manifest_can_reuse_a_durable_same_robot_ack(archive):
    source, _ = write_segment(archive.home, archive.robot, source=True)
    local, _ = write_segment(archive.root, archive.robot, ack=True)
    before = (local / 'uploaded.json').read_bytes()
    report, status = source_upload(archive)
    assert (status, report['state'], report['remoteSync']) == (0, 'complete', 'complete')
    assert report['alreadyCompletedSegments'] == 1 and report['uploadedThisRun'] == 0
    assert not archive.api.calls
    assert (local / 'uploaded.json').read_bytes() == before
    assert (source / 'video.mkv').exists()


def test_snapshot_return_type_stays_a_list_and_retains_invalid_count(archive):
    broken, _ = write_segment(archive.home, archive.robot, source=True)
    (broken / 'archive.json').write_text('{bad-json')
    snapshot = archive.puller.sealed_snapshot()
    assert isinstance(snapshot, list) and snapshot == []
    assert archive.puller.snapshot_invalid_segments == 1


@pytest.mark.parametrize('damage', ['path-only', 'checksum', 'path', 'robot', 'files'])
def test_snapshot_descriptor_is_verified_before_any_local_ack_can_short_circuit(archive, damage):
    local, descriptor = write_segment(archive.root, archive.robot, ack=True)
    if damage == 'path-only':
        descriptor = dict(path=descriptor['path'])
    elif damage == 'checksum':
        descriptor['sha256'] = '0' * 64
    elif damage == 'path':
        descriptor['path'] = '2026-11-04/原相机前/../capture'
    else:
        value = json.loads(base64.b64decode(descriptor['manifest']))
        if damage == 'robot':
            value['robotId'] = str(uuid4())
        else:
            value['files'] = ['../outside']
        raw = json.dumps(value).encode()
        descriptor.update(manifest=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    source = SimpleNamespace(sealed_snapshot=lambda: [descriptor], _pull=Mock())
    report, status = foreground.run_upload(archive.root, archive.values, sync_source=True,
        source_factory=lambda _: source, client_factory=archive.client)
    assert status == 3 and report['state'] == report['remoteSync'] == 'incomplete'
    assert report['sourceInvalidSegments'] == 1
    assert report['lastError'] == ('ARCHIVE_ROBOT_IDENTITY_MISMATCH'
                                   if damage == 'robot' else 'ARCHIVE_SOURCE_SNAPSHOT_INVALID')
    source._pull.assert_not_called()
    assert not archive.api.calls
    assert (local / 'uploaded.json').exists()


@pytest.mark.parametrize('dry_run', [False, True])
def test_foreign_robot_ack_cannot_be_counted_as_current_robot_completion(archive, dry_run):
    local, _ = write_segment(archive.root, str(uuid4()), ack=True)
    before = (local / 'uploaded.json').read_bytes()
    with patch.object(foreground, 'upload_config', side_effect=AssertionError('credentials read')):
        report, status = foreground.run_upload(archive.root,
            {'ARCHIVE_ROBOT_ID': archive.robot}, dry_run=dry_run,
            client_factory=lambda _: pytest.fail('foreign robot created network client'))
    assert status == 3 and report['state'] == 'incomplete'
    assert report['completedSegments'] == report['alreadyCompletedSegments'] == 0
    assert report['invalidSegments'] == 1 and report['pendingSegments'] == 1
    assert report['lastError'] == 'ARCHIVE_ROBOT_IDENTITY_MISMATCH'
    assert (local / 'uploaded.json').read_bytes() == before


def test_unscoped_dry_run_lists_ack_but_does_not_claim_current_robot_completion(archive):
    write_segment(archive.root, archive.robot, ack=True)
    with patch.object(foreground, 'upload_config', side_effect=AssertionError('credentials read')):
        report, status = foreground.run_upload(archive.root, {}, dry_run=True)
    assert (status, report['state'], report['selectedSegments']) == (0, 'dry_run', 1)
    assert report['completedSegments'] == report['alreadyCompletedSegments'] == 0
    assert report['pendingSegments'] == 1


def test_non_dry_run_uses_private_config_identity_when_public_env_id_absent(archive, tmp_path):
    local, _ = write_segment(archive.root, archive.robot, ack=True)
    upload = tmp_path / 'fixture-private.json'
    upload.write_text(json.dumps(dict(email='fixture@example.invalid', password=PASSWORD, robotId=archive.robot)))
    upload.chmod(0o600)
    report, status = foreground.run_upload(archive.root,
        {'ARCHIVE_UPLOAD_CONFIG': str(upload), 'ARCHIVE_CONTROL_DIR': archive.values['ARCHIVE_CONTROL_DIR']},
        client_factory=lambda _: pytest.fail('unneeded API client'))
    assert (status, report['completedSegments'], report['alreadyCompletedSegments']) == (0, 1, 1)
    assert (local / 'uploaded.json').exists()


def test_public_profile_and_private_upload_robot_must_agree(archive, tmp_path):
    write_segment(archive.root, archive.robot, ack=True)
    upload = tmp_path / 'fixture-private.json'
    upload.write_text(json.dumps(dict(email='fixture@example.invalid', password=PASSWORD, robotId=str(uuid4()))))
    upload.chmod(0o600)
    report, status = foreground.run_upload(archive.root,
        {'ARCHIVE_UPLOAD_CONFIG': str(upload), 'ARCHIVE_CONTROL_DIR': archive.values['ARCHIVE_CONTROL_DIR']},
        expected_robot_id=archive.robot, client_factory=lambda _: pytest.fail('mismatched API client'))
    assert status == 3 and report['lastError'] == 'ARCHIVE_ROBOT_IDENTITY_MISMATCH'
    assert report['completedSegments'] == 0


def test_cli_passes_only_public_profile_identity_to_the_dry_run(archive):
    with patch.object(foreground, 'load_environment', return_value={}), \
            patch('collector.robot_profile.validate_current_profile', return_value={'warehouseId': archive.robot}), \
            patch.object(foreground, 'run_upload', return_value=({'state': 'dry_run'}, 0)) as run, \
            redirect_stdout(io.StringIO()):
        assert foreground.main(['--dry-run', '--archive-root', str(archive.root)]) == 0
    assert run.call_args.kwargs['expected_robot_id'] == archive.robot


def budget_client(archive, calls):
    def factory(config):
        calls.append('client')
        return SimpleNamespace(config=config, api=SimpleNamespace(timeout=120),
            ensure_day=lambda *_args: calls.append('folder-api'))
    return factory


def test_optional_proof_timeout_never_starts_a_later_folder_network_call(archive):
    write_segment(archive.root, archive.robot, ack=True,
        metadata=dict(scope='garage', captureSessionId='a' * 32))
    calls = []
    started = time.monotonic()
    with patch.object(foreground, '_upload_session_ends', side_effect=lambda *_: time.sleep(.3)):
        report, status = foreground.run_upload(archive.root, archive.values, timeout=.03,
            client_factory=budget_client(archive, calls), processed_folder='处理好的数据_20261112_130000')
    assert time.monotonic() - started < .2
    assert (status, report['state'], report['completedSegments']) == (0, 'complete', 1)
    assert report['sessionEnds']['lastError'] == 'ARCHIVE_SESSION_END_TIME_BUDGET_EXPIRED'
    assert report['processedFolder']['lastError'] == 'ARCHIVE_PROCESSED_FOLDER_TIME_BUDGET_EXPIRED'
    assert calls == ['client']


def test_folder_network_call_has_its_own_remaining_foreground_budget(archive):
    write_segment(archive.root, archive.robot, ack=True)
    calls = []
    def factory(config):
        calls.append('client')
        def slow_folder(*_args):
            calls.append('folder-api')
            time.sleep(.3)
        return SimpleNamespace(config=config, api=SimpleNamespace(timeout=120), ensure_day=slow_folder)
    started = time.monotonic()
    report, status = foreground.run_upload(archive.root, archive.values, timeout=.03,
        client_factory=factory, processed_folder='处理好的数据_20261112_130000')
    assert time.monotonic() - started < .2
    assert (status, report['state'], report['completedSegments']) == (0, 'complete', 1)
    assert report['processedFolder']['lastError'] == 'ARCHIVE_PROCESSED_FOLDER_TIME_BUDGET_EXPIRED'
    assert calls == ['client', 'folder-api']
