"""Wire-level bounded upload pacing; metadata, resume and durable ACK stay valid."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from collector import archive_upload_only
from collector.archive_upload import (ArchiveUploadConfig, ArchiveUploadError,
    LocalWarehouseApi, RateLimitedBody, UploadRateLimiter, UPLOAD_SEND_BYTES,
    upload_rate)
from tests import test_archive_upload as upload_fixture


class Clock:
    def __init__(self):
        self.now = 10.0
        self.waits = []

    def __call__(self):
        return self.now

    def wait(self, seconds):
        assert 0 < seconds <= .1
        self.waits.append(seconds)
        self.now += seconds


def test_pacing_budget_covers_every_part_retry_and_file_without_idle_bursts():
    clock = Clock()
    limiter = UploadRateLimiter(UPLOAD_SEND_BYTES, clock=clock, wait=clock.wait)
    payload = b'x' * (UPLOAD_SEND_BYTES * 2 + 7)
    body = RateLimitedBody(payload, limiter)
    assert len(body) == len(payload)
    first = list(body)
    assert b''.join(first) == payload and max(map(len, first)) == UPLOAD_SEND_BYTES
    assert clock.now == pytest.approx(10 + len(payload) / UPLOAD_SEND_BYTES)
    # Retrying the same HTTP part retransmits every byte and pays again.
    assert b''.join(body) == payload
    assert clock.now == pytest.approx(10 + 2 * len(payload) / UPLOAD_SEND_BYTES)
    # A later file shares the budget; long idle time cannot create a burst.
    clock.now += 100
    before = clock.now
    assert b''.join(RateLimitedBody(b'y' * UPLOAD_SEND_BYTES, limiter)) == b'y' * UPLOAD_SEND_BYTES
    assert clock.now - before == pytest.approx(1)


def test_explicit_unlimited_still_yields_bounded_bytes_and_honors_cancellation():
    def unexpected(_):
        pytest.fail('unlimited mode slept')
    stopped = threading.Event()
    limiter = UploadRateLimiter(0, wait=unexpected, stopping=stopped)
    payload = b'z' * (UPLOAD_SEND_BYTES + 3)
    stream = iter(RateLimitedBody(payload, limiter))
    assert next(stream) == payload[:UPLOAD_SEND_BYTES]
    stopped.set()
    with pytest.raises(ArchiveUploadError, match='ARCHIVE_UPLOAD_CANCELLED'):
        next(stream)


def test_waiting_upload_can_be_cancelled_without_waiting_for_full_part():
    stopped = threading.Event()
    limiter = UploadRateLimiter(1, stopping=stopped)
    result = []
    def send():
        try:
            list(RateLimitedBody(b'x' * UPLOAD_SEND_BYTES, limiter))
        except ArchiveUploadError as error:
            result.append(error.code)
    worker = threading.Thread(target=send)
    worker.start()
    stopped.set()
    worker.join(timeout=.5)
    assert not worker.is_alive()
    assert result == ['ARCHIVE_UPLOAD_CANCELLED']


@pytest.mark.parametrize('value', [-1, True, 1.5, '1.0', '-1', '', ' 1024', 'NaN', 1024 ** 3 + 1])
def test_invalid_rate_is_rejected(value):
    with pytest.raises(ArchiveUploadError, match='ARCHIVE_UPLOAD_RATE_INVALID'):
        upload_rate(value)


def test_foreground_literal_env_and_daemon_env_use_same_default_and_override(monkeypatch):
    values = {'ARCHIVE_DBPROG_EMAIL': 'fixture@example.invalid', 'ARCHIVE_DBPROG_PASSWORD': 'fixture-password'}
    for key in ('ARCHIVE_UPLOAD_CONFIG', 'ARCHIVE_UPLOAD_BYTES_PER_SECOND'):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert archive_upload_only.upload_config(values).upload_bytes_per_second == 1024 ** 2
    assert ArchiveUploadConfig.from_environment().upload_bytes_per_second == 1024 ** 2
    for rate in ('0', '524288'):
        monkeypatch.setenv('ARCHIVE_UPLOAD_BYTES_PER_SECOND', rate)
        assert ArchiveUploadConfig.from_environment().upload_bytes_per_second == int(rate)
        assert archive_upload_only.upload_config({**values, 'ARCHIVE_UPLOAD_BYTES_PER_SECOND': rate}).upload_bytes_per_second == int(rate)


def test_actual_urllib_stream_has_fixed_length_correct_bytes_and_paces_before_send():
    payload = bytes(range(251)) * 2200  # 552200 bytes, several socket blocks.
    observed, received = {}, []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_args):
            pass
        def do_PUT(self):
            observed.update(length=self.headers.get('Content-Length'), transfer=self.headers.get('Transfer-Encoding'))
            remaining = int(observed['length'])
            while remaining:
                block = self.rfile.read(min(UPLOAD_SEND_BYTES, remaining))
                if not block:
                    raise AssertionError('truncated HTTP body')
                received.append((time.monotonic(), block))
                remaining -= len(block)
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        rate = 1024 ** 2
        api = LocalWarehouseApi(f'http://127.0.0.1:{server.server_port}/api', upload_bytes_per_second=rate)
        started = time.monotonic()
        assert api.request('/v1/uploads/fixture/parts/1', 'PUT', body=payload,
            headers={'Content-Type': 'application/octet-stream'}) == {}
        assert observed == {'length': str(len(payload)), 'transfer': None}
        assert hashlib.sha256(b''.join(block for _, block in received)).digest() == hashlib.sha256(payload).digest()
        elapsed = received[-1][0] - started
        assert elapsed >= len(payload) / rate * .85
        assert received[0][0] - started >= UPLOAD_SEND_BYTES / rate * .75
        assert max(len(block) for _, block in received) <= UPLOAD_SEND_BYTES
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_metadata_requests_bypass_rate_budget_and_only_binary_put_streams():
    class Limiter:
        def before_send(self, _size):
            pytest.fail('metadata consumed upload budget')
    class Opener:
        def open(self, request, **_options):
            assert isinstance(request.data, (bytes, type(None)))
            return Reply()
    class Reply:
        status = 200
        headers = __import__('email.message', fromlist=['Message']).Message()
        def read(self, _limit): return b'{}'
        def __enter__(self): return self
        def __exit__(self, *_args): pass
    api = LocalWarehouseApi(opener=Opener(), upload_limiter=Limiter())
    api.request('/v1/auth/session')
    api.request('/v1/uploads/fixture/complete', 'POST')
    api.request('/v1/uploads', 'POST', payload={'fileSize': 10})


def test_lost_http_reply_resumes_existing_part_without_retransmit_or_false_ack():
    fixture = upload_fixture.ArchiveUploadTests()
    fixture.setUp()
    try:
        clock = Clock()
        limiter = UploadRateLimiter(1024 ** 2, clock=clock, wait=clock.wait)
        fixture.client.api.upload_limiter = limiter
        fixture.api.disconnect_part_once = True
        with pytest.raises(ArchiveUploadError, match='ARCHIVE_API_UNAVAILABLE'):
            fixture.client.upload_segment(fixture.segment)
        first_paid = clock.now - 10
        assert first_paid > 0
        assert not (fixture.segment / 'uploaded.json').exists()
        # The server retained the first part before losing its reply. Resume
        # must use that receipt, pay only for the other file, and await finish.
        assert fixture.client.upload_segment(fixture.segment)['state'] == 'pending'
        total_bytes = sum((fixture.segment / name).stat().st_size for name in fixture.manifest['files'])
        assert clock.now - 10 == pytest.approx(total_bytes / 1024 ** 2)
        fixture.api.finish()
        paid = clock.now
        assert fixture.client.upload_segment(fixture.segment)['state'] == 'complete'
        assert clock.now == paid
        assert (fixture.segment / 'uploaded.json').exists()
        assert all((fixture.segment / name).is_file() for name in fixture.manifest['files'])
    finally:
        fixture.tearDown()


def test_mid_part_cancel_retains_source_and_does_not_create_acknowledgement():
    fixture = upload_fixture.ArchiveUploadTests()
    fixture.setUp()
    try:
        stop = threading.Event()
        class Cancel:
            def before_send(self, _size):
                stop.set()
                raise ArchiveUploadError('ARCHIVE_UPLOAD_CANCELLED')
        fixture.client.api.upload_limiter = Cancel()
        original = {name: (fixture.segment / name).read_bytes() for name in fixture.manifest['files']}
        with pytest.raises(ArchiveUploadError, match='ARCHIVE_UPLOAD_CANCELLED'):
            fixture.client.upload_segment(fixture.segment)
        assert not (fixture.segment / 'uploaded.json').exists()
        assert (fixture.segment / 'upload-state.json').is_file()
        assert all((fixture.segment / name).read_bytes() == data for name, data in original.items())
    finally:
        fixture.tearDown()
