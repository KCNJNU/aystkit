"""Offline RGB archive compression, preserving frame order and VFR.

Only already sealed video files are read. This module never opens a camera,
changes a capture service, or publishes an archive manifest. Depth and other
greater-than-eight-bit tracks are explicitly ineligible for lossy compression.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile
import time

from .archive_core import valid_file_size


class HevcCompressionError(RuntimeError):
    pass


class HevcNotSmaller(HevcCompressionError):
    pass


_RGB_FORMATS = frozenset(('rgb24', 'bgr24', 'bgr0', 'bgra', 'rgba',
    'yuv420p', 'yuv422p', 'yuv444p', 'yuvj420p', 'yuvj422p', 'yuvj444p'))
_MAX_JSON = 64 * 1024**2


def _remaining(deadline):
    value = deadline-time.monotonic()
    if value <= 0:
        raise HevcCompressionError('hevc_compression_timeout')
    return value


def _run(arguments, work, deadline, name, *, json_output=False):
    output = work/(name+'.stdout')
    with output.open('xb') as out, (work/(name+'.stderr')).open('xb') as errors:
        try:
            result = subprocess.run(arguments, stdout=out, stderr=errors,
                timeout=_remaining(deadline), check=False)
        except subprocess.TimeoutExpired as error:
            raise HevcCompressionError('hevc_compression_timeout') from error
        except OSError as error:
            raise HevcCompressionError('hevc_tool_unavailable') from error
    if result.returncode:
        raise HevcCompressionError('hevc_'+name+'_failed')
    if json_output:
        with output.open('rb') as stream:
            raw = stream.read(_MAX_JSON+1)
        if len(raw) > _MAX_JSON:
            raise HevcCompressionError('hevc_probe_too_large')
        try:
            return json.loads(raw)
        except ValueError as error:
            raise HevcCompressionError('hevc_probe_invalid') from error


def _inspect(path, ffprobe, work, deadline, name):
    # Combining show_packets and show_data makes ffprobe format a hexdump of
    # every large packet even when show_entries excludes packet.data. Read
    # packet metadata in one full pass and CodecPrivate in a stream-only pass.
    base = [ffprobe, '-v', 'error', '-threads', '1', '-analyzeduration', '1',
            '-probesize', '32768', '-fpsprobesize', '0', '-select_streams', 'v:0']
    packet_value = _run(base + ['-show_packets', '-show_entries',
        'packet=pos,size,flags,pts_time,dts_time,duration_time', '-of', 'json', str(path)],
        work, deadline, name+'-packets', json_output=True)
    stream_value = _run(base + ['-show_streams', '-show_data', '-show_entries',
        'stream=codec_name,width,height,pix_fmt,has_b_frames,color_range,color_space,extradata',
        '-of', 'json', str(path)], work, deadline, name+'-stream', json_output=True)
    streams, packets = stream_value.get('streams'), packet_value.get('packets')
    if (not isinstance(streams, list) or len(streams) != 1 or not isinstance(packets, list)
            or not 1 <= len(packets) <= 200_000):
        raise HevcCompressionError('hevc_video_structure_invalid')
    return streams[0], packets


def _pts(packet):
    try:
        return int(Decimal(packet['pts_time'])*1_000_000)
    except (KeyError, TypeError, ValueError) as error:
        raise HevcCompressionError('hevc_timestamp_invalid') from error


def _signed(identity, value):
    from .archive_video import _element
    for length in range(1, 9):
        if -(1 << (8*length-1)) <= value < (1 << (8*length-1)):
            return _element(identity, value.to_bytes(length, 'big', signed=True))
    raise HevcCompressionError('hevc_reference_timestamp_invalid')


def _write_hevc(path, encoded, packets, timings, dimensions, private, attachments, full_range):
    from .archive_video import (_element, _number, _text, _write_attachments, _packet_data)
    if not timings or timings[0] != 0 or any(b <= a for a, b in zip(timings, timings[1:])):
        raise HevcCompressionError('hevc_source_timestamps_invalid')
    positive = sorted(b-a for a, b in zip(timings, timings[1:]))
    tail = positive[len(positive)//2] if positive else 33_333
    duration = timings[-1]+tail
    header = _element('1A45DFA3', _number('4286', 1)+_number('42F7', 1)+_number('42F2', 4)
        +_number('42F3', 8)+_text('4282', 'matroska')+_number('4287', 4)+_number('4285', 2))
    info = _element('1549A966', _number('2AD7B1', 1000)+_text('4D80', 'robot-native-archive')
        +_text('5741', 'robot-native-archive')+_element('4489', struct.pack('>d', float(duration))))
    colour = _element('55B0', _number('55B1', 5)+_number('55B2', 8)
        +_number('55B9', 2 if full_range else 1)+_number('55BA', 6)+_number('55BB', 5))
    track = _element('1654AE6B', _element('AE', _number('D7', 1)+_number('73C5', 1)
        +_number('83', 1)+_number('9C', 0)+_text('86', 'V_MPEGH/ISO/HEVC')+_element('63A2', private)
        +_element('E0', _number('B0', dimensions[0])+_number('BA', dimensions[1])+colour)))
    def seekhead(position):
        return _element('114D9B74', _element('4DBB', _element('53AB', bytes.fromhex('1C53BB6B'))
            +_element('53AC', position.to_bytes(8, 'big'))))
    with path.open('xb') as output, encoded.open('rb') as source:
        os.chmod(path, 0o600)
        output.write(header+bytes.fromhex('1853806701ffffffffffffff'))
        segment_start = output.tell();head_position = output.tell();output.write(seekhead(0))
        output.write(info+track)
        if attachments:
            _write_attachments(output, attachments)
        cues = []
        for index, (packet, timestamp) in enumerate(zip(packets, timings)):
            keyframe = 'K' in packet.get('flags', '')
            if index == 0 and not keyframe:
                raise HevcCompressionError('hevc_initial_keyframe_missing')
            position = output.tell()-segment_start
            payload = _packet_data(source, packet)
            block = _element('A1', b'\x81'+struct.pack('>hB', 0, 0)+payload)
            end = timings[index+1] if index+1 < len(timings) else duration
            group = block+_number('9B', end-timestamp)
            if not keyframe:
                # Encoder uses no B frames and one reference frame. A negative
                # ReferenceBlock makes dependency/seek semantics truthful.
                group += _signed('FB', timings[index-1]-timestamp)
            else:
                cues.append((timestamp, position))
            output.write(_element('1F43B675', _number('E7', timestamp)+_element('A0', group)))
        cues_position = output.tell()-segment_start
        output.write(_element('1C53BB6B', b''.join(_element('BB', _number('B3', timestamp)
            +_element('B7', _number('F7', 1)+_number('F1', position))) for timestamp, position in cues)))
        output.seek(head_position);output.write(seekhead(cues_position));output.flush()
        os.fsync(output.fileno())


def output_dimensions(width, height, max_width=None, max_height=None):
    """Fit within even pixel bounds without enlarging or cropping the source."""
    if max_width is None and max_height is None:
        return width, height
    if (type(max_width) is not int or type(max_height) is not int
            or not 16 <= max_width <= 8192 or not 16 <= max_height <= 8192
            or max_width % 2 or max_height % 2):
        raise ValueError('invalid HEVC output dimensions')
    numerator, denominator = 1, 1
    if width > max_width:
        numerator, denominator = max_width, width
    if height * numerator > max_height * denominator:
        numerator, denominator = max_height, height
    target = tuple(value*numerator//denominator//2*2 for value in (width, height))
    if min(target) < 16:
        raise HevcCompressionError('hevc_scaled_dimensions_unsupported')
    return target


def hardware_arguments(source, target, codec, full_range, qp_i, qp_p, gop_frames, gst_launch,
                       *, dimensions=None, bitrate_bps=None, resized=False):
    # The hardware decoder accepts the native 4:2:2 MJPEG stream. Keep its
    # full-range samples through NVMM; the later SPS correction labels them.
    # FFV1/RGB still uses an explicit BT.601 conversion before hardware input.
    if codec == 'mjpeg':
        decode = ['jpegparse', '!', 'nvv4l2decoder', 'mjpeg=true']
    else:
        colour = '1:4:0:0' if full_range else 'bt601'
        decode = ['avdec_ffv1', '!', 'videoconvert', '!',
                  'video/x-raw,format=I420,colorimetry='+colour]
    caps = 'video/x-raw(memory:NVMM),format=I420'
    if dimensions is not None:
        caps += ',width='+str(dimensions[0])+',height='+str(dimensions[1])
    rate = (['preset-level=1', 'ratecontrol-enable=true', 'control-rate=1',
             'bitrate='+str(bitrate_bps)] if bitrate_bps is not None else
            ['preset-level=0', 'ratecontrol-enable=false', 'quant-i-frames='+str(qp_i),
             'quant-p-frames='+str(qp_p)])
    # gst-inspect on the source Jetson reports 0=Nearest (the default),
    # 3=10-Tap and 5=Nicest. Only downscaled videos request Nicest.
    converter = ['nvvidconv', 'interpolation-method=5'] if resized else ['nvvidconv']
    return [gst_launch, '-q', 'filesrc', 'location='+str(source), '!', 'matroskademux', '!',
        *decode, '!', *converter, '!', caps, '!', 'nvv4l2h265enc',
        *rate, 'num-B-Frames=0', 'num-Ref-Frames=1',
        'iframeinterval='+str(gop_frames), 'idrinterval='+str(gop_frames),
        'insert-sps-pps=true', 'copy-timestamp=true', '!', 'h265parse', '!',
        'matroskamux', 'timecodescale=1000', '!', 'filesink', 'location='+str(target)]


def _software_arguments(source, target, full_range, crf, gop_frames, ffmpeg,
                        *, dimensions=None, bitrate_bps=None, resized=False):
    range_name = 'pc' if full_range else 'tv'
    scale = 'scale='
    if dimensions is not None:
        scale += str(dimensions[0])+':'+str(dimensions[1])+':'
    if resized:
        scale += 'flags=lanczos:'
    scale += 'out_range='+range_name+':out_color_matrix=bt601,format=yuv420p'
    rate = (['-b:v', str(bitrate_bps), '-minrate', str(bitrate_bps), '-maxrate', str(bitrate_bps),
             '-bufsize', str(bitrate_bps)] if bitrate_bps is not None else ['-crf', str(crf)])
    parameters = ('bframes=0:keyint='+str(gop_frames)+':min-keyint=1:scenecut=0:'
        'pools=1:frame-threads=1:log-level=error:range='+('full' if full_range else 'limited'))
    if bitrate_bps is not None:
        parameters += ':strict-cbr=1'
    return [ffmpeg, '-nostdin', '-v', 'error', '-threads', '1',
        '-filter_threads', '1', '-filter_complex_threads', '1', '-i', str(source), '-map', '0:v:0', '-an',
        '-threads', '1', '-vf', scale, '-c:v', 'libx265', '-preset', 'fast', *rate,
        '-x265-params', parameters,
        '-color_range', range_name, '-colorspace', 'bt470bg', '-color_primaries', 'bt470bg',
        '-color_trc', 'smpte170m', '-vsync', '0', str(target)]


def compress_rgb_mkv(source, destination, *, qp_i, qp_p, attachments=(), backend='jetson',
                     crf=18, gop_frames=50, timeout_s=120, require_smaller=True,
                     gst_launch='gst-launch-1.0', ffmpeg='ffmpeg', ffprobe='ffprobe',
                     max_width=None, max_height=None, bitrate_bps=None):
    """Create a verified HEVC candidate from a sealed visual camera video.

    The caller explicitly chooses quality. ``backend='software'`` is useful for
    isolated tests or a deliberately configured CPU fallback; it is never an
    automatic replacement for a failed hardware encoder.
    """
    from .archive_video import _digest, _private_data, _sync_directory
    source, destination = Path(source), Path(destination)
    if (source.is_symlink() or not source.is_file() or source.resolve() == destination.resolve()
            or destination.exists() or destination.is_symlink() or not destination.parent.is_dir()):
        raise HevcCompressionError('hevc_input_or_destination_invalid')
    info = source.stat()
    if not stat.S_ISREG(info.st_mode) or not valid_file_size(info.st_size):
        raise HevcCompressionError('hevc_source_size_invalid')
    if (backend not in ('jetson', 'software') or type(qp_i) is not int or type(qp_p) is not int
            or not 0 <= qp_i <= qp_p <= 40 or type(gop_frames) is not int or not 1 <= gop_frames <= 300
            or not math.isfinite(timeout_s) or timeout_s <= 0 or not 0 <= crf <= 35):
        raise ValueError('invalid HEVC quality or process limits')
    if ((max_width is None) != (max_height is None)
            or (bitrate_bps is not None and (type(bitrate_bps) is not int
                or not 100_000 <= bitrate_bps <= 20_000_000))):
        raise ValueError('invalid HEVC scale or bitrate limits')
    # Validate optional bounds before starting any codec or writing a candidate.
    output_dimensions(16, 16, max_width, max_height)
    deadline = time.monotonic()+timeout_s
    source_sha = _digest(source)
    with tempfile.TemporaryDirectory(prefix='.hevc-working-', dir=destination.parent) as directory:
        work = Path(directory)
        original, source_packets = _inspect(source, ffprobe, work, deadline, 'source-probe')
        width, height = original.get('width'), original.get('height')
        if (original.get('codec_name') not in ('mjpeg', 'ffv1') or original.get('pix_fmt') not in _RGB_FORMATS):
            raise HevcCompressionError('hevc_rgb_only_depth_is_ineligible')
        if (type(width) is not int or type(height) is not int or width < 16 or height < 16
                or width % 2 or height % 2):
            raise HevcCompressionError('hevc_rgb_dimensions_unsupported')
        target_width, target_height = output_dimensions(width, height, max_width, max_height)
        dimensions = (target_width, target_height) if max_width is not None else None
        resized = (target_width, target_height) != (width, height)
        timings = [_pts(packet) for packet in source_packets]
        full_range = original.get('codec_name') == 'mjpeg' or original.get('color_range') == 'pc'
        # RGB FFV1 is converted to limited-range BT.601 by videoconvert; only
        # existing JPEG/YUV full-range values retain PC-range coding.
        if original.get('pix_fmt') in ('rgb24', 'bgr24', 'bgr0', 'rgba', 'bgra'):
            full_range = False
        encoded = work/'hardware.mkv'
        if backend == 'jetson':
            arguments = hardware_arguments(source, encoded, original['codec_name'], full_range,
                qp_i, qp_p, gop_frames, gst_launch,
                dimensions=dimensions, bitrate_bps=bitrate_bps, resized=resized)
        else:
            arguments = _software_arguments(source, encoded, full_range, crf, gop_frames, ffmpeg,
                dimensions=dimensions, bitrate_bps=bitrate_bps, resized=resized)
        started = time.monotonic();_run(arguments, work, deadline, 'encoder')
        elapsed = time.monotonic()-started
        corrected = work/'corrected.mkv'
        # Jetson's encoder marks PC JPEG samples as limited range. Correct the
        # SPS metadata without changing the already encoded pixel packets.
        vui = ('hevc_metadata=video_full_range_flag='+('1' if full_range else '0')
            +':matrix_coefficients=5:colour_primaries=5:transfer_characteristics=6')
        _run([ffmpeg, '-nostdin', '-v', 'error', '-i', str(encoded), '-map', '0:v:0',
              '-c:v', 'copy', '-bsf:v', vui, str(corrected)], work, deadline, 'range-metadata')
        actual, packets = _inspect(corrected, ffprobe, work, deadline, 'encoded-probe')
        if (actual.get('codec_name') != 'hevc' or actual.get('has_b_frames') != 0
                or (actual.get('width'), actual.get('height')) != (target_width, target_height)
                or len(packets) != len(source_packets)):
            raise HevcCompressionError('hevc_frame_count_dimensions_or_reorder_mismatch')
        if any(_pts(right) <= _pts(left) for left, right in zip(packets, packets[1:])):
            raise HevcCompressionError('hevc_encoded_packet_order_invalid')
        output = work/'video.mkv'
        _write_hevc(output, corrected, packets, timings, (target_width, target_height),
                    _private_data(actual.get('extradata')), attachments, full_range)
        ready, ready_packets = _inspect(output, ffprobe, work, deadline, 'final-probe')
        if (len(ready_packets) != len(source_packets) or [_pts(packet) for packet in ready_packets] != timings
                or (ready.get('width'), ready.get('height')) != (target_width, target_height)
                or ready.get('color_range') != ('pc' if full_range else 'tv')
                or ready.get('color_space') != 'bt470bg'):
            raise HevcCompressionError('hevc_final_timestamps_or_colour_invalid')
        if (source.stat().st_size, source.stat().st_mtime_ns, _digest(source)) != (info.st_size, info.st_mtime_ns, source_sha):
            raise HevcCompressionError('hevc_source_changed')
        result = {'videoCodec': 'hevc', 'videoContainer': 'matroska', 'videoReady': True,
            'receivedFrames': len(source_packets), 'width': target_width, 'height': target_height,
            'videoPixelFormat': ready.get('pix_fmt'),
            'sourceWidth': width, 'sourceHeight': height,
            'targetWidth': target_width, 'targetHeight': target_height,
            'reencoded': True, 'resized': resized, 'losslessPixelValues': False,
            'videoCompression': 'high-quality-lossy-rgb', 'videoEncoder': backend,
            'videoQpI': qp_i if backend == 'jetson' and bitrate_bps is None else None,
            'videoQpP': qp_p if backend == 'jetson' and bitrate_bps is None else None,
            'videoCrf': crf if backend == 'software' and bitrate_bps is None else None,
            'videoColourRange': 'full' if full_range else 'limited',
            'videoColourMatrix': 'BT.601', 'videoEncodingSeconds': elapsed,
            'videoOriginalContainerBytes': info.st_size, 'videoOriginalContainerSha256': source_sha,
            'videoBytes': output.stat().st_size, 'videoSha256': _digest(output),
            'videoTimecodeScaleNs': 1000, 'videoBFrames': 0, 'videoReferenceFrames': 1}
        if resized:
            result['videoResizeInterpolation'] = 'nvvidconv-nicest' if backend == 'jetson' else 'lanczos'
        if bitrate_bps is not None:
            result.update(videoBitrateBps=bitrate_bps, videoRateControl='constant-bitrate')
            presets = {1_200_000: '720p-economy-v1', 2_000_000: '720p-balanced-v1',
                       3_000_000: '720p-quality-v1'}
            result['videoArchivePreset'] = (presets.get(bitrate_bps, 'custom')
                if (max_width, max_height) == (1280, 720) else 'custom')
            result['videoCompression'] = 'bitrate-limited-lossy-rgb'
        result['videoSizeReductionFraction'] = 1-result['videoBytes']/info.st_size
        if require_smaller and result['videoBytes'] >= info.st_size:
            raise HevcNotSmaller('hevc_candidate_not_smaller')
        output.replace(destination)
        _sync_directory(destination.parent)
        return result


def measure_quality(source, candidate, *, ffmpeg='ffmpeg', timeout_s=180):
    """Full-frame SSIM/PSNR acceptance measurement, aligned by frame index.

    This is an explicit verification operation, not implicit per-segment work.
    Matroska timebase quantization cannot shift the compared frames.
    """
    source, candidate = Path(source), Path(candidate)
    if not source.is_file() or not candidate.is_file():
        raise HevcCompressionError('hevc_quality_source_missing')
    with tempfile.TemporaryDirectory(prefix='hevc-quality-') as directory:
        work = Path(directory);deadline=time.monotonic()+timeout_s
        filters = ('[0:v]setpts=N/TB,split=2[a][c];[1:v]setpts=N/TB,split=2[b][d];'
                   '[a][b]ssim;[c][d]psnr')
        _run([ffmpeg, '-nostdin', '-v', 'info', '-i', str(source), '-i', str(candidate),
              '-filter_complex', filters, '-vsync', '0', '-f', 'null', '-'], work, deadline, 'quality')
        text = (work/'quality.stderr').read_text()
        ssim = re.findall(r'SSIM .*All:([0-9.]+)', text)
        psnr = re.findall(r'PSNR .*average:([0-9.]+|inf)', text)
        if not ssim or not psnr:
            raise HevcCompressionError('hevc_quality_metrics_missing')
        return {'ssim': float(ssim[-1]), 'psnrDb': None if psnr[-1] == 'inf' else float(psnr[-1]),
                'psnrInfinite': psnr[-1] == 'inf', 'alignment': 'frame-index', 'fullResolution': True}
