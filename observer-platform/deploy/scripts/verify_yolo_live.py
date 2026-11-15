#!/usr/bin/env python3
"""ECS-only live five-camera YOLO acceptance; no pixels or secrets are emitted.

Mount initial-access.json read-only at /run/observer-initial-access.json and this
script plus verify_live.py/verify_native_video.py at /verify. Credentials remain
inside ECS memory. --proxy-port reaches public TLS through an IPC proxy, while
the default uses ECS loopback TLS and cannot establish viewer WAN performance.
Only the verifier's login/logout changes server state. No capture or control.
--without-native-streams measures metadata and JPEG delivery alongside existing
viewers; it does not test an additional viewer's four native video streams.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import ssl
import statistics
import sys
import time

import aiohttp

if __package__:
    from .verify_live import COOKIE, CREDENTIALS, ORIGIN, credentials, jpeg_dimensions, require
    from .verify_native_video import LoopbackResolver
else:
    from verify_live import COOKIE, CREDENTIALS, ORIGIN, credentials, jpeg_dimensions, require
    from verify_native_video import LoopbackResolver

try:
    from shared.detections import (DETECTION_KEYS, DETECTION_MAX_BYTES, decode_detections,
                                   sanitize_detections, video_session)
except ImportError:
    # Fixed deployed image path, or this script's source checkout for --help.
    project = Path('/app') if Path('/app/shared/detections.py').is_file() else Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(project))
    from shared.detections import (DETECTION_KEYS, DETECTION_MAX_BYTES, decode_detections,
                                   sanitize_detections, video_session)

NATIVE = tuple(key for key in DETECTION_KEYS if key != 'color')
JPEG_KEYS = ('color', 'depth')
MAX_JPEG = 500_000
MAX_VIDEO = 16 * 1024 * 1024


def summary(values):
    values = sorted(value for value in values if type(value) in (int, float) and math.isfinite(value))
    if not values:
        return None
    return {'min': round(values[0], 3), 'mean': round(statistics.mean(values), 3),
            'p95': round(values[max(0, math.ceil(len(values) * .95) - 1)], 3),
            'max': round(values[-1], 3)}


def network():
    """Host-network ECS interface, not Docker veth or duplicate loopback bytes."""
    for row in Path('/proc/net/dev').read_text().splitlines():
        if row.strip().startswith('eth0:'):
            fields = row.split(':', 1)[1].split()
            return time.monotonic(), int(fields[0]), int(fields[8])
    return None


def header_number(headers, field):
    try:
        value = float(headers[field])
    except (KeyError, ValueError, TypeError):
        raise ValueError('invalid camera freshness header') from None
    require(math.isfinite(value) and value >= 0, 'jpeg_freshness_header')
    return value


async def bounded(response, maximum):
    require(response.content_length is None or response.content_length <= maximum, 'body_size')
    raw = bytearray()
    async for chunk in response.content.iter_chunked(16384):
        raw.extend(chunk)
        require(len(raw) <= maximum, 'body_size')
    return bytes(raw)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=20, choices=range(10, 61))
    parser.add_argument('--proxy-port', type=int)
    parser.add_argument('--require-aruco', action='store_true', help='Require five marker channels and their ROS-derived observation')
    parser.add_argument('--without-native-streams', action='store_true',
                        help='Leave existing viewers undisturbed; test metadata and JPEG only')
    args = parser.parse_args()
    if args.proxy_port is not None and not 1024 <= args.proxy_port <= 65535:
        parser.error('proxy-port must be in 1024..65535')
    proxy = f'http://127.0.0.1:{args.proxy_port}' if args.proxy_port else None
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(),
                                    **({} if proxy else {'resolver': LoopbackResolver()}))
    timeout = aiohttp.ClientTimeout(total=5, connect=3, sock_read=3)
    async with aiohttp.ClientSession(connector=connector, trust_env=False, timeout=timeout,
            headers={'Origin': ORIGIN}, cookie_jar=aiohttp.CookieJar()) as session:
        async with session.get(ORIGIN + '/native-camera/detections', proxy=proxy,
                               allow_redirects=False) as response:
            require(response.status == 401, 'anonymous_detections_denied')
        login, issued = credentials(CREDENTIALS), None
        try:
            async with session.post(ORIGIN + '/api/login', json=login, proxy=proxy,
                                    allow_redirects=False) as response:
                require(response.status == 200, 'yolo_login')
                cookie = response.cookies.get(COOKIE)
                require(cookie is not None, 'yolo_session_cookie')
                issued = cookie.value
        finally:
            login.clear()

        rows = {key: {'status': Counter(), 'ids': set(), 'age': [], 'inference': [],
                      'boxes': [], 'session_matches': 0, 'session_mismatches': 0,
                      'image_matches': set(), 'progress': 0, 'last_counter': None,
                      'inference_sessions': set(), 'marker_counts': [], 'marker_ids': set(), 'marker_frames': 0} for key in DETECTION_KEYS}
        jpeg_rows = {key: {'http': Counter(), 'bytes': 0, 'latency': [], 'remaining_ttl': [],
                          'dimensions': set(), 'hashes': set(), 'etag': None, 'digest': None,
                          'identity': None, 'expired': 0, 'errors': Counter()} for key in JPEG_KEYS}
        streams = {key: {'http': None, 'session': None, 'bytes': 0, 'fragments': 0,
                         'max_gap_s': 0, 'initialization': False, 'stop': None} for key in NATIVE}
        latest_results = {key: [] for key in DETECTION_KEYS}
        pending_images, seen_images = {}, set()
        unmatched_images = 0
        metadata_bytes, metadata_encoded_bytes, metadata_samples, metadata_latency = 0, 0, 0, []
        metadata_encodings = Counter()
        errors, failures = Counter(), []
        aruco_observations = {'states': Counter(), 'cameras': {key: Counter() for key in DETECTION_KEYS}, 'ids': {key: set() for key in DETECTION_KEYS}}
        started = time.monotonic()
        deadline = started + args.seconds
        first_network = network()
        network_samples = []
        network_before = first_network
        workers = []

        def match_images():
            nonlocal unmatched_images
            now = time.monotonic()
            available = {result['imageId'] for result in latest_results['color']}
            for digest, expires in list(pending_images.items()):
                if now >= expires:
                    unmatched_images += 1
                    del pending_images[digest]
                elif digest in available:
                    rows['color']['image_matches'].add(digest)
                    del pending_images[digest]

        async def metadata():
            nonlocal metadata_bytes, metadata_encoded_bytes, metadata_samples, network_before
            began = time.monotonic()
            try:
                async with session.get(ORIGIN + '/native-camera/detections', proxy=proxy,
                                       allow_redirects=False) as response:
                    require(response.status == 200 and response.content_type == 'application/json',
                            'detections_response')
                    require(response.headers.get('Cache-Control') == 'no-store', 'detections_cache')
                    raw = await bounded(response, DETECTION_MAX_BYTES)
                    encoding = response.headers.get('Content-Encoding', 'identity')
                    require(encoding in ('gzip', 'identity'), 'detections_content_encoding')
                    # aiohttp transparently decompresses the body but preserves
                    # the wire Content-Length for this finite metadata response.
                    wire_bytes = response.content_length
                    require(type(wire_bytes) is int and 0 <= wire_bytes <= DETECTION_MAX_BYTES,
                            'detections_wire_size')
                    metadata_encoded_bytes += wire_bytes
                    metadata_encodings[encoding] += 1
                elapsed = (time.monotonic() - began) * 1000
                value = decode_detections(raw)
                require(sanitize_detections(value) == value, 'detections_public_schema')
                metadata_bytes += len(raw)
                metadata_samples += 1
                metadata_latency.append(elapsed)
                for key in DETECTION_KEYS:
                    row, current = rows[key], value['cameras'][key]
                    row['status'][current['status']] += 1
                    latest_results[key] = [item for item in current['results'] if item['ageMs'] + elapsed < 5000]
                    for item in latest_results[key]:
                        identity = item['id']
                        if identity not in row['ids']:
                            source_session, counter_text = identity.split(':')
                            counter = int(counter_text)
                            if source_session in row['inference_sessions'] and row['last_counter'] is not None:
                                row['progress'] += counter > row['last_counter']
                            row['inference_sessions'].add(source_session)
                            row['last_counter'] = counter
                            row['ids'].add(identity)
                            row['age'].append(item['ageMs'] + elapsed)
                            row['inference'].append(item['inferenceMs'])
                            row['boxes'].append(len(item['boxes']))
                            if args.require_aruco:
                                require(isinstance(item.get('markers'), list), 'aruco_markers_present')
                            markers = item.get('markers', [])
                            row['marker_frames'] += 'markers' in item
                            row['marker_counts'].append(len(markers))
                            row['marker_ids'].update(marker['id'] for marker in markers)
                        if key in NATIVE and streams[key]['session'] is not None:
                            field = 'session_matches' if item['streamSession'] == streams[key]['session'] else 'session_mismatches'
                            row[field] += 1
                match_images()
                current_network = network()
                if current_network is not None and network_before is not None:
                    delta = current_network[0] - network_before[0]
                    if delta > 0:
                        network_samples.append(((current_network[1] - network_before[1]) * 8 / delta / 1e6,
                                                (current_network[2] - network_before[2]) * 8 / delta / 1e6))
                    network_before = current_network
            except asyncio.CancelledError:
                raise
            except Exception as error:
                errors[type(error).__name__] += 1
                for key in DETECTION_KEYS:
                    latest_results[key] = []

        async def aruco_observation():
            try:
                async with session.get(ORIGIN + '/snapshot.json', proxy=proxy, allow_redirects=False) as response:
                    require(response.status == 200, 'aruco_observation_response')
                    value = json.loads(await bounded(response, 2 * 1024 * 1024))
                preview = value.get('devices', {}).get('visionDocking', {}).get('arucoPreview', {})
                aruco_observations['states'][str(preview.get('status'))] += 1
                for key in DETECTION_KEYS:
                    row = preview.get('cameras', {}).get(key, {})
                    aruco_observations['cameras'][key][str(row.get('status'))] += 1
                    if row.get('status') == 'AVAILABLE':
                        require(row.get('dictionary') == 'DICT_5X5_100', 'aruco_observation_dictionary')
                        require(isinstance(row.get('ids'), list), 'aruco_observation_ids')
                        require(all(type(item) is int and 0 <= item <= 99 for item in row['ids']), 'aruco_observation_id_range')
                        aruco_observations['ids'][key].update(row['ids'])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                errors['aruco_observation:' + type(error).__name__] += 1

        async def jpeg(key):
            row, began = jpeg_rows[key], time.monotonic()
            validator = row['etag']
            try:
                async with session.get(ORIGIN + '/camera/' + key + '.jpg', proxy=proxy,
                        headers={'If-None-Match': validator} if validator else {},
                        allow_redirects=False) as response:
                    row['http'][str(response.status)] += 1
                    require(response.status in (200, 304), 'jpeg_available')
                    digest = response.headers.get('X-Frame-Image-SHA256')
                    identity = response.headers.get('X-Frame-Sequence')
                    if response.status == 200:
                        require(response.content_type == 'image/jpeg', 'jpeg_type')
                        raw = await bounded(response, MAX_JPEG)
                        require(hashlib.sha256(raw).hexdigest() == digest, 'jpeg_exact_hash')
                        row['bytes'] += len(raw)
                        row['dimensions'].add(tuple(jpeg_dimensions(raw)))
                        row['digest'], row['identity'] = digest, identity
                        row['etag'] = response.headers.get('ETag')
                        row['hashes'].add(digest)
                    else:
                        require(validator and response.headers.get('ETag') == validator
                                and digest == row['digest'] and identity == row['identity'], 'jpeg_304_identity')
                    remaining = (header_number(response.headers, 'X-Frame-TTL-S')
                                 - header_number(response.headers, 'X-Frame-Age-S')
                                 - (time.monotonic() - began))
                    row['remaining_ttl'].append(remaining)
                    row['expired'] += remaining <= 0
                    if key == 'color' and remaining > 0 and digest not in seen_images:
                        seen_images.add(digest)
                        pending_images[digest] = time.monotonic() + 1
                    match_images()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                row['errors'][type(error).__name__] += 1
                row['etag'] = None
            finally:
                row['latency'].append((time.monotonic() - began) * 1000)

        async def poll(callback, interval):
            due = time.monotonic()
            while time.monotonic() < deadline:
                await callback()
                due = max(due + interval, time.monotonic())
                await asyncio.sleep(min(max(0, due - time.monotonic()), max(0, deadline - time.monotonic())))

        async def video(key):
            row = streams[key]
            last_fragment = None
            try:
                async with session.get(ORIGIN + '/native-camera/' + key + '/video.mp4', proxy=proxy,
                        timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=3),
                        allow_redirects=False) as response:
                    row['http'] = response.status
                    require(response.status == 200 and response.content_type == 'video/mp4', 'video_response')
                    row['session'] = video_session(response.headers.get('X-Video-Session'))
                    require(row['session'] is not None, 'video_session_identity')
                    first_box = True
                    while time.monotonic() < deadline:
                        header = await response.content.readexactly(8)
                        size, kind = int.from_bytes(header[:4], 'big'), header[4:]
                        header_size = 8
                        if size == 1:
                            size = int.from_bytes(await response.content.readexactly(8), 'big')
                            header_size = 16
                        require(header_size <= size <= 2 * 1024 * 1024 and row['bytes'] + size <= MAX_VIDEO,
                                'video_size_limit')
                        require(kind in (b'ftyp', b'moov', b'moof', b'mdat', b'free', b'skip', b'wide', b'styp', b'sidx'),
                                'video_box_type')
                        require(not first_box or kind == b'ftyp', 'video_initial_type')
                        first_box = False
                        remaining = size - header_size
                        while remaining:
                            amount = min(remaining, 65536)
                            await response.content.readexactly(amount)
                            remaining -= amount
                        row['bytes'] += size
                        if kind == b'moov':
                            row['initialization'] = True
                        if kind == b'moof':
                            require(row['initialization'], 'video_initialized_before_media')
                            now = time.monotonic()
                            if last_fragment is not None:
                                row['max_gap_s'] = max(row['max_gap_s'], now - last_fragment)
                            last_fragment = now
                            row['fragments'] += 1
                    row['stop'] = 'window_complete'
            except asyncio.CancelledError:
                row['stop'] = 'window_complete'
                raise
            except asyncio.IncompleteReadError:
                row['stop'] = 'early_eof'
            except Exception as error:
                row['stop'] = type(error).__name__

        try:
            workers = [asyncio.create_task(poll(metadata, .5)),
                       *(asyncio.create_task(poll(lambda key=key: jpeg(key), .25)) for key in JPEG_KEYS)]
            if args.require_aruco:
                workers.append(asyncio.create_task(poll(aruco_observation, 1.0)))
            if not args.without_native_streams:
                workers.extend(asyncio.create_task(video(key)) for key in NATIVE)
            await asyncio.sleep(max(0, deadline - time.monotonic()))
        finally:
            for task in workers:
                task.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            match_images()
            async with session.post(ORIGIN + '/api/logout', proxy=proxy, allow_redirects=False) as response:
                require(response.status == 200, 'yolo_logout')
            session.cookie_jar.clear()
            async with session.get(ORIGIN + '/native-camera/detections', proxy=proxy,
                    cookies={COOKIE: issued}, allow_redirects=False) as response:
                require(response.status == 401, 'revoked_detections_denied')

        public_rows = {}
        for key, row in rows.items():
            if row['status']['AVAILABLE'] == 0 or len(row['ids']) < 2 or row['progress'] < 1:
                failures.append('detections_not_progressing:' + key)
            if (not args.without_native_streams and key in NATIVE
                    and (row['session_matches'] == 0 or row['session_mismatches'])):
                failures.append('video_session_mismatch:' + key)
            public_rows[key] = {'states': dict(row['status']), 'uniqueResults': len(row['ids']),
                'progressEvents': row['progress'], 'sourceSessions': len(row['inference_sessions']),
                'ageAtReceiptMs': summary(row['age']), 'inferenceMs': summary(row['inference']),
                'boxCount': summary(row['boxes']), 'markerCount': summary(row['marker_counts']),
                'arucoIds': sorted(row['marker_ids']), 'framesWithMarkerField': row['marker_frames'],
                'nativeSessionMatches': row['session_matches'],
                'nativeSessionMismatches': row['session_mismatches']}
        matched = len(rows['color']['image_matches'])
        if matched < 2:
            failures.append('color_exact_images_not_matched')
        public_jpeg = {}
        for key, row in jpeg_rows.items():
            if row['errors'] or row['expired'] or len(row['hashes']) < 2:
                failures.append('jpeg_not_stable:' + key)
            public_jpeg[key] = {'http': dict(row['http']), 'totalBytes': row['bytes'],
                'uniqueImages': len(row['hashes']), 'dimensions': sorted(row['dimensions']),
                'responseMs': summary(row['latency']), 'remainingTtlS': summary(row['remaining_ttl']),
                'expiredAtArrival': row['expired'], 'errors': dict(row['errors'])}
        public_streams = {}
        for key, row in (() if args.without_native_streams else streams.items()):
            if (row['http'] != 200 or not row['initialization'] or row['fragments'] < 5
                    or row['stop'] != 'window_complete' or row['max_gap_s'] >= 3):
                failures.append('video_not_stable:' + key)
            public_streams[key] = {name: row[name] for name in ('http', 'bytes', 'fragments', 'initialization', 'stop')}
            public_streams[key]['maxFragmentGapS'] = round(row['max_gap_s'], 3)
        if args.require_aruco:
            if aruco_observations['states']['AVAILABLE'] < 2:
                failures.append('aruco_ros_observation_not_available')
            for key in DETECTION_KEYS:
                if rows[key]['marker_frames'] < 2:
                    failures.append('aruco_frames_missing:' + key)
                if aruco_observations['cameras'][key]['AVAILABLE'] < 2:
                    failures.append('aruco_ros_camera_not_available:' + key)
        if errors:
            failures.append('detection_transport_errors')
        average = None
        if first_network is not None and network_before is not None and network_before[0] > first_network[0]:
            delta = network_before[0] - first_network[0]
            average = {'receiveMbps': round((network_before[1] - first_network[1]) * 8 / delta / 1e6, 3),
                       'transmitMbps': round((network_before[2] - first_network[2]) * 8 / delta / 1e6, 3)}
        result = {'status': 'PASS' if not failures else 'FAIL', 'failures': failures,
            'scope': 'PUBLIC_TLS_VIA_IPC_PROXY' if proxy else 'ECS_LOOPBACK_TLS_NOT_VIEWER_WAN',
            'nativeStreamsRequested': not args.without_native_streams,
            'scopeDetails': ('METADATA_AND_JPEG_ONLY_NO_NEW_NATIVE_PLAYBACK_ACCEPTANCE'
                             if args.without_native_streams else 'FOUR_NATIVE_STREAMS_AND_METADATA_AND_JPEG'),
            'windowS': args.seconds, 'metadataSamples': metadata_samples, 'metadataBytes': metadata_bytes,
            'metadataEncodedBytes': metadata_encoded_bytes, 'metadataContentEncoding': dict(metadata_encodings),
            'metadataResponseMs': summary(metadata_latency), 'metadataErrors': dict(errors),
            'detections': public_rows, 'jpeg': public_jpeg, 'nativeStreams': public_streams,
            'arucoRequired': args.require_aruco, 'arucoObservation': aruco_observations,
            'colorExactImageMatches': matched, 'colorImagesWithoutMatchWithin1S': unmatched_images,
            'colorImagesPendingAtDeadline': len(pending_images),
            'colorExactImageMatchRatio': round(matched / len(seen_images), 3) if seen_images else None,
            'ecsEth0Average': average,
            'ecsEth0ReceiveMbps': summary([value[0] for value in network_samples]),
            'ecsEth0TransmitMbps': summary([value[1] for value in network_samples]),
            'anonymousDenied': True, 'logoutRevoked': True, 'pixelsSaved': False}
        aruco_observations['ids'] = {key: sorted(value) for key, value in aruco_observations['ids'].items()}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        return not failures


if __name__ == '__main__':
    try:
        sys.exit(0 if asyncio.run(main()) else 1)
    except Exception as error:
        # Fixed type names only: HTTP payloads, passwords and cookies stay private.
        print(json.dumps({'status': 'FAIL', 'exception': type(error).__name__}), flush=True)
        sys.exit(1)
