"""Finalize native camera data as one verified Matroska object.

Depth retains exact pixel values with FFV1 unless a sealed capture explicitly
requests visual-only pseudocolor. New compact visual captures use HEVC when
smaller; a verified FFV1/MJPEG visualization is the fallback. Frame metadata and CameraInfo CDR stay embedded as attachments.
The durable raw spool is only released by separately verified retention.
"""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote
from uuid import uuid4

from .archive_core import MAX_FILE_BYTES
from .archive_policy import allowed_archive_manifest, allowed_manifest_path

MAX_MANIFEST = 256 * 1024
MAX_INDEX = 64 * 1024**2
MAX_LINE = 2 * 1024**2
MAX_FRAME = 32 * 1024**2
MAX_SOURCE = MAX_FILE_BYTES  # Exact integer protocol bound; no fixed2GiB file ceiling.
MAX_FRAMES = 200_000
MAX_PROBE = 64 * 1024**2
_SLICE_CACHE = {}
_SLICE_LOCK = threading.Lock()


class VideoFinalizeError(RuntimeError):
    """An incomplete derivative is not ready for upload; raw sources remain."""


class VideoFinalizeBusy(VideoFinalizeError):
    pass


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(128 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _json(path, limit=MAX_MANIFEST):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise VideoFinalizeError('video_metadata_too_large')
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise VideoFinalizeError('video_metadata_invalid') from error
    if not isinstance(result, dict):
        raise VideoFinalizeError('video_metadata_invalid')
    return result


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp-' + uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _discard_verified_sources(segment, files):
    """Release raw sources after the verified derivative is durable."""
    if os.environ.get('ARCHIVE_DISCARD_RAW_AFTER_DERIVATIVE') != '1':
        return
    for name in files:
        path = segment / name
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            continue
    for parent in sorted({(segment / name).parent for name in files}, key=lambda item: len(item.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass


def _safe_file(segment, name):
    if (not isinstance(name, str) or not name or '\\' in name
            or Path(name).is_absolute() or str(Path(name)) != name
            or any(part in ('', '.', '..') for part in Path(name).parts)):
        raise VideoFinalizeError('video_source_path_invalid')
    current = segment
    for part in Path(name).parts:
        current = current / part
        if current.is_symlink():
            raise VideoFinalizeError('video_source_link')
    info = current.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_SOURCE:
        raise VideoFinalizeError('video_source_file_invalid')
    return current


@contextmanager
def _segment_lock(segment):
    descriptor = os.open(segment / '.video-finalize.lock',
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise VideoFinalizeError('video_lock_invalid')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise VideoFinalizeBusy('video_finalizer_busy') from error
        yield
    finally:
        os.close(descriptor)


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise VideoFinalizeError('video_finalize_timeout')
    return value


def _size(value):
    for length in range(1, 9):
        if value < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | value).to_bytes(length, 'big')
    raise VideoFinalizeError('matroska_element_too_large')


def _element(identity, payload):
    return bytes.fromhex(identity) + _size(len(payload)) + payload


def _number(identity, value):
    return _element(identity, value.to_bytes(max(1, (value.bit_length() + 7) // 8), 'big'))


def _text(identity, value):
    return _element(identity, value.encode('utf-8'))


def _packet_data(stream, packet):
    """ffprobe Matroska positions address the Block payload, not codec bytes."""
    position, length = int(packet['pos']), int(packet['size'])
    if position < 0 or not 0 < length <= MAX_FRAME:
        raise VideoFinalizeError('video_packet_bounds')
    stream.seek(position)
    first = stream.read(1)
    if not first or not first[0]:
        raise VideoFinalizeError('video_packet_track')
    vint_length = next((size for size in range(1, 9) if first[0] & (1 << (8-size))), None)
    if vint_length is None:
        raise VideoFinalizeError('video_packet_track')
    track = int.from_bytes(first + stream.read(vint_length-1), 'big') & ((1 << (7*vint_length))-1)
    header = stream.read(3)
    if track != 1 or len(header) != 3 or header[2] & 6:
        raise VideoFinalizeError('video_packet_lacing_or_track')
    value = stream.read(length)
    if len(value) != length:
        raise VideoFinalizeError('video_packet_truncated')
    return value


def _write_attachments(output, attachments):
    headers, total = [], 0
    for uid, (source, filename, mime) in enumerate(attachments, 1):
        length = source.stat().st_size
        if not 0 < length <= MAX_INDEX:
            raise VideoFinalizeError('video_attachment_size')
        prefix = _text('466E', filename) + _text('4660', mime) + _number('46AE', uid)
        data_header = bytes.fromhex('465C') + _size(length)
        attached_header = bytes.fromhex('61A7') + _size(len(prefix)+len(data_header)+length)
        headers.append((source, length, attached_header+prefix+data_header))
        total += len(attached_header)+len(prefix)+len(data_header)+length
    output.write(bytes.fromhex('1941A469') + _size(total))
    for source, length, prefix in headers:
        output.write(prefix)
        with source.open('rb') as stream:
            copied = 0
            for block in iter(lambda: stream.read(128*1024), b''):
                output.write(block); copied += len(block)
            if copied != length:
                raise VideoFinalizeError('video_attachment_changed')


def _write_matroska(path, frames, timings, width, height, *, codec, private=b'', attachments=()):
    if not timings or not 0 < width <= 8192 or not 0 < height <= 8192:
        raise VideoFinalizeError('video_track_invalid')
    deltas = [right-left for left, right in zip(timings, timings[1:]) if right > left]
    tail = sorted(deltas)[len(deltas)//2] if deltas else 33_333
    tail = max(1, tail)
    end = timings[-1] + tail
    header = _element('1A45DFA3', _number('4286', 1) + _number('42F7', 1)
        + _number('42F2', 4) + _number('42F3', 8) + _text('4282', 'matroska')
        + _number('4287', 4) + _number('4285', 2))
    info = _element('1549A966', _number('2AD7B1', 1000) + _text('4D80', 'robot-native-archive')
        + _text('5741', 'robot-native-archive') + _element('4489', struct.pack('>d', float(end))))
    track = (_number('D7', 1) + _number('73C5', 1) + _number('83', 1) + _number('9C', 0)
             + _text('86', codec) + (_element('63A2', private) if private else b'')
             + _element('E0', _number('B0', width) + _number('BA', height)))
    with path.open('xb') as output:
        os.chmod(path, 0o600)
        output.write(header + bytes.fromhex('1853806701ffffffffffffff') + info
                     + _element('1654AE6B', _element('AE', track)))
        # Attachments precede the first cluster, so streaming demuxers discover
        # them while reading the header without requiring a second seek index.
        if attachments:
            _write_attachments(output, attachments)
        count = 0
        for count, packet in enumerate(frames, 1):
            if count > len(timings) or not isinstance(packet, bytes) or not 0 < len(packet) <= MAX_FRAME:
                raise VideoFinalizeError('video_packet_count_or_size')
            timestamp = timings[count-1]
            duration = timings[count]-timestamp if count < len(timings) else tail
            block = _element('A1', b'\x81' + struct.pack('>hB', 0, 0) + packet)
            output.write(_element('1F43B675', _number('E7', timestamp)
                + _element('A0', block + _number('9B', max(1, duration)))))
        if count != len(timings):
            raise VideoFinalizeError('video_packet_count')
        output.flush(); os.fsync(output.fileno())


def _probe(path, executable, work, deadline):
    # Keep packet validation complete without producing a hexdump per frame.
    # CodecPrivate is small and is requested separately from packet metadata.
    base = [executable, '-v', 'error', '-threads', '1', '-analyzeduration', '1',
            '-probesize', '32768', '-fpsprobesize', '0', '-select_streams', 'v:0']
    sections = {}
    for section, arguments in (
            ('packets', ['-show_packets', '-show_entries',
                         'packet=pos,size,flags,pts_time,duration_time']),
            ('streams', ['-show_streams', '-show_data', '-show_entries',
                         'stream=codec_name,extradata,width,height,pix_fmt'])):
        result_path = work / ('probe-' + uuid4().hex + '.json')
        with result_path.open('xb') as out, (work/'probe.stderr.log').open('ab') as errors:
            try:
                result = subprocess.run(base + arguments + ['-of', 'json', str(path)],
                    stdout=out, stderr=errors, timeout=_remaining(deadline), check=False)
            except subprocess.TimeoutExpired as error:
                raise VideoFinalizeError('video_finalize_timeout') from error
        if result.returncode:
            raise VideoFinalizeError('video_probe_failed')
        sections[section] = _json(result_path, MAX_PROBE).get(section)
    streams, packets = sections['streams'], sections['packets']
    if (not isinstance(streams, list) or len(streams) != 1 or not isinstance(packets, list)
            or not 1 <= len(packets) <= MAX_FRAMES):
        raise VideoFinalizeError('video_probe_structure')
    return streams[0], packets


def _private_data(value):
    if not isinstance(value, str) or len(value) > 256*1024:
        raise VideoFinalizeError('ffv1_private_data_invalid')
    try:
        result = b''.join(bytes.fromhex(line.split(':', 1)[1].split('  ', 1)[0].replace(' ', ''))
                          for line in value.splitlines() if line.strip())
    except (ValueError, IndexError) as error:
        raise VideoFinalizeError('ffv1_private_data_invalid') from error
    if not 0 < len(result) <= 64*1024:
        raise VideoFinalizeError('ffv1_private_data_invalid')
    return result


def _timing(timestamp_ns, timings, state):
    if type(timestamp_ns) is not int or timestamp_ns <= 0:
        raise VideoFinalizeError('video_timestamp_invalid')
    if state.get('first') is None:
        state['first'] = timestamp_ns
    desired = max(0, (timestamp_ns-state['first'])//1000)
    value = max(timings[-1]+1, desired) if timings else 0
    state['adjusted'] += int(value != desired)
    timings.append(value)
    if len(timings) > MAX_FRAMES:
        raise VideoFinalizeError('video_frame_count_limit')
    return value


def _native_frames(segment, index_path):
    from .archive_camera import _jpeg_shape
    frames = _safe_file(segment, 'frames.mjpg')
    offset = 0
    with frames.open('rb') as raw, index_path.open('rb') as index:
        for line in iter(lambda: index.readline(MAX_LINE+1), b''):
            if len(line) > MAX_LINE or not line.endswith(b'\n'):
                raise VideoFinalizeError('video_index_line_too_large')
            row = json.loads(line)
            length = row.get('length')
            if (type(length) is not int or not 0 < length <= MAX_FRAME or row.get('offset') != offset):
                raise VideoFinalizeError('video_native_index_bounds')
            frame = raw.read(length)
            if len(frame) != length or _digest_bytes(frame) != row.get('sha256'):
                raise VideoFinalizeError('video_native_frame_checksum')
            if _jpeg_shape(frame) != (row.get('width'), row.get('height')):
                raise VideoFinalizeError('video_native_dimensions')
            offset += length
            yield row, frame
        if raw.read(1):
            raise VideoFinalizeError('video_native_unindexed_tail')


def _digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _native_video(segment, manifest, work, destination, probe, deadline):
    index = _safe_file(segment, 'index.jsonl')
    if index.stat().st_size > MAX_INDEX:
        raise VideoFinalizeError('video_index_too_large')
    timings, state, dimensions = [], {'first': None, 'adjusted': 0}, None
    hashes = []
    for row, frame in _native_frames(segment, index):
        current = row['width'], row['height']
        if dimensions is not None and dimensions != current:
            raise VideoFinalizeError('video_dimensions_changed')
        dimensions = current
        _timing(row['timestampNs'], timings, state)
        hashes.append(_digest_bytes(frame))
    if not timings:
        raise VideoFinalizeError('video_has_no_frames')
    _write_matroska(destination, (frame for _, frame in _native_frames(segment, index)), timings,
        *dimensions, codec='V_MJPEG', attachments=[(index, 'index.jsonl', 'application/x-ndjson')])
    stream, packets = _probe(destination, probe, work, deadline)
    if stream.get('codec_name') != 'mjpeg' or len(packets) != len(timings):
        raise VideoFinalizeError('video_native_probe_mismatch')
    with destination.open('rb') as source:
        for packet, expected, timestamp in zip(packets, hashes, timings):
            if _digest_bytes(_packet_data(source, packet)) != expected or _pts_us(packet) != timestamp:
                raise VideoFinalizeError('video_native_packet_mismatch')
    return {'videoCodec': 'mjpeg', 'receivedFrames': len(timings), 'width': dimensions[0],
        'height': dimensions[1], 'reencoded': False, 'resized': False,
        'videoTimestampAdjustments': state['adjusted'], 'videoTimecodeScaleNs': 1000,
        'videoAttachments': [{'name': 'index.jsonl', 'sha256': _digest(index), 'bytes': index.stat().st_size}]}


def _pts_us(packet):
    try:
        return int(Decimal(packet['pts_time'])*1_000_000)
    except (KeyError, ValueError, TypeError) as error:
        raise VideoFinalizeError('video_packet_timestamp') from error


def _default_ros_reader(segment, manifest):
    """A ROS bag is a source file format, never an application database."""
    paths = [_safe_file(segment, name) for name in manifest['files'] if name.endswith('.db3')]
    if not paths:
        raise VideoFinalizeError('video_ros_bag_missing')
    for path in sorted(paths):
        connection = sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True)
        try:
            for topic, kind, timestamp, data in connection.execute(
                'SELECT t.name,t.type,m.timestamp,m.data FROM messages m JOIN topics t ON t.id=m.topic_id ORDER BY m.timestamp,m.id'):
                yield topic, kind, timestamp, data
        finally:
            connection.close()


def _image_message(payload):
    if not isinstance(payload, bytes):
        return payload
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import Image
    return deserialize_message(payload, Image)


def _normalize_image(message):
    encodings = {'rgb8': ('rgb24', 'bgr0', 3), 'bgr8': ('bgr24', 'bgr0', 3),
        'rgba8': ('rgba', 'bgra', 4), 'bgra8': ('bgra', 'bgra', 4),
        'mono8': ('gray', 'gray', 1), 'mono16': ('gray16le', 'gray16le', 2),
        '16UC1': ('gray16le', 'gray16le', 2)}
    encoding = message.encoding
    if encoding not in encodings:
        raise VideoFinalizeError('video_ros_encoding_unsupported')
    width, height, step = message.width, message.height, message.step
    if (type(width) is not int or type(height) is not int or type(step) is not int
            or not 0 < width <= 8192 or not 0 < height <= 8192):
        raise VideoFinalizeError('video_ros_image_dimensions')
    pixel_format, encoded_format, channels = encodings[encoding]
    row_size = width*channels
    raw = bytes(message.data)
    if step < row_size or step*height != len(raw) or not 0 < len(raw) <= MAX_FRAME:
        raise VideoFinalizeError('video_ros_image_stride')
    pixels = b''.join(raw[row*step:row*step+row_size] for row in range(height))
    if channels == 2 and bool(message.is_bigendian):
        little = bytearray(len(pixels));little[0::2] = pixels[1::2];little[1::2] = pixels[0::2]
        pixels = bytes(little)
    padding = b''.join(raw[row*step+row_size:(row+1)*step] for row in range(height))
    header = getattr(message, 'header', None);stamp = getattr(header, 'stamp', None)
    metadata = {'width': width, 'height': height, 'encoding': encoding, 'step': step,
        'isBigendian': bool(message.is_bigendian), 'frameId': getattr(header, 'frame_id', ''),
        'sourceHeaderStamp': {'sec': getattr(stamp, 'sec', 0), 'nanosec': getattr(stamp, 'nanosec', 0)},
        'sourcePayloadSha256': _digest_bytes(raw), 'pixelSha256': _digest_bytes(pixels),
        'pixelFormat': pixel_format, 'pixelBytes': len(pixels)}
    if padding:
        metadata['rowPaddingBase64'] = base64.b64encode(padding).decode('ascii')
    return pixels, pixel_format, encoded_format, metadata


def _write_index(stream, row):
    encoded = (json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n').encode()
    if len(encoded) > MAX_LINE or stream.tell()+len(encoded) > MAX_INDEX:
        raise VideoFinalizeError('video_index_capacity')
    stream.write(encoded)


@contextmanager
def _timed_process(arguments, work, deadline, *, stdin=None, stdout=None):
    remaining = _remaining(deadline)
    with (work/'ffmpeg.stderr.log').open('ab') as errors:
        process = subprocess.Popen(arguments, stdin=stdin, stdout=stdout, stderr=errors)
        timer = threading.Timer(remaining, process.kill)
        timer.daemon = True;timer.start()
        try:
            yield process
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=_remaining(deadline))
            if process.returncode:
                raise VideoFinalizeError('video_codec_process_failed')
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill();process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


def _verify_pixels(video, index, ffmpeg, work, deadline, pixel_format):
    arguments = [ffmpeg, '-nostdin', '-v', 'error', '-threads', '1',
                 '-filter_threads', '1', '-filter_complex_threads', '1', '-i', str(video), '-map', '0:v:0',
                 '-threads', '1', '-vsync', '0', '-f', 'rawvideo', '-pix_fmt', pixel_format, 'pipe:1']
    with _timed_process(arguments, work, deadline, stdout=subprocess.PIPE) as process:
        with index.open('rb') as stream:
            for line in stream:
                row = json.loads(line);remaining=row['pixelBytes'];digest=hashlib.sha256()
                while remaining:
                    block = process.stdout.read(min(128*1024, remaining))
                    if not block:
                        raise VideoFinalizeError('video_decoded_frame_truncated')
                    digest.update(block);remaining-=len(block)
                if digest.hexdigest() != row['pixelSha256']:
                    raise VideoFinalizeError('video_lossless_pixel_mismatch')
            if process.stdout.read(1):
                raise VideoFinalizeError('video_extra_decoded_frames')


def _ros_contract(manifest, observed, image_topics):
    """Match every source message against the sealed writer's topic contract."""
    metadata = manifest['metadata']
    declared = metadata.get('topics')
    if declared is not None:
        if not isinstance(declared, dict) or not declared:
            raise VideoFinalizeError('video_topic_contract_invalid')
        expected = Counter()
        for topic, value in declared.items():
            if (not isinstance(topic, str) or not topic.startswith('/') or not isinstance(value, dict)
                    or value.get('type') not in ('sensor_msgs/msg/Image', 'sensor_msgs/msg/CameraInfo')
                    or type(value.get('count')) is not int or value['count'] <= 0):
                raise VideoFinalizeError('video_topic_contract_invalid')
            expected[(topic, value['type'])] = value['count']
        if observed != expected:
            raise VideoFinalizeError('video_topic_contract_mismatch')
    declared_images = metadata.get('videoImageTopics')
    if declared_images is not None:
        if (not isinstance(declared_images, list) or not declared_images
                or not all(isinstance(topic, str) and topic.startswith('/') for topic in declared_images)
                or len(set(declared_images)) != len(declared_images)
                or set(declared_images) != image_topics):
            raise VideoFinalizeError('video_image_topic_contract_mismatch')
    messages = metadata.get('receivedMessages')
    if messages is not None and (type(messages) is not int or messages != sum(observed.values())):
        raise VideoFinalizeError('video_source_message_count_mismatch')


def _clean_interrupted_work(segment):
    """Remove only our private derivative staging, never pending/raw payloads."""
    fixed = {'video.mkv', 'ffv1-intermediate.mkv', 'frames.index.jsonl', 'camera-info.cdr.jsonl',
             'ffmpeg.stderr.log', 'probe.stderr.log'}
    for path in segment.glob('.video-working-*'):
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
            raise VideoFinalizeError('video_interrupted_staging_unsafe')
        for child in path.iterdir():
            info = child.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or not (child.name in fixed or re.fullmatch(r'probe-[a-f0-9]{32}\.json', child.name))):
                raise VideoFinalizeError('video_interrupted_staging_unsafe')
        shutil.rmtree(path)


def _ffv1_slices(executable, deadline, width, height, pixel_format, encoded_format, pixels):
    """Probe the actual encoder; older FFmpeg rejects a single FFV1 v3 slice."""
    resolved = shutil.which(executable)
    if resolved is None:
        raise VideoFinalizeError('video_ffmpeg_missing')
    binary = Path(resolved).resolve()
    info = binary.stat()
    key = (str(binary), info.st_size, info.st_mtime_ns, width, height, pixel_format, encoded_format)
    with _SLICE_LOCK:
        if key in _SLICE_CACHE:
            return _SLICE_CACHE[key]
        for slices in (1, 4):
            arguments = [executable, '-nostdin', '-v', 'error',
                '-filter_threads', '1', '-filter_complex_threads', '1', '-f', 'rawvideo', '-pixel_format',
                pixel_format, '-video_size', f'{width}x{height}', '-framerate', '30', '-i', 'pipe:0',
                '-frames:v', '1', '-an', '-c:v', 'ffv1', '-level', '3', '-slicecrc', '1',
                '-g', '1', '-slices', str(slices), '-pix_fmt', encoded_format, '-threads', '1',
                '-f', 'matroska', 'pipe:1']
            try:
                result = subprocess.run(arguments, input=pixels, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, timeout=_remaining(deadline), check=False)
            except subprocess.TimeoutExpired as error:
                raise VideoFinalizeError('video_finalize_timeout') from error
            if result.returncode == 0:
                _SLICE_CACHE[key] = slices
                return slices
            if slices != 1 or b'slice' not in result.stderr.lower():
                break
        raise VideoFinalizeError('video_ffv1_slice_configuration_unsupported')


def _ros_video(segment, manifest, work, destination, ffmpeg, probe, deadline, reader):
    depth_mode = manifest['metadata'].get('depthVideoMode', 'lossless')
    if depth_mode not in ('lossless', 'pseudocolor'):
        raise VideoFinalizeError('video_depth_mode_invalid')
    pseudocolor = depth_mode == 'pseudocolor'
    if pseudocolor and manifest.get('sensor') != 'Berxel深度':
        raise VideoFinalizeError('video_depth_policy_sensor_mismatch')
    frames_index, camera_info = work/'frames.index.jsonl', work/'camera-info.cdr.jsonl'
    timings, state, first, process = [], {'first': None, 'adjusted': 0}, None, None
    intermediate = work/'ffv1-intermediate.mkv'
    camera_count = 0
    ffv1_slices = None
    observed, image_topics = Counter(), set()
    with frames_index.open('xb') as index, camera_info.open('xb') as info:
        context = None
        try:
            for topic, kind, timestamp, payload in (reader or _default_ros_reader)(segment, manifest):
                _remaining(deadline)
                if not isinstance(topic, str) or not topic.startswith('/'):
                    raise VideoFinalizeError('video_source_topic_invalid')
                observed[(topic, kind)] += 1
                if kind == 'sensor_msgs/msg/CameraInfo':
                    if not isinstance(payload, bytes):
                        raise VideoFinalizeError('video_camera_info_requires_cdr')
                    _write_index(info, {'topic': topic, 'type': kind, 'timestampNs': timestamp,
                                       'cdrBase64': base64.b64encode(payload).decode('ascii')})
                    camera_count += 1
                    continue
                if kind != 'sensor_msgs/msg/Image':
                    raise VideoFinalizeError('video_nonimage_topic_requires_separate_archive')
                image_topics.add(topic)
                pixels, pixel_format, encoded_format, row = _normalize_image(_image_message(payload))
                if pseudocolor:
                    if (topic != '/berxel/depth/image_raw' or pixel_format != 'gray16le'
                            or row['encoding'] not in ('16UC1', 'mono16')):
                        raise VideoFinalizeError('video_depth_visualization_source_mismatch')
                    from .archive_depth_video import pseudocolor_depth16
                    original_sha, original_bytes = row['pixelSha256'], len(pixels)
                    pixels = pseudocolor_depth16(pixels, row['width'], row['height'])
                    pixel_format, encoded_format = 'bgr24', 'bgr0'
                    # Hashes retain provenance; original depth values/padding are
                    # not embedded in a video that declares visual-only storage.
                    row.pop('rowPaddingBase64', None)
                    row.update(originalDepthPixelFormat='gray16le',
                               originalDepthPixelSha256=original_sha,
                               originalDepthPixelBytes=original_bytes,
                               pixelFormat=pixel_format, pixelBytes=len(pixels),
                               pixelSha256=_digest_bytes(pixels),
                               depthRepresentation='pseudocolor', depthMeasurementAvailable=False)
                current = (topic, row['width'], row['height'], pixel_format, encoded_format)
                if first is None:
                    first = current
                    ffv1_slices = _ffv1_slices(ffmpeg, deadline, row['width'], row['height'],
                                               pixel_format, encoded_format, pixels)
                    arguments = [ffmpeg, '-nostdin', '-v', 'error',
                        '-filter_threads', '1', '-filter_complex_threads', '1', '-f', 'rawvideo', '-pixel_format',
                        pixel_format, '-video_size', f'{row["width"]}x{row["height"]}', '-framerate', '30',
                        '-i', 'pipe:0', '-an', '-c:v', 'ffv1', '-level', '3', '-slicecrc', '1', '-g', '1',
                        '-slices', str(ffv1_slices),
                        '-pix_fmt', encoded_format, '-threads', '1', str(intermediate)]
                    context = _timed_process(arguments, work, deadline, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
                    process = context.__enter__()
                if current != first:
                    raise VideoFinalizeError('video_ros_image_format_changed')
                row.update(topic=topic, sequence=len(timings)+1, timestampNs=timestamp,
                           videoPtsUs=_timing(timestamp, timings, state))
                _write_index(index, row)
                process.stdin.write(pixels)
            if context is not None:
                context.__exit__(None, None, None);context=None
        finally:
            if context is not None:
                context.__exit__(*__import__('sys').exc_info())
        for output in (index, info):
            output.flush();os.fsync(output.fileno())
    if first is None or not timings:
        raise VideoFinalizeError('video_has_no_frames')
    _ros_contract(manifest, observed, image_topics)
    stream, packets = _probe(intermediate, probe, work, deadline)
    if stream.get('codec_name') != 'ffv1' or len(packets) != len(timings):
        raise VideoFinalizeError('video_ffv1_packet_mismatch')
    private = _private_data(stream.get('extradata'))
    def encoded_frames():
        with intermediate.open('rb') as source:
            for packet in packets:
                if 'K' not in packet.get('flags', ''):
                    raise VideoFinalizeError('video_ffv1_nonkey_packet')
                yield _packet_data(source, packet)
    attachments = [(frames_index, 'frames.index.jsonl', 'application/x-ndjson')]
    if camera_count:
        attachments.append((camera_info, 'camera-info.cdr.jsonl', 'application/x-ndjson'))
    _write_matroska(destination, encoded_frames(), timings, first[1], first[2],
                    codec='V_FFV1', private=private, attachments=attachments)
    actual, ready_packets = _probe(destination, probe, work, deadline)
    if (actual.get('codec_name') != 'ffv1' or actual.get('pix_fmt') != first[4]
            or len(ready_packets) != len(timings)
            or any(_pts_us(packet) != timestamp for packet, timestamp in zip(ready_packets, timings))):
        raise VideoFinalizeError('video_ffv1_final_probe_mismatch')
    _verify_pixels(destination, frames_index, ffmpeg, work, deadline, first[3])
    details = {'videoCodec': 'ffv1', 'receivedFrames': len(timings), 'width': first[1], 'height': first[2],
        'videoPixelFormat': first[4], 'sourcePixelFormat': first[3], 'losslessPixelValues': True,
        'videoFfv1Slices': ffv1_slices,
        'reencoded': True, 'resized': False, 'videoTimestampAdjustments': state['adjusted'],
        'videoTimecodeScaleNs': 1000, 'cameraInfoMessages': camera_count,
        'videoAttachments': [{'name': name, 'sha256': _digest(path), 'bytes': path.stat().st_size}
                             for path, name, _ in attachments]}
    if pseudocolor:
        details.update(sourcePixelFormat='gray16le', videoInputPixelFormat='bgr24',
                       losslessPixelValues=False, depthVideoMode='pseudocolor',
                       depthRepresentation='pseudocolor', depthMeasurementAvailable=False,
                       depthVisualizationRangeMm=[0, 5000], depthVisualizationPalette='TURBO',
                       originalDepthPixelFormat='gray16le')
    return details


def _compact_colour(segment, manifest, work, output, details, ffmpeg, ffprobe, deadline,
                    backend, qp_i, qp_p):
    """Use the sealed capture's explicit preset; verified original-quality stays fallback."""
    if manifest['metadata'].get('archiveProfile') != 'compact-v1':
        return details
    eligible = (manifest['metadata']['source'] == 'native-camera-jpeg'
                or details.get('sourcePixelFormat') in ('rgb24', 'bgr24')
                or (details.get('depthRepresentation') == 'pseudocolor'
                    and details.get('videoInputPixelFormat') == 'bgr24'
                    and details.get('depthMeasurementAvailable') is False))
    if not eligible:
        return {**details, 'videoCompression': 'lossless-depth-or-non-colour',
                'videoArchivePresetApplied': False}
    from .archive_hevc import compress_rgb_mkv, HevcCompressionError, HevcNotSmaller
    attachments = []
    for row in details.get('videoAttachments', []):
        name = row['name']
        source = segment/'index.jsonl' if name == 'index.jsonl' else work/name
        attachments.append((source, name, 'application/x-ndjson'))
    candidate = work/'compact.mkv'
    fallback_mode = ('pseudocolor-fallback' if details.get('depthRepresentation') == 'pseudocolor'
                     else 'lossless-fallback')
    presets = {'720p-economy-v1': 1_200_000, '720p-balanced-v1': 2_000_000}
    bitrate = presets.get(manifest['metadata'].get('videoArchivePreset'))
    options = {'max_width': 1280, 'max_height': 720, 'bitrate_bps': bitrate} if bitrate else {}
    try:
        compact = compress_rgb_mkv(output, candidate, qp_i=qp_i, qp_p=qp_p,
            attachments=attachments, backend=backend, timeout_s=_remaining(deadline),
            require_smaller=True, ffmpeg=ffmpeg, ffprobe=ffprobe, **options)
    except HevcNotSmaller:
        return {**details, 'videoCompression': fallback_mode,
                'videoCompressionFallback': 'hevc_candidate_not_smaller', 'videoArchivePresetApplied': False}
    except HevcCompressionError as error:
        # A codec/hardware outage must not discard successfully sealed data.
        # The already validated MKV is uploaded with an explicit reason.
        return {**details, 'videoCompression': fallback_mode,
                'videoCompressionFallback': str(error)[:256], 'videoArchivePresetApplied': False}
    os.replace(candidate, output)
    result = {**details, **compact, 'videoArchivePresetApplied': bool(options)}
    if 'cameraCalibration' in manifest['metadata']:
        # Both archive_hevc codec paths only scale to the probed dimensions:
        # nvvidconv/ffmpeg scale add neither cropping nor padding. Freeze this
        # path evidence so native K/P cannot be mistaken for 720p video K/P.
        result['videoImageTransform'] = dict(
            kind='resize' if compact.get('resized') is True else 'identity',
            sourceSize=dict(width=details.get('width'), height=details.get('height')),
            size=dict(width=compact.get('width'), height=compact.get('height')),
            crop=False, letterbox=False)
    return result


def finalize_pending(segment, *, ffmpeg='ffmpeg', ffprobe='ffprobe', timeout_s=120,
                     reserve_bytes=10*1024**3, ros_reader=None, colour_backend='jetson',
                     colour_qp_i=22, colour_qp_p=24):
    """Finalize one sealed segment; no source bytes, devices or server records change.

    ``ros_reader(segment, pending)`` may be injected for isolated tests. It yields
    ``(topic, ROS type, receive_timestamp_ns, CDR bytes or decoded Image)``.
    """
    segment = Path(segment)
    if segment.is_symlink() or not segment.is_dir():
        raise VideoFinalizeError('video_segment_invalid')
    if not math.isfinite(timeout_s) or timeout_s <= 0 or reserve_bytes < 0:
        raise ValueError('invalid video finalizer limits')
    deadline = time.monotonic()+timeout_s
    with _segment_lock(segment):
        ready = segment/'archive.json'
        if ready.exists():
            if ready.is_symlink():
                raise VideoFinalizeError('video_existing_archive_link')
            published = _json(ready)
            metadata = published.get('metadata', {})
            if (published.get('files') != ['video.mkv'] or metadata.get('videoReady') is not True):
                raise VideoFinalizeError('video_existing_archive_conflict')
            if not (segment/'video.mkv').exists():
                ack, pruned = _json(segment/'uploaded.json'), _json(segment/'local-copy-pruned.json')
                rows = ack.get('files')
                if (ack.get('state') != 'complete' or pruned.get('state') not in ('pruning', 'pruned')
                        or ack.get('manifestSha256') != _digest(ready) or not isinstance(rows, list)
                        or len(rows) != 1 or rows[0].get('relativePath') != 'video.mkv'
                        or (rows[0].get('sha256'), rows[0].get('size')) != (metadata.get('videoSha256'), metadata.get('videoBytes'))):
                    raise VideoFinalizeError('video_existing_archive_missing')
                return published
            video = _safe_file(segment, 'video.mkv')
            if video.stat().st_size != metadata.get('videoBytes') or _digest(video) != metadata.get('videoSha256'):
                raise VideoFinalizeError('video_existing_archive_checksum')
            return published
        pending_path = _safe_file(segment, 'video-pending.json')
        manifest = _json(pending_path)
        metadata = manifest.get('metadata', {})
        files = manifest.get('files')
        if (manifest.get('schemaVersion') != 1 or not isinstance(metadata, dict)
                or metadata.get('scope') != 'garage' or metadata.get('videoRequired') is not True
                or metadata.get('source') not in ('native-camera-jpeg', 'native-ros-cdr')
                or not isinstance(files, list) or not files or len(files) > 4096
                or not all(isinstance(name, str) for name in files) or len(set(files)) != len(files)):
            raise VideoFinalizeError('video_pending_manifest_invalid')
        sources = [_safe_file(segment, name) for name in files]
        _clean_interrupted_work(segment)
        source_bytes = sum(path.stat().st_size for path in sources)
        if source_bytes > MAX_SOURCE or shutil.disk_usage(segment).free-source_bytes*3 <= reserve_bytes:
            raise VideoFinalizeError('video_insufficient_working_space')
        before = {name: (path.stat().st_size, path.stat().st_mtime_ns, _digest(path))
                  for name, path in zip(files, sources)}
        expected = metadata.get('sourceFiles')
        if expected is not None:
            if (not isinstance(expected, list) or len(expected) != len(files)
                    or not all(isinstance(row, dict) and isinstance(row.get('relativePath'), str) for row in expected)):
                raise VideoFinalizeError('video_source_manifest_checksum')
            expected_map = {row.get('relativePath'): row for row in expected if isinstance(row, dict)}
            if set(expected_map) != set(files) or any((expected_map[name].get('size'), expected_map[name].get('sha256'))
                    != (value[0], value[2]) for name, value in before.items()):
                raise VideoFinalizeError('video_source_manifest_checksum')
        work = Path(tempfile.mkdtemp(prefix='.video-working-', dir=segment))
        try:
            output = work/'video.mkv'
            if metadata['source'] == 'native-camera-jpeg':
                if files != ['frames.mjpg', 'index.jsonl']:
                    raise VideoFinalizeError('video_native_source_files')
                details = _native_video(segment, manifest, work, output, ffprobe, deadline)
            else:
                details = _ros_video(segment, manifest, work, output, ffmpeg, ffprobe, deadline, ros_reader)
            if metadata.get('receivedFrames') is not None and (type(metadata['receivedFrames']) is not int
                    or metadata['receivedFrames'] != details['receivedFrames']):
                raise VideoFinalizeError('video_source_frame_count_mismatch')
            source_details = details
            details = _compact_colour(segment, manifest, work, output, details, ffmpeg, ffprobe,
                                      deadline, colour_backend, colour_qp_i, colour_qp_p)
            from .archive_video_calibration import calibration_metadata
            calibration = calibration_metadata(metadata, source_details, details)
            for name, path in zip(files, sources):
                if (path.stat().st_size, path.stat().st_mtime_ns, _digest(path)) != before[name]:
                    raise VideoFinalizeError('video_source_changed')
            video_bytes, video_sha = output.stat().st_size, _digest(output)
            if not 0 < video_bytes <= MAX_SOURCE:
                raise VideoFinalizeError('video_output_size_invalid')
            published = {**manifest, 'files': ['video.mkv'], 'metadata': {**metadata, **details, **calibration,
                'format': 'matroska/'+details['videoCodec'], 'videoContainer': 'matroska',
                'videoMimeType': 'video/x-matroska', 'videoReady': True,
                'videoSha256': video_sha, 'videoBytes': video_bytes, 'videoOriginalFiles': list(files),
                'sourceFiles': [{'relativePath': name, 'size': value[0], 'sha256': value[2]}
                                for name, value in before.items()]}}
            os.replace(output, segment/'video.mkv');_sync_directory(segment)
            _atomic_json(ready, published)
            _discard_verified_sources(segment, files)
            return published
        except (subprocess.SubprocessError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, ImportError) as error:
            if isinstance(error, VideoFinalizeError):
                raise
            raise VideoFinalizeError('video_finalize_failed:'+type(error).__name__) from error
        finally:
            shutil.rmtree(work)


class VideoFinalizer:
    """One bounded pass over sealed day folders; scheduling belongs to the owner."""
    def __init__(self, root, robot_id, *, camera_only=False, **options):
        if type(camera_only) is not bool:
            raise ValueError('camera_only must be a boolean')
        self.root, self.robot_id, self.options = Path(root), robot_id, options
        self.camera_only = camera_only
        self.counters, self.errors = Counter(), {}
        self.lock = threading.Lock()
        self.cursor = 0
        # A terminal source error is retried after the next process start so
        # an operator can repair/replace the source.  During this run it is
        # deferred; otherwise a bad segment consumes a CPU pass every 0.5s.
        self.deferred = set()
        # Historical archive trees can contain thousands of already uploaded
        # segments. Share discovery between the worker and status publisher.
        self.discovery_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.pending_cache = None
        self.pending_scanned_at = float('-inf')
        self.selected_error_keys = frozenset()

    def pending_candidates(self, *, force=False):
        with self.discovery_lock:
            now = time.monotonic()
            if force or self.pending_cache is None or now - self.pending_scanned_at >= 5:
                self.pending_cache = tuple(self._candidates())
                self.pending_scanned_at = time.monotonic()
            else:
                # Completed files can also be published by another finalizer.
                # Stat only the small pending set, never the historical tree.
                self.pending_cache = tuple(path for path in self.pending_cache
                    if path.is_file() and not (path.parent/'archive.json').exists())
            return self.pending_cache

    def refresh_pending(self):
        """Discover newly sealed segments immediately before shutdown/drain."""
        return self.pending_candidates(force=True)

    def _completed(self, pending):
        with self.discovery_lock:
            if self.pending_cache is not None:
                self.pending_cache = tuple(path for path in self.pending_cache if path != pending)

    def _candidates(self):
        days = self.root/'days'
        if self.root.is_symlink() or not days.is_dir() or days.is_symlink():
            return []
        # Filter before applying the pass limit/cursor so old excluded backlog
        # cannot starve an eligible camera. No excluded source is rewritten.
        return [p for p in sorted(days.rglob('video-pending.json'))
                if not (p.parent/'archive.json').exists()
                and allowed_manifest_path(p, camera_only=self.camera_only)]

    def _selected_errors(self):
        errors = self.errors.copy()
        if not self.camera_only:
            return errors
        days = self.root/'days'
        selected = {key: error for key, error in errors.items()
                    if any(allowed_manifest_path(days/key/name, camera_only=True)
                           for name in ('video-pending.json', 'archive.json'))}
        self.selected_error_keys = frozenset(selected)
        return selected

    def run_once(self, max_segments=4):
        if type(max_segments) is not int or not 1 <= max_segments <= 4:
            raise ValueError('invalid video pass limit')
        if not self.lock.acquire(blocking=False):
            return 0
        done = 0
        try:
            days = self.root/'days'
            pending = self.pending_candidates()
            with self.state_lock:
                candidates = [path for path in pending
                              if str(path.parent.relative_to(days)) not in self.deferred]
                selected_errors = self._selected_errors()
                error_keys = tuple(self.errors)
            for key in error_keys:
                if key not in selected_errors:
                    with self.state_lock:
                        self.errors.pop(key, None)
                    continue
                ready = days/key/'archive.json'
                try:
                    if ready.is_symlink() or not ready.is_file():
                        continue
                    published = _json(ready)
                    if (published.get('robotId') == self.robot_id
                            and published.get('id') == ready.parent.name
                            and published.get('files') == ['video.mkv']
                            and published.get('metadata', {}).get('videoReady') is True):
                        with self.state_lock:
                            self.errors.pop(key, None)
                            self.deferred.discard(key)
                except (VideoFinalizeError, OSError, ValueError, TypeError):
                    pass
            count = len(candidates)
            for step in range(min(max_segments, count)):
                pending = candidates[(self.cursor+step) % count]
                try:
                    manifest = _json(pending)
                    if not allowed_archive_manifest(manifest, camera_only=self.camera_only):
                        continue
                    if manifest.get('robotId') != self.robot_id:
                        raise VideoFinalizeError('video_robot_identity_mismatch')
                    finalize_pending(pending.parent, **self.options)
                    self._completed(pending)
                    done += 1
                    with self.state_lock:
                        self.counters['finalizedSegments'] += 1
                        self.errors.pop(str(pending.parent.relative_to(days)), None)
                except VideoFinalizeBusy:
                    with self.state_lock:
                        self.counters['busySegments'] += 1
                except (VideoFinalizeError, OSError, ValueError) as error:
                    key = str(pending.parent.relative_to(days))
                    with self.state_lock:
                        self.counters['failedAttempts'] += 1
                        self.errors[key] = str(error)
                        if any(code in str(error) for code in (
                                'video_source_file_invalid',
                                'video_interrupted_staging_unsafe')):
                            self.deferred.add(key)
            if count:
                self.cursor = (self.cursor+min(max_segments, count)) % count
            return done
        finally:
            self.lock.release()

    def status(self, *, cached=False):
        if cached:
            # Discovery and policy checks can wait on slow storage while they
            # hold either lock. Telemetry must not join that work or wait for
            # it: cache tuples/frozensets are replaced atomically by the worker.
            pending = self.pending_cache
            counters, errors, deferred = dict(self.counters), self.errors.copy(), self.deferred.copy()
            keys = frozenset(str(path.parent.relative_to(self.root/'days'))
                             for path in pending or ())
            if self.camera_only:
                selected = keys | self.selected_error_keys
                errors = {key: error for key, error in errors.items() if key in selected}
            return {**counters, 'pendingSegments': None if pending is None else len(pending),
                    'terminalPendingSegments': len(keys & deferred),
                    'errors': dict(list(errors.items())[-16:]), 'running': self.lock.locked(),
                    'discoveryReady': pending is not None,
                    'discoveryAgeS': None if pending is None else
                        max(0, time.monotonic()-self.pending_scanned_at)}
        pending = self.pending_candidates()
        with self.state_lock:
            errors = self._selected_errors()
            terminal = sum(str(path.parent.relative_to(self.root/'days')) in self.deferred
                           for path in pending)
            return {**dict(self.counters), 'pendingSegments': len(pending),
                    'terminalPendingSegments': terminal,
                    'errors': dict(list(errors.items())[-16:]), 'running': self.lock.locked()}
