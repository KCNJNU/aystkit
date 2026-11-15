"""Large-file numeric/streaming contracts without allocating or storing gigabytes.

Small existing archive tests validate real payload SHA and durable bytes; these
virtual streams exercise large lengths and bounded read/write scheduling.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from collector import archive_pull, archive_upload


@pytest.fixture(autouse=True)
def remove_large_sparse_test_payloads(tmp_path):
    yield
    # pytest retains its last temporary directories. Leave only small evidence,
    # avoiding misleading apparent multi-gigabyte file totals after these tests.
    for path in tmp_path.rglob('*'):
        if not path.is_symlink() and path.is_file() and path.stat().st_size >= 2**31:
            path.unlink()


@pytest.fixture
def remote_size_guard():
    namespace = {}
    # Load only declarations; never run a remote operation or inspect devices.
    exec(archive_pull.REMOTE_FILES.split("request=json.loads(sys.stdin.buffer.read(1048577))", 1)[0], namespace)
    return namespace['valid_file_size']


@pytest.mark.parametrize('size', [2**31 - 1, 2**31, 2**31 + 1, 3 * 2**30, 2**53 - 1])
def test_upload_and_both_ssh_ends_accept_large_exact_integer_sizes(remote_size_guard, size):
    assert archive_upload.valid_file_size(size)
    assert remote_size_guard(size)
    assert archive_pull.MAX_FILE == archive_upload.MAX_FILE_BYTES == 2**53 - 1


@pytest.mark.parametrize('size', [0, -1, True, None, '3221225472', 2.5,
                                  float('nan'), float('inf'), 2**53, 2**53 + 1])
def test_upload_and_remote_size_guards_reject_unrepresentable_or_invalid_sizes(remote_size_guard, size):
    assert not archive_upload.valid_file_size(size)
    assert not remote_size_guard(size)


class CountingDigest:
    def __init__(self):
        self.size = 0
    def update(self, data):
        self.size += len(data)
    def hexdigest(self):
        return f'{self.size:064x}'


@pytest.mark.parametrize('size', [2**31, 3 * 2**30])
def test_large_digest_is_incremental_and_keeps_exact_file_size(monkeypatch, size):
    path = Path('/virtual/large-native-video.mkv')
    block = b'x' * archive_upload.CHUNK_BYTES
    class Stream:
        remaining = size
        calls = 0
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def fileno(self): return 777777
        def read(self, limit):
            assert limit == archive_upload.CHUNK_BYTES
            self.calls += 1
            take = min(self.remaining, limit)
            self.remaining -= take
            return block if take == len(block) else block[:take]
    stream = Stream()
    open_path, fstat = Path.open, archive_upload.os.fstat
    monkeypatch.setattr(Path, 'open', lambda selected, *args, **kwargs:
        stream if selected == path else open_path(selected, *args, **kwargs))
    monkeypatch.setattr(archive_upload.os, 'fstat', lambda fd:
        SimpleNamespace(st_size=size, st_mtime_ns=42) if fd == 777777 else fstat(fd))
    monkeypatch.setattr(archive_upload.hashlib, 'sha256', CountingDigest)
    digest, recorded_size, stamp = archive_upload.ArchiveUploadClient._digest(path)
    assert (digest, recorded_size, stamp) == (f'{size:064x}', size, 42)
    assert stream.remaining == 0
    assert stream.calls == size // archive_upload.CHUNK_BYTES + 1


class Incoming:
    def __init__(self, size):
        self.remaining, self.maximum_read = size, 0
        self.header = json.dumps({'size': size, 'sha256': f'{size:064x}'}).encode() + b'\n'
        self.block = b'x' * archive_pull.BLOCK
    def readline(self, limit):
        assert limit == 16385
        return self.header
    def read(self, limit):
        assert 0 < limit <= archive_pull.BLOCK
        self.maximum_read = max(self.maximum_read, limit)
        take = min(self.remaining, limit)
        self.remaining -= take
        return self.block if take == len(self.block) else self.block[:take]


def receiver(tmp_path, monkeypatch, size):
    config = archive_upload.ArchiveUploadConfig('fixture@example.invalid', 'fixture-password')
    value = archive_pull.FilePuller([], 'fixture-only', tmp_path / 'spool', config,
                                    min_free_bytes=0, max_spool_bytes=size + 1)
    value.root.mkdir()
    stream = Incoming(size)
    process = SimpleNamespace(stdout=stream, wait=lambda **_: 0, poll=lambda: 0, kill=lambda: None)
    timer = SimpleNamespace(cancel=lambda: None, touch=lambda: True)
    monkeypatch.setattr(value, '_process', lambda request: (process, timer))
    monkeypatch.setattr(archive_pull.shutil, 'disk_usage', lambda _: SimpleNamespace(free=size + 1))
    return value, stream


@pytest.mark.parametrize('size', [2**31, 3 * 2**30])
def test_large_ssh_receive_uses_bounded_blocks_and_validates_whole_stream(tmp_path, monkeypatch, size):
    value, stream = receiver(tmp_path, monkeypatch, size)
    destination = value.root / 'video.mkv'
    temporary = destination.with_name(destination.name + '.part')
    open_path = Path.open
    written = [0]
    class VirtualSink:
        # Keep only a real empty staging inode; payload writes are counted in
        # memory so this regression test consumes no multi-gigabyte disk space.
        def __init__(self): self.file = open_path(temporary, 'wb')
        def __enter__(self): return self
        def __exit__(self, *_): self.file.close()
        def write(self, data):
            assert len(data) <= archive_pull.BLOCK
            written[0] += len(data)
            return len(data)
        def flush(self): self.file.flush()
        def fileno(self): return self.file.fileno()
    monkeypatch.setattr(Path, 'open', lambda path, mode='r', *args, **kwargs:
        VirtualSink() if path == temporary and mode == 'wb' else open_path(path, mode, *args, **kwargs))
    monkeypatch.setattr(archive_pull.hashlib, 'sha256', CountingDigest)
    value._receive('fixture-segment', 'video.mkv', destination)
    assert written[0] == size and stream.remaining == 0
    assert stream.maximum_read == archive_pull.BLOCK
    assert destination.exists() and not temporary.exists()


def test_large_ssh_receive_still_enforces_available_spool_quota(tmp_path, monkeypatch):
    value, stream = receiver(tmp_path, monkeypatch, 3 * 2**30)
    value.max_spool_bytes = 2**30
    with pytest.raises(OSError, match='archive spool full'):
        value._receive('fixture-segment', 'video.mkv', value.root / 'video.mkv')
    assert stream.remaining == 3 * 2**30
    assert not list(value.root.iterdir())



class ScheduledTimer:
    def __init__(self, delay, callback):
        self.delay, self.callback, self.cancelled = delay, callback, False
    def start(self): pass
    def cancel(self): self.cancelled = True
    def fire(self): self.callback()  # Can model a callback already racing cancel.


def watchdog():
    clock, timers, kills = [0.0], [], []
    def schedule(delay, callback):
        timer = ScheduledTimer(delay, callback)
        timers.append(timer)
        return timer
    process = SimpleNamespace(kill=lambda: kills.append(clock[0]))
    value = archive_pull._IdleReadWatchdog(process, clock=lambda: clock[0], timer_factory=schedule)
    return value, clock, timers, kills


def test_progress_extends_read_deadline_without_per_block_timer_threads():
    value, clock, timers, kills = watchdog()
    for stamp in range(1, 180):
        clock[0] = stamp
        assert value.touch()
    assert len(timers) == 1
    clock[0] = 180
    timers[0].fire()
    assert not kills and len(timers) == 2 and timers[1].delay == 179
    clock[0] = 358
    assert value.touch()  # Total transfer duration is already beyond old180s.
    clock[0] = 359
    timers[1].fire()
    assert not kills and timers[-1].delay == 179
    value.cancel()


def test_stalled_reader_is_killed_once_and_expired_watchdog_cannot_be_revived():
    value, clock, timers, kills = watchdog()
    clock[0] = 180
    timers[0].fire()
    assert kills == [180]
    assert value.touch() is False
    timers[0].fire()
    assert kills == [180]


def test_cancelled_late_watchdog_callback_cannot_kill_finished_transfer():
    value, clock, timers, kills = watchdog()
    value.cancel()
    assert timers[0].cancelled
    clock[0] = 1000
    timers[0].fire()
    assert not kills and len(timers) == 1
    assert value.touch() is False



@pytest.mark.parametrize('size', [2**31, 3 * 2**30])
def test_large_native_source_files_are_eligible_for_video_and_bag_processing(tmp_path, size):
    from collector import archive_video, archive_bag_compress
    source = tmp_path / 'large-sealed-source.bin'
    with source.open('wb') as output:
        output.truncate(size)  # Sparse metadata test; no payload allocation/read.
    assert archive_video._safe_file(tmp_path, source.name) == source
    assert archive_bag_compress._safe_file(tmp_path, source.name) == source


def test_large_native_bag_seals_without_a_file_size_product_limit(tmp_path):
    from collector.archive_core import ArchiveStore
    size = 3 * 2**30
    class Writer:
        def __init__(self, path):
            self.path = path
            path.mkdir()
        def write(self, topic, kind, raw, timestamp):
            (self.path / 'data.db3').write_bytes(raw)
        def close(self):
            with (self.path / 'data.db3').open('r+b') as output:
                output.truncate(size)
            (self.path / 'metadata.yaml').write_text('rosbag2_bagfile_information: {}\n')
    archive = ArchiveStore(tmp_path, 'fixture-only', writer_factory=Writer,
        min_free_bytes=0, disk_usage=lambda _: SimpleNamespace(free=8 * 2**30), compression=False)
    assert archive.record('fixture', '/fixture', 'fixture/msg/Data', b'x', 1, 100)
    archive.close()
    manifests = list(tmp_path.rglob('archive.json'))
    assert len(manifests) == 1 and not archive.errors
    manifest = json.loads(manifests[0].read_text())
    assert 'bag/data.db3' in manifest['files']
    assert (manifests[0].parent / 'bag/data.db3').stat().st_size == size


def test_large_interrupted_bag_reaches_integrity_check_instead_of_size_rejection(tmp_path, monkeypatch):
    from collector import archive_core
    entry = tmp_path / 'interrupted'
    (entry / 'bag').mkdir(parents=True)
    (entry / 'session.json').write_text(json.dumps({'scope': 'garage', 'source': 'native-ros-cdr'}))
    with (entry / 'bag/data.db3').open('wb') as output:
        output.truncate(3 * 2**30)
    def integrity_check(path):
        raise RuntimeError('reached actual bag integrity checker')
    monkeypatch.setattr(archive_core.sqlite3, 'connect', integrity_check)
    archive = archive_core.ArchiveStore.__new__(archive_core.ArchiveStore)
    with pytest.raises(RuntimeError, match='reached actual bag integrity checker'):
        archive._recover_bag(entry)


def test_hevc_large_source_passes_size_guard_before_codec_option_validation(tmp_path):
    from collector.archive_hevc import compress_rgb_mkv
    source = tmp_path / 'source.mkv'
    with source.open('wb') as output:
        output.truncate(3 * 2**30)
    # Invalid codec options stop immediately after file eligibility; this test
    # never invokes an encoder or reads the sparse multi-gigabyte payload.
    with pytest.raises(ValueError, match='invalid HEVC quality'):
        compress_rgb_mkv(source, tmp_path / 'output.mkv', qp_i=20, qp_p=22,
                         backend='deliberately-invalid-fixture')



def remote_declarations():
    namespace = {}
    exec(archive_pull.REMOTE_FILES.split("request=json.loads(sys.stdin.buffer.read(1048577))", 1)[0], namespace)
    return namespace


def test_sealed_large_video_header_reuses_verified_digest_without_double_read(tmp_path):
    namespace = remote_declarations()
    path = tmp_path / 'video.mkv'
    size = 3 * 2**30
    with path.open('wb') as stream:
        stream.truncate(size)
    with path.open('rb') as real:
        class HeaderOnly:
            def fileno(self): return real.fileno()
            def read(self, limit):
                raise AssertionError('Verified sealed video must not be read before header')
        before, checksum, actual = namespace['transfer_header'](HeaderOnly(), 'video.mkv', {
            'files': ['video.mkv'], 'metadata': {'videoReady': True,
                'videoSha256': 'a' * 64, 'videoBytes': size}})
    assert before.st_size == actual == size and checksum == 'a' * 64
    path.unlink()


@pytest.mark.parametrize('patch', [
    {}, {'videoReady': False}, {'videoSha256': 'invalid'}, {'videoBytes': True},
    {'videoBytes': 1234},
])
def test_unverified_or_legacy_video_header_keeps_real_stream_hashing(tmp_path, patch):
    namespace = remote_declarations()
    path = tmp_path / 'video.mkv'
    payload = b'bounded legacy video payload'
    path.write_bytes(payload)
    metadata = {'videoReady': True, 'videoSha256': 'a' * 64, 'videoBytes': len(payload)}
    if patch:
        metadata.update(patch)
    else:
        metadata = {'videoReady': True}  # Legacy finalizer without output digest.
    with path.open('rb') as stream:
        _, checksum, size = namespace['transfer_header'](stream, 'video.mkv', {
            'files': ['video.mkv'], 'metadata': metadata})
        assert stream.tell() == 0
    assert checksum == namespace['hashlib'].sha256(payload).hexdigest() and size == len(payload)
