"""Bounded, independent HTTPS uploader. No ROS, serial or robot control imports."""
from __future__ import annotations

import asyncio
import copy
import logging
import json
import math
import os
import random
import sqlite3
import ssl
import threading
import time
import uuid
from urllib.parse import urlsplit
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

import aiohttp

from shared.protocol import MEDIA_KEYS, ProtocolError, encode_envelope, validate_envelope
from .config import ingest_url

LOG = logging.getLogger(__name__)
LIVE_MEDIA_SECONDS = 2.0
DIAGNOSTIC_INTERVAL_SECONDS = 60.0
PROXY_ROUTE_HOLD_SECONDS = 300.0
PROXY_FAILURE_COOLDOWN_SECONDS = 60.0
MAX_RETRY_DELAY_SECONDS = 3.0


class UploadNetworkRoute:
    """Prefer direct TLS; retain a working explicit proxy across queued packets."""
    def __init__(self, url, environ=None, clock=time.monotonic):
        values = os.environ if environ is None else environ
        self.clock = clock
        self.mode = values.get('COLLECTOR_NETWORK_MODE', 'auto').strip().lower()
        if self.mode not in ('auto', 'direct', 'proxy'):
            raise ValueError('COLLECTOR_NETWORK_MODE must be auto, direct or proxy')
        self.proxy_url = None
        self.proxy_until = 0.0
        self.proxy_failed_until = 0.0
        # Local fixtures never consult proxies. Only the explicit HTTPS proxy
        # is considered: no ALL_PROXY, implicit netrc, or environment auth.
        if urlsplit(url).scheme == 'https' and self.mode != 'direct':
            candidate = values.get('HTTPS_PROXY') or values.get('https_proxy') or ''
            try:
                parsed = urlsplit(candidate)
                if (candidate and len(candidate) <= 2048 and not any(ch.isspace() for ch in candidate)
                        and parsed.scheme in ('http', 'https') and parsed.hostname
                        and parsed.path in ('', '/') and not parsed.query and not parsed.fragment):
                    _port = parsed.port  # Reject malformed ports without logging the URL.
                    self.proxy_url = candidate
            except ValueError:
                pass
        if self.mode == 'proxy' and self.proxy_url is None:
            raise ValueError('Proxy mode requires a valid explicit HTTPS_PROXY for HTTPS uploads')

    def proxy_for_request(self):
        now = self.clock()
        if self.mode == 'proxy' or (self.mode == 'auto' and self.proxy_failed_until <= now < self.proxy_until):
            return self.proxy_url
        return None

    def succeeded(self, proxy):
        if proxy is None:
            self.proxy_until = 0.0
        elif self.mode == 'auto':
            self.proxy_until = self.clock() + PROXY_ROUTE_HOLD_SECONDS
            self.proxy_failed_until = 0.0

    def failed(self, proxy, error, *, entry=None):
        # A TLS trust error is never worked around by choosing another route.
        if isinstance(error, (ssl.SSLError, aiohttp.ClientSSLError, aiohttp.ServerFingerprintMismatch)):
            return
        if self.mode != 'auto' or self.proxy_url is None:
            return
        if not isinstance(error, (aiohttp.ClientError, OSError, asyncio.TimeoutError)):
            return
        if (entry is not None and entry.live_age is not None
                and isinstance(error, asyncio.TimeoutError)
                and not isinstance(error, getattr(aiohttp, 'ConnectionTimeoutError', ()))):
            # A live preview can have only a few milliseconds of freshness
            # left. Exhausting that total/read budget does not establish a
            # broken route. Explicit connection failures still fall back;
            # control packets retain their normal connection probes. Older
            # aiohttp versions without a distinct connection timeout class
            # conservatively leave ambiguous media timeouts to those probes.
            return
        if proxy is None:
            if self.clock() < self.proxy_failed_until:
                return
            self.proxy_until = self.clock() + PROXY_ROUTE_HOLD_SECONDS
            LOG.warning('Cloud direct connection unavailable; trying configured proxy on next retry')
        else:
            self.proxy_until = 0.0
            self.proxy_failed_until = self.clock() + PROXY_FAILURE_COOLDOWN_SECONDS
            LOG.warning('Cloud configured proxy unavailable; using direct HTTPS during proxy cooldown')


@dataclass
class Entry:
    key: str
    envelope: dict[str, Any]
    size: int
    generation: int
    monotonic_time: float
    queued_since: float
    live_age: float | None = None


class LatestQueue:
    """Latest item per key, bounded by both count and serialized bytes."""
    def __init__(self, max_keys: int = 12, max_bytes: int = 4 * 1024 * 1024,
                 clock=time.monotonic, wall_clock=time.time):
        self.max_keys, self.max_bytes, self.clock = max_keys, max_bytes, clock
        self.wall_clock = wall_clock
        self.lock = threading.RLock()
        self.entries: OrderedDict[str, Entry] = OrderedDict()
        self.byte_count = self.generation = self.dropped = 0
        self.expired_live = 0
        self.next_lane = 0

    def put(self, key: str, envelope: dict[str, Any]) -> None:
        value = validate_envelope(envelope)
        size = len(encode_envelope(value))
        if size > self.max_bytes:
            raise ValueError('Single entry exceeds bounded queue capacity')
        with self.lock:
            old = self.entries.get(key)
            if old:
                self.byte_count -= old.size
            self.generation += 1
            while ((len(self.entries) + (old is None) > self.max_keys)
                   or self.byte_count + size > self.max_bytes):
                victim = next(pending for pending in self.entries if pending != key)
                removed = self.entries.pop(victim)
                self.byte_count -= removed.size
                self.dropped += 1
            now, wall_now = self.clock(), self.wall_clock()
            payload = value['payload']
            live_age = None
            if (value['kind'] == 'media' and payload['status'] == 'AVAILABLE'
                    and payload['kind'] in ('image', 'cloud')):
                # Use the same conservative lower bounds as the cloud: source
                # age plus time since collection, and known sample/receipt ages.
                # A future source wall clock must not subtract from known age.
                source_age = payload.get('ageS') or 0.0
                ages = [source_age + max(0, wall_now - value['collector_timestamp'])]
                for stamp in (value['message_timestamp'], payload.get('received_timestamp')):
                    if stamp is not None:
                        ages.append(max(0, wall_now - stamp))
                live_age = max(ages)
            # Assigning an existing OrderedDict key preserves its pending turn.
            # Frequent fresh frames must not continually move that camera last.
            self.entries[key] = Entry(key, value, size, self.generation, now,
                                      old.queued_since if old else now, live_age)
            self.byte_count += size

    def _expire(self):
        now = self.clock()
        # Sending a frame whose known age already exceeds the 2 s contract
        # wastes the serial uploader's turn. Status/maps retain their TTL.
        for key, item in list(self.entries.items()):
            ttl = 40 if item.envelope['kind'] == 'media' and item.envelope['payload'].get('kind') == 'map' else 10
            elapsed = max(0, now - item.monotonic_time)
            expired_live = item.live_age is not None and item.live_age + elapsed > LIVE_MEDIA_SECONDS
            if expired_live or elapsed > ttl:
                self.byte_count -= self.entries.pop(key).size
                self.dropped += 1
                self.expired_live += int(expired_live)

    def peek(self) -> Entry | None:
        with self.lock:
            self._expire()
            return copy.deepcopy(next(iter(self.entries.values()), None))

    def next_upload(self) -> Entry | None:
        with self.lock:
            self._expire()
            # One serial source sequence serves all lanes. Give fresh control
            # and observation packets a turn between bulk requests, while every
            # round reserves a turn for media/events/metrics. Rotate that lane
            # on attempt, not just success, so one slow key cannot block others.
            lanes = ('heartbeat', 'observation', None)
            for offset in range(len(lanes)):
                lane_index = (self.next_lane + offset) % len(lanes)
                lane = lanes[lane_index]
                key = (lane if lane in self.entries else None) if lane else next(
                    (key for key in self.entries if key not in lanes[:2]), None)
                if key is None:
                    continue
                self.next_lane = (lane_index + 1) % len(lanes)
                item = self.entries[key]
                self.entries.move_to_end(key)
                return copy.deepcopy(item)
            return None

    def remove(self, entry: Entry, *, expired_live=False) -> None:
        with self.lock:
            current = self.entries.get(entry.key)
            if current and current.generation == entry.generation:
                self.byte_count -= self.entries.pop(entry.key).size
                if expired_live:
                    self.dropped += 1
                    self.expired_live += 1
            elif current:
                # A fresh replacement arrived while the previous version was
                # in flight. Keep it, but let the other pending keys go first.
                self.entries.move_to_end(entry.key)
                current.queued_since = self.clock()

    def stats(self) -> dict[str, int]:
        with self.lock:
            return {'keys': len(self.entries), 'bytes': self.byte_count, 'dropped': self.dropped,
                    'expired_live': self.expired_live}


class UploadDiagnostics:
    """Fixed stream keys and bounded recent timing samples; no payloads/URLs."""
    def __init__(self, media_keys):
        self.lock = threading.Lock()
        self.streams = {}
        for key in ('heartbeat', 'observation', *('media:' + key for key in media_keys if key in MEDIA_KEYS)):
            self.streams[key] = {'attempts': 0, 'accepted': 0, 'bytes': 0, 'last_result': None,
                                 'queue_ms': deque(maxlen=64), 'post_ms': deque(maxlen=64),
                                 'age_on_finish_ms': deque(maxlen=64)}

    def record(self, entry, *, queue_s, post_s, age_on_finish_s, body_bytes, result):
        kind, payload = entry.envelope['kind'], entry.envelope['payload']
        key = 'media:' + payload['key'] if kind == 'media' else kind
        with self.lock:
            item = self.streams.get(key)
            if item is None:
                return
            item['attempts'] += 1
            item['accepted'] += int(result in ('accepted', 'duplicate'))
            item['bytes'] += body_bytes
            item['last_result'] = result
            item['queue_ms'].append(max(0, queue_s) * 1000)
            item['post_ms'].append(max(0, post_s) * 1000)
            if age_on_finish_s is not None:
                item['age_on_finish_ms'].append(max(0, age_on_finish_s) * 1000)

    def snapshot(self):
        with self.lock:
            result = {}
            for key, item in self.streams.items():
                if not item['attempts']:
                    continue
                row = {field: item[field] for field in ('attempts', 'accepted', 'bytes', 'last_result')}
                for field in ('queue_ms', 'post_ms', 'age_on_finish_ms'):
                    values = sorted(item[field])
                    if values:
                        row[field] = {'mean': round(sum(values) / len(values), 1),
                                      'p95': round(values[max(0, math.ceil(len(values) * 0.95) - 1)], 1),
                                      'max': round(values[-1], 1)}
                result[key] = row
            return result


def retry_delay(attempt: int, random_value: float | None = None) -> float:
    """Bounded recovery probes using real pending packets, not synthetic state."""
    value = random.random() if random_value is None else max(0, min(1, random_value))
    return min(MAX_RETRY_DELAY_SECONDS, 0.5 * (2 ** min(attempt, 7))) * (0.75 + value * 0.25)


def upload_timeout(entry: Entry, now: float) -> aiohttp.ClientTimeout | None:
    """Keep a slow bulk request from monopolizing this source's single sequence."""
    kind = entry.envelope['kind']
    seconds = 2.0 if kind == 'heartbeat' else 3.0
    if kind == 'media':
        if entry.envelope['payload'].get('kind') == 'map':
            seconds = 4.0  # Maps have a longer TTL and can be larger than previews.
        elif entry.live_age is not None:
            age = entry.live_age + max(0, now - entry.monotonic_time)
            if age >= LIVE_MEDIA_SECONDS:
                return None  # aiohttp total=0 disables its deadline; never send it.
            seconds = LIVE_MEDIA_SECONDS - age
    return aiohttp.ClientTimeout(total=seconds, connect=min(2.0, seconds))


class Uploader:
    def __init__(self, config, queue: LatestQueue | None = None):
        self.config = config
        self.upload_url = ingest_url(config.cloud_url, config.allow_insecure_local)
        if len(config.token) < 32 or any(ch.isspace() for ch in config.token):
            raise ValueError('Device token must contain at least 32 non-whitespace characters')
        # Hold one latest packet for every enabled stream plus this role's
        # non-media packets. A fixed 12-key cap evicted heartbeat/observation
        # when a primary's 11 media keys were synchronized after reconnect.
        packet_kinds = 2 if config.role == 'primary' else 1
        self.queue = queue if queue is not None else LatestQueue(max_keys=len(config.media_keys) + packet_kinds)
        self.session_id = str(uuid.uuid4())
        self.sequence = 0
        self.started = time.monotonic()
        self.stop_event = threading.Event()
        self.thread = None
        self.connected = False
        self.connection_generation = 0
        self.permanent_error = None
        self.sent = 0
        self.diagnostics = UploadDiagnostics(config.media_keys)
        self.last_diagnostic_log = time.monotonic()
        self.event_ack = None
        self.fault_monitor = None
        self.metric_ack = None
        self.test_control_handler = None

    def enable_events(self, acknowledge, monitor=None):
        # Enable one independent fault batch slot without changing legacy capacity.
        if self.event_ack is None:
            with self.queue.lock:
                self.queue.max_keys += 1
            self.diagnostics.streams['events'] = {'attempts': 0, 'accepted': 0, 'bytes': 0, 'last_result': None,
                'queue_ms': deque(maxlen=64), 'post_ms': deque(maxlen=64), 'age_on_finish_ms': deque(maxlen=64)}
        self.event_ack = acknowledge
        self.fault_monitor = monitor

    def enable_test_metrics(self, acknowledge, control_handler):
        if self.config.role != 'primary':
            raise ValueError('Only the primary collector records tests')
        if self.metric_ack is None:
            with self.queue.lock:
                self.queue.max_keys += 1
            self.diagnostics.streams['test_metrics'] = {
                'attempts':0,'accepted':0,'bytes':0,'last_result':None,
                'queue_ms':deque(maxlen=64),'post_ms':deque(maxlen=64),
                'age_on_finish_ms':deque(maxlen=64)}
        self.metric_ack, self.test_control_handler = acknowledge, control_handler

    def _ack_test_metrics(self, entry):
        if entry.envelope['kind']=='test_metrics' and self.metric_ack is not None:
            self.metric_ack([sample['id'] for sample in entry.envelope['payload']['samples']])

    def _test_control(self, receipt, round_trip_s):
        if self.test_control_handler is None or not isinstance(receipt,dict) or 'test_control' not in receipt:
            return
        try:
            self.test_control_handler(receipt['test_control'],receipt['server_timestamp'],round_trip_s)
        except (TypeError,ValueError,OSError,sqlite3.Error):
            LOG.error('Test recording state synchronization temporarily unavailable')

    def _ack_events(self, entry):
        if entry.envelope['kind'] == 'events' and self.event_ack is not None:
            # A newer queued batch may contain additional IDs: ack only the sent one.
            self.event_ack([event['id'] for event in entry.envelope['payload']['events']])

    def diagnostic_snapshot(self):
        return {'queue': self.queue.stats(), 'streams': self.diagnostics.snapshot()}

    def _log_diagnostics(self):
        now = time.monotonic()
        if now - self.last_diagnostic_log >= DIAGNOSTIC_INTERVAL_SECONDS:
            self.last_diagnostic_log = now
            LOG.info('Cloud upload timings: %s', json.dumps(self.diagnostic_snapshot(), sort_keys=True))

    def enqueue(self, kind: str, topic: str, payload: dict[str, Any], message_timestamp: float | None = None,
                collector_timestamp: float | None = None) -> None:
        if self.config.role == 'cameras' and kind in ('observation','test_metrics'):
            raise ValueError('A camera-only credential cannot upload the primary observation')
        if kind == 'media' and payload.get('key') not in self.config.media_keys:
            raise ValueError('Media key is not enabled for this source')
        packet = {'schema_version': 1, 'robot_id': self.config.robot_id, 'source_id': self.config.source_id,
                  'session_id': self.session_id, 'sequence': 1, 'kind': kind, 'topic': topic,
                  'message_timestamp': message_timestamp,
                  'collector_timestamp': time.time() if collector_timestamp is None else collector_timestamp,
                  'payload': payload}
        key = 'media:' + payload['key'] if kind == 'media' else kind
        self.queue.put(key, packet)
        if kind == 'media' and payload.get('kind') == 'image' and self.fault_monitor is not None:
            try:
                self.fault_monitor.observe_media_status(payload['key'], payload)
            except (ValueError, TypeError, OSError, sqlite3.Error):
                LOG.error('Camera fault capture temporarily unavailable')

    async def _pause(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(0.2, max(0.01, deadline - time.monotonic())))

    async def run(self) -> None:
        attempts = 0
        uncertain_entry = None
        uncertain_mixed = False
        timeout = aiohttp.ClientTimeout(total=3, connect=2)
        route = UploadNetworkRoute(self.upload_url)
        # TLS verification stays enabled for both direct requests and explicit
        # HTTPS CONNECT requests. Never inherit netrc or other proxy settings.
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                                         connector=aiohttp.TCPConnector(ssl=True, limit=1)) as session:
            while not self.stop_event.is_set():
                self._log_diagnostics()
                if self.permanent_error:
                    await self._pause(1)
                    continue
                entry = self.queue.next_upload()
                if entry is None:
                    await self._pause(0.1)
                    continue
                # An unacknowledged attempt does not consume a sequence number. Otherwise
                # a failed first request would make the source's first actual packet #2.
                candidate = self.sequence + 1
                packet = dict(entry.envelope, sequence=candidate, session_id=self.session_id)
                post_started = finished_at = None
                body_bytes = 0
                result = 'transport_error'
                queue_s = max(0, self.queue.clock() - entry.queued_since)
                fresh_age = (None if entry.live_age is None else
                             entry.live_age + max(0, self.queue.clock() - entry.monotonic_time))
                proxy = route.proxy_for_request()
                try:
                    body = encode_envelope(validate_envelope(packet))
                    request_timeout = upload_timeout(entry, self.queue.clock())
                    if request_timeout is None:
                        self.queue.remove(entry, expired_live=True)
                        continue  # Encoding/queue time consumed the frame's real TTL.
                    identity = self.session_id, candidate, entry.key, entry.generation
                    if uncertain_entry is None:
                        uncertain_entry = identity
                    elif identity != uncertain_entry:
                        uncertain_mixed = True
                    body_bytes = len(body)
                    post_started = time.monotonic()
                    async with session.post(self.upload_url, data=body, allow_redirects=False, proxy=proxy,
                                            timeout=request_timeout,
                                            headers={'Authorization': 'Bearer ' + self.config.token,
                                                     'Content-Type': 'application/json'}) as response:
                        # Never dump server body or credentials into logs.
                        code = response.status
                        response_body = await response.content.read(4096)
                        finished_at = time.monotonic()
                        result = 'http_' + str(code)
                        if 200 <= code < 300:
                            route.succeeded(proxy)
                            try:
                                receipt = json.loads(response_body)
                            except (TypeError, ValueError):
                                receipt = None
                            if packet['kind'] in ('events','test_metrics'):
                                if not isinstance(receipt, dict) or receipt.get('accepted') is not True:
                                    self.connected = False
                                    result = 'missing_event_receipt'
                                    await self._pause(1)
                                    continue
                            self._ack_events(entry)
                            self._ack_test_metrics(entry)
                            self._test_control(receipt, finished_at-post_started)
                            result = 'accepted'
                            self.queue.remove(entry)
                            self.sequence = candidate
                            uncertain_entry, uncertain_mixed = None, False
                            self.sent += 1
                            attempts = 0
                            if not self.connected:
                                self.connection_generation += 1
                                LOG.info('Cloud upload connected; latest state synchronization resumed')
                            self.connected = True
                            continue
                        self.connected = False
                        if code == 409:
                            try:
                                conflict = json.loads(response_body)
                                expected = conflict.get('expected_sequence') if isinstance(conflict, dict) else None
                                reason = conflict.get('reason', conflict.get('error')) if isinstance(conflict, dict) else None
                            except (ValueError, TypeError):
                                expected, reason = None, None
                            if reason == 'duplicate' and type(expected) is int and expected == candidate + 1:
                                # Server committed the previous attempt but its response was lost.
                                self.sequence = candidate
                                # Expiry/coalescing may have selected a different
                                # packet for the retry. The duplicate checkpoint
                                # does not acknowledge that replacement payload.
                                if not uncertain_mixed:
                                    self._ack_events(entry)
                                    self._ack_test_metrics(entry)
                                    self.queue.remove(entry)
                                result = 'duplicate_checkpoint' if uncertain_mixed else 'duplicate'
                                uncertain_entry, uncertain_mixed = None, False
                                attempts = 0
                                continue
                            if expected == 1 and candidate > 1:
                                # A newly initialized cloud database needs a fresh source session.
                                self.session_id, self.sequence = str(uuid.uuid4()), 0
                                uncertain_entry, uncertain_mixed = None, False
                                self.connection_generation += 1
                                attempts = 0
                                LOG.warning('Cloud sequence state reset; beginning a fresh upload session')
                                continue
                            self.queue.remove(entry)
                            LOG.warning('Conflicting upload dropped (%s)', str(reason)[:60])
                            await self._pause(1)
                            continue
                        if code in (401, 403):
                            self.permanent_error = 'Device credential or source permission rejected: HTTP ' + str(code)
                            LOG.error('%s; restart after correcting configuration', self.permanent_error)
                            continue
                        if code == 429:
                            try:
                                delay = min(60, max(1, float(response.headers.get('Retry-After', '1'))))
                            except ValueError:
                                delay = retry_delay(attempts)
                            attempts += 1
                            await self._pause(delay)
                            continue
                        if 300 <= code < 500:
                            # Redirects are never followed; malformed/oversize/replayed packets are dropped.
                            self.queue.remove(entry)
                            LOG.warning('Rejected upload dropped: HTTP %s (%s)', code, entry.envelope['kind'])
                            await self._pause(1)
                            continue
                        LOG.warning('Cloud unavailable: HTTP %s', code)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, sqlite3.Error) as exc:
                    self.connected = False
                    route.failed(proxy, exc, entry=entry)
                    LOG.warning('Cloud connection retry via %s: %s',
                                'configured proxy' if proxy else 'direct HTTPS', type(exc).__name__)
                except (ProtocolError, ValueError):
                    self.queue.remove(entry)
                    LOG.error('Invalid pending protocol item dropped')
                    continue
                finally:
                    if post_started is not None:
                        post_s = max(0, (finished_at or time.monotonic()) - post_started)
                        self.diagnostics.record(entry, queue_s=queue_s, post_s=post_s,
                                                age_on_finish_s=None if fresh_age is None else fresh_age + post_s,
                                                body_bytes=body_bytes, result=result)
                await self._pause(retry_delay(attempts))
                attempts += 1

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True, name='cloud-uploader')
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=11)
