#!/usr/bin/env python3
"""Remux camera archive packets into one timestamped MKV without re-encoding.

The worker supplies private local files and verified object identities. Packet
metadata, indexes, attachments and media bytes are streamed; a long recording
does not require a JSON array containing every frame. The Matroska writer uses
the same microsecond time base as the native archive writer.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time

MAX_REQUEST = 8 * 1024**2
MAX_LINE = 2 * 1024**2
MAX_PACKET = 32 * 1024**2
MAX_SOURCES = 10000
CODECS = {'hevc': 'V_MPEGH/ISO/HEVC', 'mjpeg': 'V_MJPEG', 'ffv1': 'V_FFV1'}
ATTACHMENTS = {'index.jsonl', 'frames.index.jsonl', 'camera-info.cdr.jsonl'}


class AssemblyError(ValueError):
    pass


def fail(code):
    raise AssemblyError(code)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def regular(path, root):
    path = Path(path)
    if (not path.is_absolute() or not path.is_relative_to(root)
            or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True))):
        fail('source_path_outside_workspace')
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            fail('source_symlink')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0:
        fail('source_not_regular')
    return path


def json_file(path, limit=MAX_REQUEST):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        fail('json_too_large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        fail('invalid_json_object')
    return value


def json_line(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()


def index_rows(path):
    with path.open('rb') as stream:
        while True:
            raw = stream.readline(MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE or not raw.endswith(b'\n'):
                fail('invalid_index_line')
            value = json.loads(raw)
            if not isinstance(value, dict):
                fail('invalid_index_row')
            yield value


def as_us(value):
    if value in (None, 'N/A'):
        fail('missing_packet_timestamp')
    # ffprobe writes fixed decimal seconds. Decimal avoids rounding epoch PTS.
    from decimal import Decimal
    return int(Decimal(str(value)) * 1000000)


def iso_ns(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        fail('timestamp_timezone_required')
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000000000 + delta.microseconds * 1000


class Commands:
    def __init__(self, work, timeout):
        self.work = work
        self.deadline = time.monotonic() + timeout

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            fail('assembly_timeout')
        return value

    def run(self, args, *, limit=1024 * 1024):
        with tempfile.TemporaryFile(dir=self.work) as output, tempfile.TemporaryFile(dir=self.work) as errors:
            result = subprocess.run(args, stdout=output, stderr=errors, timeout=self.remaining(), check=False)
            if result.returncode:
                fail('media_command_failed')
            output.seek(0)
            raw = output.read(limit + 1)
            if len(raw) > limit:
                fail('media_metadata_too_large')
            return raw

    @contextmanager
    def packets(self, path):
        args = ['ffprobe', '-v', 'error', '-threads', '1', '-select_streams', 'v:0',
                '-show_packets', '-show_entries', 'packet=pos,size,flags,pts_time,dts_time,duration_time',
                '-of', 'compact=p=0:nk=0', str(path)]
        timeout = self.remaining()
        with tempfile.TemporaryFile(dir=self.work) as errors:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors)
            timer = threading.Timer(timeout, process.kill)
            timer.daemon = True
            timer.start()
            try:
                def rows():
                    while True:
                        raw = process.stdout.readline(16385)
                        if not raw:
                            break
                        if len(raw) > 16384:
                            fail('packet_metadata_too_large')
                        yield dict(item.split('=', 1) for item in raw.decode().strip().split('|') if '=' in item)
                yield rows()
                if process.wait(timeout=self.remaining()):
                    fail('packet_probe_failed')
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()


def probe(commands, path):
    base = ['ffprobe', '-v', 'error', '-threads', '1']
    stream = json.loads(commands.run(base + ['-select_streams', 'v:0', '-show_streams', '-show_data',
        '-show_entries', 'stream=codec_name,extradata,width,height,pix_fmt,has_b_frames,time_base,color_range,color_space,color_transfer,color_primaries,bits_per_raw_sample',
        '-of', 'json', str(path)]))['streams']
    all_streams = json.loads(commands.run(base + ['-show_streams', '-show_entries',
        'stream=index,codec_type:stream_tags=filename,mimetype', '-of', 'json', str(path)]))['streams']
    if len(stream) != 1 or sum(s.get('codec_type') == 'video' for s in all_streams) != 1:
        fail('single_video_track_required')
    if any(s.get('codec_type') not in ('video', 'attachment') for s in all_streams):
        fail('unsupported_media_track')
    value = stream[0]
    if value.get('codec_name') not in CODECS or value.get('has_b_frames', 0):
        fail('unsupported_codec_or_b_frames')
    for key in ('width', 'height'):
        if not 0 < value.get(key, 0) <= 8192:
            fail('invalid_video_dimensions')
    private = bytearray()
    for line in value.get('extradata', '').splitlines():
        if ':' in line:
            text = line.split(':', 1)[1].split('  ', 1)[0].replace(' ', '')
            if text:
                private.extend(bytes.fromhex(text))
    if len(private) > 256 * 1024:
        fail('codec_private_too_large')
    value['_private'] = bytes(private)
    return value, [s for s in all_streams if s.get('codec_type') == 'attachment']


def extract_indexes(commands, path, streams, directory, metadata):
    result = {}
    expected = {a['name']: a for a in metadata.get('videoAttachments', []) if isinstance(a, dict) and 'name' in a}
    for ordinal, attachment in enumerate(streams):
        name = attachment.get('tags', {}).get('filename')
        if name not in ATTACHMENTS or name in result:
            fail('unsupported_or_duplicate_attachment')
        destination = directory / name
        commands.run(['ffmpeg', '-nostdin', '-v', 'error', '-y',
            '-dump_attachment:t:' + str(ordinal), str(destination), '-i', str(path),
            '-map', '0:v:0', '-t', '0', '-c', 'copy', '-f', 'null', '-'])
        regular(destination, directory)
        row = expected.get(name)
        if row and (digest(destination) != row.get('sha256') or destination.stat().st_size != row.get('bytes')):
            fail('source_attachment_checksum_mismatch')
        result[name] = destination
    if expected and set(expected) != set(result):
        fail('source_attachment_missing')
    if ('index.jsonl' in result) == ('frames.index.jsonl' in result):
        fail('one_source_frame_index_required')
    return result


def packet_bytes(source, packet):
    position, length = int(packet['pos']), int(packet['size'])
    if position < 0 or not 0 < length <= MAX_PACKET:
        fail('packet_bounds_invalid')
    source.seek(position)
    first = source.read(1)
    if not first or not first[0]:
        fail('packet_track_invalid')
    size = next((n for n in range(1, 9) if first[0] & (1 << (8 - n))), None)
    if size is None:
        fail('packet_track_invalid')
    track = int.from_bytes(first + source.read(size - 1), 'big') & ((1 << (7 * size)) - 1)
    header = source.read(3)
    if track != 1 or len(header) != 3 or header[2] & 6:
        fail('packet_lacing_or_track_unsupported')
    raw = source.read(length)
    if len(raw) != length:
        fail('packet_truncated')
    return raw


def vint(value):
    for length in range(1, 9):
        if value < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | value).to_bytes(length, 'big')
    fail('matroska_element_too_large')


def element(identity, value):
    return bytes.fromhex(identity) + vint(len(value)) + value


def number(identity, value):
    return element(identity, value.to_bytes(max(1, (value.bit_length() + 7) // 8), 'big'))


def signed(identity, value):
    for length in range(1, 9):
        if -(1 << (8 * length - 1)) <= value < (1 << (8 * length - 1)):
            return element(identity, value.to_bytes(length, 'big', signed=True))
    fail('matroska_reference_invalid')


def text(identity, value):
    return element(identity, value.encode())


def write_attachments(output, attachments):
    headers = []
    total = 0
    for uid, (name, path) in enumerate(attachments.items(), 1):
        length = path.stat().st_size
        prefix = text('466E', name) + text('4660', 'application/json' if name.endswith('.json') else 'application/x-ndjson') + number('46AE', uid)
        data = bytes.fromhex('465C') + vint(length)
        header = bytes.fromhex('61A7') + vint(len(prefix) + len(data) + length) + prefix + data
        headers.append((path, header))
        total += len(header) + length
    output.write(bytes.fromhex('1941A469') + vint(total))
    for path, header in headers:
        output.write(header)
        with path.open('rb') as source:
            shutil.copyfileobj(source, output, 128 * 1024)


def colour_track(stream):
    mappings = {'bt709': 1, 'bt470bg': 5, 'smpte170m': 6, 'bt2020nc': 9, 'bt2020c': 10}
    transfer = {'bt709': 1, 'gamma22': 4, 'gamma28': 5, 'smpte170m': 6, 'iec61966-2-1': 13, 'bt2020-10': 14, 'bt2020-12': 15}
    primaries = {'bt709': 1, 'bt470m': 4, 'bt470bg': 5, 'smpte170m': 6, 'bt2020': 9}
    payload = b''
    for identity, value in [('55B1', mappings.get(stream.get('color_space'))),
                            ('55B9', {'pc': 2, 'tv': 1}.get(stream.get('color_range'))),
                            ('55BA', transfer.get(stream.get('color_transfer'))),
                            ('55BB', primaries.get(stream.get('color_primaries')))]:
        if value is not None:
            payload += number(identity, value)
    return element('55B0', payload) if payload else b''


def write_video(path, parts, stream, attachments, duration, work):
    header = element('1A45DFA3', number('4286', 1) + number('42F7', 1) + number('42F2', 4)
        + number('42F3', 8) + text('4282', 'matroska') + number('4287', 4) + number('4285', 2))
    info = element('1549A966', number('2AD7B1', 1000) + text('4D80', 'robot-continuous-archive')
        + text('5741', 'robot-continuous-archive') + element('4489', struct.pack('>d', float(duration))))
    track = number('D7', 1) + number('73C5', 1) + number('83', 1) + number('9C', 0)
    track += text('86', CODECS[stream['codec_name']])
    if stream['_private']:
        track += element('63A2', stream['_private'])
    track += element('E0', number('B0', stream['width']) + number('BA', stream['height']) + colour_track(stream))
    def seekhead(position):
        return element('114D9B74', element('4DBB', element('53AB', bytes.fromhex('1C53BB6B'))
            + element('53AC', position.to_bytes(8, 'big'))))
    expected_hash = hashlib.sha256()
    count = 0
    previous = None
    with path.open('xb') as output, (work / 'cues.bin').open('w+b') as cues:
        os.fchmod(output.fileno(), 0o600)
        output.write(header + bytes.fromhex('1853806701ffffffffffffff'))
        segment_start = output.tell()
        head_position = output.tell()
        output.write(seekhead(0) + info + element('1654AE6B', element('AE', track)))
        write_attachments(output, attachments)
        for ordinal, part in enumerate(parts):
            with part['path'].open('rb') as source:
                for frame_number, packet in enumerate(index_rows(part['packets']), 1):
                    pts = packet['videoPtsUs']
                    payload = packet_bytes(source, packet)
                    expected_hash.update(len(payload).to_bytes(8, 'big'))
                    expected_hash.update(payload)
                    position = output.tell() - segment_start
                    block = element('A1', b'\x81' + struct.pack('>hB', 0, 0) + payload)
                    frame_duration = packet['durationUs']
                    if frame_number == part['frames'] and ordinal + 1 < len(parts):
                        frame_duration = parts[ordinal + 1]['firstGlobalPtsUs'] - pts
                    group = block + number('9B', frame_duration)
                    if packet['keyframe']:
                        cues.write(element('BB', number('B3', pts) + element('B7', number('F7', 1) + number('F1', position))))
                    else:
                        if previous is None:
                            fail('initial_keyframe_missing')
                        group += signed('FB', previous - pts)
                    output.write(element('1F43B675', number('E7', pts) + element('A0', group)))
                    previous = pts
                    count += 1
        cue_position = output.tell() - segment_start
        cue_size = cues.tell()
        output.write(bytes.fromhex('1C53BB6B') + vint(cue_size))
        cues.seek(0)
        shutil.copyfileobj(cues, output, 128 * 1024)
        output.seek(head_position)
        output.write(seekhead(cue_position))
        output.flush()
        os.fsync(output.fileno())
    return count, expected_hash.hexdigest()


def assemble(request, output, commands, work):
    sources = request.get('sources')
    if request.get('schemaVersion') != 1 or not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        fail('invalid_assembly_request')
    parts = []
    baseline = None
    origin = previous_global = None
    total = 0
    merged = {}
    output_indexes = {}
    gaps = []
    last_tail = 33333
    try:
        for ordinal, entry in enumerate(sources):
            commands.remaining()
            if not isinstance(entry, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(entry.get('id', ''))):
                fail('invalid_source_identity')
            path = regular(entry['path'], commands.work.parent)
            if type(entry.get('frameCount')) is not int or entry['frameCount'] <= 0:
                fail('invalid_source_frame_count')
            size = path.stat().st_size
            if str(size) != str(entry.get('size')) or digest(path) != entry.get('checksum'):
                fail('source_checksum_or_size_mismatch')
            before = path.stat()
            directory = work / ('part-' + str(ordinal))
            directory.mkdir(mode=0o700)
            stream, attachments = probe(commands, path)
            signature = tuple(stream.get(key) for key in ('codec_name', 'width', 'height', 'pix_fmt', '_private', 'color_range', 'color_space', 'color_transfer', 'color_primaries'))
            if baseline is None:
                baseline = signature
                first_stream = stream
            elif signature != baseline:
                fail('incompatible_video_tracks')
            extracted = extract_indexes(commands, path, attachments, directory, entry.get('metadata') or {})
            frame_name = 'index.jsonl' if 'index.jsonl' in extracted else 'frames.index.jsonl'
            if parts and parts[0]['frameName'] != frame_name:
                fail('incompatible_frame_indexes')
            for name in extracted:
                if name not in output_indexes:
                    merged[name] = work / ('merged-' + name)
                    output_indexes[name] = merged[name].open('xb')
            if 'camera-info.cdr.jsonl' in extracted:
                for row in index_rows(extracted['camera-info.cdr.jsonl']):
                    output_indexes['camera-info.cdr.jsonl'].write(json_line({**row, 'sourceSegmentId': entry['id']}))
            packet_path = directory / 'packets.jsonl'
            count = 0
            first_timestamp = last_timestamp = first_pts = previous_pts = None
            intervals = deque(maxlen=1024)
            previous_packet = None
            packet_hash = hashlib.sha256()
            with commands.packets(path) as packets, packet_path.open('xb') as packet_index, path.open('rb') as media:
                rows = iter(index_rows(extracted[frame_name]))
                for packet in packets:
                    row = next(rows, None)
                    if row is None or type(row.get('timestampNs')) is not int or row['timestampNs'] <= 0:
                        fail('frame_index_count_or_timestamp_mismatch')
                    timestamp = row['timestampNs']
                    if first_timestamp is None:
                        first_timestamp = timestamp
                        first_pts = as_us(packet['pts_time'])
                        if 'K' not in packet.get('flags', ''):
                            fail('source_initial_keyframe_missing')
                    pts = as_us(packet['pts_time']) - first_pts
                    if packet.get('dts_time') not in (None, 'N/A') and as_us(packet['dts_time']) != as_us(packet['pts_time']):
                        fail('reordered_video_packets_unsupported')
                    expected = (timestamp - first_timestamp) // 1000
                    if previous_pts is not None:
                        expected = max(previous_expected + 1, expected)
                    from fractions import Fraction
                    tolerance = max(1, math.ceil(float(Fraction(stream.get('time_base', '1/1000000'))) * 1000000))
                    if abs(pts - expected) > tolerance or (previous_pts is not None and pts <= previous_pts):
                        fail('source_index_pts_mismatch')
                    previous_expected = expected
                    if origin is None:
                        origin = first_timestamp
                    # Recover the original microseconds from the attached
                    # source index, even for a legacy 1 ms Matroska time base.
                    global_pts = (first_timestamp - origin) // 1000 + expected
                    if previous_global is not None:
                        if global_pts <= previous_global:
                            fail('source_time_overlap_or_reset')
                        interval = global_pts - previous_global
                        if count:
                            intervals.append(interval)
                        if interval > max(250000, last_tail * 3):
                            if len(gaps) < 1000:
                                gaps.append({'afterFrame': total, 'startSeconds': previous_global / 1000000,
                                             'endSeconds': global_pts / 1000000, 'durationSeconds': interval / 1000000})
                    raw = packet_bytes(media, packet)
                    packet_hash.update(len(raw).to_bytes(8, 'big'))
                    packet_hash.update(raw)
                    detail = {k: packet[k] for k in ('pos', 'size')}
                    detail.update(videoPtsUs=global_pts, keyframe='K' in packet.get('flags', ''))
                    if previous_packet is not None:
                        previous_packet['durationUs'] = global_pts - previous_packet['videoPtsUs']
                        packet_index.write(json_line(previous_packet))
                    previous_packet = detail
                    original_pts = row.get('videoPtsUs')
                    combined = {**row, 'sourceSegmentId': entry['id'], 'recordingSequence': total + 1, 'videoPtsUs': global_pts}
                    if original_pts is not None:
                        combined['sourceVideoPtsUs'] = original_pts
                    output_indexes[frame_name].write(json_line(combined))
                    count += 1
                    total += 1
                    previous_global = global_pts
                    previous_pts = pts
                    last_timestamp = timestamp
                if next(rows, None) is not None or count != entry.get('frameCount') or not count:
                    fail('source_frame_count_mismatch')
                last_tail = max(1, int(statistics.median(intervals))) if intervals else 33333
                previous_packet['durationUs'] = last_tail
                packet_index.write(json_line(previous_packet))
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                fail('source_changed_during_assembly')
            parts.append({'path': path, 'packets': packet_path, 'frameName': frame_name,
                          'id': entry['id'], 'checksum': entry['checksum'], 'size': str(size),
                          'frames': count, 'firstTimestampNs': first_timestamp, 'lastTimestampNs': last_timestamp,
                          'firstGlobalPtsUs': (first_timestamp - origin) // 1000,
                          'packetSha256': packet_hash.hexdigest(), 'before': (before.st_size, before.st_mtime_ns, before.st_ino)})
    finally:
        for stream in output_indexes.values():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
    duration = previous_global + last_tail
    provenance = work / 'recording.json'
    provenance.write_bytes(json_line({'schemaVersion': 1, 'recordingId': request.get('recordingId'),
        'robotId': request.get('robotId'), 'sensor': request.get('sensor'), 'losslessRemux': True,
        'timecodeScaleNs': 1000, 'originTimestampNs': origin, 'frameCount': total, 'gaps': gaps,
        'sources': [{k: p[k] for k in ('id', 'checksum', 'size', 'frames', 'firstTimestampNs', 'lastTimestampNs')} for p in parts]}))
    merged['recording.json'] = provenance
    partial = work / 'recording.mkv'
    written, expected_hash = write_video(partial, parts, first_stream, merged, duration, work)
    verified_hash = hashlib.sha256()
    count = 0
    frame_index = merged[parts[0]['frameName']]
    with commands.packets(partial) as packets, partial.open('rb') as source:
        expected_rows = iter(index_rows(frame_index))
        for packet in packets:
            row = next(expected_rows, None)
            if row is None or as_us(packet['pts_time']) != row['videoPtsUs']:
                fail('output_pts_verification_failed')
            raw = packet_bytes(source, packet)
            verified_hash.update(len(raw).to_bytes(8, 'big'))
            verified_hash.update(raw)
            count += 1
        if next(expected_rows, None) is not None or count != total or written != total or verified_hash.hexdigest() != expected_hash:
            fail('output_packet_verification_failed')
    for part in parts:
        info = part['path'].stat()
        if (info.st_size, info.st_mtime_ns, info.st_ino) != part['before'] or digest(part['path']) != part['checksum']:
            fail('source_changed_during_assembly')
    result = {'schemaVersion': 1, 'sha256': digest(partial), 'size': str(partial.stat().st_size),
              'frameCount': total, 'durationSeconds': duration / 1000000, 'codec': first_stream['codec_name'],
              'width': first_stream['width'], 'height': first_stream['height'], 'sourceCount': len(parts),
              'indexSha256': digest(frame_index), 'losslessRemux': True, 'packetSha256Validated': True,
              'timecodeScaleNs': 1000, 'gaps': gaps, 'attachments': [
                  {'name': name, 'sha256': digest(path), 'bytes': path.stat().st_size} for name, path in merged.items()]}
    # Same-filesystem hard link publishes atomically and refuses any pre-existing
    # output. Sources and earlier complete exports are never overwritten.
    os.link(partial, output)
    descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--result', required=True, type=Path)
    parser.add_argument('--timeout-seconds', type=float, default=1800)
    args = parser.parse_args()
    if not math.isfinite(args.timeout_seconds) or not 0 < args.timeout_seconds <= 86400:
        parser.error('timeout must be in (0, 86400]')
    source = args.input.absolute()
    root = source.parent
    published = False
    owns_destination = False
    def cancelled(_signum, _frame):
        raise AssemblyError('assembly_cancelled')
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    try:
        regular(source, root)
        for destination in (args.output.absolute(), args.result.absolute()):
            if destination.parent != root or destination.exists() or destination.is_symlink():
                fail('output_must_be_new_workspace_file')
        owns_destination = True
        request = json_file(source)
        with tempfile.TemporaryDirectory(prefix='.assemble-', dir=root) as temporary:
            work = Path(temporary)
            commands = Commands(work, args.timeout_seconds)
            result = assemble(request, args.output.absolute(), commands, work)
            published = True
            with args.result.absolute().open('xb') as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(json_line(result))
                output.flush()
                os.fsync(output.fileno())
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (AssemblyError, ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if published or owns_destination:
            args.output.absolute().unlink(missing_ok=True)
        print(json.dumps({'ok': False, 'errorCode': str(error) if isinstance(error, AssemblyError) else 'assembly_failed',
                          'errorType': type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
