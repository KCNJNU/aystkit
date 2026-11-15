"""Original timestamp bytes survive upload/prune before a capture session ends."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from collector import archive_frame_index_cache as cache
from collector import archive_sync_index as sync
from collector.archive_pull import REMOTE_FILES
from collector.archive_retention import prune_confirmed_copy
from collector.archive_upload import ArchiveUploadClient, ArchiveUploadConfig, ArchiveUploadError
from tests.test_archive_sync_index import source, ROBOT, SESSION
from tests.test_archive_upload import FakeApi, PASSWORD


def load(path):
    return json.loads(path.read_text())


def cached_index(segment, manifest):
    usb = manifest['metadata']['source'] == 'native-camera-jpeg'
    name = 'index.jsonl' if usb else 'frames.index.jsonl'
    row = next(row for row in manifest['metadata']['videoAttachments'] if row['name'] == name)
    return segment / '.sync-frame-indexes' / row['sha256'] / name


def ack(path):
    value = load(path)
    video = path.parent / 'video.mkv'
    result = dict(state='complete', segmentId=value['id'], manifestSha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        datasetId='fixture-dataset', folderPath='fixture/folder', completedAt='2026-11-13T00:00:00+00:00',
        files=[dict(relativePath='video.mkv', robotFileId='fixture-file',
            sha256=hashlib.sha256(video.read_bytes()).hexdigest(), size=video.stat().st_size)])
    (path.parent / 'uploaded.json').write_text(json.dumps(result))
    return result


class RetentionClient:
    def _manifest(self, segment):
        return segment, load(segment / 'archive.json'), None

    def _confirmed_file(self, *args):
        return {}


@pytest.mark.parametrize('sensor', ['原相机前', 'Berxel深度', 'Berxel彩色'])
def test_exact_source_bytes_are_durable_and_not_an_upload_file(tmp_path, sensor):
    path, original = source(tmp_path, sensor=sensor)
    manifest = load(path)
    before = path.read_bytes()
    result = cache.preserve_frame_time_index(path.parent, manifest)
    target = cached_index(path.parent, manifest)
    assert target.read_bytes() == original
    assert result['state'] == 'preserved'
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert load(path)['files'] == ['video.mkv'] and path.read_bytes() == before
    assert (path.parent / 'video.mkv').is_file()
    assert cache.preserve_frame_time_index(path.parent, manifest)['path'] == result['path']


def test_extracts_only_the_verified_attachment_when_source_sidecar_is_gone(tmp_path):
    path, original = source(tmp_path, sidecar=False)
    manifest = load(path)
    called = []

    def extract(video, position):
        called.append((video, position))
        return original

    cache.preserve_frame_time_index(path.parent, manifest, extractor=extract)
    assert called == [(path.parent / 'video.mkv', 0)]
    target = cached_index(path.parent, manifest)
    assert target.read_bytes() == original
    (path.parent / 'video.mkv').unlink()
    cache.preserve_frame_time_index(path.parent, manifest,
        extractor=lambda *args: pytest.fail('cached index reopened a removed video'))


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='FFmpeg is optional for this real attachment fixture')
def test_real_mkv_attachment_is_preserved_without_the_original_index(tmp_path):
    import cv2
    import numpy as np
    from collector.archive_video import _write_matroska
    path, original = source(tmp_path)
    video = path.parent / 'video.mkv'
    video.unlink()
    ok, encoded = cv2.imencode('.jpg', np.zeros((12, 16, 3), dtype=np.uint8))
    assert ok
    frame = encoded.tobytes()
    _write_matroska(video, [frame, frame], [0, 50_000], 16, 12, codec='V_MJPEG',
        attachments=[(path.parent / 'index.jsonl', 'index.jsonl', 'application/x-ndjson')])
    (path.parent / 'index.jsonl').unlink()
    result = cache.preserve_frame_time_index(path.parent, load(path))
    assert result['state'] == 'preserved'
    assert cached_index(path.parent, load(path)).read_bytes() == original


@pytest.mark.parametrize('kind', ['cache_corrupt', 'cache_link', 'cache_parent_link', 'source_corrupt', 'source_link'])
def test_invalid_cache_or_source_is_not_overwritten_or_followed(tmp_path, kind):
    path, original = source(tmp_path)
    manifest = load(path)
    target = cached_index(path.parent, manifest)
    outside = tmp_path / 'protected'
    outside.write_bytes(b'protected outside data')
    if kind.startswith('cache'):
        cache.preserve_frame_time_index(path.parent, manifest)
        if kind == 'cache_corrupt':
            target.write_bytes(b'changed')
        elif kind == 'cache_link':
            target.unlink()
            target.symlink_to(outside)
        else:
            target.unlink()
            target.parent.rmdir()
            target.parent.symlink_to(tmp_path, target_is_directory=True)
    elif kind == 'source_corrupt':
        (path.parent / 'index.jsonl').write_bytes(b'bad index')
    else:
        (path.parent / 'index.jsonl').unlink()
        (path.parent / 'index.jsonl').symlink_to(outside)
    with pytest.raises((cache.FrameIndexCacheError, OSError)):
        cache.preserve_frame_time_index(path.parent, manifest,
            extractor=lambda *args: pytest.fail('corrupt evidence silently fell back to video'))
    assert outside.read_bytes() == b'protected outside data'
    assert (path.parent / 'video.mkv').is_file()


def test_common_client_preserves_index_before_complete_ack_without_changing_media_contract(tmp_path):
    path, original = source(tmp_path)
    api = FakeApi()
    client = ArchiveUploadClient(ArchiveUploadConfig('fixture@example.invalid', PASSWORD,
        robot_id=ROBOT, upload_bytes_per_second=0), opener=api)
    assert client.upload_segment(path.parent)['state'] == 'pending'
    assert not (path.parent / 'uploaded.json').exists()
    api.finish()
    result = client.upload_segment(path.parent)
    assert result['state'] == 'complete'
    assert cached_index(path.parent, load(path)).read_bytes() == original
    assert len(api.uploads) == 1
    assert {row['originalName'] for row in api.uploads.values()} == {'video.mkv'}
    assert [row['relativePath'] for row in result['files']] == ['video.mkv']
    assert load(path.parent / 'frame-time-index-cache.json')['state'] == 'preserved'


def test_cache_failure_keeps_video_upload_success_but_prevents_destructive_retention(tmp_path, monkeypatch):
    path, _ = source(tmp_path, sidecar=False)
    monkeypatch.setattr(sync, '_extract_attachment', lambda *args: b'incorrect attachment')
    api = FakeApi()
    client = ArchiveUploadClient(ArchiveUploadConfig('fixture@example.invalid', PASSWORD,
        robot_id=ROBOT, upload_bytes_per_second=0), opener=api)
    assert client.upload_segment(path.parent)['state'] == 'pending'
    api.finish()
    assert client.upload_segment(path.parent)['state'] == 'complete'
    assert load(path.parent / 'frame-time-index-cache.json')['state'] == 'failed'
    with pytest.raises(ArchiveUploadError, match='FRAME_TIME_INDEX'):
        prune_confirmed_copy(path.parent, RetentionClient(), retain_seconds=0)
    assert (path.parent / 'video.mkv').is_file()
    assert not (path.parent / 'local-copy-pruned.json').exists()


def test_local_prune_keeps_index_even_when_sourcefiles_names_it(tmp_path):
    path, original = source(tmp_path)
    manifest = load(path)
    raw = path.parent / 'frames.mjpg'
    raw.write_bytes(b'raw JPEG spool')
    manifest['metadata']['sourceFiles'] = [dict(relativePath=name,
        sha256=hashlib.sha256((path.parent / name).read_bytes()).hexdigest(),
        size=(path.parent / name).stat().st_size) for name in ('frames.mjpg', 'index.jsonl')]
    path.write_text(json.dumps(manifest))
    ack(path)
    assert prune_confirmed_copy(path.parent, RetentionClient(), retain_seconds=0)
    assert not (path.parent / 'video.mkv').exists()
    assert not (path.parent / 'index.jsonl').exists() and not raw.exists()
    assert cached_index(path.parent, manifest).read_bytes() == original
    assert load(path.parent / 'local-copy-pruned.json')['state'] == 'pruned'


def test_closed_session_can_build_all_pairs_after_video_upload_and_prune(tmp_path):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    for path in (rgb, depth):
        manifest = load(path)
        name = 'index.jsonl' if path == rgb else 'frames.index.jsonl'
        # USB sourceFiles is deliberately eligible for removal. A depth frame
        # sidecar is normally only inside the video, so remove its original
        # after caching to represent the finalizer's temporary work cleanup.
        if path == rgb:
            manifest['metadata']['sourceFiles'] = [dict(relativePath=name,
                sha256=hashlib.sha256((path.parent / name).read_bytes()).hexdigest(),
                size=(path.parent / name).stat().st_size)]
            path.write_text(json.dumps(manifest))
        else:
            original_bag = path.parent / 'bag' / 'metadata.yaml'
            original_bag.parent.mkdir()
            original_bag.write_bytes(b'original native bag metadata')
            manifest['metadata']['sourceFiles'] = [dict(relativePath='bag/metadata.yaml',
                sha256=hashlib.sha256(original_bag.read_bytes()).hexdigest(), size=original_bag.stat().st_size)]
            path.write_text(json.dumps(manifest))
        cache.preserve_frame_time_index(path.parent, manifest)
        ack(path)
        assert prune_confirmed_copy(path.parent, RetentionClient(), retain_seconds=0)
        (path.parent / name).unlink(missing_ok=True)
        assert not (path.parent / 'video.mkv').exists()
    built = sync.build_sync_indexes([rgb, depth], tmp_path, ROBOT, training_only=False)
    assert not built['errors'] and built['generated'] == 1
    rows = [json.loads(line) for line in (Path(built['readyManifestPaths'][0]).parent / 'sync-index.jsonl').read_text().splitlines()]
    assert len(rows) == 2 and all(row['state'] == 'matched' for row in rows)


@pytest.mark.parametrize('deployed', [True, False])
def test_standalone_remote_ack_preserves_cache_or_refuses_prune_if_module_missing(tmp_path, deployed):
    home = tmp_path / 'remote-home'
    home.mkdir()
    root = home / '.local/share/robot-native-archive'
    root.mkdir(parents=True)
    path, original = source(root)
    manifest = load(path)
    manifest['metadata']['sourceFiles'] = [dict(relativePath='index.jsonl',
        sha256=hashlib.sha256(original).hexdigest(), size=len(original))]
    path.write_text(json.dumps(manifest))
    # The SSH contract includes all hierarchy labels in the manifest.
    relative = path.parent.relative_to(root / 'days').as_posix()
    value = ack(path)
    (path.parent / 'uploaded.json').unlink()
    if deployed:
        package = home / '.local/share/robot-cloud/collector'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        shutil.copyfile(Path(cache.__file__), package / 'archive_frame_index_cache.py')
    request = dict(op='ack', segment=relative, ack=value, prune=True)
    process = subprocess.run([sys.executable, '-B', '-c', REMOTE_FILES],
        input=json.dumps(request).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=home, env={**os.environ, 'HOME': str(home), 'PYTHONPATH': ''}, timeout=10)
    if deployed:
        assert process.returncode == 0, process.stderr.decode()
        assert json.loads(process.stdout)['pruned'] is True
        assert not (path.parent / 'video.mkv').exists()
        assert cached_index(path.parent, manifest).read_bytes() == original
    else:
        assert process.returncode != 0
        assert (path.parent / 'video.mkv').is_file()
        assert not (path.parent / 'local-copy-pruned.json').exists()
