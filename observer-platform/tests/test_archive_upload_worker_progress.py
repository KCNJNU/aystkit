"""Automatic retry must isolate bad segments and back off global outages."""
import json

import pytest

from collector import archive_main, archive_retention
from collector.archive_upload import ArchiveUploadClient, ArchiveUploadConfig, ArchiveUploadError


class OnePass:
    def __init__(self):
        self.waits = []

    def is_set(self):
        return bool(self.waits)

    def wait(self, seconds):
        self.waits.append(seconds)
        return True


def segment(root, name):
    path = root / 'days/2026-10-27/雷达MID360' / name
    path.mkdir(parents=True)
    (path / 'record.db3.zst').write_bytes(b'original training data')
    (path / 'archive.json').write_text(json.dumps(dict(schemaVersion=1, id=name,
        day='2026-10-27', sensor='雷达MID360', label=name,
        robotId='365c77c6-0029-4406-a504-739014d0a672',
        startedAt='2026-10-27T06:50:00Z', endAt='2026-10-27T07:50:00Z',
        files=['record.db3.zst'])))
    return path


def run_pass(root, client, monkeypatch, *, gate=None):
    stopping = OnePass()
    pruned = []
    monkeypatch.setattr(archive_main, 'ArchiveUploadClient', lambda _: client)
    monkeypatch.setattr(archive_retention, 'prune_confirmed_copy',
        lambda path, _client, **_kwargs: pruned.append(path) and False)
    worker = archive_main.UploadWorker(root, None, stopping, gate=gate)
    worker.run()
    return worker.snapshot(), stopping.waits, pruned


def test_malformed_first_manifest_does_not_starve_a_valid_later_recording(tmp_path, monkeypatch):
    bad = segment(tmp_path, 'a-bad')
    good = segment(tmp_path, 'z-good')
    (bad / 'archive.json').write_text('{broken-json')
    real_client = ArchiveUploadClient(ArchiveUploadConfig('upload@example.invalid', 'fixture'))
    calls = []

    class Client:
        def upload_segment(self, path):
            calls.append(path)
            real_client._manifest(path)  # Exercise the real local manifest parser.
            return {'state': 'complete'}

    report, waits, pruned = run_pass(tmp_path, Client(), monkeypatch)

    assert calls == [bad, good]
    assert (report['pendingSegments'], report['completedSegments']) == (1, 1)
    assert report['lastError'] == 'ARCHIVE_MANIFEST_INVALID'
    assert report['lastSuccessAt']
    assert waits == [30]
    assert pruned == [bad, good]  # Local failure still permits independent cleanup.
    assert (bad / 'record.db3.zst').read_bytes() == b'original training data'
    assert not (bad / 'uploaded.json').exists()


@pytest.mark.parametrize('code', [
    'ARCHIVE_MANIFEST_INVALID', 'ARCHIVE_STATE_INVALID', 'ARCHIVE_MANIFEST_CHANGED',
    'ARCHIVE_FILE_CHANGED', 'ARCHIVE_FILE_SIZE_INVALID', 'ARCHIVE_UPLOAD_LOCK_INVALID',
    'ARCHIVE_UPLOAD_STATE_MISMATCH', 'ARCHIVE_PENDING_SEGMENT_INVALID',
    'ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT',
    'ARCHIVE_ROBOT_IDENTITY_MISMATCH',
])
def test_local_segment_error_does_not_block_good_files_or_hide_the_error(tmp_path, monkeypatch, code):
    bad, good = segment(tmp_path, 'a-bad'), segment(tmp_path, 'z-good')
    calls = []

    class Client:
        def upload_segment(self, path):
            calls.append(path)
            if path == bad:
                raise ArchiveUploadError(code)
            return {'state': 'complete'}

    report, waits, pruned = run_pass(tmp_path, Client(), monkeypatch)

    assert calls == [bad, good]
    assert report['lastError'] == code  # The later successful segment must not clear it.
    assert (report['pendingSegments'], report['completedSegments']) == (1, 1)
    assert waits == [30]
    assert pruned == [bad, good]
    assert (bad / 'record.db3.zst').read_bytes() == b'original training data'


@pytest.mark.parametrize('code,status', [
    ('ARCHIVE_API_UNAVAILABLE', None), ('ARCHIVE_HTTP_401', 401),
    ('ARCHIVE_HTTP_403', 403), ('ARCHIVE_HTTP_429', 429),
    ('ARCHIVE_HTTP_500', 500), ('ARCHIVE_HTTP_503', 503),
    ('ARCHIVE_API_RESPONSE_INVALID', None), ('ARCHIVE_LOGIN_RESPONSE_INVALID', None),
    ('ARCHIVE_MANIFEST_INVALID', 503),
])
def test_global_api_error_stops_the_pass_and_backs_off_without_more_requests(
        tmp_path, monkeypatch, code, status):
    first, later = segment(tmp_path, 'a-first'), segment(tmp_path, 'z-later')
    calls = []

    class Client:
        def upload_segment(self, path):
            calls.append(path)
            raise ArchiveUploadError(code, status)

    report, waits, pruned = run_pass(tmp_path, Client(), monkeypatch)

    assert calls == [first]
    assert report['lastError'] == code
    assert (report['pendingSegments'], report['completedSegments']) == (2, 0)
    assert waits == [30]
    assert not pruned  # A known unavailable API must not be hammered by retention.
    assert all((path / 'record.db3.zst').exists() for path in (first, later))


def test_gate_directory_request_failure_uses_one_global_backoff(tmp_path, monkeypatch):
    segment(tmp_path, 'a-first')
    segment(tmp_path, 'z-later')
    calls = []

    class Gate:
        def enabled(self):
            return True

    class Client:
        def ensure_day(self, *_args):
            calls.append('ensure-day')
            raise ArchiveUploadError('ARCHIVE_API_UNAVAILABLE')

        def upload_segment(self, _path):
            pytest.fail('known-offline warehouse was contacted again')

    report, waits, pruned = run_pass(tmp_path, Client(), monkeypatch, gate=Gate())
    assert calls == ['ensure-day']
    assert report['lastError'] == 'ARCHIVE_API_UNAVAILABLE'
    assert waits == [30]
    assert not pruned
