"""Real lossless container checks using isolated generated pixels, never devices."""
from pathlib import Path
import base64
import fcntl
import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from collector.archive_video import (VideoFinalizer, VideoFinalizeBusy, VideoFinalizeError,
    finalize_pending, _normalize_image)

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                                reason='real ffmpeg and ffprobe are required')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def manifest(path, source, files, **metadata):
    value = {'schemaVersion': 1, 'id': path.name, 'day': '2026-10-28', 'sensor': '相机',
        'label': '相机_14:50-14:50', 'robotId': 'robot-fixture',
        'startedAt': '2026-10-28T06:50:00+00:00', 'endAt': '2026-10-28T06:50:01+00:00',
        'metadata': {'scope': 'garage', 'source': source, 'videoRequired': True, **metadata}, 'files': files}
    value['metadata']['sourceFiles'] = [{'relativePath': name, 'size': (path/name).stat().st_size,
        'sha256': sha((path/name).read_bytes())} for name in files]
    (path/'video-pending.json').write_text(json.dumps(value))
    return value


def native(path, count=3):
    path.mkdir(parents=True)
    raw, rows, offset = [], [], 0
    for i in range(count):
        ok, encoded = cv2.imencode('.jpg', np.full((24, 32, 3), 45+i*65, dtype=np.uint8))
        assert ok
        packet = encoded.tobytes();raw.append(packet)
        rows.append({'sequence': i+1, 'timestampNs': 1_000_000_000+[0, 33_333_000, 85_000_000][i],
            'wallTime': 1.0+i/30, 'offset': offset, 'length': len(packet), 'width': 32, 'height': 24,
            'sha256': sha(packet), 'gapBefore': 0, 'sequenceReset': False})
        offset += len(packet)
    (path/'frames.mjpg').write_bytes(b''.join(raw))
    (path/'index.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    manifest(path, 'native-camera-jpeg', ['frames.mjpg', 'index.jsonl'], receivedFrames=count,
        framesSha256=sha(b''.join(raw)), indexSha256=sha((path/'index.jsonl').read_bytes()))
    return raw


def probe(path):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_packets', '-show_streams', '-show_format', '-of', 'json', str(path)], text=True))


def attachment(path, output, index=0):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-dump_attachment:t:'+str(index), str(output),
        '-i', str(path), '-map', '0:v:0', '-c:v', 'copy', '-f', 'null', '-'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    return output.read_bytes()


def test_native_jpeg_is_one_video_with_exact_original_frames_and_index_attachment(tmp_path):
    raw = native(tmp_path/'segment')
    path = tmp_path/'segment';before = {p.name: p.read_bytes() for p in path.iterdir()}
    result = finalize_pending(path, reserve_bytes=0)
    assert result['files'] == ['video.mkv']
    assert result['metadata']['videoReady'] and not result['metadata']['reencoded']
    meta = probe(path/'video.mkv')
    assert meta['streams'][0]['codec_name'] == 'mjpeg'
    assert [row['pts_time'] for row in meta['packets']] == ['0.000000', '0.033333', '0.085000']
    actual = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path/'video.mkv'),
        '-map', '0:v:0', '-c:v', 'copy', '-f', 'image2pipe', 'pipe:1'])
    assert actual == b''.join(raw)
    assert attachment(path/'video.mkv', tmp_path/'embedded.jsonl') == before['index.jsonl']
    assert all((path/name).read_bytes() == content for name, content in before.items())
    assert finalize_pending(path, reserve_bytes=0) == result


def test_single_frame_short_session_has_a_playable_positive_duration(tmp_path):
    path=tmp_path/'one';native(path, 1)
    finalize_pending(path, reserve_bytes=0)
    result=probe(path/'video.mkv')
    assert len(result['packets'])==1
    assert float(result['format']['duration']) > 0


def image(data, width, height, encoding, step, big=False):
    return SimpleNamespace(data=data, width=width, height=height, encoding=encoding, step=step,
        is_bigendian=big, header=SimpleNamespace(frame_id='depth-optical',
            stamp=SimpleNamespace(sec=123, nanosec=456)))


def ros_segment(path):
    path.mkdir();(path/'bag').mkdir();(path/'bag/raw.db3').write_bytes(b'ISOLATED ROS READER FIXTURE')
    manifest(path, 'native-ros-cdr', ['bag/raw.db3'])


def test_ffv1_depth_preserves_uint16_values_stride_endianness_and_camera_info(tmp_path, monkeypatch):
    from collector import archive_video
    original_process, verification = archive_video._timed_process, []
    def tracked_process(arguments, *args, **options):
        if arguments[-1] == 'pipe:1':
            verification.append(arguments)
        return original_process(arguments, *args, **options)
    monkeypatch.setattr(archive_video, '_timed_process', tracked_process)
    path=tmp_path/'depth';ros_segment(path)
    values=np.array([[0,1,65535,32768,256], [9,63,255,256,900], [40000,50000,60000,128,42]], dtype=np.uint16)
    rows=[];expected=b''
    for i,big in enumerate((True,False,True)):
        current=(values+i).astype(np.uint16);canonical=current.astype('<u2').tobytes();expected+=canonical
        source=current.astype('>u2' if big else '<u2').tobytes()
        padded=b''.join(source[row*10:(row+1)*10]+b'\x12\x34' for row in range(3))
        rows.append(('/depth', 'sensor_msgs/msg/Image', 1_000_000_000+[0,33_333_000,85_000_000][i],
                     image(padded,5,3,'16UC1',12,big)))
    rows.insert(0,('/depth/camera_info','sensor_msgs/msg/CameraInfo',999_999_999,b'original-camera-info-cdr'))
    result=finalize_pending(path,reserve_bytes=0,ros_reader=lambda *_:iter(rows))
    assert result['files']==['video.mkv'] and result['metadata']['videoPixelFormat']=='gray16le'
    assert result['metadata']['losslessPixelValues'] and result['metadata']['receivedFrames']==3
    assert len(verification) == 1
    arguments = verification[0]
    split = arguments.index('-i')
    assert arguments[:split][-6:] == ['-threads', '1', '-filter_threads', '1', '-filter_complex_threads', '1']
    assert arguments[split+2:].count('-threads') == 1 and arguments[arguments.index('-map')+2:][:2] == ['-threads', '1']
    decoded=subprocess.check_output(['ffmpeg','-v','error','-i',str(path/'video.mkv'),'-map','0:v:0',
        '-vsync','0','-f','rawvideo','-pix_fmt','gray16le','pipe:1'])
    assert decoded==expected
    indexes=[json.loads(line) for line in attachment(path/'video.mkv',tmp_path/'frames.jsonl').splitlines()]
    assert indexes[0]['isBigendian'] and indexes[0]['step']==12
    assert base64.b64decode(indexes[0]['rowPaddingBase64'])==b'\x12\x34'*3
    info=json.loads(attachment(path/'video.mkv',tmp_path/'info.jsonl',1))
    assert base64.b64decode(info['cdrBase64'])==b'original-camera-info-cdr'
    assert [row['pts_time'] for row in probe(path/'video.mkv')['packets']]==['0.000000','0.033333','0.085000']


@pytest.mark.parametrize('encoding,channels,format', [('rgb8',3,'rgb24'),('bgr8',3,'bgr24'),
    ('rgba8',4,'rgba'),('bgra8',4,'bgra'),('mono8',1,'gray'),('mono16',2,'gray16le')])
def test_supported_ros_formats_decode_to_exact_original_pixels(tmp_path,encoding,channels,format):
    path=tmp_path/encoding;ros_segment(path)
    pixels=bytes((i*29)%256 for i in range(8*6*channels))
    rows=[('/image','sensor_msgs/msg/Image',1_000_000_000,image(pixels,8,6,encoding,8*channels))]
    finalize_pending(path,reserve_bytes=0,ros_reader=lambda *_:iter(rows))
    actual=subprocess.check_output(['ffmpeg','-v','error','-i',str(path/'video.mkv'),'-map','0:v:0',
        '-vsync','0','-f','rawvideo','-pix_fmt',format,'pipe:1'])
    assert actual==pixels


def test_nonimage_ros_topics_are_never_silently_discarded(tmp_path):
    path=tmp_path/'points';ros_segment(path)
    with pytest.raises(VideoFinalizeError,match='nonimage_topic'):
        finalize_pending(path,reserve_bytes=0,ros_reader=lambda *_:iter([('/points','sensor_msgs/msg/PointCloud2',1,b'raw')]))
    assert (path/'bag/raw.db3').read_bytes()==b'ISOLATED ROS READER FIXTURE'
    assert not (path/'archive.json').exists()


def test_codec_failure_keeps_pending_sources_and_can_retry(tmp_path):
    path=tmp_path/'retry';ros_segment(path)
    rows=[('/image','sensor_msgs/msg/Image',1_000_000_000,image(b'\x01'*48,8,6,'mono8',8))]
    with pytest.raises(VideoFinalizeError):
        finalize_pending(path,reserve_bytes=0,ffmpeg='/bin/false',ros_reader=lambda *_:iter(rows))
    assert (path/'video-pending.json').is_file() and (path/'bag/raw.db3').is_file()
    assert not (path/'archive.json').exists() and not list(path.glob('.video-working-*'))
    assert finalize_pending(path,reserve_bytes=0,ros_reader=lambda *_:iter(rows))['files']==['video.mkv']


def test_concurrent_finalizer_cannot_publish_or_overwrite_sources(tmp_path):
    path=tmp_path/'busy';native(path,1)
    with (path/'.video-finalize.lock').open('w') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(VideoFinalizeBusy):finalize_pending(path,reserve_bytes=0)
    assert not (path/'archive.json').exists()


def test_corrupt_native_source_stays_pending(tmp_path):
    path=tmp_path/'bad';native(path,1);(path/'frames.mjpg').write_bytes(b'not a frame')
    with pytest.raises(VideoFinalizeError,match='source_manifest_checksum'):
        finalize_pending(path,reserve_bytes=0)
    assert (path/'frames.mjpg').read_bytes()==b'not a frame'
    assert not (path/'archive.json').exists()


def test_insufficient_space_does_not_create_a_derivative(tmp_path):
    path=tmp_path/'space';native(path,1)
    with pytest.raises(VideoFinalizeError,match='working_space'):
        finalize_pending(path,reserve_bytes=2**63)
    assert not (path/'video.mkv').exists()


def test_run_once_only_processes_sealed_day_segments(tmp_path):
    active=tmp_path/'.camera-active/active';native(active,1)
    sealed=tmp_path/'days/2026-10-28/相机/时段/sealed';native(sealed,1)
    worker=VideoFinalizer(tmp_path,'robot-fixture',reserve_bytes=0)
    assert worker.status()['pendingSegments']==1
    assert worker.run_once()==1 and worker.run_once()==0
    assert not (active/'archive.json').exists()
    assert worker.status()['pendingSegments']==0


def test_bad_stride_is_rejected_instead_of_dropping_pixels():
    with pytest.raises(VideoFinalizeError,match='stride'):
        _normalize_image(image(b'\x00'*6,4,1,'mono16',6))


def test_interrupted_derivatives_and_orphan_video_retry_without_touching_raw(tmp_path):
    path=tmp_path/'interrupted';raw=native(path,1)
    working=path/'.video-working-abandoned';working.mkdir()
    (working/'ffv1-intermediate.mkv').write_bytes(b'partial derivative')
    (path/'video.mkv').write_bytes(b'orphan video before manifest commit')
    result=finalize_pending(path,reserve_bytes=0)
    assert result['metadata']['videoReady']
    assert not working.exists() and (path/'frames.mjpg').read_bytes()==raw[0]


def test_codec_timeout_is_bounded_and_keeps_raw_for_retry(tmp_path):
    path=tmp_path/'timeout';ros_segment(path)
    tool=tmp_path/'sleep-codec';tool.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(30)\n');tool.chmod(0o700)
    rows=[('/image','sensor_msgs/msg/Image',1_000_000_000,image(b'\x01'*48,8,6,'mono8',8))]
    with pytest.raises(VideoFinalizeError):
        finalize_pending(path,reserve_bytes=0,ffmpeg=str(tool),timeout_s=.15,ros_reader=lambda *_:iter(rows))
    assert (path/'video-pending.json').is_file() and not (path/'archive.json').exists()
    assert not list(path.glob('.video-working-*'))


def test_default_ros_reader_uses_closed_bag_without_changing_original(tmp_path,monkeypatch):
    path=tmp_path/'sqlite-bag';path.mkdir();(path/'bag').mkdir()
    db=path/'bag/camera.db3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE topics (id INTEGER PRIMARY KEY,name TEXT,type TEXT)')
        connection.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY,topic_id INTEGER,timestamp INTEGER,data BLOB)')
        connection.executemany('INSERT INTO topics VALUES (?,?,?)',[(1,'/image','sensor_msgs/msg/Image'),(2,'/camera_info','sensor_msgs/msg/CameraInfo')])
        connection.executemany('INSERT INTO messages VALUES (?,?,?,?)',[(1,2,999_999_999,b'camera-info-cdr'),(2,1,1_000_000_000,b'image-cdr')])
    original=db.read_bytes();manifest(path,'native-ros-cdr',['bag/camera.db3'])
    from collector import archive_video
    monkeypatch.setattr(archive_video,'_image_message',lambda value:image(bytes(range(48)),8,6,'mono8',8))
    result=finalize_pending(path,reserve_bytes=0)
    assert result['metadata']['receivedFrames']==1 and db.read_bytes()==original
    assert result['metadata']['cameraInfoMessages']==1


def test_confirmed_pruned_video_is_not_regenerated(tmp_path):
    path=tmp_path/'pruned';native(path,1);result=finalize_pending(path,reserve_bytes=0)
    info=result['metadata']
    (path/'uploaded.json').write_text(json.dumps({'state':'complete','manifestSha256':sha((path/'archive.json').read_bytes()),
        'files':[{'relativePath':'video.mkv','sha256':info['videoSha256'],'size':info['videoBytes'],'robotFileId':'fixture-file'}]}))
    (path/'local-copy-pruned.json').write_text(json.dumps({'state':'pruned'}));(path/'video.mkv').unlink()
    assert finalize_pending(path,reserve_bytes=0)==result
    assert not (path/'video.mkv').exists()


@pytest.mark.parametrize('problem', ['image-topic','image-type','image-count','missing-camera-info',
    'camera-info-count','image-topic-list','message-count'])
def test_sealed_ros_topic_contract_must_match_all_actual_messages(tmp_path,problem):
    path=tmp_path/problem;ros_segment(path)
    value=json.loads((path/'video-pending.json').read_text())
    value['metadata'].update(topics={
        '/image':{'type':'sensor_msgs/msg/Image','count':1},
        '/camera_info':{'type':'sensor_msgs/msg/CameraInfo','count':1}},
        videoImageTopics=['/image'],receivedMessages=2,receivedFrames=1)
    rows=[('/camera_info','sensor_msgs/msg/CameraInfo',999_999_999,b'info-cdr'),
          ('/image','sensor_msgs/msg/Image',1_000_000_000,image(b'\x01'*48,8,6,'mono8',8))]
    if problem=='image-topic':
        rows[-1]=('/other-image',*rows[-1][1:])
    elif problem=='image-type':
        value['metadata']['topics']['/image']['type']='sensor_msgs/msg/CameraInfo'
    elif problem=='image-count':
        value['metadata']['topics']['/image']['count']=2
    elif problem=='missing-camera-info':
        rows.pop(0)
    elif problem=='camera-info-count':
        value['metadata']['topics']['/camera_info']['count']=2
    elif problem=='image-topic-list':
        value['metadata']['videoImageTopics']=['/not-the-image']
    else:
        value['metadata']['receivedMessages']=3
    (path/'video-pending.json').write_text(json.dumps(value))
    with pytest.raises(VideoFinalizeError,match='contract|message_count'):
        finalize_pending(path,reserve_bytes=0,ros_reader=lambda *_:iter(rows))
    assert not (path/'archive.json').exists() and (path/'bag/raw.db3').is_file()


def test_matching_sealed_ros_contract_is_published(tmp_path):
    path=tmp_path/'valid-contract';ros_segment(path)
    value=json.loads((path/'video-pending.json').read_text())
    value['metadata'].update(topics={'/image':{'type':'sensor_msgs/msg/Image','count':1},
        '/camera_info':{'type':'sensor_msgs/msg/CameraInfo','count':1}},
        videoImageTopics=['/image'],receivedMessages=2,receivedFrames=1)
    (path/'video-pending.json').write_text(json.dumps(value))
    rows=[('/camera_info','sensor_msgs/msg/CameraInfo',999_999_999,b'info-cdr'),
          ('/image','sensor_msgs/msg/Image',1_000_000_000,image(b'\x01'*48,8,6,'mono8',8))]
    result=finalize_pending(path,reserve_bytes=0,ros_reader=lambda *_:iter(rows))
    assert result['metadata']['topics']==value['metadata']['topics']
    assert result['metadata']['receivedFrames']==1 and result['metadata']['cameraInfoMessages']==1


def test_external_success_clears_old_worker_error(tmp_path):
    sealed=tmp_path/'days/2026-10-28/相机/时段/sealed';native(sealed,1)
    worker=VideoFinalizer(tmp_path,'robot-fixture',reserve_bytes=0)
    worker.errors[str(sealed.relative_to(tmp_path/'days'))]='previous failed attempt'
    finalize_pending(sealed,reserve_bytes=0)
    assert worker.run_once()==0
    assert worker.status()['errors']=={} and worker.status()['pendingSegments']==0


def test_ffv1_slice_probe_falls_back_for_older_encoder_and_is_cached(tmp_path,monkeypatch):
    from collector import archive_video
    tool=tmp_path/'ffmpeg';tool.write_text('fixture executable identity');tool.chmod(0o700)
    calls=[]
    def run(arguments,**kwargs):
        slices=arguments[arguments.index('-slices')+1]
        geometry=arguments[arguments.index('-video_size')+1];calls.append((geometry,slices))
        assert kwargs['input'] and kwargs['stdout']==subprocess.DEVNULL
        assert kwargs['timeout']>0
        unsupported=geometry=='640x400' and slices=='1'
        return subprocess.CompletedProcess(arguments,1 if unsupported else 0,stderr=
            b'Unsupported number 1 of slices requested' if unsupported else b'')
    monkeypatch.setattr(archive_video.subprocess,'run',run)
    assert archive_video._ffv1_slices(str(tool),time.monotonic()+5,8,8,'gray16le','gray16le',b'\x00'*128)==1
    assert archive_video._ffv1_slices(str(tool),time.monotonic()+5,640,400,'gray16le','gray16le',b'\x00'*(640*400*2))==4
    assert archive_video._ffv1_slices(str(tool),time.monotonic()+5,640,400,'gray16le','gray16le',b'\x01'*(640*400*2))==4
    assert calls==[('8x8','1'),('640x400','1'),('640x400','4')]


def test_tiny_four_slice_encode_is_exact_or_stays_pending(tmp_path,monkeypatch):
    from collector import archive_video
    path=tmp_path/'old-tiny';ros_segment(path)
    pixels=bytes(range(30))
    rows=[('/depth','sensor_msgs/msg/Image',1_000_000_000,image(pixels,5,3,'mono16',10))]
    monkeypatch.setattr(archive_video,'_ffv1_slices',lambda *_:4)
    try:
        finalize_pending(path,reserve_bytes=0,ros_reader=lambda *_:iter(rows))
    except VideoFinalizeError as error:
        assert 'pixel_mismatch' in str(error)
        assert not (path/'archive.json').exists()
    else:
        actual=subprocess.check_output(['ffmpeg','-v','error','-i',str(path/'video.mkv'),
            '-vsync','0','-f','rawvideo','-pix_fmt','gray16le','pipe:1'])
        assert actual==pixels
    assert (path/'bag/raw.db3').is_file()


def test_new_colour_profile_makes_smaller_hevc_with_exact_vfr_and_embedded_index(tmp_path):
    path=tmp_path/'compact';path.mkdir()
    rows=[];raw=[];offset=0
    pixels=np.zeros((96,128,3),dtype=np.uint8)
    pixels[:,:,0]=np.arange(128,dtype=np.uint8)[None,:]
    pixels[:,:,1]=np.arange(96,dtype=np.uint8)[:,None]*2
    for i in range(48):
        frame=pixels.copy();frame[20:40,30+i%10:50+i%10,2]=225
        ok,encoded=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,96]);assert ok
        packet=encoded.tobytes();raw.append(packet)
        rows.append({'sequence':i+1,'timestampNs':1_000_000_000+i*40_000_000+(i%2)*3_000_000,
            'wallTime':1+i/25,'offset':offset,'length':len(packet),'width':128,'height':96,
            'sha256':sha(packet),'gapBefore':0,'sequenceReset':False});offset+=len(packet)
    (path/'frames.mjpg').write_bytes(b''.join(raw))
    index=''.join(json.dumps(row)+'\n' for row in rows).encode();(path/'index.jsonl').write_bytes(index)
    manifest(path,'native-camera-jpeg',['frames.mjpg','index.jsonl'],receivedFrames=48,archiveProfile='compact-v1')
    original={name:(path/name).read_bytes() for name in ('frames.mjpg','index.jsonl')}
    result=finalize_pending(path,reserve_bytes=0,colour_backend='software')
    assert result['metadata']['videoCodec']=='hevc'
    assert result['metadata']['losslessPixelValues'] is False
    assert result['metadata']['videoBytes'] < result['metadata']['videoOriginalContainerBytes']
    assert attachment(path/'video.mkv',tmp_path/'embedded-index')==index
    actual=probe(path/'video.mkv')
    assert len(actual['packets'])==48 and actual['streams'][0]['width']==128
    assert [round(float(row['pts_time'])*1_000_000) for row in actual['packets']]==[
        (row['timestampNs']-rows[0]['timestampNs'])//1000 for row in rows]
    assert all((path/name).read_bytes()==data for name,data in original.items())
    before=(path/'video.mkv').read_bytes();stamp=(path/'archive.json').stat().st_mtime_ns
    assert finalize_pending(path,reserve_bytes=0)['metadata']['videoCodec']=='hevc'
    assert (path/'video.mkv').read_bytes()==before and (path/'archive.json').stat().st_mtime_ns==stamp


def test_compact_profile_keeps_16_bit_depth_exactly_lossless(tmp_path):
    path=tmp_path/'compact-depth';ros_segment(path)
    pending=json.loads((path/'video-pending.json').read_text());pending['metadata']['archiveProfile']='compact-v1'
    (path/'video-pending.json').write_text(json.dumps(pending))
    pixels=np.arange(640*400,dtype=np.uint16).tobytes()
    result=finalize_pending(path,reserve_bytes=0,ros_reader=lambda *_:iter([
        ('/depth','sensor_msgs/msg/Image',1_000_000_000,image(pixels,640,400,'16UC1',1280))]))
    assert result['metadata']['videoCodec']=='ffv1' and result['metadata']['losslessPixelValues']
    assert result['metadata']['videoCompression']=='lossless-depth-or-non-colour'
    assert subprocess.check_output(['ffmpeg','-v','error','-i',str(path/'video.mkv'),'-map','0:v:0',
        '-vsync','0','-f','rawvideo','-pix_fmt','gray16le','pipe:1'])==pixels


def test_compact_colour_codec_unavailable_still_publishes_verified_lossless_video(tmp_path,monkeypatch):
    from collector import archive_hevc
    path=tmp_path/'fallback';raw=native(path)
    pending=json.loads((path/'video-pending.json').read_text());pending['metadata']['archiveProfile']='compact-v1'
    (path/'video-pending.json').write_text(json.dumps(pending))
    def unavailable(*args,**kwargs):raise archive_hevc.HevcCompressionError('hardware_unavailable')
    monkeypatch.setattr(archive_hevc,'compress_rgb_mkv',unavailable)
    result=finalize_pending(path,reserve_bytes=0)
    assert result['metadata']['videoCodec']=='mjpeg' and result['metadata']['videoCompressionFallback']=='hardware_unavailable'
    actual=probe(path/'video.mkv')
    with (path/'video.mkv').open('rb') as f:
        for packet,expected in zip(actual['packets'],raw):
            from collector.archive_video import _packet_data
            assert _packet_data(f,packet)==expected


def test_compact_ros_image_with_alpha_keeps_all_channels_lossless(tmp_path):
    path=tmp_path/'alpha';ros_segment(path)
    pending=json.loads((path/'video-pending.json').read_text());pending['metadata']['archiveProfile']='compact-v1'
    (path/'video-pending.json').write_text(json.dumps(pending))
    pixels=bytes((i*23)%256 for i in range(32*24*4))
    result=finalize_pending(path,reserve_bytes=0,ros_reader=lambda *_:iter([
        ('/rgba','sensor_msgs/msg/Image',1_000_000_000,image(pixels,32,24,'rgba8',128))]))
    assert result['metadata']['videoCodec']=='ffv1' and result['metadata']['losslessPixelValues']
    assert subprocess.check_output(['ffmpeg','-v','error','-i',str(path/'video.mkv'),'-map','0:v:0',
        '-vsync','0','-f','rawvideo','-pix_fmt','rgba','pipe:1'])==pixels



@pytest.mark.parametrize('preset,expected', [
    (None, {}),
    ('720p-economy-v1', {'max_width': 1280, 'max_height': 720, 'bitrate_bps': 1_200_000}),
    ('720p-balanced-v1', {'max_width': 1280, 'max_height': 720, 'bitrate_bps': 2_000_000}),
])
def test_saved_capture_policy_selects_economy_parameters_and_truthful_hardware_fallback(tmp_path, monkeypatch, preset, expected):
    from collector import archive_hevc
    path = tmp_path/'policy-fallback'; raw = native(path)
    pending = json.loads((path/'video-pending.json').read_text())
    pending['metadata']['archiveProfile'] = 'compact-v1'
    if preset is not None:
        pending['metadata']['videoArchivePreset'] = preset
    (path/'video-pending.json').write_text(json.dumps(pending))
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        raise archive_hevc.HevcCompressionError('hardware_unavailable')
    monkeypatch.setattr(archive_hevc, 'compress_rgb_mkv', unavailable)
    result = finalize_pending(path, reserve_bytes=0)
    assert len(calls) == 1
    assert {key: calls[0][key] for key in ('max_width', 'max_height', 'bitrate_bps') if key in calls[0]} == expected
    meta = result['metadata']
    assert meta['videoCodec'] == 'mjpeg' and meta['videoArchivePresetApplied'] is False
    assert meta['videoCompressionFallback'] == 'hardware_unavailable'
    assert (meta['width'], meta['height']) == (32, 24)
    assert 'videoBitrateBps' not in meta
    actual = probe(path/'video.mkv')
    with (path/'video.mkv').open('rb') as stream:
        for packet, original in zip(actual['packets'], raw):
            from collector.archive_video import _packet_data
            assert _packet_data(stream, packet) == original


@pytest.mark.parametrize('preset', ['720p-economy-v1', '720p-balanced-v1'])
def test_bounded_preset_never_sends_depth_values_to_colour_encoder(tmp_path, monkeypatch, preset):
    from collector import archive_hevc
    path = tmp_path/'economy-depth'; ros_segment(path)
    pending = json.loads((path/'video-pending.json').read_text())
    pending['metadata'].update(archiveProfile='compact-v1', videoArchivePreset=preset)
    (path/'video-pending.json').write_text(json.dumps(pending))
    def forbidden(*args, **kwargs):
        raise AssertionError('Depth must not pass through the RGB economy encoder')
    monkeypatch.setattr(archive_hevc, 'compress_rgb_mkv', forbidden)
    pixels = np.arange(640*400, dtype=np.uint16).tobytes()
    result = finalize_pending(path, reserve_bytes=0, ros_reader=lambda *_: iter([
        ('/depth', 'sensor_msgs/msg/Image', 1_000_000_000, image(pixels, 640, 400, '16UC1', 1280))]))
    meta = result['metadata']
    assert meta['videoCodec'] == 'ffv1' and meta['losslessPixelValues']
    assert meta['videoArchivePresetApplied'] is False
    assert (meta['width'], meta['height']) == (640, 400)
    assert subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path/'video.mkv'),
        '-map', '0:v:0', '-vsync', '0', '-f', 'rawvideo', '-pix_fmt', 'gray16le', 'pipe:1']) == pixels


@pytest.mark.parametrize('preset,bitrate', [('720p-economy-v1', 1_200_000), ('720p-balanced-v1', 2_000_000)])
def test_bounded_rgb_video_is_scaled_with_all_frames_pts_and_original_index(tmp_path, preset, bitrate):
    path = tmp_path/'economy-colour'; path.mkdir()
    raw, rows, offset = [], [], 0
    pixels = np.zeros((900, 1600, 3), dtype=np.uint8)
    pixels[:, :, 0] = np.arange(1600, dtype=np.uint16)[None, :] % 256
    pixels[:, :, 1] = np.arange(900, dtype=np.uint16)[:, None] % 256
    for index in range(12):
        frame = pixels.copy(); frame[100:220, 150+index:270+index, 2] = 235
        ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 96]); assert ok
        packet = encoded.tobytes(); raw.append(packet)
        rows.append({'sequence': index+1, 'timestampNs': 1_000_000_000+index*40_000_000+(index%2)*3_000_000,
            'wallTime': 1+index/25, 'offset': offset, 'length': len(packet), 'width': 1600, 'height': 900,
            'sha256': sha(packet), 'gapBefore': 0, 'sequenceReset': False})
        offset += len(packet)
    source = b''.join(raw); (path/'frames.mjpg').write_bytes(source)
    index_bytes = ''.join(json.dumps(row)+'\n' for row in rows).encode()
    (path/'index.jsonl').write_bytes(index_bytes)
    manifest(path, 'native-camera-jpeg', ['frames.mjpg', 'index.jsonl'], receivedFrames=12,
        archiveProfile='compact-v1', videoArchivePreset=preset)
    result = finalize_pending(path, reserve_bytes=0, colour_backend='software')
    meta = result['metadata']; actual = probe(path/'video.mkv')
    assert meta['videoCodec'] == 'hevc' and meta['videoArchivePresetApplied'] is True
    assert meta['videoBitrateBps'] == bitrate and meta['resized'] is True
    assert meta['videoArchivePreset'] == preset
    assert (meta['sourceWidth'], meta['sourceHeight']) == (1600, 900)
    assert (actual['streams'][0]['width'], actual['streams'][0]['height']) == (1280, 720)
    assert len(actual['packets']) == meta['receivedFrames'] == 12
    assert [round(float(packet['pts_time'])*1_000_000) for packet in actual['packets']] == [
        (row['timestampNs']-rows[0]['timestampNs'])//1000 for row in rows]
    assert attachment(path/'video.mkv', tmp_path/'original-frame-index') == index_bytes
    assert (path/'frames.mjpg').read_bytes() == source
    previous = (path/'video.mkv').read_bytes()
    assert finalize_pending(path, reserve_bytes=0) == result
    assert (path/'video.mkv').read_bytes() == previous


def _set_video_policy_sensor(segment, sensor, topics=None):
    pending=segment/'video-pending.json'
    value=json.loads(pending.read_text());value['sensor']=sensor
    if topics is not None:value['metadata']['topics']=topics
    pending.write_text(json.dumps(value,ensure_ascii=False))


def test_camera_only_video_queue_filters_before_limit_preserves_excluded_backlog(tmp_path):
    excluded=[]
    for index in range(3):
        segment=tmp_path/f'days/2026-10-27/雷达MID360/时段/old-{index}'
        native(segment,1);_set_video_policy_sensor(segment,'雷达MID360')
        excluded.append(segment)
    mixed=tmp_path/'days/2026-10-27/Berxel彩色/时段/mixed'
    native(mixed,1)
    _set_video_policy_sensor(mixed,'Berxel彩色',{'/berxel/color/image_raw':{'type':'sensor_msgs/msg/Image','count':1},
        '/berxel/points':{'type':'sensor_msgs/msg/PointCloud2','count':1}})
    excluded.append(mixed)
    good=tmp_path/'days/2026-10-28/原相机前/时段/camera';native(good,1)
    _set_video_policy_sensor(good,'原相机前')
    protected={str(p):p.read_bytes() for segment in excluded for p in segment.iterdir() if p.is_file()}
    worker=VideoFinalizer(tmp_path,'robot-fixture',camera_only=True,reserve_bytes=0)
    stale=str(excluded[0].relative_to(tmp_path/'days'));worker.errors[stale]='previous non-camera failure'
    assert worker.status()['pendingSegments']==1 and not worker.status()['errors']
    assert worker.run_once(max_segments=1)==1
    assert (good/'archive.json').exists() and worker.status()['pendingSegments']==0
    assert stale not in worker.errors
    assert all(Path(name).read_bytes()==raw for name,raw in protected.items())
    assert all(not (segment/'archive.json').exists() and not (segment/'uploaded.json').exists() for segment in excluded)


def test_camera_only_video_rechecks_manifest_after_candidate_discovery(tmp_path,monkeypatch):
    segment=tmp_path/'days/2026-10-28/原相机前/时段/changed';native(segment,1)
    _set_video_policy_sensor(segment,'原相机前')
    worker=VideoFinalizer(tmp_path,'robot-fixture',camera_only=True,reserve_bytes=0)
    discover=worker._candidates
    def changed():
        candidates=discover()
        _set_video_policy_sensor(segment,'Berxel点云',{'/berxel/points':{'type':'sensor_msgs/msg/PointCloud2','count':1}})
        return candidates
    monkeypatch.setattr(worker,'_candidates',changed)
    raw=(segment/'frames.mjpg').read_bytes()
    assert worker.run_once(max_segments=1)==0
    assert (segment/'frames.mjpg').read_bytes()==raw
    assert not (segment/'archive.json').exists() and not (segment/'uploaded.json').exists()
    assert not worker.counters['finalizedSegments'] and not worker.counters['failedAttempts']


def test_camera_only_video_retains_eligible_failed_camera_error(tmp_path):
    segment=tmp_path/'days/2026-10-28/原相机前/时段/broken';native(segment,1)
    _set_video_policy_sensor(segment,'原相机前')
    (segment/'frames.mjpg').write_bytes(b'corrupt fixture, original retained')
    worker=VideoFinalizer(tmp_path,'robot-fixture',camera_only=True,reserve_bytes=0)
    assert worker.run_once()==0
    status=worker.status()
    assert status['pendingSegments']==1 and status['failedAttempts']==1 and status['errors']
    assert (segment/'frames.mjpg').read_bytes()==b'corrupt fixture, original retained'
    assert not (segment/'archive.json').exists() and not (segment/'uploaded.json').exists()
