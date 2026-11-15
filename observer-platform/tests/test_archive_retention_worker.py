"""Independent confirmed-copy cleanup must keep making safe progress."""
import hashlib
import json

from collector import archive_main
from collector.archive_core import atomic_json
from collector.archive_upload import ArchiveUploadError


class Passes:
    """Run a bounded number of uploader passes without threads or sleeps."""

    def __init__(self, count=1, after_pass=None):
        self.count = count
        self.after_pass = after_pass
        self.waits = []

    def is_set(self):
        return len(self.waits) >= self.count

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.after_pass is not None:
            self.after_pass(len(self.waits))
        return self.is_set()


def segment_fixture(root, name, *, acknowledged):
    segment = root / 'days' / '2026-10-27' / '雷达MID360' / name
    segment.mkdir(parents=True)
    content = ('training radar bytes: ' + name).encode()
    (segment / 'record.db3.zst').write_bytes(content)
    manifest_path = segment / 'archive.json'
    atomic_json(manifest_path, {'files': ['record.db3.zst']})
    if acknowledged:
        atomic_json(segment / 'uploaded.json', {
            'state': 'complete', 'completedAt': '2026-10-27T00:00:00+00:00',
            'datasetId': 'dataset', 'folderPath': 'radar/' + name,
            'manifestSha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            'files': [{'relativePath': 'record.db3.zst', 'robotFileId': name,
                       'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}],
        })
    return segment, content


class Client:
    def __init__(self):
        self.uploads = []
        self.confirmations = []
        self.available = True

    def upload_segment(self, path):
        self.uploads.append(path)
        raise ArchiveUploadError('ARCHIVE_PENDING_SEGMENT_INVALID')

    def _manifest(self, path):
        return path, json.loads((path / 'archive.json').read_text()), None

    def _confirmed_file(self, file_id, *args):
        self.confirmations.append(file_id)
        if not self.available:
            raise ArchiveUploadError('ARCHIVE_API_UNAVAILABLE')


def test_bad_pending_upload_does_not_block_other_confirmed_copy_cleanup(tmp_path, monkeypatch):
    pending, original = segment_fixture(tmp_path, 'bad-pending', acknowledged=False)
    confirmed, _ = segment_fixture(tmp_path, 'confirmed', acknowledged=True)
    client, stopping = Client(), Passes()
    monkeypatch.setattr(archive_main, 'ArchiveUploadClient', lambda _: client)
    worker = archive_main.UploadWorker(tmp_path, None, stopping)

    worker.run()

    assert client.uploads == [pending]
    assert client.confirmations == ['confirmed']
    assert not (confirmed / 'record.db3.zst').exists()
    assert json.loads((confirmed / 'local-copy-pruned.json').read_text())['state'] == 'pruned'
    assert (pending / 'record.db3.zst').read_bytes() == original
    assert not (pending / 'uploaded.json').exists()
    assert not (pending / 'local-copy-pruned.json').exists()
    assert worker.snapshot()['lastError'] == 'ARCHIVE_PENDING_SEGMENT_INVALID'
    assert stopping.waits == [30]


def test_unconfirmed_pending_copy_is_preserved_without_remote_cleanup_check(tmp_path, monkeypatch):
    pending, original = segment_fixture(tmp_path, 'finalizing', acknowledged=False)
    client, stopping = Client(), Passes()
    monkeypatch.setattr(client, 'upload_segment', lambda _: {'state': 'pending'})
    monkeypatch.setattr(archive_main, 'ArchiveUploadClient', lambda _: client)
    worker = archive_main.UploadWorker(tmp_path, None, stopping)

    worker.run()

    assert (pending / 'record.db3.zst').read_bytes() == original
    assert not (pending / 'uploaded.json').exists()
    assert not (pending / 'local-copy-pruned.json').exists()
    assert not client.confirmations
    assert worker.snapshot()['pendingSegments'] == 1
    assert stopping.waits == [15]


def test_cleanup_network_failure_keeps_bytes_and_retries_next_pass(tmp_path, monkeypatch):
    confirmed, original = segment_fixture(tmp_path, 'confirmed', acknowledged=True)
    client = Client()
    client.available = False

    def after_pass(number):
        if number == 1:
            assert (confirmed / 'record.db3.zst').read_bytes() == original
            assert not (confirmed / 'local-copy-pruned.json').exists()
            assert confirmed not in worker.retention_complete
            client.available = True

    stopping = Passes(count=2, after_pass=after_pass)
    monkeypatch.setattr(archive_main, 'ArchiveUploadClient', lambda _: client)
    worker = archive_main.UploadWorker(tmp_path, None, stopping)

    worker.run()

    assert client.confirmations == ['confirmed', 'confirmed']
    assert not (confirmed / 'record.db3.zst').exists()
    assert json.loads((confirmed / 'local-copy-pruned.json').read_text())['state'] == 'pruned'
    assert confirmed in worker.retention_complete
    assert stopping.waits == [15, 15]
