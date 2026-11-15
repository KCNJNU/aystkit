"""Explicit foreground result directories must not change media upload truth."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from collector import archive_upload_folders as folders
from collector.archive_upload import ArchiveUploadError


ROBOT = '00000000-0000-0000-0000-000000000001'
NAME = '处理好的数据_20261112_081530'


def manifest(tmp_path, name='one', **values):
    path = tmp_path / name / 'archive.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    value = dict(schemaVersion=1, day='2026-11-04', robotId=ROBOT)
    value.update(values)
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


class Client:
    config = SimpleNamespace(robot_id=ROBOT)

    def __init__(self, fail_days=()):
        self.calls = []
        self.fail_days = set(fail_days)

    def ensure_day(self, day, sensors=()):
        self.calls.append((day, sensors))
        if day in self.fail_days:
            raise ArchiveUploadError('PRIVATE_API_RESPONSE_WITH_PASSWORD')
        return 'dataset-id'


def test_name_uses_beijing_timezone_across_the_utc_date_boundary():
    instant = datetime(2026, 11, 11, 16, 15, 30, tzinfo=timezone.utc)
    expected = '处理好的数据_20261112_001530'
    assert folders.processed_folder_name(instant) == expected
    assert folders.processed_folder_name(instant.astimezone(timezone(timedelta(hours=-7)))) == expected


@pytest.mark.parametrize('value', [datetime(2026, 11, 12), '2026-11-12', 0, True])
def test_injected_naive_or_non_datetime_instant_is_rejected(value):
    with pytest.raises(ValueError, match='ARCHIVE_PROCESSED_FOLDER_TIME_INVALID'):
        folders.processed_folder_name(value)


def test_distinct_datasets_share_one_name_and_repeated_days_are_created_once(tmp_path):
    first = manifest(tmp_path, 'one')
    second = manifest(tmp_path, 'two')
    third = manifest(tmp_path, 'three', day='2026-11-05')
    client = Client()
    result = folders.ensure_processed_folders(client, [first, second, third, first], NAME)
    assert client.calls == [('2026-11-04', (NAME,)), ('2026-11-05', (NAME,))]
    assert result == dict(name=NAME, createdDatasets=2, pendingDatasets=0, lastError=None)


def test_completed_ack_and_pruned_media_do_not_block_an_empty_result_directory(tmp_path):
    path = manifest(tmp_path, files=['already-pruned-video.mkv'])
    ack = path.parent / 'uploaded.json'
    ack.write_text(json.dumps(dict(state='complete', datasetId='untrusted-dataset-id')))
    state = path.parent / 'upload-state.json'
    state.write_bytes(b'unchanged-checkpoint')
    before = {item: item.read_bytes() for item in tmp_path.rglob('*') if item.is_file()}
    client = Client()
    result = folders.ensure_processed_folders(client, [path], NAME)
    assert client.calls == [('2026-11-04', (NAME,))]
    assert result['createdDatasets'] == 1
    assert not (path.parent / 'already-pruned-video.mkv').exists()
    assert {item: item.read_bytes() for item in tmp_path.rglob('*') if item.is_file()} == before


@pytest.mark.parametrize('changes', [
    {'schemaVersion': True}, {'schemaVersion': 2}, {'day': '2026-02-30'},
    {'day': '2026-1-04'}, {'day': '20261104'}, {'day': '../2026-11-04'},
    {'day': None}, {'robotId': 'another-robot'}, {'robotId': None},
    {'robotId': '{' + ROBOT + '}'},
    {'robotId': '00000000-0000-0000-0000-000000000002'},
])
def test_invalid_or_mismatched_metadata_never_selects_a_dataset(tmp_path, changes):
    path = manifest(tmp_path)
    value = json.loads(path.read_text()); value.update(changes)
    path.write_text(json.dumps(value))
    client = Client()
    result = folders.ensure_processed_folders(client, [path], NAME)
    assert client.calls == []
    assert result == dict(name=NAME, createdDatasets=0, pendingDatasets=1,
                          lastError=folders.MANIFEST_ERROR)


@pytest.mark.parametrize('identity', ['', None, 'not-a-uuid', '{' + ROBOT + '}'])
def test_invalid_client_identity_fails_closed_without_reading_manifests(identity):
    client = Client(); client.config = SimpleNamespace(robot_id=identity)
    class Unreadable:
        def __iter__(self):
            raise AssertionError('must fail identity before metadata reads')
    result = folders.ensure_processed_folders(client, Unreadable(), NAME)
    assert client.calls == []
    assert result['lastError'] == folders.IDENTITY_ERROR


@pytest.mark.parametrize('name', [None, '', NAME + '/child', '../' + NAME, NAME + '\n',
                                  NAME.replace('20261112', '20260230'),
                                  NAME.replace('081530', '251530'),
                                  NAME.replace('081530', '０８１５３０'), '处理好的数据_20261112'])
def test_only_a_valid_timestamp_folder_name_can_reach_the_api(name):
    client = Client()
    result = folders.ensure_processed_folders(client, [], name)
    assert client.calls == []
    assert result['lastError'] == folders.NAME_ERROR


@pytest.mark.parametrize('kind', ['symlink', 'parent_symlink', 'directory', 'fifo', 'oversized',
                                  'malformed', 'non_object', 'non_utf8'])
def test_only_bounded_regular_manifests_are_read(tmp_path, kind):
    original = manifest(tmp_path)
    path = original
    if kind == 'symlink':
        path = tmp_path / 'link.json'; path.symlink_to(original)
    elif kind == 'parent_symlink':
        link = tmp_path / 'link'; link.symlink_to(original.parent, target_is_directory=True)
        path = link / 'archive.json'
    elif kind == 'directory':
        path = tmp_path / 'directory'; path.mkdir()
    elif kind == 'fifo':
        path = tmp_path / 'pipe'; os.mkfifo(path)
    elif kind == 'oversized':
        with path.open('wb') as stream:
            stream.truncate(folders.MAX_MANIFEST_BYTES + 1)
    elif kind == 'malformed':
        path.write_text('{broken')
    elif kind == 'non_object':
        path.write_text('[]')
    else:
        path.write_bytes(b'\xff')
    client = Client()
    result = folders.ensure_processed_folders(client, [path], NAME)
    assert client.calls == []
    assert result['lastError'] == folders.MANIFEST_ERROR


def test_one_failed_dataset_does_not_block_other_days_or_leak_api_exception(tmp_path):
    first = manifest(tmp_path, 'one')
    second = manifest(tmp_path, 'two', day='2026-11-05')
    client = Client(fail_days=['2026-11-04'])
    result = folders.ensure_processed_folders(client, [first, second], NAME)
    assert client.calls == [('2026-11-04', (NAME,)), ('2026-11-05', (NAME,))]
    assert result == dict(name=NAME, createdDatasets=1, pendingDatasets=1,
                          lastError=folders.FOLDER_ERROR)
    assert 'PRIVATE' not in json.dumps(result)


def test_invalid_metadata_does_not_block_an_unrelated_valid_day(tmp_path):
    bad = manifest(tmp_path, 'bad'); bad.write_text('{broken')
    good = manifest(tmp_path, 'good')
    client = Client()
    result = folders.ensure_processed_folders(client, [bad, good], NAME)
    assert client.calls == [('2026-11-04', (NAME,))]
    assert result['createdDatasets'] == result['pendingDatasets'] == 1
    assert result['lastError'] == folders.MANIFEST_ERROR


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt, SystemExit])
def test_process_interrupts_and_upload_deadline_are_never_swallowed(tmp_path, interrupt):
    from collector.archive_upload_only import UploadDeadlineExpired
    path = manifest(tmp_path)
    for kind in (interrupt, UploadDeadlineExpired):
        client = Client()
        def interrupted(*_args, **_kwargs):
            raise kind()
        client.ensure_day = interrupted
        with pytest.raises(kind):
            folders.ensure_processed_folders(client, [path], NAME)


def test_an_empty_manifest_snapshot_does_not_create_a_dataset():
    client = Client()
    assert folders.ensure_processed_folders(client, [], NAME) == dict(
        name=NAME, createdDatasets=0, pendingDatasets=0, lastError=None)
    assert client.calls == []
