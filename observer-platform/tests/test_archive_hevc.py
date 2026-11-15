"""Offline HEVC verification with generated frames; no camera or hardware needed."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess

import cv2
import numpy as np
import pytest

from collector.archive_hevc import (HevcCompressionError, HevcNotSmaller,
    compress_rgb_mkv, hardware_arguments, measure_quality, output_dimensions)
from collector.archive_video import finalize_pending

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                                reason='real ffmpeg and ffprobe required')


def source_video(path, frames=12, size=(192, 320)):
    path.mkdir()
    payload=[];rows=[];offset=0
    rng=np.random.default_rng(17)
    height, width = size
    background=rng.integers(20,220,(height,width,3),dtype=np.uint8)
    for index in range(frames):
        pixels=background.copy()
        cv2.rectangle(pixels,(20+index*3,40),(80+index*3,100),(245,20,40),-1)
        ok,value=cv2.imencode('.jpg',pixels,[cv2.IMWRITE_JPEG_QUALITY,95]);assert ok
        jpeg=value.tobytes();payload.append(jpeg)
        rows.append({'sequence':index+1,'timestampNs':1_000_000_000+index*40_000_000+(index%3)*1_000_000,
            'wallTime':1+index*.04,'offset':offset,'length':len(jpeg),'width':width,'height':height,
            'sha256':hashlib.sha256(jpeg).hexdigest(),'gapBefore':0,'sequenceReset':False})
        offset+=len(jpeg)
    (path/'frames.mjpg').write_bytes(b''.join(payload))
    (path/'index.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    (path/'video-pending.json').write_text(json.dumps({'schemaVersion':1,'id':'source','day':'2026-10-28',
        'sensor':'fixture','label':'fixture','robotId':'fixture','startedAt':'2026-10-28T00:00:00+00:00',
        'endAt':'2026-10-28T00:00:01+00:00','files':['frames.mjpg','index.jsonl'],
        'metadata':{'scope':'garage','source':'native-camera-jpeg','videoRequired':True,'receivedFrames':frames}}))
    finalize_pending(path,reserve_bytes=0)
    return path/'video.mkv'


def probe(path):
    return json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
        '-show_packets','-show_streams','-show_entries',
        'stream=codec_name,width,height,color_range,color_space,has_b_frames:packet=pts_time,flags',
        '-of','json',str(path)],text=True))


def test_software_hevc_preserves_vfr_all_frames_colour_and_metadata_attachment(tmp_path, monkeypatch):
    from collector import archive_hevc
    original_run, encoders = archive_hevc._run, []
    def tracked_run(arguments, *args, **options):
        if 'libx265' in arguments:
            encoders.append(arguments)
        return original_run(arguments, *args, **options)
    monkeypatch.setattr(archive_hevc, '_run', tracked_run)
    source=source_video(tmp_path/'source');before=source.read_bytes();output=tmp_path/'compressed.mkv'
    result=compress_rgb_mkv(source,output,qp_i=18,qp_p=20,backend='software',crf=18,
        gop_frames=5,attachments=[(source.parent/'index.jsonl','index.jsonl','application/x-ndjson')])
    assert result['videoCodec']=='hevc' and result['receivedFrames']==12
    assert len(encoders) == 1
    arguments = encoders[0]
    split = arguments.index('-i')
    assert arguments[:split][-6:] == ['-threads', '1', '-filter_threads', '1', '-filter_complex_threads', '1']
    assert arguments[split+2:].count('-threads') == 1
    assert 'pools=1:frame-threads=1' in arguments[arguments.index('-x265-params')+1]
    assert result['reencoded'] and not result['losslessPixelValues'] and not result['resized']
    pixel_format=subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=pix_fmt','-of','csv=p=0',str(output)],text=True).strip()
    assert result['videoPixelFormat'] == pixel_format
    assert result['videoBytes']<len(before) and source.read_bytes()==before
    original,actual=probe(source),probe(output)
    assert actual['streams'][0]['has_b_frames']==0
    assert actual['streams'][0]['color_range']=='pc'
    assert actual['streams'][0]['color_space']=='bt470bg'
    assert [r['pts_time'] for r in actual['packets']]==[r['pts_time'] for r in original['packets']]
    assert any('K' not in r['flags'] for r in actual['packets'])
    embedded=tmp_path/'embedded.jsonl'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-dump_attachment:t:0',str(embedded),'-i',str(output),
        '-map','0:v:0','-c:v','copy','-f','null','-'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
    assert embedded.read_bytes()==(source.parent/'index.jsonl').read_bytes()
    count=subprocess.check_output(['ffprobe','-v','error','-count_frames','-select_streams','v:0',
        '-show_entries','stream=nb_read_frames','-of','csv=p=0',str(output)],text=True).strip()
    assert count=='12'


def test_hevc_keyframe_cues_allow_random_seek_into_dependent_frames(tmp_path):
    source=source_video(tmp_path/'source');output=tmp_path/'seek.mkv'
    compress_rgb_mkv(source,output,qp_i=18,qp_p=20,backend='software',gop_frames=5)
    result=subprocess.run(['ffmpeg','-nostdin','-v','error','-ss','0.32','-i',str(output),
        '-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],capture_output=True,check=True)
    assert len(result.stdout)==320*192*3 and not result.stderr


def test_quality_measurement_aligns_by_frame_index(tmp_path):
    source=source_video(tmp_path/'source');output=tmp_path/'quality.mkv'
    compress_rgb_mkv(source,output,qp_i=16,qp_p=18,backend='software',crf=14)
    quality=measure_quality(source,output)
    assert quality['alignment']=='frame-index' and quality['fullResolution']
    assert quality['ssim']>.98 and quality['psnrDb']>35


def test_depth_16_bit_video_is_never_eligible_for_hevc(tmp_path):
    source=tmp_path/'depth.mkv'
    raw=(np.arange(32*24,dtype=np.uint16)*73).tobytes()
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','rawvideo','-pixel_format','gray16le',
        '-video_size','32x24','-i','pipe:0','-c:v','ffv1','-level','3','-slices','1',str(source)],input=raw,check=True)
    before=source.read_bytes()
    with pytest.raises(HevcCompressionError,match='depth_is_ineligible'):
        compress_rgb_mkv(source,tmp_path/'bad.mkv',qp_i=18,qp_p=20,backend='software')
    assert source.read_bytes()==before and not (tmp_path/'bad.mkv').exists()


def test_failed_encoder_does_not_publish_or_change_original(tmp_path):
    source=source_video(tmp_path/'source');before=source.read_bytes()
    with pytest.raises(HevcCompressionError):
        compress_rgb_mkv(source,tmp_path/'bad.mkv',qp_i=18,qp_p=20,gst_launch='/bin/false')
    assert source.read_bytes()==before and not (tmp_path/'bad.mkv').exists()
    assert not list(tmp_path.glob('.hevc-working-*'))


def test_larger_candidate_is_rejected_atomically_and_original_is_preserved(tmp_path):
    source=source_video(tmp_path/'source', frames=1, size=(32,32))
    before=source.read_bytes();output=tmp_path/'candidate.mkv'
    with pytest.raises(HevcNotSmaller,match='not_smaller'):
        compress_rgb_mkv(source,output,qp_i=18,qp_p=20,backend='software',crf=0)
    assert source.read_bytes()==before and not output.exists()
    assert not list(tmp_path.glob('.hevc-working-*'))


@pytest.mark.parametrize('pixel_format', ['gray', 'gray16be'])
def test_monochrome_tracks_are_ineligible_even_with_small_pixel_values(tmp_path,pixel_format):
    source=tmp_path/'monochrome.mkv'
    depth=np.zeros((24,32), dtype=np.uint16 if pixel_format=='gray16be' else np.uint8)
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','rawvideo','-pixel_format',pixel_format,
        '-video_size','32x24','-i','pipe:0','-c:v','ffv1','-level','3','-slices','1',str(source)],
        input=depth.tobytes(),check=True)
    with pytest.raises(HevcCompressionError,match='depth_is_ineligible'):
        compress_rgb_mkv(source,tmp_path/'bad.mkv',qp_i=18,qp_p=20,backend='software')
    assert not (tmp_path/'bad.mkv').exists()


def test_rgb_ffv1_converts_range_without_darkening_or_tinting_rgb_values(tmp_path):
    source=tmp_path/'rgb.mkv';output=tmp_path/'rgb-hevc.mkv'
    pixels=np.empty((48,64,3),dtype=np.uint8);pixels[:24]=[220,40,80];pixels[24:]=[20,180,230]
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','rawvideo','-pixel_format','rgb24',
        '-video_size','64x48','-i','pipe:0','-c:v','ffv1','-level','3','-slices','1',str(source)],
        input=pixels.tobytes(),check=True)
    result=compress_rgb_mkv(source,output,qp_i=18,qp_p=20,backend='software',crf=12,require_smaller=False)
    assert result['videoColourRange']=='limited'
    decoded=subprocess.check_output(['ffmpeg','-nostdin','-v','error','-i',str(output),
        '-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
    actual=np.frombuffer(decoded,dtype=np.uint8).reshape((48,64,3))
    assert np.max(np.abs(actual[8:16].astype(int)-pixels[8:16].astype(int))) <= 8
    assert np.max(np.abs(actual[32:40].astype(int)-pixels[32:40].astype(int))) <= 8


def test_hardware_command_has_no_frame_reduction_or_device_input():
    args=hardware_arguments(Path('/tmp/source.mkv'),Path('/tmp/output.mkv'),'mjpeg',True,18,20,50,'gst-launch-1.0')
    assert 'num-B-Frames=0' in args and 'num-Ref-Frames=1' in args
    assert 'copy-timestamp=true' in args
    assert args[args.index('matroskademux')+2:args.index('nvvidconv')] == [
        'jpegparse','!','nvv4l2decoder','mjpeg=true','!']
    assert 'videoconvert' not in args and 'jpegdec' not in args
    assert not any(v in ' '.join(args) for v in ('videorate','videoscale','v4l2src','nvarguscamerasrc','drop=true','shell'))


def test_hardware_ffv1_retains_explicit_limited_colour_conversion():
    args=hardware_arguments(Path('/tmp/source.mkv'),Path('/tmp/output.mkv'),'ffv1',False,22,24,50,'gst-launch-1.0')
    assert 'avdec_ffv1' in args and 'videoconvert' in args
    assert 'video/x-raw,format=I420,colorimetry=bt601' in args
    assert 'jpegparse' not in args and 'nvv4l2decoder' not in args
    assert 'quant-i-frames=22' in args and 'quant-p-frames=24' in args


@pytest.mark.parametrize('quality',[{'qp_i':20,'qp_p':18},{'qp_i':-1,'qp_p':20},{'qp_i':18,'qp_p':50}])
def test_invalid_quality_does_not_run_a_codec(tmp_path,quality):
    source=source_video(tmp_path/'source')
    with pytest.raises(ValueError):compress_rgb_mkv(source,tmp_path/'bad.mkv',**quality)


@pytest.mark.parametrize('codec',['mjpeg','hevc','ffv1'])
def test_split_probes_keep_every_packet_and_codec_private_without_packet_hexdumps(tmp_path,monkeypatch,codec):
    """Probe optimization keeps the original validation information byte exact."""
    from collector import archive_hevc,archive_video
    source=source_video(tmp_path/'source',frames=6,size=(48,64))
    if codec=='hevc':
        target=tmp_path/'hevc.mkv'
        compress_rgb_mkv(source,target,qp_i=22,qp_p=24,backend='software',require_smaller=False)
        source=target
    elif codec=='ffv1':
        source=tmp_path/'depth.mkv'
        raw=(np.arange(64*48,dtype=np.uint16)*17).tobytes()
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','rawvideo','-pixel_format','gray16le',
            '-video_size','64x48','-i','pipe:0','-c:v','ffv1','-level','3','-slices','1',str(source)],
            input=raw,check=True)
    fields_hevc=('stream=codec_name,width,height,pix_fmt,has_b_frames,color_range,color_space,extradata:'
                 'packet=pos,size,flags,pts_time,dts_time,duration_time')
    fields_video='stream=codec_name,extradata,width,height,pix_fmt:packet=pos,size,flags,pts_time,duration_time'
    def baseline(fields):
        return json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
            '-show_packets','-show_streams','-show_data','-show_entries',fields,'-of','json',str(source)],text=True))
    old_hevc,old_video=baseline(fields_hevc),baseline(fields_video)
    calls=[];original=subprocess.run
    def capture(arguments,**kwargs):
        calls.append(arguments)
        return original(arguments,**kwargs)
    monkeypatch.setattr(subprocess,'run',capture)
    import time
    stream,packets=archive_hevc._inspect(source,'ffprobe',tmp_path,time.monotonic()+10,'split-probe')
    assert stream==old_hevc['streams'][0] and packets==old_hevc['packets']
    stream,packets=archive_video._probe(source,'ffprobe',tmp_path,time.monotonic()+10)
    assert stream==old_video['streams'][0] and packets==old_video['packets']
    assert len(calls)==4
    assert all(not ('-show_packets' in args and '-show_data' in args) for args in calls)
    assert sum('-show_packets' in args for args in calls)==2
    assert sum('-show_data' in args for args in calls)==2


@pytest.mark.parametrize('size,target', [
    ((2592,1944),(960,720)), ((1920,1080),(1280,720)),
    ((1280,720),(1280,720)), ((640,400),(640,400)),
    ((4096,2160),(1280,674)), ((320,192),(320,192)),
])
def test_economy_dimensions_fit_720p_without_upscaling_or_cropping(size,target):
    assert output_dimensions(*size,1280,720)==target
    assert all(value%2==0 for value in target)
    assert target[0]<=1280 and target[1]<=720
    # At most one even pixel step is rounded down in either axis.
    assert abs(target[0]/target[1]-size[0]/size[1]) < 0.01


def test_economy_hardware_uses_real_scaling_and_cbr_without_fixed_qp_or_dropping_frames():
    args=hardware_arguments(Path('/tmp/source.mkv'),Path('/tmp/output.mkv'),'mjpeg',True,
        22,24,50,'gst-launch-1.0',dimensions=(960,720),bitrate_bps=1_200_000,resized=True)
    assert 'video/x-raw(memory:NVMM),format=I420,width=960,height=720' in args
    assert 'interpolation-method=5' in args
    assert 'ratecontrol-enable=true' in args and 'control-rate=1' in args and 'bitrate=1200000' in args
    assert not any(value.startswith(('quant-i-frames=','quant-p-frames=')) for value in args)
    assert 'copy-timestamp=true' in args and 'num-B-Frames=0' in args
    assert not any(value in ' '.join(args) for value in ('videorate','drop=true','v4l2src','nvarguscamerasrc'))


def test_economy_software_720p_preserves_every_vfr_frame_and_original_attachment(tmp_path):
    source=source_video(tmp_path/'source',frames=12,size=(960,1280))
    before=source.read_bytes();output=tmp_path/'economy.mkv'
    result=compress_rgb_mkv(source,output,qp_i=22,qp_p=24,backend='software',
        max_width=1280,max_height=720,bitrate_bps=1_200_000,gop_frames=5,
        attachments=[(source.parent/'index.jsonl','index.jsonl','application/x-ndjson')])
    assert result['videoArchivePreset']=='720p-economy-v1'
    assert result['videoRateControl']=='constant-bitrate' and result['videoBitrateBps']==1_200_000
    assert result['sourceWidth']==1280 and result['sourceHeight']==960
    assert (result['width'],result['height'])==(960,720)
    assert (result['targetWidth'],result['targetHeight'])==(960,720)
    assert result['resized'] and result['receivedFrames']==12
    assert result['videoResizeInterpolation']=='lanczos'
    assert result['videoCrf'] is None and result['videoQpI'] is None
    assert not result['losslessPixelValues'] and result['videoBytes']<len(before)//5
    original,actual=probe(source),probe(output)
    assert (actual['streams'][0]['width'],actual['streams'][0]['height'])==(960,720)
    assert [p['pts_time'] for p in actual['packets']]==[p['pts_time'] for p in original['packets']]
    decoded=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(output),
        '-f','null','-'],capture_output=True,check=True)
    assert not decoded.stderr
    embedded=tmp_path/'embedded.jsonl'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-dump_attachment:t:0',str(embedded),'-i',str(output),
        '-map','0:v:0','-c:v','copy','-f','null','-'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
    assert embedded.read_bytes()==(source.parent/'index.jsonl').read_bytes()
    seek=subprocess.run(['ffmpeg','-nostdin','-v','error','-ss','0.32','-i',str(output),
        '-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],capture_output=True,check=True)
    assert len(seek.stdout)==960*720*3 and not seek.stderr
    assert source.read_bytes()==before


@pytest.mark.parametrize('parameters', [
    {'max_width':1280}, {'max_width':1280,'max_height':None},
    {'max_width':1279,'max_height':720}, {'max_width':True,'max_height':720},
    {'max_width':1280,'max_height':0}, {'max_width':8194,'max_height':720},
    {'bitrate_bps':True}, {'bitrate_bps':99_999}, {'bitrate_bps':20_000_001},
    {'bitrate_bps':1_200_000.0},
])
def test_invalid_economy_limits_are_rejected_before_running_codec(tmp_path,monkeypatch,parameters):
    source=source_video(tmp_path/'source',frames=1,size=(32,32))
    def unexpected_codec(*args,**kwargs):
        raise AssertionError('Invalid parameters must not start a codec')
    monkeypatch.setattr('collector.archive_hevc._inspect',unexpected_codec)
    with pytest.raises(ValueError):
        compress_rgb_mkv(source,tmp_path/'bad.mkv',qp_i=22,qp_p=24,**parameters)
    assert not (tmp_path/'bad.mkv').exists()


def test_depth_remains_ineligible_in_720p_bitrate_mode(tmp_path):
    source=tmp_path/'depth.mkv';target=tmp_path/'bad.mkv'
    raw=(np.arange(32*24,dtype=np.uint16)*73).tobytes()
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','rawvideo','-pixel_format','gray16le',
        '-video_size','32x24','-i','pipe:0','-c:v','ffv1','-level','3','-slices','1',str(source)],input=raw,check=True)
    before=source.read_bytes()
    with pytest.raises(HevcCompressionError,match='depth_is_ineligible'):
        compress_rgb_mkv(source,target,qp_i=22,qp_p=24,backend='software',
            max_width=1280,max_height=720,bitrate_bps=1_200_000)
    assert source.read_bytes()==before and not target.exists()


def test_economy_hardware_failure_never_silently_uses_cpu_or_changes_source(tmp_path):
    source=source_video(tmp_path/'source',frames=2,size=(48,64));before=source.read_bytes()
    with pytest.raises(HevcCompressionError):
        compress_rgb_mkv(source,tmp_path/'bad.mkv',qp_i=22,qp_p=24,gst_launch='/bin/false',
            max_width=1280,max_height=720,bitrate_bps=1_200_000)
    assert source.read_bytes()==before and not (tmp_path/'bad.mkv').exists()
    assert not list(tmp_path.glob('.hevc-working-*'))


@pytest.mark.parametrize('bitrate,preset', [
    (1_200_000,'720p-economy-v1'), (2_000_000,'720p-balanced-v1'),
    (3_000_000,'720p-quality-v1'), (1_500_000,'custom'),
])
def test_bitrate_presets_are_reported_accurately_without_upscaling(tmp_path,bitrate,preset):
    source=source_video(tmp_path/'source',frames=4,size=(48,64));output=tmp_path/'candidate.mkv'
    result=compress_rgb_mkv(source,output,qp_i=22,qp_p=24,backend='software',
        max_width=1280,max_height=720,bitrate_bps=bitrate,require_smaller=False)
    assert result['videoArchivePreset']==preset and result['videoBitrateBps']==bitrate
    assert result['resized'] is False
    assert 'videoResizeInterpolation' not in result
    assert (result['width'],result['height'])==(64,48)


def test_custom_dimensions_never_claim_one_of_the_720p_presets(tmp_path):
    source=source_video(tmp_path/'source',frames=4,size=(48,64));output=tmp_path/'candidate.mkv'
    result=compress_rgb_mkv(source,output,qp_i=22,qp_p=24,backend='software',
        max_width=640,max_height=360,bitrate_bps=1_200_000,require_smaller=False)
    assert result['videoArchivePreset']=='custom'


def test_legacy_hardware_conversion_keeps_its_original_interpolation_behavior():
    args=hardware_arguments(Path('/tmp/source.mkv'),Path('/tmp/output.mkv'),'mjpeg',True,
        22,24,50,'gst-launch-1.0')
    assert not any(value.startswith('interpolation-method=') for value in args)
    assert 'ratecontrol-enable=false' in args and 'quant-i-frames=22' in args
