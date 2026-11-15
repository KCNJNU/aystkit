"""Real-media regression tests for server-side archive video assembly.

Fixtures use tiny CPU-encoded videos. No camera, ROS node, storage service or
existing archive is opened. Run with Python's unittest or pytest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from uuid import uuid4


HELPER = Path(__file__).resolve().parents[1] / 'assemble-archive-video.py'
EPOCH = datetime(2026, 10, 31, 4, 30, tzinfo=timezone.utc)
EPOCH_NS = int(EPOCH.timestamp()) * 1_000_000_000


def run_tool(arguments, *, timeout=45):
    result = subprocess.run(arguments, capture_output=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(result.stderr.decode(errors='replace')[-4000:])
    return result.stdout


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def iso(seconds):
    return (EPOCH + timedelta(microseconds=int(Decimal(str(seconds)) * 1_000_000))).isoformat()


def packets(path):
    """Small-fixture inspector; production must inspect packets as a stream."""
    value = json.loads(run_tool(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_packets', '-show_entries', 'packet=pts_time,duration_time,data_hash',
        '-show_data_hash', 'sha256', '-of', 'json', str(path)]))
    return value['packets']


def decoded_hashes(path):
    value = run_tool(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
        '-i', str(path), '-map', '0:v:0', '-vsync', '0', '-threads', '1',
        '-f', 'framemd5', '-']).decode()
    return [line.rsplit(',', 1)[-1].strip() for line in value.splitlines()
            if line and not line.startswith('#')]


def encode_fixture(directory, name, *, codec='mjpeg', frames=4, width=64,
                   height=48, attachments=()):
    path = directory / (name + '.mkv')
    args = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
        f'testsrc2=size={width}x{height}:rate=25', '-frames:v', str(frames), '-an']
    if codec == 'mjpeg':
        args += ['-c:v', 'mjpeg', '-q:v', '3', '-pix_fmt', 'yuvj420p']
    elif codec == 'ffv1':
        args += ['-c:v', 'ffv1', '-level', '3', '-g', '1', '-slicecrc', '1',
                 '-pix_fmt', 'gray16le']
    elif codec == 'hevc':
        args += ['-c:v', 'libx265', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-x265-params', 'pools=none:frame-threads=1:wpp=0:bframes=0:'
            'keyint=10:min-keyint=10:scenecut=0:repeat-headers=1:log-level=error']
    else:
        raise ValueError(codec)
    args += ['-threads', '1']
    for index, (attachment, filename, mime) in enumerate(attachments):
        args += ['-attach', str(attachment), f'-metadata:s:t:{index}',
                 'filename=' + filename, f'-metadata:s:t:{index}', 'mimetype=' + mime]
    run_tool(args + [str(path)])
    return path


def attachment_payloads(path, directory):
    """Return attachment contents keyed by the Matroska filename tag."""
    streams = json.loads(run_tool(['ffprobe', '-v', 'error', '-show_streams',
        '-show_entries', 'stream=index,codec_type:stream_tags=filename,mimetype',
        '-of', 'json', str(path)]))['streams']
    result = {}
    for index, stream in enumerate(streams):
        if stream.get('codec_type') != 'attachment':
            continue
        destination = directory / f'extracted-{index}-{uuid4().hex}.bin'
        # dump_attachment is an input option; explicitly write the video to
        # the null muxer so FFmpeg has an output without touching an archive.
        run_tool(['ffmpeg', '-nostdin', '-v', 'error', '-dump_attachment:' +
            str(stream['index']), str(destination), '-i', str(path),
            '-map', '0:v:0', '-c:v', 'copy', '-f', 'null', '-'])
        result[stream.get('tags', {}).get('filename', str(index))] = destination.read_bytes()
    return result


class RealMediaFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise unittest.SkipTest('FFmpeg and FFprobe are required')
        encoders = run_tool(['ffmpeg', '-hide_banner', '-encoders']).decode()
        cls.has_hevc = 'libx265' in encoders

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='archive-video-test-')
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def source(self, name, *, codec='mjpeg', start_s=0, frames=4, width=64,
               ros=False, calibration=False, step_ns=40_000_000):
        if codec == 'hevc' and not self.has_hevc:
            self.skipTest('libx265 is unavailable')
        start_ns = EPOCH_NS + int(Decimal(str(start_s)) * 1_000_000_000)
        rows = []
        for index in range(frames):
            timestamp = start_ns + index * step_ns
            row = {'sourceMarker': f'{name}-frame-{index}', 'sequence': index + 1,
                   'timestampNs': timestamp, 'width': width, 'height': 48}
            if ros:
                row.update(topic='/berxel/depth/image_raw', videoPtsUs=index * step_ns // 1000,
                    sourcePixelFormat='16UC1', pixelSha256=hashlib.sha256(
                        f'{name}-{index}'.encode()).hexdigest())
            else:
                row.update(wallTime=timestamp / 1e9, offset=index * 100, length=100,
                           gapBefore=0, sequenceReset=False,
                           sha256=hashlib.sha256(f'{name}-{index}'.encode()).hexdigest())
            rows.append(row)
        index_name = 'frames.index.jsonl' if ros else 'index.jsonl'
        index_path = self.directory / (name + '-' + index_name)
        index_path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        attachments = [(index_path, index_name, 'application/x-ndjson')]
        if calibration:
            info = self.directory / (name + '-camera-info.cdr.jsonl')
            info.write_text(json.dumps({'sourceMarker': name + '-calibration',
                'topic': '/berxel/depth/camera_info', 'type': 'sensor_msgs/msg/CameraInfo',
                'timestampNs': start_ns, 'cdrBase64': base64.b64encode(
                    (name + '-original-cdr').encode()).decode()}) + '\n')
            attachments.append((info, 'camera-info.cdr.jsonl', 'application/x-ndjson'))
        path = encode_fixture(self.directory, name, codec=codec, frames=frames,
                              width=width, attachments=attachments)
        metadata = {'source': 'native-ros-cdr' if ros else 'native-camera-jpeg',
            'videoReady': True, 'videoCodec': codec,
            # FFmpeg's fixture Matroska uses milliseconds. The assembler's
            # output must use the original index's more precise timestamps.
            'videoTimecodeScaleNs': 1_000_000,
            'videoAttachments': [{'name': filename, 'sha256': digest(file),
                                  'bytes': file.stat().st_size}
                                 for file, filename, _ in attachments]}
        return {'id': str(uuid4()), 'path': str(path), 'checksum': digest(path),
            'size': str(path.stat().st_size), 'startedAt': iso(start_s),
            'endAt': iso(Decimal(str(start_s)) + Decimal((frames - 1) * step_ns) /
                         Decimal(1_000_000_000)),
            'frameCount': frames, 'metadata': metadata}

    def assemble(self, sources, *, success=True, suffix=''):
        if not HELPER.exists():
            self.fail(f'Assembler helper is missing: {HELPER}')
        request = {'schemaVersion': 1, 'recordingId': str(uuid4()),
            'robotId': str(uuid4()), 'sensor': '原相机前',
            'startedAt': sources[0]['startedAt'], 'endAt': sources[-1]['endAt'],
            'sources': sources}
        manifest = self.directory / ('input' + suffix + '.json')
        output = self.directory / ('assembled' + suffix + '.mkv')
        result_path = self.directory / ('result' + suffix + '.json')
        manifest.write_text(json.dumps(request, ensure_ascii=False))
        result = subprocess.run([sys.executable, str(HELPER), '--input', str(manifest),
            '--output', str(output), '--result', str(result_path),
            '--timeout-seconds', '60'], capture_output=True, timeout=70)
        if not success:
            self.assertNotEqual(result.returncode, 0, result.stdout.decode(errors='replace'))
            self.assertFalse(output.exists(), 'Rejected input published an output video')
            self.assertFalse(result_path.exists(), 'Rejected input published success metadata')
            return result
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace') +
                         result.stdout.decode(errors='replace'))
        self.assertTrue(output.is_file())
        metadata = json.loads(result_path.read_text())
        self.assertEqual(metadata['schemaVersion'], 1)
        self.assertEqual(metadata['sha256'], digest(output))
        self.assertEqual(metadata['size'], str(output.stat().st_size))
        self.assertEqual(metadata['frameCount'], sum(row['frameCount'] for row in sources))
        self.assertEqual(metadata['sourceCount'], len(sources))
        self.assertIs(metadata['losslessRemux'], True)
        self.assertIs(metadata['packetSha256Validated'], True)
        self.assertEqual(metadata['timecodeScaleNs'], 1000)
        self.assertRegex(metadata['indexSha256'], r'^[a-f0-9]{64}$')
        for row in sources:
            self.assertEqual(digest(row['path']), row['checksum'], 'A source archive changed')
        return output, metadata


class AssembleArchiveVideoTests(RealMediaFixtures):
    def test_stream_copy_keeps_every_decoded_frame_and_exact_boundary(self):
        for codec in ('mjpeg', 'hevc', 'ffv1'):
            with self.subTest(codec=codec):
                sources = [self.source(codec + '-a', codec=codec),
                    self.source(codec + '-b', codec=codec, start_s=Decimal('.160123'))]
                expected_hashes = [value for row in sources for value in decoded_hashes(row['path'])]
                expected_packets = [row['data_hash'] for part in sources for row in packets(part['path'])]
                output, metadata = self.assemble(sources, suffix='-' + codec)
                self.assertEqual(decoded_hashes(output), expected_hashes)
                actual = packets(output)
                self.assertEqual([row['data_hash'] for row in actual], expected_packets)
                expected_pts = [Decimal('.04') * i for i in range(4)] + [
                    Decimal('.160123') + Decimal('.04') * i for i in range(4)]
                self.assertEqual([Decimal(row['pts_time']) for row in actual], expected_pts)
                self.assertEqual(metadata['codec'], codec)
                self.assertEqual(metadata['width'], 64)
                self.assertEqual(metadata['height'], 48)

    def test_original_microsecond_frame_times_override_rounded_input_timecodes(self):
        sources = [self.source('sub-ms-a', step_ns=40_123_000),
                   self.source('sub-ms-b', start_s=Decimal('.160777'), step_ns=40_123_000)]
        output, _ = self.assemble(sources)
        expected = [Decimal('.040123') * i for i in range(4)] + [
            Decimal('.160777') + Decimal('.040123') * i for i in range(4)]
        self.assertEqual([Decimal(row['pts_time']) for row in packets(output)], expected)

    def test_real_gap_is_preserved_without_fake_frames_or_time_compression(self):
        sources = [self.source('gap-a'), self.source('gap-b', start_s=Decimal('12.345678'))]
        output, metadata = self.assemble(sources)
        values = [Decimal(row['pts_time']) for row in packets(output)]
        self.assertEqual(len(values), 8)
        self.assertEqual(values[4], Decimal('12.345678'))
        self.assertEqual(values[4] - values[3], Decimal('12.225678'))
        self.assertGreater(metadata['durationSeconds'], 12.46)
        self.assertTrue(metadata['gaps'], 'A real capture outage was not reported')
        self.assertEqual(decoded_hashes(output), [value for row in sources
            for value in decoded_hashes(row['path'])])

    def test_usb_frame_metadata_from_every_part_remains_embedded(self):
        sources = [self.source('usb-a'), self.source('usb-b', start_s=Decimal('.16'))]
        output, metadata = self.assemble(sources)
        embedded = attachment_payloads(output, self.directory)
        joined = b'\n'.join(embedded.values())
        self.assertIn('index.jsonl', embedded)
        for name in ('usb-a', 'usb-b'):
            for frame in range(4):
                self.assertIn(f'{name}-frame-{frame}'.encode(), joined)
        for row in metadata['attachments']:
            self.assertIn(row['name'], embedded)
            self.assertEqual(hashlib.sha256(embedded[row['name']]).hexdigest(), row['sha256'])
            self.assertEqual(len(embedded[row['name']]), row['bytes'])

    def test_ros_frame_metadata_and_original_calibration_cdr_are_preserved(self):
        sources = [self.source('depth-a', codec='ffv1', ros=True, calibration=True),
            self.source('depth-b', codec='ffv1', ros=True, calibration=True, start_s=Decimal('.16'))]
        output, _ = self.assemble(sources)
        embedded = attachment_payloads(output, self.directory)
        self.assertIn('frames.index.jsonl', embedded)
        self.assertIn('camera-info.cdr.jsonl', embedded)
        for name in ('depth-a', 'depth-b'):
            self.assertIn(base64.b64encode((name + '-original-cdr').encode()),
                          embedded['camera-info.cdr.jsonl'])
            for frame in range(4):
                self.assertIn(f'{name}-frame-{frame}'.encode(), embedded['frames.index.jsonl'])

    def test_single_source_is_a_real_verified_mkv_with_all_attachments(self):
        sources = [self.source('single', codec='ffv1', ros=True, calibration=True)]
        output, _ = self.assemble(sources)
        self.assertEqual(decoded_hashes(output), decoded_hashes(sources[0]['path']))
        self.assertEqual([row['pts_time'] for row in packets(output)],
                         [row['pts_time'] for row in packets(sources[0]['path'])])
        embedded = attachment_payloads(output, self.directory)
        self.assertIn(b'single-original-cdr', base64.b64decode(json.loads(
            embedded['camera-info.cdr.jsonl'].splitlines()[0])['cdrBase64']))

    def test_bad_checksum_rejected_before_publishing(self):
        source = self.source('bad-sha')
        source['checksum'] = '0' * 64
        self.assemble([source], success=False)

    def test_bad_expected_attachment_checksum_rejected(self):
        source = self.source('bad-index-sha')
        source['metadata']['videoAttachments'][0]['sha256'] = '0' * 64
        self.assemble([source], success=False)

    def test_mixed_codec_rejected_without_changing_sources(self):
        sources = [self.source('mixed-jpeg'),
                   self.source('mixed-ffv1', codec='ffv1', start_s=Decimal('.16'))]
        self.assemble(sources, success=False)
        for row in sources:
            self.assertEqual(digest(row['path']), row['checksum'])

    def test_incompatible_dimensions_rejected_without_changing_sources(self):
        sources = [self.source('size-64'), self.source('size-80', width=80, start_s=Decimal('.16'))]
        self.assemble(sources, success=False)
        for row in sources:
            self.assertEqual(digest(row['path']), row['checksum'])

    def test_truncated_media_rejected_even_when_declared_checksum_matches(self):
        source = self.source('truncated')
        path = Path(source['path'])
        path.write_bytes(path.read_bytes()[:100])
        source.update(checksum=digest(path), size=str(path.stat().st_size))
        self.assemble([source], success=False)

    def test_source_symlink_rejected(self):
        source = self.source('original')
        link = self.directory / 'linked.mkv'
        link.symlink_to(source['path'])
        source['path'] = str(link)
        self.assemble([source], success=False)

    def test_lexical_parent_traversal_outside_workspace_is_rejected(self):
        source = self.source('inside')
        with tempfile.TemporaryDirectory(prefix='archive-outside-test-') as outside:
            outside_path = Path(outside) / 'outside.mkv'
            shutil.copyfile(source['path'], outside_path)
            source['path'] = str(self.directory / '..' / Path(outside).name / 'outside.mkv')
            self.assertTrue(Path(source['path']).is_relative_to(self.directory))
            self.assertFalse(Path(source['path']).resolve().is_relative_to(self.directory))
            self.assemble([source], success=False)

    def test_overlapping_source_times_are_rejected_without_dropping_frames(self):
        sources = [self.source('overlap-a'), self.source('overlap-b', start_s=Decimal('.08'))]
        self.assemble(sources, success=False)

    def test_missing_part_frame_is_not_silently_accepted(self):
        source = self.source('wrong-count')
        source['frameCount'] += 1
        self.assemble([source], success=False)

    def test_long_logical_timeline_keeps_large_offsets_and_many_part_boundaries(self):
        sources = [self.source(f'long-{i}', frames=2, start_s=i * 3600 + Decimal('.000123') * i)
                   for i in range(16)]
        output, metadata = self.assemble(sources)
        values = [Decimal(row['pts_time']) for row in packets(output)]
        self.assertEqual(len(values), 32)
        for i in range(16):
            self.assertEqual(values[i * 2], Decimal(i * 3600) + Decimal('.000123') * i)
            self.assertEqual(values[i * 2 + 1], values[i * 2] + Decimal('.04'))
        self.assertGreater(metadata['durationSeconds'], 15 * 3600)


class PacketStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='archive-packet-test-')
        self.work = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        specification = importlib.util.spec_from_file_location('archive_assembler_under_test', HELPER)
        self.helper = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(self.helper)

    def fake_probe(self, *, total=1_000_000, overlong=False, returncode=0):
        class Lines:
            emitted = 0
            closed = False
            requested = []

            def readline(stream, limit):
                stream.requested.append(limit)
                if overlong:
                    return b'x' * limit
                if stream.emitted >= total:
                    return b''
                stream.emitted += 1
                return (f'pts_time={stream.emitted}.000000|dts_time={stream.emitted}.000000|'
                        'pos=12|size=4|flags=K_\n').encode()

            def read(stream, *args):
                raise AssertionError('Packet metadata must not be read into one buffer')

            def close(stream):
                stream.closed = True

        class Process:
            stdout = Lines()
            running = True
            killed = False
            waited = 0

            def kill(process):
                process.killed = True
                process.running = False

            def poll(process):
                return None if process.running else returncode

            def wait(process, timeout=None):
                process.waited += 1
                process.running = False
                return returncode

        class Timer:
            started = False
            cancelled = False

            def __init__(timer, delay, callback):
                timer.delay, timer.callback = delay, callback

            def start(timer):
                timer.started = True

            def cancel(timer):
                timer.cancelled = True

        process = Process()
        timers = []

        def create_timer(delay, callback):
            timer = Timer(delay, callback)
            timers.append(timer)
            return timer

        return process, timers, create_timer

    def test_million_packet_stream_stays_lazy_and_cancellation_reaps_probe(self):
        process, timers, timer_factory = self.fake_probe()
        with mock.patch.object(self.helper.subprocess, 'Popen', return_value=process) as popen, \
                mock.patch.object(self.helper.threading, 'Timer', side_effect=timer_factory):
            commands = self.helper.Commands(self.work, 10)
            with self.assertRaisesRegex(RuntimeError, 'consumer cancelled'):
                with commands.packets(self.work / 'unused.mkv') as rows:
                    self.assertEqual(process.stdout.emitted, 0)
                    self.assertEqual(next(rows)['pts_time'], '1.000000')
                    self.assertEqual(next(rows)['pts_time'], '2.000000')
                    self.assertEqual(next(rows)['pts_time'], '3.000000')
                    self.assertEqual(process.stdout.emitted, 3)
                    raise RuntimeError('consumer cancelled')
        self.assertEqual(process.stdout.requested, [16385] * 3)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.killed)
        self.assertGreater(process.waited, 0)
        self.assertTrue(timers[0].started)
        self.assertTrue(timers[0].cancelled)
        self.assertGreater(timers[0].delay, 0)
        self.assertLessEqual(timers[0].delay, 10)
        arguments = popen.call_args.args[0]
        self.assertIn('compact=p=0:nk=0', arguments)
        self.assertNotIn('json', arguments)

    def test_successful_packet_iteration_closes_and_reaps_probe(self):
        process, timers, timer_factory = self.fake_probe(total=3)
        with mock.patch.object(self.helper.subprocess, 'Popen', return_value=process), \
                mock.patch.object(self.helper.threading, 'Timer', side_effect=timer_factory):
            with self.helper.Commands(self.work, 10).packets(self.work / 'unused.mkv') as rows:
                self.assertEqual(len(list(rows)), 3)
        self.assertFalse(process.killed)
        self.assertTrue(process.stdout.closed)
        self.assertGreater(process.waited, 0)
        self.assertTrue(timers[0].cancelled)

    def test_oversized_packet_metadata_fails_and_reaps_probe(self):
        process, timers, timer_factory = self.fake_probe(overlong=True)
        with mock.patch.object(self.helper.subprocess, 'Popen', return_value=process), \
                mock.patch.object(self.helper.threading, 'Timer', side_effect=timer_factory):
            with self.assertRaisesRegex(self.helper.AssemblyError, 'packet_metadata_too_large'):
                with self.helper.Commands(self.work, 10).packets(self.work / 'unused.mkv') as rows:
                    next(rows)
        self.assertTrue(process.killed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(timers[0].cancelled)

    def test_failing_probe_cannot_publish_partial_packet_list(self):
        process, timers, timer_factory = self.fake_probe(total=0, returncode=9)
        with mock.patch.object(self.helper.subprocess, 'Popen', return_value=process), \
                mock.patch.object(self.helper.threading, 'Timer', side_effect=timer_factory):
            with self.assertRaisesRegex(self.helper.AssemblyError, 'packet_probe_failed'):
                with self.helper.Commands(self.work, 10).packets(self.work / 'unused.mkv') as rows:
                    list(rows)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(timers[0].cancelled)

    def test_real_stalled_child_is_killed_at_deadline(self):
        original_popen = subprocess.Popen
        children = []

        def stalled_probe(arguments, **kwargs):
            process = original_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(process)
            return process

        started = time.monotonic()
        with mock.patch.object(self.helper.subprocess, 'Popen', side_effect=stalled_probe):
            with self.assertRaises(self.helper.AssemblyError):
                with self.helper.Commands(self.work, .15).packets(self.work / 'unused.mkv') as rows:
                    list(rows)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)


if __name__ == '__main__':
    unittest.main()

