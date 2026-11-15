from datetime import datetime, timezone
import hashlib
import json

import pytest

from collector.archive_core import atomic_json
from collector.archive_retention import prune_confirmed_copy
from collector.archive_upload import ArchiveUploadError


class Client:
    def __init__(self, available=True):
        self.available = available
    def _manifest(self, path):
        return path, json.loads((path / 'archive.json').read_text()), None
    def _digest(self, path):
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest(), len(data), 0
    def _confirmed_file(self, *args):
        if not self.available:
            raise ArchiveUploadError('ARCHIVE_API_UNAVAILABLE')


def fixture(path):
    (path / 'bag').mkdir()
    (path / 'bag/data.db3').write_bytes(b'original-native-data')
    atomic_json(path / 'archive.json', {'files': ['bag/data.db3']})
    ack = {'state': 'complete', 'completedAt': '2026-10-27T00:00:00+00:00',
        'datasetId': 'id', 'folderPath': 'sensor/label/id',
        'manifestSha256': hashlib.sha256((path / 'archive.json').read_bytes()).hexdigest(),
        'files': [{'relativePath': 'bag/data.db3', 'robotFileId': 'id',
                   'sha256': hashlib.sha256(b'original-native-data').hexdigest(), 'size': 20}]}
    atomic_json(path / 'uploaded.json', ack)


def test_only_confirmed_local_transfer_copy_is_pruned_after_remote_and_local_checks(tmp_path):
    fixture(tmp_path)
    now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    assert prune_confirmed_copy(tmp_path, Client(), now=now)
    assert not (tmp_path / 'bag/data.db3').exists()
    assert (tmp_path / 'archive.json').exists() and (tmp_path / 'uploaded.json').exists()
    assert json.loads((tmp_path / 'local-copy-pruned.json').read_text())['state'] == 'pruned'
    assert not prune_confirmed_copy(tmp_path, Client(), now=now)


def test_no_cleanup_when_ack_missing_recent_or_remote_unavailable(tmp_path):
    fixture(tmp_path)
    assert not prune_confirmed_copy(tmp_path, Client(), now=datetime(2026, 10, 27, 0, tzinfo=timezone.utc))
    with pytest.raises(ArchiveUploadError):
        prune_confirmed_copy(tmp_path, Client(False), retain_seconds=0)
    assert (tmp_path / 'bag/data.db3').exists()
    (tmp_path / 'uploaded.json').unlink()
    assert not prune_confirmed_copy(tmp_path, Client())


def test_changed_raw_bytes_are_retained_and_interrupted_cleanup_resumes(tmp_path):
    fixture(tmp_path)
    raw = tmp_path / 'bag/data.db3'
    raw.write_bytes(b'changed')
    with pytest.raises(ArchiveUploadError):
        prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    assert raw.read_bytes() == b'changed'
    raw.write_bytes(b'original-native-data')
    atomic_json(tmp_path / 'local-copy-pruned.json', {'state': 'pruning'})
    raw.unlink()
    assert prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)


def video_fixture(path, *, inputs_present=True, bag=False):
    source_names = ('bag/data.db3', 'bag/metadata.yaml') if bag else ('frames.mjpg', 'index.jsonl')
    rows = []
    for name in source_names:
        content = ('native source ' + name).encode()
        rows.append({'relativePath': name, 'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)})
        if inputs_present:
            source = path / name; source.parent.mkdir(parents=True, exist_ok=True); source.write_bytes(content)
    video = b'validated-video-container'
    (path / 'video.mkv').write_bytes(video)
    manifest = {'files': ['video.mkv'], 'metadata': {'videoReady': True, 'sourceFiles': rows}}
    atomic_json(path / 'archive.json', manifest)
    ack = {'state': 'complete', 'completedAt': '2026-10-27T00:00:00+00:00',
           'datasetId': 'dataset', 'folderPath': 'sensor/label/id',
           'manifestSha256': hashlib.sha256((path / 'archive.json').read_bytes()).hexdigest(),
           'files': [{'relativePath': 'video.mkv', 'robotFileId': 'video-id',
                      'sha256': hashlib.sha256(video).hexdigest(), 'size': len(video)}]}
    atomic_json(path / 'uploaded.json', ack)
    (path / 'video-pending.json').write_text('pending conversion state')
    (path / 'recorder.lock').write_text('keep lock')
    (path / 'recovery').mkdir()
    (path / 'recovery/broken-source').write_bytes(b'keep interrupted capture')
    return rows, ack


@pytest.mark.parametrize('bag', [False, True])
@pytest.mark.parametrize('inputs_present', [False, True])
def test_video_prunes_only_declared_cache_inputs_and_preserves_state(tmp_path, bag, inputs_present):
    rows, ack = video_fixture(tmp_path, inputs_present=inputs_present, bag=bag)
    assert prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    assert not (tmp_path / 'video.mkv').exists()
    assert all(not (tmp_path / row['relativePath']).exists() for row in rows)
    marker = json.loads((tmp_path / 'local-copy-pruned.json').read_text())
    assert marker['sourceFiles'] == rows and marker['files'] == ack['files'] and marker['state'] == 'pruned'
    for name in ('archive.json', 'uploaded.json', 'video-pending.json', 'recorder.lock', 'recovery/broken-source'):
        assert (tmp_path / name).is_file()


def test_video_cache_is_retained_if_server_verification_fails(tmp_path):
    rows, _ = video_fixture(tmp_path)
    with pytest.raises(ArchiveUploadError, match='ARCHIVE_API_UNAVAILABLE'):
        prune_confirmed_copy(tmp_path, Client(False), retain_seconds=0)
    assert (tmp_path / 'video.mkv').exists()
    assert all((tmp_path / row['relativePath']).exists() for row in rows)
    assert not (tmp_path / 'local-copy-pruned.json').exists()


def test_changed_video_input_prevents_all_deletion(tmp_path):
    rows, _ = video_fixture(tmp_path)
    (tmp_path / rows[-1]['relativePath']).write_bytes(b'changed native source')
    with pytest.raises(ArchiveUploadError, match='FILE_CHANGED'):
        prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    assert (tmp_path / 'video.mkv').exists()
    assert all((tmp_path / row['relativePath']).exists() for row in rows)
    assert not (tmp_path / 'local-copy-pruned.json').exists()


@pytest.mark.parametrize('name', ['../outside', '/tmp/outside', 'bag/../outside', 'archive.json',
                                  'uploaded.json', 'recorder.lock', 'recovery/native.db3',
                                  'bag/metadata.yaml/child', 'bag\\data.db3'])
def test_video_source_list_rejects_escape_and_state_paths(tmp_path, name):
    video_fixture(tmp_path)
    manifest = json.loads((tmp_path / 'archive.json').read_text())
    manifest['metadata']['sourceFiles'][0]['relativePath'] = name
    atomic_json(tmp_path / 'archive.json', manifest)
    ack = json.loads((tmp_path / 'uploaded.json').read_text())
    ack['manifestSha256'] = hashlib.sha256((tmp_path / 'archive.json').read_bytes()).hexdigest()
    atomic_json(tmp_path / 'uploaded.json', ack)
    with pytest.raises(ArchiveUploadError, match='SOURCE_LIST_INVALID'):
        prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    assert (tmp_path / 'video.mkv').exists() and (tmp_path / 'frames.mjpg').exists()


@pytest.mark.parametrize('kind', ['leaf-link', 'parent-link', 'hardlink'])
def test_video_cache_links_are_not_followed_or_deleted(tmp_path, kind):
    video_fixture(tmp_path, bag=kind == 'parent-link')
    outside = tmp_path.parent / (tmp_path.name + '-outside');outside.mkdir()
    protected = outside / 'protected';protected.write_bytes(b'protected bytes')
    if kind == 'parent-link':
        for path in (tmp_path / 'bag').iterdir():path.unlink()
        (tmp_path / 'bag').rmdir();(tmp_path / 'bag').symlink_to(outside, target_is_directory=True)
    else:
        raw = tmp_path / 'frames.mjpg';raw.unlink()
        if kind == 'leaf-link':raw.symlink_to(protected)
        else:
            import os
            os.link(protected, raw)
    with pytest.raises(ArchiveUploadError):
        prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    assert protected.read_bytes() == b'protected bytes' and (tmp_path / 'video.mkv').exists()


def test_video_pruning_resumes_after_partial_deletion_and_rechecks_server(tmp_path, monkeypatch):
    import collector.archive_retention as retention
    rows, ack = video_fixture(tmp_path)
    original = retention._unlink_verified
    calls = []
    def interrupted(segment, name, fingerprint):
        calls.append(name)
        if len(calls) == 2:raise OSError('simulated power interruption')
        original(segment, name, fingerprint)
    monkeypatch.setattr(retention, '_unlink_verified', interrupted)
    with pytest.raises(OSError, match='power interruption'):
        prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    assert not (tmp_path / 'video.mkv').exists()
    marker = json.loads((tmp_path / 'local-copy-pruned.json').read_text())
    assert marker['state'] == 'pruning' and marker['sourceFiles'] == rows and marker['files'] == ack['files']
    monkeypatch.setattr(retention, '_unlink_verified', original)
    with pytest.raises(ArchiveUploadError, match='ARCHIVE_API_UNAVAILABLE'):
        prune_confirmed_copy(tmp_path, Client(False), retain_seconds=0)
    assert all((tmp_path / row['relativePath']).exists() for row in rows)
    assert prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    assert all(not (tmp_path / row['relativePath']).exists() for row in rows)


def test_video_resume_rejects_changed_cache_or_checkpoint(tmp_path):
    rows, ack = video_fixture(tmp_path)
    atomic_json(tmp_path / 'local-copy-pruned.json', {'state': 'pruning', 'files': ack['files'], 'sourceFiles': rows})
    (tmp_path / 'video.mkv').unlink();(tmp_path / rows[0]['relativePath']).unlink()
    remaining = tmp_path / rows[-1]['relativePath'];original = remaining.read_bytes();remaining.write_bytes(b'changed')
    with pytest.raises(ArchiveUploadError, match='FILE_CHANGED'):
        prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    remaining.write_bytes(original)
    marker = json.loads((tmp_path / 'local-copy-pruned.json').read_text());marker['sourceFiles'] = []
    atomic_json(tmp_path / 'local-copy-pruned.json', marker)
    with pytest.raises(ArchiveUploadError, match='MARKER_INVALID'):
        prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    assert remaining.exists()


def test_legacy_or_unready_manifest_never_prunes_undeclared_inputs(tmp_path):
    video_fixture(tmp_path)
    manifest = json.loads((tmp_path / 'archive.json').read_text());manifest['metadata']['videoReady'] = False
    atomic_json(tmp_path / 'archive.json', manifest)
    ack = json.loads((tmp_path / 'uploaded.json').read_text())
    ack['manifestSha256'] = hashlib.sha256((tmp_path / 'archive.json').read_bytes()).hexdigest();atomic_json(tmp_path / 'uploaded.json', ack)
    assert prune_confirmed_copy(tmp_path, Client(), retain_seconds=0)
    assert (tmp_path / 'frames.mjpg').exists() and (tmp_path / 'index.jsonl').exists()


def compressed_bag_fixture(path, *, compact=True):
    rows, _ = video_fixture(path, bag=True)
    (path/'video.mkv').unlink()
    (path/'compressed-bag').mkdir()
    files = ['compressed-bag/data.db3.zst', 'compressed-bag/metadata.yaml']
    for name in files:(path/name).write_bytes(('verified output '+name).encode())
    metadata = {'bagCompressionReady': True, 'sourceFiles': rows}
    if compact:metadata['archiveProfile']='compact-v1'
    atomic_json(path/'archive.json', {'files': files, 'metadata': metadata})
    ack = json.loads((path/'uploaded.json').read_text())
    ack.update(manifestSha256=hashlib.sha256((path/'archive.json').read_bytes()).hexdigest(),
               completedAt=datetime.now(timezone.utc).isoformat(),
               files=[{'relativePath': name, 'robotFileId': name,
                       'sha256': hashlib.sha256((path/name).read_bytes()).hexdigest(),
                       'size': (path/name).stat().st_size} for name in files])
    atomic_json(path/'uploaded.json', ack)
    return rows, files


def test_new_compact_bag_releases_verified_duplicate_immediately_but_keeps_warehouse_state(tmp_path):
    rows, files = compressed_bag_fixture(tmp_path)
    assert prune_confirmed_copy(tmp_path, Client(), retain_seconds=300)
    assert all(not (tmp_path/name).exists() for name in files+[r['relativePath'] for r in rows])
    assert (tmp_path/'archive.json').exists() and (tmp_path/'uploaded.json').exists()


def test_legacy_retention_and_unavailable_or_changed_compact_bag_keep_sources(tmp_path):
    rows, files = compressed_bag_fixture(tmp_path, compact=False)
    assert not prune_confirmed_copy(tmp_path, Client(), retain_seconds=300)
    manifest = json.loads((tmp_path/'archive.json').read_text())
    manifest['metadata']['archiveProfile']='compact-v1';atomic_json(tmp_path/'archive.json',manifest)
    ack=json.loads((tmp_path/'uploaded.json').read_text())
    ack['manifestSha256']=hashlib.sha256((tmp_path/'archive.json').read_bytes()).hexdigest()
    atomic_json(tmp_path/'uploaded.json',ack)
    with pytest.raises(ArchiveUploadError):prune_confirmed_copy(tmp_path,Client(False))
    assert all((tmp_path/name).exists() for name in files+[r['relativePath'] for r in rows])
    (tmp_path/rows[0]['relativePath']).write_bytes(b'changed')
    with pytest.raises(ArchiveUploadError,match='FILE_CHANGED'):prune_confirmed_copy(tmp_path,Client())
    assert all((tmp_path/name).exists() for name in files)
