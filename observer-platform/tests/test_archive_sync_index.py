"""Optional sealed-video timestamp indexes; no live devices, ROS or network."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from collector import archive_sync_index as sync
from collector.archive_upload import ArchiveUploadClient, ArchiveUploadConfig


ROBOT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
SESSION = 'b' * 32
CLOCK = 'linux-boot:cccccccc-cccc-4ccc-8ccc-cccccccccccc'
BASE = 1_794_000_000_000_000_000


def payload(rows):
    return b''.join(json.dumps(row).encode() + b'\n' for row in rows)


def source(root, segment='rgb1', *, sensor='原相机前', times=(BASE, BASE + 50_000_000),
           camera='camera1', session=SESSION, clock=CLOCK, robot=ROBOT, sidecar=True):
    folder = root / 'days' / '2026-11-13' / sensor / 'fixture' / segment
    folder.mkdir(parents=True)
    is_usb = sensor.startswith('原相机')
    rows = [dict(sequence=i + 1, timestampNs=t) for i, t in enumerate(times)]
    if not is_usb:
        for i, row in enumerate(rows):
            row.update(videoPtsUs=i * 50_000,
                sourceHeaderStamp=dict(sec=1234, nanosec=567 + i))
    raw = payload(rows)
    name = 'index.jsonl' if is_usb else 'frames.index.jsonl'
    if sidecar:
        (folder / name).write_bytes(raw)
    (folder / 'video.mkv').write_bytes(b'ISOLATED VIDEO FIXTURE ' + segment.encode())
    metadata = dict(source='native-camera-jpeg' if is_usb else 'native-ros-cdr',
        scope='garage', videoReady=True, cameraId=camera,
        captureSessionId=session, clockDomain=clock, clockSource='CLOCK_REALTIME',
        timestampSemantics='host_read_complete' if is_usb else 'host_subscription_receive',
        videoSha256='d' * 64, videoAttachments=[dict(name=name, bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest())])
    manifest = dict(schemaVersion=1, id=segment, day='2026-11-13', robotId=robot,
        sensor=sensor, label='fixture', startedAt='2026-11-13T01:00:00+00:00',
        endAt='2026-11-13T01:00:02+00:00', metadata=metadata, files=['video.mkv'])
    path = folder / 'archive.json'
    path.write_text(json.dumps(manifest))
    return path, raw


def update(path, change):
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))


def artifact(report):
    path = Path(report['localManifestPaths'][0])
    return path, json.loads(path.read_text()), [json.loads(line) for line in
        (path.parent / 'sync-index.jsonl').read_text().splitlines()]


def build(paths, root, **kwargs):
    return sync.build_sync_indexes(paths, root, ROBOT, training_only=False, **kwargs)


def test_complete_pair_artifact_preserves_sources_and_supports_upload_contract(tmp_path, monkeypatch):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度', times=(BASE + 10_000_000, BASE + 80_000_000))
    color, _ = source(tmp_path, 'color', sensor='Berxel彩色', times=(BASE + 8_000_000,))
    before = {path: path.read_bytes() for selected in (rgb, depth, color) for path in selected.parent.iterdir()}
    result = build([rgb, depth, color], tmp_path)
    assert not result['errors'] and result['generated'] == 1
    assert result['readyManifestPaths'] == result['localManifestPaths']
    path, manifest, rows = artifact(result)
    assert path.is_relative_to(tmp_path / 'sync-indexes')
    assert manifest['sensor'] == '同步配对'
    assert manifest['files'] == ['sync-index.jsonl', 'sync-summary.json']
    assert manifest['metadata']['source'] == 'native-time-sync'
    assert manifest['metadata']['syncScope'] == 'selected_sealed_session_snapshot'
    assert len(rows) == 3 and all(row['state'] == 'matched' for row in rows)
    assert rows[0]['rgb']['cameraId'] == 'berxel_color'
    assert rows[1]['rgb']['videoPtsUs'] == 0 and rows[2]['rgb']['videoPtsUs'] == 50_000
    assert rows[1]['depth']['sourceHeaderStamp'] == dict(sec=1234, nanosec=567)
    assert all(row['hardwareSynchronized'] is False for row in rows)
    summary = json.loads((path.parent / 'sync-summary.json').read_text())
    assert summary['matchedFrames'] == 3 and summary['unmatchedFrames'] == 0
    assert summary['scope'] == 'selected_sealed_session_snapshot'
    assert summary['depthReusedFrames'] == sum(row.get('depthReusedWithinCamera') is True for row in rows)
    assert {path: path.read_bytes() for path in before} == before
    monkeypatch.setenv('ARCHIVE_TRAINING_ONLY', '0')
    client = ArchiveUploadClient(ArchiveUploadConfig('fixture@example.invalid', 'fixture-only', robot_id=ROBOT))
    assert client._manifest(path.parent)[1]['files'] == manifest['files']


def test_index_retry_is_hash_idempotent_after_video_retention(tmp_path):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    first = build([rgb, depth], tmp_path)
    path, manifest, _ = artifact(first)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.iterdir()}
    for selected in (rgb, depth):
        for item in selected.parent.iterdir():
            if item.name != 'archive.json':
                item.unlink()

    def no_extract(*args):
        raise AssertionError('a durable completed index must survive source pruning')

    again = build([depth, rgb, rgb], tmp_path, extractor=no_extract)
    assert again['reused'] == 1 and again['generated'] == 0 and not again['errors']
    assert again['readyManifestPaths'] == first['readyManifestPaths']
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.iterdir()} == before
    assert manifest['metadata']['sourceFingerprint']


def test_hash_conflict_never_overwrites_existing_index(tmp_path):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    first = build([rgb, depth], tmp_path)
    path, _, _ = artifact(first)
    target = path.parent / 'sync-index.jsonl'
    target.write_bytes(b'OPERATOR CONTENT MUST SURVIVE')
    result = build([rgb, depth], tmp_path)
    assert result['errors'][0]['code'] == 'SYNC_EXISTING_INDEX_CONFLICT'
    assert not result['readyManifestPaths']
    assert target.read_bytes() == b'OPERATOR CONTENT MUST SURVIVE'


def test_changed_cached_sync_claim_is_not_silently_reused(tmp_path):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    first = build([rgb, depth], tmp_path)
    path, _, _ = artifact(first)
    update(path, lambda m: m['metadata'].update(hardwareSynchronized=True))
    result = build([rgb, depth], tmp_path)
    assert result['errors'][0]['code'] == 'SYNC_EXISTING_INDEX_CONFLICT'
    assert not result['readyManifestPaths']
    assert json.loads(path.read_text())['metadata']['hardwareSynchronized'] is True


def test_optional_policy_exclusion_keeps_local_index_outside_normal_days_scan(tmp_path, monkeypatch):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    monkeypatch.setattr(sync, '_eligible', lambda *_args, **_kwargs: False)
    result = build([rgb, depth], tmp_path)
    assert len(result['localManifestPaths']) == 1 and result['readyManifestPaths'] == []
    assert result['skipped']['storage_policy_excluded'] == 1
    assert result['skippedReasons'][0]['description']
    from collector.archive_upload_only import sealed_manifests
    assert sealed_manifests(tmp_path) == sorted([rgb, depth])


@pytest.mark.parametrize('invalid,reason', [
    ('clock', 'missing_clock_domain'), ('session', 'missing_session'),
    ('robot', 'robot_mismatch'), ('semantics', 'unverified_timestamp_semantics'),
    ('clock_source', 'unverified_timestamp_semantics'), ('pending', 'not_sealed_video')])
def test_unverified_clock_session_or_source_is_explicitly_skipped(tmp_path, invalid, reason):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    def change(manifest):
        if invalid == 'robot':
            manifest['robotId'] = 'other-robot'
        elif invalid == 'pending':
            manifest['metadata']['videoReady'] = False
        else:
            field = dict(clock='clockDomain', session='captureSessionId', semantics='timestampSemantics',
                         clock_source='clockSource')[invalid]
            manifest['metadata'].pop(field)
    update(rgb, change)
    result = build([rgb, depth], tmp_path)
    assert not result['localManifestPaths'] and result['skipped'][reason] == 1
    assert result['skipped']['missing_rgb_or_depth'] == 1


@pytest.mark.parametrize('split', ['clock', 'session'])
def test_never_pairs_across_boots_or_sessions(tmp_path, split):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度',
        clock='linux-boot:eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee' if split == 'clock' else CLOCK,
        session='f' * 32 if split == 'session' else SESSION)
    result = build([rgb, depth], tmp_path)
    assert result['skipped']['missing_rgb_or_depth'] == 2
    assert not result['localManifestPaths'] and not result['errors']


def test_missing_depth_is_visible_and_outside_tolerance_is_retained(tmp_path):
    rgb, _ = source(tmp_path)
    result = build([rgb], tmp_path)
    assert result['skipped']['missing_rgb_or_depth'] == 1
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度', times=(BASE + 1_000_000_000,))
    result = build([rgb, depth], tmp_path)
    _, manifest, rows = artifact(result)
    assert len(rows) == 2 and all(row['state'] == 'unmatched' and row['depth'] is None for row in rows)
    assert manifest['metadata']['unmatchedFrames'] == 2


def test_explicit_batch_does_not_scan_or_import_unselected_sessions(tmp_path, monkeypatch):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    source(tmp_path, 'unselected', camera='camera2', session='a' * 32)
    monkeypatch.setattr(os, 'walk', lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('no scan')))
    result = build([rgb, depth], tmp_path)
    _, manifest, _ = artifact(result)
    assert manifest['metadata']['sourceManifestCount'] == 2


@pytest.mark.parametrize('invalid', ['symlink_manifest', 'symlink_index', 'outside_root', 'bad_checksum',
    'bad_json', 'backward_time', 'missing_attachment', 'oversized_index'])
def test_source_errors_cannot_publish_partial_or_change_source_video(tmp_path, invalid, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    rgb, _ = source(root)
    depth, _ = source(root, 'depth', sensor='Berxel深度')
    video = rgb.parent / 'video.mkv'
    original = video.read_bytes()
    if invalid == 'symlink_manifest':
        original_manifest = rgb.with_name('manifest-original.json')
        rgb.rename(original_manifest)
        rgb.symlink_to(original_manifest)
    elif invalid == 'symlink_index':
        original_index = rgb.parent / 'original-index'
        (rgb.parent / 'index.jsonl').rename(original_index)
        (rgb.parent / 'index.jsonl').symlink_to(original_index)
    elif invalid == 'outside_root':
        other, _ = source(tmp_path, 'outside')
        rgb = other
    elif invalid == 'bad_checksum':
        (rgb.parent / 'index.jsonl').write_bytes(b'altered')
    elif invalid in ('bad_json', 'backward_time'):
        raw = b'INVALID JSON\n' if invalid == 'bad_json' else payload([
            dict(sequence=1, timestampNs=BASE + 1), dict(sequence=2, timestampNs=BASE)])
        (rgb.parent / 'index.jsonl').write_bytes(raw)
        update(rgb, lambda m: m['metadata']['videoAttachments'][0].update(
            bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
    elif invalid == 'missing_attachment':
        (rgb.parent / 'index.jsonl').unlink()
        update(rgb, lambda m: m['metadata'].pop('videoAttachments'))
    else:
        monkeypatch.setattr(sync, 'MAX_INDEX_BYTES', 10)
    result = build([rgb, depth], root)
    assert result['errors'] and result['localManifestPaths'] == []
    assert video.read_bytes() == original


def test_extractor_is_called_only_for_expected_attachment_and_preserves_pts(tmp_path):
    rgb, raw_rgb = source(tmp_path, sidecar=False, times=(BASE, BASE, BASE + 50_000_000))
    depth, raw_depth = source(tmp_path, 'depth', sensor='Berxel深度', sidecar=False)
    calls = []
    def extractor(video, position):
        calls.append((video, position))
        return raw_rgb if video.parent == rgb.parent else raw_depth
    result = build([rgb, depth], tmp_path, extractor=extractor)
    assert len(calls) == 2 and all(position == 0 for _, position in calls)
    _, _, rows = artifact(result)
    assert [row['rgb']['videoPtsUs'] for row in rows] == [0, 1, 50_000]
    assert [row['rgb']['timestampNs'] for row in rows] == [BASE, BASE, BASE + 50_000_000]


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='offline attachment reader requires ffmpeg')
def test_real_attachment_extraction_does_not_decode_or_rewrite_video(tmp_path):
    import cv2
    import numpy as np
    from collector.archive_video import _write_matroska
    ok, encoded = cv2.imencode('.jpg', np.full((16, 16, 3), 100, dtype=np.uint8))
    assert ok
    raw = payload([dict(sequence=1, timestampNs=BASE)])
    attachment = tmp_path / 'index.jsonl'
    attachment.write_bytes(raw)
    video = tmp_path / 'video.mkv'
    _write_matroska(video, iter([encoded.tobytes()]), [0], 16, 16,
        codec='V_MJPEG', attachments=[(attachment, 'index.jsonl', 'application/x-ndjson')])
    before = video.read_bytes()
    assert sync._extract_attachment(video, 0) == raw
    assert video.read_bytes() == before


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='offline attachment reader requires ffmpeg')
def test_real_multiple_attachments_can_be_selected_and_read_repeatedly(tmp_path):
    """Use real ffmpeg against an existing /dev/null and separate attachment streams."""
    import cv2
    import numpy as np
    from collector.archive_video import _write_matroska
    ok, encoded = cv2.imencode('.jpg', np.full((16, 16, 3), 70, dtype=np.uint8))
    assert ok
    indexes = tmp_path / 'frames.index.jsonl'
    raw_index = payload([dict(sequence=1, timestampNs=BASE, videoPtsUs=0)])
    indexes.write_bytes(raw_index)
    info = tmp_path / 'camera-info.cdr.jsonl'
    raw_info = payload([dict(topic='/berxel/depth/camera_info', cdrBase64='RklYVFVSRQ==')])
    info.write_bytes(raw_info)
    video = tmp_path / 'two-attachments.mkv'
    _write_matroska(video, iter([encoded.tobytes()]), [0], 16, 16,
        codec='V_MJPEG', attachments=[(indexes, indexes.name, 'application/x-ndjson'),
                                    (info, info.name, 'application/x-ndjson')])
    before = video.read_bytes()
    assert Path(os.devnull).exists()
    for _attempt in range(2):
        assert sync._extract_attachment(video, 0) == raw_index
        assert sync._extract_attachment(video, 1) == raw_info
    assert video.read_bytes() == before
    assert indexes.read_bytes() == raw_index and info.read_bytes() == raw_info
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        'camera-info.cdr.jsonl', 'frames.index.jsonl', 'two-attachments.mkv']


@pytest.mark.parametrize('scenario', ['oversize', 'timeout', 'failure'])
def test_extraction_caps_output_and_terminates_only_its_child(tmp_path, monkeypatch, scenario):
    script = tmp_path / 'fake-ffmpeg'
    body = dict(oversize="import os; os.write(1,b'x'*4096)",
                timeout='import time; time.sleep(5)', failure='raise SystemExit(7)')[scenario]
    script.write_text('#!' + sys.executable + '\n' + body + '\n')
    script.chmod(0o700)
    monkeypatch.setattr(sync, 'MAX_INDEX_BYTES', 32)
    code = dict(oversize='SYNC_ATTACHMENT_TOO_LARGE', timeout='SYNC_ATTACHMENT_TIMEOUT',
                failure='SYNC_ATTACHMENT_EXTRACTION_FAILED')[scenario]
    with pytest.raises(sync.SyncIndexError, match=code):
        sync._extract_attachment(tmp_path / 'irrelevant.mkv', 0, ffmpeg=str(script), timeout=.1)


def test_optional_output_limit_discards_uncommitted_artifact(tmp_path, monkeypatch):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    monkeypatch.setattr(sync, 'MAX_OUTPUT_BYTES', 10)
    result = build([rgb, depth], tmp_path)
    assert result['errors'][0]['code'] == 'SYNC_OUTPUT_CAPACITY'
    assert result['localManifestPaths'] == []
    assert list((tmp_path / 'sync-indexes').rglob('archive.json')) == []
    assert list((tmp_path / 'sync-indexes').rglob('.sync-writing-*')) == []


def test_each_session_has_independent_frame_budget_and_releases_decoded_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, 'MAX_ROWS', 5)
    first_rgb, _ = source(tmp_path, 'first-rgb')
    first_depth, _ = source(tmp_path, 'first-depth', sensor='Berxel深度')
    second_session = 'c' * 32
    second_rgb, _ = source(tmp_path, 'second-rgb', session=second_session)
    second_depth, _ = source(tmp_path, 'second-depth', sensor='Berxel深度', session=second_session)
    observed_sources, original_publish = [], sync._publish

    def publish(root, key, sources, rgb, depth, *args, **kwargs):
        assert len(rgb) + len(depth) == 4
        observed_sources.extend(sources)
        return original_publish(root, key, sources, rgb, depth, *args, **kwargs)

    monkeypatch.setattr(sync, '_publish', publish)
    result = build([first_rgb, first_depth, second_rgb, second_depth], tmp_path)
    assert result['generated'] == 2 and not result['errors']
    assert len(result['readyManifestPaths']) == 2
    assert observed_sources and all('rows' not in item for item in observed_sources)


def test_oversized_session_reports_capacity_without_consuming_next_session_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, 'MAX_ROWS', 5)
    times = (BASE, BASE + 50_000_000, BASE + 100_000_000)
    large_rgb, _ = source(tmp_path, 'large-rgb', times=times)
    large_depth, _ = source(tmp_path, 'large-depth', sensor='Berxel深度', times=times)
    next_session = 'c' * 32
    next_rgb, _ = source(tmp_path, 'next-rgb', session=next_session)
    next_depth, _ = source(tmp_path, 'next-depth', sensor='Berxel深度', session=next_session)
    videos = {p.parent / 'video.mkv': (p.parent / 'video.mkv').read_bytes()
              for p in (large_rgb, large_depth, next_rgb, next_depth)}
    result = build([large_rgb, large_depth, next_rgb, next_depth], tmp_path)
    assert result['generated'] == 1
    assert len(result['errors']) == 1
    assert result['errors'][0]['code'] == 'SYNC_BATCH_FRAME_CAPACITY'
    assert result['errors'][0]['captureSessionId'] == SESSION
    assert result['errors'][0]['description']
    _, manifest, _ = artifact(result)
    assert manifest['metadata']['captureSessionId'] == next_session
    assert {path: path.read_bytes() for path in videos} == videos


def test_source_limit_applies_to_complete_sessions_instead_of_history_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, 'MAX_SELECTED', 3)
    first_rgb, _ = source(tmp_path, 'old-rgb')
    first_depth, _ = source(tmp_path, 'old-depth', sensor='Berxel深度')
    latest_session = 'c' * 32
    latest_rgb, _ = source(tmp_path, 'latest-rgb', session=latest_session)
    latest_depth, _ = source(tmp_path, 'latest-depth', sensor='Berxel深度', session=latest_session)
    paths = [first_rgb, first_depth, latest_rgb, latest_depth]
    result = build(paths, tmp_path)
    assert result['generated'] == 2 and not result['errors']
    manifests = [json.loads(Path(path).read_text()) for path in result['localManifestPaths']]
    assert {row['metadata']['captureSessionId'] for row in manifests} == {SESSION, latest_session}
    assert all(row['metadata']['sourceManifestCount'] == 2 for row in manifests)
    # A historical index on the next invocation cannot permanently hide the
    # newer session simply because its manifests follow the old prefix.
    again = build(paths, tmp_path)
    assert again['reused'] == 2 and not again['errors']


def test_over_limit_session_is_not_truncated_and_other_sessions_still_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, 'MAX_SELECTED', 3)
    paths = [source(tmp_path, 'rgb-' + str(i), camera='camera' + str(i + 1))[0] for i in range(3)]
    paths.append(source(tmp_path, 'depth', sensor='Berxel深度')[0])
    next_session = 'c' * 32
    paths += [source(tmp_path, 'next-rgb', session=next_session)[0],
        source(tmp_path, 'next-depth', sensor='Berxel深度', session=next_session)[0]]
    result = build(paths, tmp_path)
    assert result['generated'] == 1
    assert len(result['errors']) == 1 and result['errors'][0]['code'] == 'SYNC_SESSION_SOURCE_CAPACITY'
    assert result['errors'][0]['captureSessionId'] == SESSION
    _, manifest, _ = artifact(result)
    assert manifest['metadata']['captureSessionId'] == next_session
    assert manifest['metadata']['sourceManifestCount'] == 2
    assert not (tmp_path / 'sync-indexes' / '2026-11-13' / SESSION).exists()


def test_interrupted_group_advances_cursor_without_starving_other_sessions(tmp_path, monkeypatch):
    first_rgb, _ = source(tmp_path, 'first-rgb')
    first_depth, _ = source(tmp_path, 'first-depth', sensor='Berxel深度')
    next_session = 'c' * 32
    next_rgb, _ = source(tmp_path, 'next-rgb', session=next_session)
    next_depth, _ = source(tmp_path, 'next-depth', sensor='Berxel深度', session=next_session)
    paths = [first_rgb, first_depth, next_rgb, next_depth]
    original_publish = sync._publish

    class Deadline(BaseException):
        pass

    def interrupted(*args, **kwargs):
        raise Deadline()

    monkeypatch.setattr(sync, '_publish', interrupted)
    with pytest.raises(Deadline):
        build(paths, tmp_path)
    assert json.loads((tmp_path / '.sync-index-cursor.json').read_text())['nextGroup'] == [ROBOT, next_session, CLOCK]
    if (tmp_path / 'sync-indexes').exists():
        assert not list((tmp_path / 'sync-indexes').rglob('archive.json'))
    order = []

    def recording(root, key, *args, **kwargs):
        order.append(key[1])
        return original_publish(root, key, *args, **kwargs)

    monkeypatch.setattr(sync, '_publish', recording)
    again = build(paths, tmp_path)
    assert again['generated'] == 2 and not again['errors']
    assert order == [next_session, SESSION]


def test_confirmed_indexes_do_not_rehash_historical_outputs_or_need_local_files(tmp_path):
    rgb, _ = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    first = build([rgb, depth], tmp_path)
    path, manifest, _ = artifact(first)
    ack = dict(schemaVersion=1, state='complete', segmentId=manifest['id'],
        manifestSha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        files=[dict(relativePath=name, robotFileId='confirmed-' + name) for name in manifest['files']])
    (path.parent / 'uploaded.json').write_text(json.dumps(ack))
    for name in manifest['files']:
        (path.parent / name).unlink()
    again = build([rgb, depth], tmp_path)
    assert again['reused'] == 1 and again['readyManifestPaths'] == first['readyManifestPaths']
    assert not again['errors']


def test_summary_counts_actual_within_camera_depth_reuse(tmp_path):
    rgb, _ = source(tmp_path, times=(BASE, BASE + 1, BASE + 2))
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度', times=(BASE,))
    result = build([rgb, depth], tmp_path)
    path, manifest, rows = artifact(result)
    assert [row['depthReusedWithinCamera'] for row in rows] == [False, True, True]
    assert manifest['metadata']['depthReusedFrames'] == 2
    assert json.loads((path.parent / 'sync-summary.json').read_text())['depthReusedFrames'] == 2


def cache_index(manifest_path, raw):
    manifest = json.loads(manifest_path.read_text())
    attachment = manifest['metadata']['videoAttachments'][0]
    path = manifest_path.parent / '.sync-frame-indexes' / attachment['sha256'] / attachment['name']
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def test_hash_bound_cache_survives_original_index_and_video_pruning(tmp_path):
    rgb, raw_rgb = source(tmp_path)
    depth, raw_depth = source(tmp_path, 'depth', sensor='Berxel深度')
    caches = {cache_index(rgb, raw_rgb): raw_rgb, cache_index(depth, raw_depth): raw_depth}
    for selected in (rgb, depth):
        for filename in ('video.mkv', 'index.jsonl', 'frames.index.jsonl'):
            (selected.parent / filename).unlink(missing_ok=True)

    def forbidden_extract(*args):
        raise AssertionError('pruned video must use the verified frame-index cache')

    result = build([rgb, depth], tmp_path, extractor=forbidden_extract)
    assert result['generated'] == 1 and not result['errors']
    _, manifest, rows = artifact(result)
    assert manifest['metadata']['matchedFrames'] == len(rows) == 2
    assert {path: path.read_bytes() for path in caches} == caches


def test_verified_cache_precedes_stale_raw_sidecar_without_altering_it(tmp_path):
    rgb, raw_rgb = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    cache = cache_index(rgb, raw_rgb)
    sidecar = rgb.parent / 'index.jsonl'
    sidecar.write_bytes(b'OLD UNRELATED RAW SIDECAR MUST SURVIVE')
    result = build([rgb, depth], tmp_path)
    assert result['generated'] == 1 and not result['errors']
    assert cache.read_bytes() == raw_rgb
    assert sidecar.read_bytes() == b'OLD UNRELATED RAW SIDECAR MUST SURVIVE'


@pytest.mark.parametrize('corruption', ['bytes', 'file_link', 'hash_directory_link'])
def test_cache_is_not_trusted_without_original_attachment_hash_or_safe_path(tmp_path, corruption):
    rgb, raw_rgb = source(tmp_path)
    depth, _ = source(tmp_path, 'depth', sensor='Berxel深度')
    cached = cache_index(rgb, raw_rgb)
    original_video = (rgb.parent / 'video.mkv').read_bytes()
    if corruption == 'bytes':
        cached.write_bytes(b'CORRUPTED CACHE')
    elif corruption == 'file_link':
        outside = tmp_path / 'outside-index.jsonl'
        outside.write_bytes(raw_rgb)
        cached.unlink()
        cached.symlink_to(outside)
    else:
        original_directory = cached.parent
        outside = tmp_path / 'outside-cache'
        original_directory.rename(outside)
        original_directory.symlink_to(outside, target_is_directory=True)
    result = build([rgb, depth], tmp_path)
    assert result['localManifestPaths'] == [] and result['errors']
    assert result['errors'][0]['code'] == ('SYNC_ATTACHMENT_CHECKSUM' if corruption == 'bytes' else 'SYNC_SOURCE_LINK')
    assert (rgb.parent / 'video.mkv').read_bytes() == original_video
    assert (rgb.parent / 'index.jsonl').read_bytes() == raw_rgb
