"""Optional post-video time indexes; fake warehouse only, no live capture."""
from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from collector import archive_sync_index as sync
from collector import archive_upload_only as foreground
from collector.archive_upload import ArchiveUploadClient, ArchiveUploadError
from tests.test_archive_sync_index import source, SESSION
from tests.test_archive_upload import FakeApi, PASSWORD, ROBOT


@pytest.fixture
def batch(tmp_path, monkeypatch):
    rgb, _ = source(tmp_path, robot=ROBOT)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度', robot=ROBOT)
    control = tmp_path / 'control'
    (control / 'session-ends').mkdir(parents=True)
    marker = control / 'session-ends' / (SESSION + '.json')
    marker.write_text(json.dumps(dict(schemaVersion=1, sessionId=SESSION, robotId=ROBOT, state='sealed')))
    values = dict(ARCHIVE_ROBOT_ID=ROBOT, ARCHIVE_DBPROG_EMAIL='fixture@example.invalid',
        ARCHIVE_DBPROG_PASSWORD=PASSWORD, ARCHIVE_CONTROL_DIR=str(control))
    api, events, factories = FakeApi(), [], []

    def factory(config):
        factories.append(config)
        return ArchiveUploadClient(config, opener=api)

    def session_ends(*args, **kwargs):
        events.append('session_ends')

    monkeypatch.setattr(foreground, '_best_effort_session_ends', session_ends)
    monkeypatch.setattr(foreground.time, 'sleep', lambda _: api.finish())
    return SimpleNamespace(root=tmp_path, rgb=rgb, depth=depth, paths=[rgb, depth],
        marker=marker, values=values, api=api, events=events, factories=factories, factory=factory)


def read_sources(batch):
    return {path: path.read_bytes() for manifest in batch.paths for path in manifest.parent.iterdir()
            if path.name in ('archive.json', 'video.mkv', 'index.jsonl', 'frames.index.jsonl')}


def test_local_uploads_videos_then_closed_session_index_and_reuses_ack(batch, monkeypatch):
    before = read_sources(batch)
    original_build = sync.build_sync_indexes
    calls = []

    def build(paths, root, robot_id, **kwargs):
        assert batch.events == ['session_ends']
        assert all(foreground.acknowledged(path, ROBOT) for path in batch.paths)
        calls.append(list(paths))
        return original_build(paths, root, robot_id, **kwargs)

    monkeypatch.setattr(sync, 'build_sync_indexes', build)
    report, code = foreground.run_upload(batch.root, batch.values, timeout=2, client_factory=batch.factory)
    assert (code, report['state'], report['selectedSegments'], report['completedSegments']) == (0, 'complete', 2, 2)
    assert report['lastError'] is None
    assert report['timeSync']['generated'] == 1
    assert (report['timeSync']['selected'], report['timeSync']['completed'], report['timeSync']['pending']) == (1, 1, 0)
    assert report['timeSync']['state'] == 'complete'
    assert len(batch.api.uploads) == 4  # Two original videos and only two index files.
    assert {row['originalName'] for row in batch.api.uploads.values()} == {
        'video.mkv', 'sync-index.jsonl', 'sync-summary.json'}
    assert read_sources(batch) == before
    assert len(foreground.sealed_manifests(batch.root)) == 2

    batch.events.clear()
    api_calls, factories = len(batch.api.calls), len(batch.factories)
    again, code = foreground.run_upload(batch.root, batch.values, timeout=2, client_factory=batch.factory)
    assert code == 0 and again['timeSync']['reused'] == 1
    assert again['timeSync']['completed'] == 1
    assert len(batch.api.calls) == api_calls and len(batch.factories) == factories
    assert len(batch.api.uploads) == 4 and len(calls) == 2


def test_source_path_reuses_existing_foreground_client_and_lock(batch, monkeypatch):
    locks = []
    real_lock = foreground.upload_lock

    @contextmanager
    def upload_lock(root):
        locks.append(root)
        with real_lock(root):
            yield

    monkeypatch.setattr(foreground, 'upload_lock', upload_lock)
    report, code = foreground.run_upload(batch.root, batch.values, timeout=2,
        sync_source=True, client_factory=batch.factory,
        source_factory=lambda _: SimpleNamespace(sealed_snapshot=lambda: []))
    assert (code, report['state'], report['remoteSync']) == (0, 'complete', 'complete')
    assert report['timeSync']['completed'] == 1
    assert len(batch.factories) == 1 and len(locks) == 1
    assert len(batch.api.uploads) == 4


@pytest.mark.parametrize('proof', ['missing', 'pending', 'wrong_robot', 'wrong_token', 'malformed', 'symlink'])
def test_without_matching_sealed_closure_does_not_build_or_create_sync_directories(batch, monkeypatch, proof):
    if proof == 'missing':
        batch.marker.unlink()
    elif proof == 'malformed':
        batch.marker.write_text('{broken')
    elif proof == 'symlink':
        outside = batch.root / 'outside-marker.json'
        outside.write_bytes(batch.marker.read_bytes())
        batch.marker.unlink()
        batch.marker.symlink_to(outside)
    else:
        value = json.loads(batch.marker.read_text())
        value.update({'state': 'pending'} if proof == 'pending' else
                     {'robotId': 'other-robot'} if proof == 'wrong_robot' else {'sessionId': 'c' * 32})
        batch.marker.write_text(json.dumps(value))
    monkeypatch.setattr(sync, 'build_sync_indexes', lambda *args, **kwargs: pytest.fail('built an active/unproven session'))
    report = dict(state='complete', lastError=None)
    with monkeypatch.context() as context:
        context.setattr(foreground, 'upload_config', lambda *_: pytest.fail('read optional credentials'))
        foreground._best_effort_time_sync(batch.root, batch.values, batch.paths, report,
            lambda *_: pytest.fail('created network client'), expected_robot_id=ROBOT)
    assert report['state'] == 'complete' and report['lastError'] is None
    assert report['timeSync']['waitingForSessionEnd'] == 2
    assert report['timeSync']['skips']['waitingForSessionEnd'] == 2
    assert not (batch.root / 'sync-indexes').exists()
    assert not (batch.root / '.upload-only.lock').exists()
    assert '等待本轮采集确认结束' in foreground.readable_summary(report)


@pytest.mark.parametrize('field', ['clockDomain', 'captureSessionId', 'timestampSemantics'])
def test_missing_timing_proof_does_not_call_builder_or_read_credentials(batch, monkeypatch, field):
    for path in batch.paths:
        manifest = json.loads(path.read_text())
        manifest['metadata'].pop(field)
        path.write_text(json.dumps(manifest))
    monkeypatch.setattr(sync, 'build_sync_indexes', lambda *args, **kwargs: pytest.fail('unverified timing was selected'))
    monkeypatch.setattr(foreground, 'upload_config', lambda *_: pytest.fail('read credentials'))
    report = {}
    foreground._best_effort_time_sync(batch.root, batch.values, batch.paths, report,
        lambda *_: pytest.fail('network'), expected_robot_id=ROBOT)
    assert report['timeSync']['generated'] == 0
    assert sum(report['timeSync']['skips'].values()) == 2
    assert not (batch.root / 'sync-indexes').exists()


def test_training_and_camera_only_policy_permit_the_two_file_index(batch, monkeypatch):
    monkeypatch.setenv('ARCHIVE_TRAINING_ONLY', '1')
    batch.values.update(ARCHIVE_TRAINING_ONLY='1', ARCHIVE_CAMERA_ONLY='1')
    report, code = foreground.run_upload(batch.root, batch.values, timeout=2, client_factory=batch.factory)
    assert code == 0 and report['timeSync']['completed'] == 1
    ready = next((batch.root / 'sync-indexes').rglob('archive.json'))
    client = batch.factory(foreground.upload_config(batch.values))
    assert client._manifest(ready.parent)[1]['files'] == ['sync-index.jsonl', 'sync-summary.json']


def test_optional_build_errors_do_not_replace_video_result_or_diagnostic(batch, monkeypatch):
    def failed(*args, **kwargs):
        raise ValueError('PRIVATE_BAD_INDEX_DIAGNOSTIC')

    monkeypatch.setattr(sync, 'build_sync_indexes', failed)
    report = dict(state='complete', selectedSegments=2, completedSegments=2,
        pendingSegments=0, uploadedThisRun=2, lastError='ARCHIVE_EXISTING_MEDIA_DIAGNOSTIC')
    before = deepcopy(report)
    foreground._best_effort_time_sync(batch.root, batch.values, batch.paths, report,
        batch.factory, expected_robot_id=ROBOT)
    assert {key: report[key] for key in before} == before
    assert report['timeSync']['lastError'] == 'ARCHIVE_TIME_SYNC_FAILED'
    assert 'PRIVATE_BAD_INDEX_DIAGNOSTIC' not in json.dumps(report)
    assert '视频完成不代表同步索引完成' in foreground.readable_summary(report)


def test_sync_upload_error_retries_later_without_resending_media(batch):
    fail = [True]

    def factory(config):
        client = batch.factory(config)
        original = client.upload_segment

        def upload(path):
            if 'sync-indexes' in Path(path).parts and fail[0]:
                fail[0] = False
                raise ArchiveUploadError('ARCHIVE_API_UNAVAILABLE')
            return original(path)

        client.upload_segment = upload
        return client

    report, code = foreground.run_upload(batch.root, batch.values, timeout=2, client_factory=factory)
    assert code == 0 and report['state'] == 'complete' and report['lastError'] is None
    assert report['timeSync']['pending'] == 1
    assert report['timeSync']['lastError'] == 'ARCHIVE_API_UNAVAILABLE'
    assert len(batch.api.uploads) == 2
    again, code = foreground.run_upload(batch.root, batch.values, timeout=2, client_factory=factory)
    assert code == 0 and again['timeSync']['reused'] == 1
    assert again['timeSync']['completed'] == 1 and again['timeSync']['lastError'] is None
    assert len(batch.api.uploads) == 4
    assert len([row for row in batch.api.uploads.values() if row['originalName'] == 'video.mkv']) == 2


def test_dry_run_and_rejected_status_flag_never_build_or_write(batch, monkeypatch):
    before = {str(path): path.read_bytes() for path in batch.root.rglob('*') if path.is_file()}
    monkeypatch.setattr(sync, 'build_sync_indexes', lambda *args, **kwargs: pytest.fail('readonly built indexes'))
    monkeypatch.setattr(foreground, 'upload_config', lambda *_: pytest.fail('readonly read credentials'))
    report, code = foreground.run_upload(batch.root, batch.values, dry_run=True, sync_source=True,
        client_factory=lambda *_: pytest.fail('readonly network'))
    assert code == 0 and report['state'] == 'dry_run' and 'timeSync' not in report
    # This existing module exposes --dry-run; unsupported --status is rejected
    # by argparse before reading configuration or invoking any upload hook.
    with pytest.raises(SystemExit):
        foreground.main(['--status'])
    assert {str(path): path.read_bytes() for path in batch.root.rglob('*') if path.is_file()} == before


def test_time_sync_deadline_is_independent_and_preserves_completed_videos(batch, monkeypatch):
    # Start with durable raw-media ACKs so only the optional step spends time.
    report, code = foreground.run_upload(batch.root, batch.values, timeout=2, client_factory=batch.factory)
    assert code == 0
    # The fixture replaced sleep globally; Event.wait gives a real bounded
    # blocking call interrupted by SIGALRM without needing network or devices.
    import threading
    waiter = threading.Event()

    def blocked(*args, **kwargs):
        waiter.wait(1)
        pytest.fail('optional deadline did not interrupt builder')

    monkeypatch.setattr(sync, 'build_sync_indexes', blocked)
    started = time.monotonic()
    again, code = foreground.run_upload(batch.root, batch.values, timeout=.03, client_factory=batch.factory)
    assert time.monotonic() - started < .5
    assert code == 0 and again['state'] == 'complete'
    assert again['lastError'] is None and again['completedSegments'] == 2
    assert again['timeSync']['lastError'] == 'ARCHIVE_TIME_SYNC_TIME_BUDGET_EXPIRED'


def test_optional_total_budget_caps_at_sixty_seconds(batch, monkeypatch):
    budgets = []

    @contextmanager
    def budget(deadline):
        budgets.append(deadline)
        yield

    monkeypatch.setattr(foreground, '_optional_time_budget', budget)
    monkeypatch.setattr(sync, 'build_sync_indexes', lambda *args, **kwargs: dict(
        generated=0, reused=0, readyManifestPaths=[], localManifestPaths=[],
        skipped={'missing_rgb_or_depth': 1}, errors=[]))
    started = time.monotonic()
    report = {}
    foreground._best_effort_time_sync(batch.root, batch.values, batch.paths, report,
        batch.factory, expected_robot_id=ROBOT, deadline=started + 600)
    assert len(budgets) == 1 and 59.9 <= budgets[0] - started <= 60.1
    assert not batch.factories and not batch.api.calls


def test_no_manifests_or_only_non_camera_data_has_no_optional_io(batch, monkeypatch):
    monkeypatch.setattr(sync, 'build_sync_indexes', lambda *args, **kwargs: pytest.fail('no source build'))
    monkeypatch.setattr(foreground, 'upload_config', lambda *_: pytest.fail('no source credentials'))
    report = {}
    foreground._best_effort_time_sync(batch.root, {}, [], report, lambda *_: pytest.fail('network'))
    assert report['timeSync']['state'] == 'not_required'
    for path in batch.paths:
        value = json.loads(path.read_text())
        value['metadata']['source'] = 'unrelated'
        path.write_text(json.dumps(value))
    foreground._best_effort_time_sync(batch.root, {}, batch.paths, report, lambda *_: pytest.fail('network'))
    assert report['timeSync']['state'] == 'not_required'
