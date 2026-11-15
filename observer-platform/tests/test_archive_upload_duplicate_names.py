"""The platform's same-content name constraint must preserve data and progress."""
import json
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest

from collector import archive_upload_only as foreground
from collector.archive_upload import ArchiveUploadClient, ArchiveUploadConfig, ArchiveUploadError
from tests.test_archive_upload import FakeApi, PASSWORD, ROBOT


CONFLICT = 'ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT'


def segment(root, name='one', files=None):
    files = {'a.bin': b'same native content', 'b.bin': b'same native content'} if files is None else files
    directory = root / 'days' / '2026-11-04' / '雷达' / '雷达_16:01-16:02' / name
    directory.mkdir(parents=True)
    for filename, content in files.items():
        path = directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (directory / 'archive.json').write_text(json.dumps(dict(schemaVersion=1, id=name,
        day='2026-11-04', sensor='雷达', label='雷达_16:01-16:02', robotId=ROBOT,
        startedAt='2026-11-04T08:01:00Z', endAt='2026-11-04T08:02:00Z', metadata={},
        files=list(files))), encoding='utf-8')
    return directory


def client(api):
    config = ArchiveUploadConfig('archive@example.invalid', PASSWORD,
                                robot_id=ROBOT, upload_bytes_per_second=0)
    return ArchiveUploadClient(config, opener=api)


class DisconnectBeforeSecondFile(FakeApi):
    disconnected = False

    def open(self, request, timeout):
        if (request.get_method() == 'POST' and urlsplit(request.full_url).path == '/api/v1/uploads'
                and json.loads(request.data)['originalName'] == 'b.bin' and not self.disconnected):
            self.disconnected = True
            raise URLError('fixture disconnect, never a production request')
        return super().open(request, timeout)


def prepare_interrupted_capture(root, *, third=False):
    files = {'a.bin': b'same native content', 'b.bin': b'same native content'}
    if third:
        files['c.bin'] = b'other healthy file in the same segment'
    directory = segment(root, files=files)
    api = DisconnectBeforeSecondFile()
    with pytest.raises(ArchiveUploadError, match='ARCHIVE_API_UNAVAILABLE'):
        client(api).upload_segment(directory)
    api.finish()
    return directory, api


def test_same_content_different_names_after_disconnect_reports_exact_constraint_and_keeps_bytes(tmp_path):
    directory, api = prepare_interrupted_capture(tmp_path, third=True)
    originals = {name: (directory / name).read_bytes() for name in ('a.bin', 'b.bin', 'c.bin')}
    for _attempt in range(2):
        with pytest.raises(ArchiveUploadError) as failure:
            client(api).upload_segment(directory)
        assert failure.value.code == CONFLICT
        api.finish()
    assert {row['originalName'] for row in api.files.values()} == {'a.bin', 'c.bin'}
    assert len(api.uploads) == 2
    assert not (directory / 'uploaded.json').exists()
    assert {name: (directory / name).read_bytes() for name in originals} == originals
    # The deterministic naming conflict never uploads or creates a new b session.
    assert not any(method == 'POST' and path == '/v1/uploads'
                   and json.loads(body)['originalName'] == 'b.bin'
                   for method, path, _headers, body in api.calls)


def test_a_healthy_later_segment_still_uploads_after_another_name_conflict(tmp_path, monkeypatch):
    conflicted, api = prepare_interrupted_capture(tmp_path)
    healthy = segment(tmp_path, 'two', files={'c.bin': b'another segment remains uploadable'})
    monkeypatch.setattr(foreground.time, 'sleep', lambda _seconds: api.finish())
    values = dict(ARCHIVE_DBPROG_EMAIL='archive@example.invalid', ARCHIVE_DBPROG_PASSWORD=PASSWORD,
                  ARCHIVE_ROBOT_ID=ROBOT, ARCHIVE_UPLOAD_BYTES_PER_SECOND='0',
                  ARCHIVE_CONTROL_DIR=str(tmp_path / 'control'))
    report, status = foreground.run_upload(tmp_path, values, timeout=3,
        client_factory=lambda config: ArchiveUploadClient(config, opener=api))
    assert status == 3 and report['state'] == 'incomplete'
    assert report['lastError'] == CONFLICT
    assert report['completedSegments'] == 1 and report['pendingSegments'] == 1
    assert foreground.acknowledged(healthy / 'archive.json')
    assert not (conflicted / 'uploaded.json').exists()
    assert (conflicted / 'a.bin').is_file() and (conflicted / 'b.bin').is_file()


def seeded_file(tmp_path):
    directory = segment(tmp_path, files={'a.bin': b'one native content'})
    api = FakeApi()
    assert client(api).upload_segment(directory)['state'] == 'pending'
    api.finish()
    # Simulate lost local progress before the final ACK: server duplicate lookup
    # must prove the exact filename and identity without retransmitting media.
    (directory / 'upload-state.json').unlink()
    api.calls.clear()
    return directory, api


def test_same_filename_checksum_size_robot_and_path_can_be_deduplicated(tmp_path):
    directory, api = seeded_file(tmp_path)
    result = client(api).upload_segment(directory)
    assert result['state'] == 'complete'
    assert len(api.uploads) == 1
    assert not any(method == 'PUT' or method == 'POST' and path == '/v1/uploads'
                   for method, path, *_other in api.calls)
    assert (directory / 'uploaded.json').is_file()
    assert (directory / 'a.bin').read_bytes() == b'one native content'


@pytest.mark.parametrize('field,value', [
    ('checksum', '0' * 64), ('fileSize', 999),
    ('robotId', '00000000-0000-0000-0000-000000000002'),
    ('datasetId', '00000000-0000-0000-0000-000000000003'), ('folderPath', 'other/path'),
])
def test_same_filename_does_not_bypass_full_remote_identity_checks(tmp_path, field, value):
    directory, api = seeded_file(tmp_path)
    stored = next(iter(api.files.values()))
    if field in ('checksum', 'datasetId', 'folderPath'):
        # Force the candidate list to be stale; the ID detail request is the
        # authoritative check and must still reject a changed target.
        original_open = api.open
        original = dict(stored)
        def stale_page(request, timeout):
            from tests.test_archive_upload import Reply
            if urlsplit(request.full_url).path == '/api/v1/files':
                return Reply({'items': [original]})
            return original_open(request, timeout)
        api.open = stale_page
    stored[field] = value
    with pytest.raises(ArchiveUploadError) as failure:
        client(api).upload_segment(directory)
    assert failure.value.code == 'ARCHIVE_REMOTE_FILE_MISMATCH'
    assert not (directory / 'uploaded.json').exists()
    assert (directory / 'a.bin').is_file()


def test_an_http_409_with_another_filename_is_not_accepted_as_upload_success(tmp_path):
    class OtherNameConflict(FakeApi):
        def open(self, request, timeout):
            result = super().open(request, timeout)
            if result.status == 409 and urlsplit(request.full_url).path == '/api/v1/uploads':
                identity = json.loads(result.data)['robotFileId']
                self.files[identity]['originalName'] = 'another-name.bin'
            return result
    directory = segment(tmp_path, files={'requested.bin': b'original bytes retained'})
    api = OtherNameConflict(); api.conflict_on_create = True
    with pytest.raises(ArchiveUploadError) as failure:
        client(api).upload_segment(directory)
    assert failure.value.code == CONFLICT
    assert not (directory / 'uploaded.json').exists()
    assert (directory / 'requested.bin').read_bytes() == b'original bytes retained'
    assert not any(method == 'PUT' for method, *_other in api.calls)


def test_completed_session_with_another_filename_is_not_acknowledged(tmp_path):
    directory = segment(tmp_path, files={'requested.bin': b'original bytes retained'})
    api = FakeApi()
    assert client(api).upload_segment(directory)['state'] == 'pending'
    api.finish()
    next(iter(api.files.values()))['originalName'] = 'another-name.bin'
    with pytest.raises(ArchiveUploadError) as failure:
        client(api).upload_segment(directory)
    assert failure.value.code == CONFLICT
    assert not (directory / 'uploaded.json').exists()
    assert len(api.uploads) == 1
