import base64
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from collector.archive_pull import FilePuller, REMOTE_FILES
from collector.archive_upload import ArchiveUploadConfig, ArchiveUploadError


def test_finished_file_rate_limit_yields_time_and_shutdown_can_interrupt():
    from collector.archive_pull import transfer_pause
    delays = []
    def wait(delay):
        delays.append(delay)
        return True
    assert transfer_pause(2 * 1024**2, 10, 1024**2, clock=lambda: 10.5, wait=wait)
    assert delays == [1.5]
    assert not transfer_pause(100, 10, 0, clock=lambda: 10, wait=wait)
    assert not transfer_pause(100, 10, 100, clock=lambda: 20, wait=wait)
    assert delays == [1.5]


@pytest.fixture
def source(tmp_path):
    home=tmp_path/'source';root=home/'.local/share/robot-native-archive'
    relative='2026-10-27/雷达/雷达_14:50-15:50/segment-id'
    directory=root/'days'/relative
    (directory/'bag').mkdir(parents=True)
    (directory/'bag/native.db3').write_bytes(b'\x00native-original\xff'*(1024*512))
    (directory/'bag/metadata.yaml').write_text('rosbag2_bagfile_information: {}\n')
    robot=str(uuid4())
    manifest=dict(schemaVersion=1,id='segment-id',day='2026-10-27',sensor='雷达',label='雷达_14:50-15:50',
        startedAt='2026-10-27T14:50:00+08:00',endAt='2026-10-27T15:50:00+08:00',robotId=robot,
        metadata=dict(scope='garage',source='native-ros-cdr'),files=['bag/native.db3','bag/metadata.yaml'])
    (directory/'archive.json').write_text(json.dumps(manifest,ensure_ascii=False))
    return home,directory,manifest,relative,robot


def puller(tmp_path, source, *, bad_checksum=False):
    home,directory,manifest,relative,robot=source
    commands=[]
    def popen(command, **kwargs):
        commands.append(command)
        code=REMOTE_FILES
        if bad_checksum:
            code=code.replace('emit(dict(size=size,sha256=sha))', "emit(dict(size=size,sha256='0'*64))")
        return subprocess.Popen(['/usr/bin/python3','-c',code],env={**os.environ,'HOME':str(home)},**kwargs)
    config=ArchiveUploadConfig('fixture@example.invalid','fixture-password',robot_id=robot)
    value=FilePuller(['/usr/bin/ssh','-T'],'nvidia@trusted-host',tmp_path/'ipc',config,
        popen=popen,min_free_bytes=0,max_spool_bytes=100*1024**2,retain_seconds=0)
    value.commands=commands
    return value


def test_real_binary_stream_copies_all_bytes_and_publishes_only_complete_segments(tmp_path,source):
    p=puller(tmp_path,source);p.once()
    _,directory,manifest,relative,_=source
    target=p.root/'days'/relative
    for name in manifest['files']:
        assert (target/name).read_bytes()==(directory/name).read_bytes()
    assert (target/'archive.json').read_bytes()==(directory/'archive.json').read_bytes()
    assert not (target/'uploaded.json').exists()
    assert p.snapshot()['pulledSegments']==1
    before=(target/'bag/native.db3').stat().st_mtime_ns
    p.once()
    assert p.snapshot()['pulledSegments']==1
    assert (target/'bag/native.db3').stat().st_mtime_ns==before
    assert all('-R' not in command and '-L' not in command for command in p.commands)


def test_bad_transfer_checksum_keeps_partial_not_ready_then_retries(tmp_path,source):
    bad=puller(tmp_path,source,bad_checksum=True)
    with pytest.raises(ValueError,match='checksum'):bad.once()
    assert list((bad.root/'.incoming').rglob('*.part'))
    assert not (bad.root/'days').exists()
    good=puller(tmp_path,source);good.once()
    assert (good.root/'days'/source[3]/'archive.json').exists()


def test_source_and_destination_links_never_escape_archive_root(tmp_path,source):
    p=puller(tmp_path,source)
    raw=source[1]/'bag/native.db3';raw.unlink();outside=tmp_path/'outside';outside.write_bytes(b'private')
    raw.symlink_to(outside)
    p.once()
    assert not (p.root/'days').exists()
    raw.unlink();raw.write_bytes(b'raw')
    (p.root/'.incoming').symlink_to(tmp_path/'elsewhere')
    with pytest.raises(ValueError,match='link'):p.once()
    assert outside.read_bytes()==b'private'


def test_spool_capacity_never_publishes_or_deletes_unconfirmed_source(tmp_path,source):
    p=puller(tmp_path,source);p.max_spool_bytes=1
    with pytest.raises(OSError,match='spool full'):p.once()
    assert (source[1]/'bag/native.db3').exists()
    assert not (p.root/'days').exists()


def test_foreground_snapshot_has_more_than_daemon_batch_and_never_acks_source(tmp_path,source):
    p=puller(tmp_path,source)
    original=source[1]/'archive.json'
    for number in range(20):
        directory=source[1].with_name('segment-'+str(number));directory.mkdir()
        manifest={**source[2], 'id':directory.name}
        (directory/'archive.json').write_text(json.dumps(manifest,ensure_ascii=False))
    assert len(p._json(dict(op='list')))==16
    snapshot=p.sealed_snapshot()
    assert len(snapshot)==21
    assert not list(source[0].rglob('uploaded.json'))
    assert original.exists()


def test_foreground_and_background_share_one_transfer_writer_lock(tmp_path,source):
    p=puller(tmp_path,source);p.root.mkdir()
    descriptor=p.sealed_snapshot()[0]
    with (p.root/'.source-pull.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert p._pull(descriptor) is False
        assert not (p.root/'.incoming').exists()
    assert p._pull(descriptor) is True
    assert (p.root/'days'/source[3]/'archive.json').is_file()


def test_source_ack_and_prune_require_matching_bytes_and_fresh_local_api_confirmation(tmp_path,source):
    p=puller(tmp_path,source);p.once()
    target=p.root/'days'/source[3]
    rows=[]
    for name in source[2]['files']:
        data=(target/name).read_bytes()
        rows.append(dict(relativePath=name,robotFileId=str(uuid4()),sha256=hashlib.sha256(data).hexdigest(),size=len(data)))
    ack=dict(state='complete',manifestSha256=hashlib.sha256((target/'archive.json').read_bytes()).hexdigest(),
        segmentId=source[2]['id'],datasetId=str(uuid4()),folderPath='雷达/雷达_14:50-15:50/segment-id',
        completedAt='2026-10-27T00:00:00+00:00',files=rows)
    (target/'uploaded.json').write_text(json.dumps(ack))
    def unavailable(*args):raise ArchiveUploadError('ARCHIVE_API_UNAVAILABLE')
    p.client._confirmed_file=unavailable
    p.once()
    assert (source[1]/'uploaded.json').exists()
    assert (source[1]/'bag/native.db3').exists()
    assert json.loads((target/'source-ack.json').read_text())['pruned'] is False
    p.client._confirmed_file=lambda *args:None
    p.once()
    assert not (source[1]/'bag/native.db3').exists()
    assert (source[1]/'archive.json').exists()
    assert (target/'bag/native.db3').exists()  # uploader owns IPC copy retention
    assert json.loads((source[1]/'local-copy-pruned.json').read_text())['state']=='pruned'
    assert json.loads((target/'source-ack.json').read_text())['pruned'] is True


def test_remote_rejects_forged_source_ack_and_retains_originals(tmp_path,source):
    p=puller(tmp_path,source)
    with pytest.raises(ValueError):
        p._json(dict(op='ack',segment=source[3],ack={'state':'complete','manifestSha256':'bad'},prune=True))
    assert (source[1]/'bag/native.db3').exists()


def test_remote_ack_never_follows_preplanted_fixed_temporary_symlink(tmp_path,source):
    p=puller(tmp_path,source);p.once()
    target=p.root/'days'/source[3]
    rows=[]
    for name in source[2]['files']:
        data=(target/name).read_bytes()
        rows.append(dict(relativePath=name,robotFileId=str(uuid4()),sha256=hashlib.sha256(data).hexdigest(),size=len(data)))
    ack=dict(state='complete',manifestSha256=hashlib.sha256((target/'archive.json').read_bytes()).hexdigest(),
        segmentId=source[2]['id'],datasetId=str(uuid4()),folderPath='雷达/雷达_14:50-15:50/segment-id',
        completedAt='2026-10-27T00:00:00+00:00',files=rows)
    outside=tmp_path/'outside-private';outside.write_bytes(b'unchanged private bytes')
    link=source[1]/'uploaded.json.ssh-tmp';link.symlink_to(outside)
    result=p._json(dict(op='ack',segment=source[3],ack=ack,prune=False))
    assert result==dict(acknowledged=True,pruned=False)
    assert outside.read_bytes()==b'unchanged private bytes'
    assert link.is_symlink()
    assert not list(source[1].glob('*.ssh-tmp-*'))


@pytest.mark.parametrize('link_level',('days','day'))
def test_existing_target_shortcut_and_acknowledgements_reject_parent_links(tmp_path,source,link_level):
    p=puller(tmp_path,source)
    descriptor=p._json(dict(op='list'))[0]
    outside=tmp_path/'outside-archive';external=outside/source[3]
    external.mkdir(parents=True)
    (external/'archive.json').write_bytes((source[1]/'archive.json').read_bytes())
    p.root.mkdir()
    if link_level=='days':
        (p.root/'days').symlink_to(outside,target_is_directory=True)
    else:
        (p.root/'days').mkdir()
        (p.root/'days'/'2026-10-27').symlink_to(outside/'2026-10-27',target_is_directory=True)
    with pytest.raises(ValueError,match='link'):p._pull(descriptor)
    # Root days/ itself must never be followed by the ACK scan. For a linked
    # date directory, rglob skips the link, so there is no outside marker read.
    if link_level=='days':
        with pytest.raises(ValueError,match='link'):p._acknowledge()
    else:
        p._acknowledge()
    assert (external/'archive.json').read_bytes()==(source[1]/'archive.json').read_bytes()


@pytest.mark.parametrize('invalid',('link','oversized'))
def test_existing_manifest_shortcut_rejects_links_and_bounded_bad_files(tmp_path,source,invalid):
    p=puller(tmp_path,source);descriptor=p._json(dict(op='list'))[0]
    target=p.root/'days'/source[3];target.mkdir(parents=True)
    path=target/'archive.json'
    if invalid=='link':
        path.symlink_to(source[1]/'archive.json')
    else:
        path.write_bytes(b' '*(256*1024+1))
    with pytest.raises(ValueError,match='manifest invalid'):p._pull(descriptor)


def video_source(source):
    home,directory,manifest,relative,robot=source
    rows=[]
    for name in manifest['files']:
        content=(directory/name).read_bytes()
        rows.append({'relativePath':name,'sha256':hashlib.sha256(content).hexdigest(),'size':len(content)})
    (directory/'video.mkv').write_bytes(b'validated video container')
    manifest['files']=['video.mkv']
    manifest['metadata'].update(videoReady=True,sourceFiles=rows)
    (directory/'archive.json').write_text(json.dumps(manifest,ensure_ascii=False))
    (directory/'video-pending.json').write_text('keep conversion checkpoint')
    return rows


def source_video_ack(source):
    _,directory,manifest,_,_=source
    video=(directory/'video.mkv').read_bytes()
    return dict(state='complete',manifestSha256=hashlib.sha256((directory/'archive.json').read_bytes()).hexdigest(),
        segmentId=manifest['id'],datasetId=str(uuid4()),folderPath='sensor/label/segment',
        completedAt='2026-10-27T00:00:00+00:00',files=[dict(relativePath='video.mkv',robotFileId=str(uuid4()),
        sha256=hashlib.sha256(video).hexdigest(),size=len(video))])


def test_video_pull_transfers_only_video_and_prunes_verified_source_cache_after_api_check(tmp_path,source):
    rows=video_source(source);p=puller(tmp_path,source);p.once()
    target=p.root/'days'/source[3];ack=source_video_ack(source)
    assert (target/'video.mkv').read_bytes()==(source[1]/'video.mkv').read_bytes()
    assert all(not (target/row['relativePath']).exists() for row in rows)
    (target/'uploaded.json').write_text(json.dumps(ack))
    p.client._confirmed_file=lambda *args: (_ for _ in ()).throw(ArchiveUploadError('ARCHIVE_API_UNAVAILABLE'))
    p.once()
    assert all((source[1]/row['relativePath']).exists() for row in rows)
    assert (source[1]/'video.mkv').exists()
    p.client._confirmed_file=lambda *args:None
    p.once()
    marker=json.loads((source[1]/'local-copy-pruned.json').read_text())
    assert marker['state']=='pruned' and marker['sourceFiles']==rows
    assert all(not (source[1]/row['relativePath']).exists() for row in rows)
    assert not (source[1]/'video.mkv').exists()
    assert (target/'video.mkv').exists()
    for name in ('archive.json','uploaded.json','video-pending.json'):
        assert (source[1]/name).is_file()


def test_source_video_ack_accepts_already_absent_input_cache(tmp_path,source):
    rows=video_source(source);ack=source_video_ack(source);p=puller(tmp_path,source)
    for row in rows:(source[1]/row['relativePath']).unlink()
    (source[1]/'bag').rmdir()
    result=p._json(dict(op='ack',segment=source[3],ack=ack,prune=True))
    assert result==dict(acknowledged=True,pruned=True)
    assert not (source[1]/'video.mkv').exists()


def test_changed_source_cache_blocks_pruning_of_video_and_all_inputs(tmp_path,source):
    rows=video_source(source);ack=source_video_ack(source);p=puller(tmp_path,source)
    raw=source[1]/rows[0]['relativePath'];raw.write_bytes(b'changed source')
    with pytest.raises(ValueError):p._json(dict(op='ack',segment=source[3],ack=ack,prune=True))
    assert (source[1]/'video.mkv').exists()
    assert all((source[1]/row['relativePath']).exists() for row in rows)
    assert not (source[1]/'local-copy-pruned.json').exists()


@pytest.mark.parametrize('name',['../outside','/tmp/outside','bag/../outside','archive.json','uploaded.json',
                                'recorder.lock','recovery/native.db3','bag\\native.db3'])
def test_source_video_ack_rejects_escaped_or_state_cache_paths(tmp_path,source,name):
    video_source(source);manifest=source[2];manifest['metadata']['sourceFiles'][0]['relativePath']=name
    (source[1]/'archive.json').write_text(json.dumps(manifest,ensure_ascii=False))
    ack=source_video_ack(source);p=puller(tmp_path,source)
    with pytest.raises(ValueError):p._json(dict(op='ack',segment=source[3],ack=ack,prune=True))
    assert (source[1]/'video.mkv').exists() and (source[1]/'bag/native.db3').exists()


@pytest.mark.parametrize('kind',['leaf-link','parent-link','hardlink'])
def test_source_video_cache_links_are_rejected(tmp_path,source,kind):
    video_source(source);ack=source_video_ack(source);p=puller(tmp_path,source)
    protected=tmp_path/'protected';protected.write_bytes(b'protected')
    if kind=='parent-link':
        outside=tmp_path/'outside';outside.mkdir()
        for child in (source[1]/'bag').iterdir():child.unlink()
        (source[1]/'bag').rmdir();(source[1]/'bag').symlink_to(outside,target_is_directory=True)
    else:
        raw=source[1]/'bag/native.db3';raw.unlink()
        if kind=='leaf-link':raw.symlink_to(protected)
        else:os.link(protected,raw)
    with pytest.raises(ValueError):p._json(dict(op='ack',segment=source[3],ack=ack,prune=True))
    assert protected.read_bytes()==b'protected' and (source[1]/'video.mkv').exists()


def test_source_video_pruning_resumes_after_process_interruption(tmp_path,source):
    rows=video_source(source);ack=source_video_ack(source);p=puller(tmp_path,source)
    original=p.popen
    def interrupt(command,**kwargs):
        code=REMOTE_FILES.replace('for name,expected in checked.items():unlink_verified(directory,name,expected)',
            "for name,expected in checked.items():\n   unlink_verified(directory,name,expected)\n   if name=='video.mkv':raise OSError('simulated process interruption')")
        return subprocess.Popen(['/usr/bin/python3','-c',code],env={**os.environ,'HOME':str(source[0])},**kwargs)
    p.popen=interrupt
    with pytest.raises(ValueError):p._json(dict(op='ack',segment=source[3],ack=ack,prune=True))
    assert not (source[1]/'video.mkv').exists()
    marker=json.loads((source[1]/'local-copy-pruned.json').read_text())
    assert marker['state']=='pruning' and marker['sourceFiles']==rows
    assert all((source[1]/row['relativePath']).exists() for row in rows)
    p.popen=original
    assert p._json(dict(op='ack',segment=source[3],ack=ack,prune=True))==dict(acknowledged=True,pruned=True)
    assert all(not (source[1]/row['relativePath']).exists() for row in rows)


def test_source_video_resume_rejects_changed_checkpoint_or_remaining_cache(tmp_path,source):
    rows=video_source(source);ack=source_video_ack(source);p=puller(tmp_path,source)
    marker=dict(state='pruning',files=ack['files'],sourceFiles=rows)
    (source[1]/'local-copy-pruned.json').write_text(json.dumps(marker))
    (source[1]/'video.mkv').unlink();(source[1]/rows[0]['relativePath']).unlink()
    last=source[1]/rows[-1]['relativePath'];original=last.read_bytes();last.write_bytes(b'changed')
    with pytest.raises(ValueError):p._json(dict(op='ack',segment=source[3],ack=ack,prune=True))
    last.write_bytes(original);marker['sourceFiles']=[]
    (source[1]/'local-copy-pruned.json').write_text(json.dumps(marker))
    with pytest.raises(ValueError):p._json(dict(op='ack',segment=source[3],ack=ack,prune=True))
    assert last.exists()


def test_compact_compressed_bag_pulls_final_files_and_prunes_sources_only_after_fresh_api_check(tmp_path,source):
    rows=video_source(source)
    _,directory,manifest,relative,_=source
    (directory/'compressed-bag').mkdir()
    files=['compressed-bag/native.db3.zst','compressed-bag/metadata.yaml']
    for name in files:(directory/name).write_bytes(('verified '+name).encode())
    manifest['files']=files
    manifest['metadata']={'scope':'garage','source':'native-ros-cdr','archiveProfile':'compact-v1',
        'bagCompressionReady':True,'sourceFiles':rows}
    (directory/'archive.json').write_text(json.dumps(manifest,ensure_ascii=False))
    p=puller(tmp_path,source);p.retain_seconds=300;p.once()
    target=p.root/'days'/relative
    assert all((target/name).read_bytes()==(directory/name).read_bytes() for name in files)
    assert all(not (target/row['relativePath']).exists() for row in rows)
    ack=source_video_ack(source)
    ack.update(completedAt=datetime.now(timezone.utc).isoformat(),
        files=[dict(relativePath=name,robotFileId=str(uuid4()),
            sha256=hashlib.sha256((directory/name).read_bytes()).hexdigest(),size=(directory/name).stat().st_size) for name in files])
    (target/'uploaded.json').write_text(json.dumps(ack))
    p.client._confirmed_file=lambda *args:(_ for _ in ()).throw(ArchiveUploadError('ARCHIVE_API_UNAVAILABLE'))
    p.once()
    assert all((directory/name).exists() for name in files+[row['relativePath'] for row in rows])
    p.client._confirmed_file=lambda *args:None;p.once()
    assert all(not (directory/name).exists() for name in files+[row['relativePath'] for row in rows])
    assert (directory/'archive.json').exists() and (directory/'uploaded.json').exists()
    assert json.loads((directory/'local-copy-pruned.json').read_text())['sourceFiles']==rows



def camera_source(source):
    home,directory,manifest,relative,robot=source
    manifest['sensor']='Berxel彩色';manifest['label']='Berxel彩色_14:50-15:50'
    manifest['metadata']['topics']={'/berxel/color/image_raw':{'type':'sensor_msgs/msg/Image','count':1}}
    manifest['files']=['video.mkv'];(directory/'video.mkv').write_bytes(b'camera-only-video')
    new_relative='/'.join((manifest['day'],manifest['sensor'],manifest['label'],manifest['id']))
    destination=home/'.local/share/robot-native-archive/days'/new_relative
    destination.parent.mkdir(parents=True);directory.rename(destination)
    (destination/'archive.json').write_text(json.dumps(manifest,ensure_ascii=False))
    return home,destination,manifest,new_relative,robot


def test_camera_only_remote_filters_before_listing_limit_and_copies_all_camera_bytes(tmp_path,source,monkeypatch):
    camera=camera_source(source)
    home,_,manifest,_,_=camera
    days=home/'.local/share/robot-native-archive/days'
    # Excluded historical segments must not consume the remote 16-entry limit.
    for index in range(20):
        old={**manifest,'sensor':'雷达MID360','label':'雷达MID360_14:50-15:50','id':f'old-{index}'}
        old['metadata']={'scope':'garage','topics':{'/livox/lidar':{'type':'livox_ros_driver2/msg/CustomMsg','count':1}}}
        directory=days/old['day']/old['sensor']/old['label']/old['id'];directory.mkdir(parents=True)
        (directory/'video.mkv').write_bytes(b'old lidar bytes')
        (directory/'archive.json').write_text(json.dumps(old,ensure_ascii=False))
    import sys
    # Force every excluded record before the camera to exercise the hard cap.
    monkeypatch.setattr(sys.modules[__name__], 'REMOTE_FILES', REMOTE_FILES.replace(
        "for p in days.rglob('archive.json'):",
        "for p in sorted(days.rglob('archive.json'), key=lambda p: ('Berxel彩色' in str(p), str(p))):"))
    monkeypatch.setenv('ARCHIVE_CAMERA_ONLY','1')
    p=puller(tmp_path,camera)
    descriptors=p._json(dict(op='list'))
    assert len(descriptors)==1 and descriptors[0]['path']==camera[3]
    p.once()
    assert p.snapshot()['pulledSegments']==1
    assert (p.root/'days'/camera[3]/'video.mkv').read_bytes()==b'camera-only-video'
    assert not (p.root/'days'/'2026-10-27'/'雷达MID360').exists()
    assert not list(days.rglob('uploaded.json'))


def test_camera_only_defense_skips_forged_descriptor_and_remote_read_or_ack(tmp_path,source,monkeypatch):
    legacy=puller(tmp_path,source);descriptor=legacy._json(dict(op='list'))[0]
    monkeypatch.setenv('ARCHIVE_CAMERA_ONLY','1')
    p=puller(tmp_path,source)
    p._pull(descriptor)
    assert not (p.root/'days').exists()
    with pytest.raises(ValueError,match='source transfer failed'):
        p._json(dict(op='read',segment=source[3],file=source[2]['files'][0]))
    with pytest.raises(ValueError,match='source transfer failed'):
        p._json(dict(op='ack',segment=source[3],ack={},prune=True))
    assert (source[1]/'bag/native.db3').exists()
    assert not (source[1]/'uploaded.json').exists()


def test_switching_camera_only_keeps_historical_non_camera_ack_and_cache_untouched(tmp_path,source,monkeypatch):
    p=puller(tmp_path,source);p.once()
    target=p.root/'days'/source[3]
    rows=[]
    for name in source[2]['files']:
        raw=(target/name).read_bytes();rows.append(dict(relativePath=name,robotFileId=str(uuid4()),sha256=hashlib.sha256(raw).hexdigest(),size=len(raw)))
    ack=dict(state='complete',manifestSha256=hashlib.sha256((target/'archive.json').read_bytes()).hexdigest(),
        segmentId=source[2]['id'],datasetId=str(uuid4()),folderPath='雷达/雷达_14:50-15:50/segment-id',
        completedAt='2026-10-27T00:00:00+00:00',files=rows)
    (target/'uploaded.json').write_text(json.dumps(ack));p.client._confirmed_file=lambda *args:None
    p.camera_only=True;p.once()
    assert not (source[1]/'uploaded.json').exists()
    assert not (source[1]/'local-copy-pruned.json').exists()
    assert not (target/'source-ack.json').exists()
    assert (source[1]/'bag/native.db3').exists() and (target/'bag/native.db3').exists()


def test_camera_label_with_pointcloud_topic_is_rejected_at_both_sides(tmp_path,source,monkeypatch):
    camera=camera_source(source);camera[2]['metadata']['topics']['/berxel/points']={'type':'sensor_msgs/msg/PointCloud2','count':1}
    (camera[1]/'archive.json').write_text(json.dumps(camera[2],ensure_ascii=False))
    p=puller(tmp_path,camera);descriptor=p._json(dict(op='list'))[0]
    p.camera_only=True
    assert p._json(dict(op='list'))==[]
    p._pull(descriptor)
    assert not (p.root/'days').exists()
    assert (camera[1]/'video.mkv').exists()
