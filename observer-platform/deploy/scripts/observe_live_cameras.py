#!/usr/bin/env python3
"""Continuously measure real camera delivery without uploading or controlling anything.

Run inside the ECS verification container with verify_live.py mounted alongside
this file and initial-access credentials mounted read-only at their fixed path.
Only login/logout modify session state. No images or credentials are printed.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import math
import socket
import ssl
import sys
import time

if __package__:
    from .verify_live import (COOKIE, CREDENTIALS, HTTP_CAMERAS, MAX_BODY, ORIGIN, camera_keys_value,
                             VerificationError, credentials, frame_identity,
                             jpeg_dimensions, json, number, read_json, report,
                             require, section, status)
else:
    from verify_live import (COOKIE, CREDENTIALS, HTTP_CAMERAS, MAX_BODY, ORIGIN, camera_keys_value,
                            VerificationError, credentials, frame_identity,
                            jpeg_dimensions, json, number, read_json, report,
                            require, section, status)


def seconds_value(value):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError('seconds must be an integer from 10 to 300') from exc
    if not 10 <= result <= 300:
        raise argparse.ArgumentTypeError('seconds must be an integer from 10 to 300')
    return result


def distribution(values):
    if not values:
        return {'samples': 0, 'min': None, 'median': None, 'p95': None, 'max': None}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {'samples': len(ordered), 'min': ordered[0], 'median': median,
            'p95': ordered[math.ceil(len(ordered) * .95) - 1], 'max': ordered[-1]}


def header_number(headers, name):
    raw = headers.get(name)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise VerificationError('camera_freshness_header') from exc
    require(math.isfinite(value) and value >= 0, 'camera_freshness_header')
    return value


@dataclass
class CameraStats:
    requests: int = 0
    jpeg200: int = 0
    noFrame204: int = 0
    errors: int = 0
    timeouts: int = 0
    serverErrors: int = 0
    clientHttpErrors: int = 0
    transportErrors: int = 0
    incompleteAtDeadline: int = 0
    expiredAtArrival: int = 0
    metadataSamples: int = 0
    metadataAvailable: int = 0
    sequenceAdvances: int = 0
    sessionChanges: int = 0
    metadataStates: dict = field(default_factory=dict)
    jpeg_ages: list = field(default_factory=list)
    metadata_ages: list = field(default_factory=list)
    response_ms: list = field(default_factory=list)
    jpeg_bytes: list = field(default_factory=list)
    dimensions: set = field(default_factory=set)
    identities: set = field(default_factory=set)
    last_identity: tuple | None = None
    last_frame: bytes | None = None  # At most one validated frame; never printed or saved.

    def summary(self):
        counts = {name: getattr(self, name) for name in (
            'requests', 'jpeg200', 'noFrame204', 'errors', 'timeouts', 'serverErrors',
            'clientHttpErrors', 'transportErrors', 'incompleteAtDeadline',
            'expiredAtArrival', 'metadataSamples', 'metadataAvailable',
            'sequenceAdvances', 'sessionChanges')}
        return dict(**counts, distinctFrames=len(self.identities),
                    metadataAvailableRatio=(self.metadataAvailable / self.metadataSamples
                                            if self.metadataSamples else None),
                    metadataStates=self.metadataStates,
                    jpegAgeS=distribution(self.jpeg_ages), metadataAgeS=distribution(self.metadata_ages),
                    responseMs=distribution(self.response_ms), jpegBytes=distribution(self.jpeg_bytes),
                    totalJpegBytes=sum(self.jpeg_bytes), dimensions=sorted(self.dimensions))


async def sample_camera(session, key, stats, deadline):
    import aiohttp

    started = time.monotonic()
    stats.requests += 1
    try:
        timeout = aiohttp.ClientTimeout(total=min(5, max(.001, deadline - started)))
        async with session.get(ORIGIN + f'/camera/{key}.jpg', timeout=timeout,
                               allow_redirects=False) as response:
            require(response.status not in (401, 403), 'camera_authentication')
            if response.status == 204:
                stats.noFrame204 += 1
                stats.last_frame = None
                return
            if 500 <= response.status <= 599:
                stats.errors += 1
                stats.serverErrors += 1
                return
            if 400 <= response.status <= 499:
                stats.errors += 1
                stats.clientHttpErrors += 1
                return
            require(response.status == 200, 'camera_unexpected_http_status')
            require(response.content_type == 'image/jpeg', 'camera_content_type')
            identity = frame_identity(response.headers.get('X-Frame-Sequence'))
            age = header_number(response.headers, 'X-Frame-Age-S')
            ttl = header_number(response.headers, 'X-Frame-TTL-S')
            require(ttl > 0 and age <= ttl, 'camera_freshness_header')
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                require(len(raw) <= MAX_BODY, 'camera_body_size')
            dimensions = jpeg_dimensions(raw)
            previous = stats.last_identity
            if previous is not None:
                if previous[0] == identity[0]:
                    require(identity[1] >= previous[1], 'camera_sequence_regressed')
                    stats.sequenceAdvances += int(identity[1] > previous[1])
                else:
                    stats.sessionChanges += 1
            stats.jpeg200 += 1
            stats.identities.add(identity)
            stats.last_identity = identity
            stats.last_frame = bytes(raw)
            stats.jpeg_ages.append(age)
            stats.jpeg_bytes.append(len(raw))
            stats.dimensions.add(dimensions)
            # Conservative browser-equivalent budget includes the entire request.
            stats.expiredAtArrival += int(age + time.monotonic() - started > ttl)
    except asyncio.TimeoutError:
        if time.monotonic() >= deadline:
            stats.incompleteAtDeadline += 1
        else:
            stats.errors += 1
            stats.timeouts += 1
    except aiohttp.ClientError:
        stats.errors += 1
        stats.transportErrors += 1
    except VerificationError:
        stats.errors += 1
        raise
    finally:
        stats.response_ms.append((time.monotonic() - started) * 1000)


async def camera_worker(session, key, stats, start, deadline, offset):
    next_request = start + offset
    while next_request < deadline:
        await asyncio.sleep(max(0, next_request - time.monotonic()))
        if time.monotonic() >= deadline:
            break
        requested_at = time.monotonic()
        await sample_camera(session, key, stats, deadline)
        # Never overlap requests or burst to catch up after a slow response.
        next_request = max(requested_at + 1, time.monotonic())


async def read_snapshots(ws, cameras, ws_stats, deadline):
    import aiohttp

    previous_timestamp = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            message = await asyncio.wait_for(ws.receive(), timeout=min(5, remaining))
        except asyncio.TimeoutError:
            if time.monotonic() < deadline:
                ws_stats['timeouts'] += 1
            continue
        require(message.type == aiohttp.WSMsgType.TEXT, 'wss_closed_or_non_text')
        byte_count = len(message.data.encode('utf-8'))
        require(byte_count <= MAX_BODY, 'wss_snapshot_size')
        try:
            snapshot = json.loads(message.data)
        except (ValueError, TypeError) as exc:
            raise VerificationError('wss_snapshot_json') from exc
        require(isinstance(snapshot, dict), 'wss_snapshot_object')
        meta = section(snapshot.get('meta'))
        timestamp = number(meta.get('timestamp'))
        require(meta.get('readOnly') is True and timestamp is not None, 'wss_snapshot_metadata')
        require(previous_timestamp is None or timestamp > previous_timestamp, 'wss_timestamp_progress')
        previous_timestamp = timestamp
        ws_stats['jsonBytes'].append(byte_count)
        require(len(ws_stats['jsonBytes']) <= 4000, 'wss_sample_count_bound')
        metadata = section(section(snapshot.get('devices')).get('cameras'))
        for key, stats in cameras.items():
            camera = section(metadata.get(key))
            state = status(camera.get('status'))
            stats.metadataSamples += 1
            stats.metadataAvailable += int(state == 'AVAILABLE')
            stats.metadataStates[state] = stats.metadataStates.get(state, 0) + 1
            age = number(camera.get('ageS'))
            if age is not None and age >= 0:
                stats.metadata_ages.append(age)
            if state == 'AVAILABLE':
                frame_identity(camera.get('frameIdentity'))


async def progress(cameras, started, deadline):
    next_report = started + 20
    while next_report < deadline:
        await asyncio.sleep(max(0, next_report - time.monotonic()))
        report('camera_observation_progress', elapsedS=round(time.monotonic() - started, 1),
               cameras={key: {'jpeg200': item.jpeg200, 'noFrame204': item.noFrame204,
                               'errors': item.errors} for key, item in cameras.items()})
        next_report += 20


async def run_observation(session, seconds, connect_loopback, camera_keys=HTTP_CAMERAS):
    cameras = {key: CameraStats() for key in camera_keys}
    ws_stats = {'connections': 0, 'timeouts': 0, 'jsonBytes': []}
    started = time.monotonic()
    deadline = started + seconds
    tasks = []
    completed = False
    try:
        # Exactly one WSS connection. Only automatic protocol PONGs are sent;
        # never send JSON, binary application messages or camera/control commands.
        async with session.ws_connect('wss://aystkit.cn/ws', origin=ORIGIN,
                autoping=True, heartbeat=None, max_msg_size=MAX_BODY) as ws:
            ws_stats['connections'] += 1
            tasks.append(asyncio.create_task(read_snapshots(ws, cameras, ws_stats, deadline)))
            tasks.extend(asyncio.create_task(camera_worker(session, key, cameras[key],
                         started, deadline, index / len(cameras))) for index, key in enumerate(cameras))
            tasks.append(asyncio.create_task(progress(cameras, started, deadline)))
            await asyncio.gather(*tasks)
        require(len(ws_stats['jsonBytes']) >= 2, 'wss_insufficient_samples')
        require(all(item.sequenceAdvances > 0 for item in cameras.values()), 'camera_no_advancing_frames')
        completed = True
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        drops = ws_stats['timeouts'] > 0 or any(
            item.noFrame204 or item.errors or item.expiredAtArrival
            or item.metadataAvailable < item.metadataSamples for item in cameras.values())
        report('camera_observation',
               status=('COMPLETED_WITH_DROPS' if drops else 'COMPLETED') if completed else 'FAILED',
               elapsedS=round(time.monotonic() - started, 3), requestedSeconds=seconds,
               selectedCameraKeys=list(cameras),
               samplingScope=('ECS loopback TCP with real domain TLS; excludes user Internet/Wi-Fi'
                              if connect_loopback else 'ECS to public domain; excludes user Internet/Wi-Fi'),
               ws={'connections': ws_stats['connections'], 'timeouts': ws_stats['timeouts'],
                   'jsonBytes': distribution(ws_stats['jsonBytes'])},
               cameras={key: item.summary() for key, item in cameras.items()})
        for item in cameras.values():
            item.last_frame = None


async def observe(args):
    import aiohttp

    class LoopbackResolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            require(host == 'aystkit.cn', 'resolver_hostname')
            return [{'hostname': host, 'host': '127.0.0.1', 'port': port,
                     'family': socket.AF_INET, 'proto': socket.IPPROTO_TCP,
                     'flags': socket.AI_NUMERICHOST}]

        async def close(self):
            pass

    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(),
        resolver=LoopbackResolver() if args.connect_loopback else None)
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=10), headers={'Origin': ORIGIN},
            cookie_jar=aiohttp.CookieJar()) as session:
        logged_in, issued_cookie = False, None
        try:
            login = credentials(CREDENTIALS)
            try:
                async with session.post(ORIGIN + '/api/login', json=login,
                                        allow_redirects=False) as response:
                    require(response.status == 200, 'login_http_status')
                    logged_in = True
                    cookie = response.cookies.get(COOKIE)
                    issued_cookie = cookie.value if cookie else None
                    require(cookie is not None and issued_cookie and cookie['secure']
                            and cookie['httponly'] and cookie['samesite'].lower() == 'strict',
                            'login_cookie_security')
                    require((await read_json(response)).get('authenticated') is True,
                            'login_authenticated')
            finally:
                login.clear()
            await run_observation(session, args.seconds, args.connect_loopback, args.camera_keys)
        finally:
            try:
                if logged_in:
                    async with session.post(ORIGIN + '/api/logout', allow_redirects=False) as response:
                        require(response.status == 200, 'logout_http_status')
                        require((await read_json(response)).get('authenticated') is False,
                                'logout_authenticated_false')
                    session.cookie_jar.clear()
                    if issued_cookie:
                        async with session.get(ORIGIN + '/api/session', cookies={COOKIE: issued_cookie},
                                               allow_redirects=False) as response:
                            require(response.status == 401, 'logout_session_revoked')
                    report('camera_observation_logout', revoked=True)
            finally:
                issued_cookie = None
                session.cookie_jar.clear()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=seconds_value, default=120)
    parser.add_argument('--camera-keys', type=camera_keys_value, default=HTTP_CAMERAS,
                        help='Comma-separated known HTTP camera keys; legacy three by default')
    parser.add_argument('--connect-loopback', action='store_true',
                        help='Connect TCP to loopback, preserving domain URL, SNI and TLS validation')
    args = parser.parse_args(argv)
    try:
        asyncio.run(observe(args))
    except VerificationError as exc:
        report('camera_observation_error', reason=str(exc))
        return 1
    except KeyboardInterrupt:
        report('camera_observation_error', reason='interrupted')
        return 130
    except Exception as exc:
        report('camera_observation_error', reason=type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
