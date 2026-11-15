"""Lossless sealed ROS bag compression; private fixtures, no ROS graph or hardware."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

import collector.archive_bag_compress as bags

ROBOT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


@pytest.fixture
def zstd():
    executable = shutil.which('zstd')
    if executable is None:
        pytest.skip('system zstd is needed for real compression tests')
    return executable


def make_segment(root, *, name='segment-id', count=4, files=1, robot=ROBOT,
                 sensor='Berxel点云', topic='/berxel/points', type_name='sensor_msgs/msg/PointCloud2', day='2026-10-28'):
    segment = root/'days'/day/sensor/(sensor+'_14:50-14:50')/name
    (segment/'bag').mkdir(parents=True)
    database_names = []
    for number in range(files):
        path = segment/'bag'/f'bag_{number}.db3'; database_names.append(path.name)
        with sqlite3.connect(path) as database:
            database.execute('CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT, serialization_format TEXT, offered_qos_profiles TEXT)')
            database.execute('INSERT INTO topics VALUES (1, ?, ?, "cdr", "")',(topic,type_name))
            database.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, timestamp INTEGER, data BLOB)')
            for index in range(count):
                # This deliberately includes all byte values, repeated values,
                # padding-like regions and non-canonical floating-point bits.
                raw = bytes(range(256))*16 + b'\x00\x00\xc0\x7f'*1024 + bytes([index])*4096
                database.execute('INSERT INTO messages VALUES (?, 1, ?, ?)',
                                 (index + 1, 1_000_000 + number*1000 + index, raw))
    information = {'version': 5, 'storage_identifier': 'sqlite3',
        'duration': {'nanoseconds': 500}, 'starting_time': {'nanoseconds_since_epoch': 1_000_000},
        'message_count': count*files, 'topics_with_message_count': [
            {'topic_metadata': {'name': topic, 'type': type_name,
                'serialization_format': 'cdr', 'offered_qos_profiles': ''}, 'message_count': count*files}],
        'compression_format': '', 'compression_mode': '', 'relative_file_paths': database_names,
        'files': [{'path': name, 'starting_time': {'nanoseconds_since_epoch': 1_000_000},
                   'duration': {'nanoseconds': 500}, 'message_count': count} for name in database_names],
        'custom_data': {'preserve': '所有原始字节与原始字段', 'nested': [1, None, True]}}
    (segment/'bag/metadata.yaml').write_text(yaml.safe_dump({'rosbag2_bagfile_information': information}, sort_keys=False))
    manifest = {'schemaVersion': 1, 'robotId': robot, 'id': name, 'day': day,
        'sensor': sensor, 'label': sensor+'_14:50-14:50',
        'startedAt': day+'T06:50:00+00:00', 'endAt': day+'T06:50:00.0005+00:00',
        'metadata': {'scope': 'garage', 'source': 'native-ros-cdr', 'bagCompressionRequired': True,
                     'archiveProfile': 'compact-v1',
                     'receivedMessages': count*files, 'rawBytes': 1234},
        'files': ['bag/'+name for name in database_names] + ['bag/metadata.yaml']}
    write_manifest(segment, manifest)
    return segment, manifest


def write_manifest(segment, manifest):
    (segment/'bag-pending.json').write_text(json.dumps(manifest, ensure_ascii=False))


def original_bytes(segment, manifest):
    return {name: (segment/name).read_bytes() for name in manifest['files']}


@pytest.mark.parametrize('count,files', [(1, 1), (4, 2)])
def test_real_zstd_retains_entire_original_bag_and_only_publishes_compressed_package(tmp_path,zstd,count,files):
    segment, pending = make_segment(tmp_path,count=count,files=files)
    original = original_bytes(segment,pending)
    result = bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert result['files'] == [f'compressed-bag/bag_{i}.db3.zst' for i in range(files)] + ['compressed-bag/metadata.yaml']
    metadata = result['metadata']
    assert metadata['bagCompressionReady'] and metadata['losslessSourceBytes']
    assert metadata['archiveProfile'] == 'compact-v1'
    assert metadata['compressionLevel'] == 3 and metadata['compressionThreads'] == 1
    assert metadata['receivedMessages'] == count*files
    assert original_bytes(segment,pending) == original
    assert {row['relativePath']: (row['size'], row['sha256']) for row in metadata['sourceFiles']} == {
        name: (len(raw), hashlib.sha256(raw).hexdigest()) for name,raw in original.items()}
    for row in metadata['compressedFiles']:
        raw = (segment/row['relativePath']).read_bytes()
        assert (len(raw),hashlib.sha256(raw).hexdigest()) == (row['size'],row['sha256'])
    rewritten = yaml.safe_load((segment/'compressed-bag/metadata.yaml').read_text())
    expected = yaml.safe_load(original['bag/metadata.yaml'])
    value = expected['rosbag2_bagfile_information']
    value.update(compression_format='zstd',compression_mode='file',
                 relative_file_paths=[name+'.zst' for name in value['relative_file_paths']])
    for row in value['files']:row['path'] += '.zst'
    assert rewritten == expected
    assert (segment/'bag-pending.json').exists()
    assert json.loads((segment/'archive.json').read_text()) == result
    assert not list(segment.glob('.bag-compress-working-*'))


def test_manifest_database_order_does_not_reorder_original_yaml_schema(tmp_path,zstd):
    segment, pending = make_segment(tmp_path,files=2)
    pending['files'].reverse();write_manifest(segment,pending)
    result = bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    value = yaml.safe_load((segment/'compressed-bag/metadata.yaml').read_text())['rosbag2_bagfile_information']
    assert value['relative_file_paths'] == ['bag_0.db3.zst','bag_1.db3.zst']
    assert value['custom_data']['nested'] == [1,None,True]
    assert result['metadata']['receivedMessages'] == 8


@pytest.mark.parametrize('problem', ['not-required','wrong-source','bad-name','missing-yaml','duplicate','wrong-checksum'])
def test_only_explicit_new_sealed_source_manifests_are_processed(tmp_path,zstd,problem):
    segment,pending = make_segment(tmp_path);original = original_bytes(segment,pending)
    if problem == 'not-required':pending['metadata']['bagCompressionRequired'] = False
    elif problem == 'wrong-source':pending['metadata']['source'] = 'web-preview'
    elif problem == 'bad-name':pending['files'][0] = '../outside.db3'
    elif problem == 'missing-yaml':pending['files'] = [pending['files'][0]]
    elif problem == 'duplicate':pending['files'].append(pending['files'][0])
    else:
        pending['metadata']['sourceFiles'] = [{'relativePath':name,'size':len(raw),'sha256':'0'*64} for name,raw in original.items()]
    write_manifest(segment,pending)
    with pytest.raises(bags.BagCompressionError):bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert all((segment/name).read_bytes() == raw for name,raw in original.items())
    assert not (segment/'archive.json').exists() and not (segment/'compressed-bag').exists()


def test_an_active_bag_without_pending_marker_is_never_compressed(tmp_path,zstd):
    segment,pending = make_segment(tmp_path);(segment/'bag-pending.json').unlink()
    original = original_bytes(segment,pending)
    with pytest.raises((bags.BagCompressionError,OSError)):bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert original_bytes(segment,pending) == original
    assert not (segment/'compressed-bag').exists()


def test_existing_legacy_archive_is_immutable(tmp_path,zstd):
    segment,pending = make_segment(tmp_path);legacy = json.dumps({'schemaVersion':1,'files':pending['files']}).encode()
    (segment/'archive.json').write_bytes(legacy);original=original_bytes(segment,pending)
    with pytest.raises(bags.BagCompressionError,match='existing_archive_conflict'):
        bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert (segment/'archive.json').read_bytes() == legacy and original_bytes(segment,pending) == original
    assert not (segment/'compressed-bag').exists()


@pytest.mark.parametrize('kind',['empty','count-mismatch','wrong-path','compressed-source','not-sqlite'])
def test_zero_data_or_invalid_ros_metadata_cannot_fake_success(tmp_path,zstd,kind):
    segment,pending = make_segment(tmp_path,count=0 if kind == 'empty' else 2)
    path=segment/'bag/metadata.yaml';metadata=yaml.safe_load(path.read_text());value=metadata['rosbag2_bagfile_information']
    if kind == 'count-mismatch':value['message_count'] += 1
    elif kind == 'wrong-path':value['relative_file_paths'] = ['elsewhere.db3']
    elif kind == 'compressed-source':value['compression_format']='zstd';value['compression_mode']='file'
    elif kind == 'not-sqlite':(segment/'bag/bag_0.db3').write_bytes(b'not a ROS SQLite database')
    path.write_text(yaml.safe_dump(metadata,sort_keys=False))
    original=original_bytes(segment,pending)
    with pytest.raises(bags.BagCompressionError,match='metadata_or_message_count_invalid'):
        bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert original_bytes(segment,pending) == original and not (segment/'archive.json').exists()


@pytest.mark.parametrize('kind',['leaf-link','directory-link','hardlink','segment-link'])
def test_source_links_are_rejected_without_touching_external_files(tmp_path,zstd,kind):
    segment,pending=make_segment(tmp_path/'root');outside=tmp_path/'outside';outside.mkdir()
    protected=outside/'protected';protected.write_bytes(b'private external bytes')
    if kind=='leaf-link':
        (segment/'bag/bag_0.db3').unlink();(segment/'bag/bag_0.db3').symlink_to(protected)
    elif kind=='directory-link':
        shutil.rmtree(segment/'bag');(segment/'bag').symlink_to(outside,target_is_directory=True)
    elif kind=='hardlink':
        (segment/'bag/bag_0.db3').unlink();os.link(protected,segment/'bag/bag_0.db3')
    else:
        link=tmp_path/'segment-link';link.symlink_to(segment,target_is_directory=True);segment=link
    with pytest.raises(bags.BagCompressionError):bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert protected.read_bytes()==b'private external bytes' and not (segment/'archive.json').exists()


def test_busy_segment_lock_prevents_two_compressors(tmp_path,zstd):
    segment,pending=make_segment(tmp_path)
    with bags._segment_lock(segment):
        with pytest.raises(bags.BagCompressionBusy):bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert not (segment/'archive.json').exists()


def test_space_protection_and_missing_codec_preserve_source(tmp_path,zstd,monkeypatch):
    segment,pending=make_segment(tmp_path);original=original_bytes(segment,pending)
    with pytest.raises(bags.BagCompressionError,match='zstd_missing'):
        bags.finalize_pending(segment,zstd='/no-such-fixture-zstd',reserve_bytes=0)
    monkeypatch.setattr(bags.shutil,'disk_usage',lambda path:SimpleNamespace(free=1))
    with pytest.raises(bags.BagCompressionError,match='insufficient_working_space'):
        bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert original_bytes(segment,pending)==original and not (segment/'archive.json').exists()


def test_corrupt_derivative_is_rejected_by_decompressed_sha_and_source_is_retained(tmp_path,zstd,monkeypatch):
    segment,pending=make_segment(tmp_path);original=original_bytes(segment,pending);compress=bags._compress
    def corrupted(source,destination,*args):
        compress(source,destination,*args)
        with destination.open('r+b') as stream:stream.seek(6);stream.write(b'corrupt derivative')
    monkeypatch.setattr(bags,'_compress',corrupted)
    with pytest.raises(bags.BagCompressionError):bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert original_bytes(segment,pending)==original and not (segment/'archive.json').exists()
    assert not list(segment.glob('.bag-compress-working-*'))


def test_changed_original_during_compression_does_not_publish(tmp_path,zstd,monkeypatch):
    segment,pending=make_segment(tmp_path);compress=bags._compress
    def changed(source,*args):
        compress(source,*args)
        with source.open('ab') as stream:stream.write(b'concurrent source change')
    monkeypatch.setattr(bags,'_compress',changed)
    with pytest.raises(bags.BagCompressionError,match='source_changed'):
        bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert not (segment/'archive.json').exists() and (segment/'bag/bag_0.db3').exists()


def test_timeout_kills_compression_or_stalled_decompression_and_keeps_sources(tmp_path,zstd):
    segment,pending=make_segment(tmp_path/'source');original=original_bytes(segment,pending)
    executable=tmp_path/'hang-zstd';executable.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(60)\n');executable.chmod(0o700)
    started=time.monotonic()
    with pytest.raises(bags.BagCompressionError,match='timeout'):
        bags.finalize_pending(segment,zstd=str(executable),reserve_bytes=0,timeout_s=.1)
    assert time.monotonic()-started<2 and original_bytes(segment,pending)==original
    path=tmp_path/'sample.zst';path.write_bytes(b'fixture')
    with pytest.raises(bags.BagCompressionError,match='timeout'):
        bags._verify_compressed(path,{'size':10,'sha256':'0'*64},str(executable),time.monotonic()+.1)
    assert time.monotonic()-started<3


def test_derivative_rename_before_manifest_interruption_is_reused_without_recompression(tmp_path,zstd,monkeypatch):
    segment,pending=make_segment(tmp_path);publish=bags._atomic_json
    monkeypatch.setattr(bags,'_atomic_json',lambda *args:(_ for _ in ()).throw(OSError('fixture publication interrupted')))
    with pytest.raises(bags.BagCompressionError):bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert (segment/'compressed-bag/bag_0.db3.zst').exists() and not (segment/'archive.json').exists()
    before={p.name:p.stat().st_mtime_ns for p in (segment/'compressed-bag').iterdir()}
    monkeypatch.setattr(bags,'_atomic_json',publish)
    monkeypatch.setattr(bags,'_compress',lambda *args:pytest.fail('valid staged package must not be recompressed'))
    result=bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert result['metadata']['bagCompressionReady']
    assert before=={p.name:p.stat().st_mtime_ns for p in (segment/'compressed-bag').iterdir()}


@pytest.mark.parametrize('kind',['known','unknown','link'])
def test_interrupted_staging_cleanup_never_deletes_unknown_or_linked_content(tmp_path,zstd,kind):
    segment,pending=make_segment(tmp_path);stage=segment/'.bag-compress-working-fixture';stage.mkdir()
    protected=tmp_path/'protected';protected.write_bytes(b'outside bytes')
    if kind=='known':(stage/'bag_0.db3.zst').write_bytes(b'partial derivative')
    elif kind=='unknown':(stage/'important.bin').write_bytes(b'unknown data')
    else:(stage/'bag_0.db3.zst').symlink_to(protected)
    if kind=='known':
        assert bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)['metadata']['bagCompressionReady']
        assert not stage.exists()
    else:
        with pytest.raises(bags.BagCompressionError,match='staging_unsafe'):
            bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
        assert stage.exists() and not (segment/'archive.json').exists()
    assert protected.read_bytes()==b'outside bytes'


def test_published_package_is_idempotent_and_missing_derivative_needs_prune_proof(tmp_path,zstd):
    segment,pending=make_segment(tmp_path);result=bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    assert bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)==result
    ack={'state':'complete','manifestSha256':hashlib.sha256((segment/'archive.json').read_bytes()).hexdigest(),
         'files':[dict(row,robotFileId=str(uuid4())) for row in result['metadata']['compressedFiles']]}
    (segment/'uploaded.json').write_text(json.dumps(ack))
    for name in result['files']:(segment/name).unlink()
    with pytest.raises((bags.BagCompressionError,OSError)):bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)
    (segment/'local-copy-pruned.json').write_text(json.dumps({'state':'pruned','files':ack['files'],'sourceFiles':result['metadata']['sourceFiles']}))
    for name in pending['files']:(segment/name).unlink()
    assert bags.finalize_pending(segment,zstd=zstd,reserve_bytes=0)==result
    assert not (segment/'compressed-bag/bag_0.db3.zst').exists()


def test_worker_status_keeps_failed_pending_visible_and_ignores_history(tmp_path,zstd):
    bad,pending=make_segment(tmp_path,name='bad');pending['metadata']['bagCompressionRequired']=False;write_manifest(bad,pending)
    good,_=make_segment(tmp_path,name='good')
    old,_=make_segment(tmp_path,name='historical');(old/'archive.json').write_text('{"metadata":{"format":"original"}}')
    original_history=(old/'archive.json').read_bytes()
    worker=bags.BagCompressionFinalizer(tmp_path,ROBOT,zstd=zstd,reserve_bytes=0)
    assert worker.status()['pendingSegments']==2
    assert worker.run_once()==1
    status=worker.status()
    assert status['pendingSegments']==1 and status['failedAttempts']==1 and status['finalizedSegments']==1
    assert len(status['errors'])==1 and not status['running']
    assert (good/'archive.json').exists() and (old/'archive.json').read_bytes()==original_history


def test_worker_rejects_wrong_robot_or_segment_identity(tmp_path,zstd):
    segment,pending=make_segment(tmp_path,robot='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
    worker=bags.BagCompressionFinalizer(tmp_path,ROBOT,zstd=zstd,reserve_bytes=0)
    assert worker.run_once()==0 and worker.status()['pendingSegments']==1
    assert not (segment/'archive.json').exists()


def test_single_segment_pass_bounds_work_without_starving_later_segments(tmp_path,zstd):
    for name in ('one','two','three'):
        make_segment(tmp_path,name=name)
    worker = bags.BagCompressionFinalizer(tmp_path,ROBOT,zstd=zstd,reserve_bytes=0)
    assert worker.run_once(max_segments=1) == 1
    assert worker.status()['pendingSegments'] == 2
    assert worker.run_once(max_segments=1) == 1
    assert worker.run_once(max_segments=1) == 1
    assert worker.status()['pendingSegments'] == 0
    for limit in (0,5,True):
        with pytest.raises(ValueError,match='batch size'):
            worker.run_once(max_segments=limit)


def test_camera_only_bags_keep_camera_info_and_skip_old_non_camera_before_limit(tmp_path,zstd):
    excluded=[]
    for index in range(3):
        segment,pending=make_segment(tmp_path,name=f'old-{index}',day='2026-10-27')
        pending['metadata']['topics']={'/berxel/points':{'type':'sensor_msgs/msg/PointCloud2','count':4}}
        write_manifest(segment,pending);excluded.append(segment)
    good,pending=make_segment(tmp_path,name='camera-info',sensor='Berxel深度',
        topic='/berxel/depth/camera_info',type_name='sensor_msgs/msg/CameraInfo')
    pending['metadata'].update(cameraMetadataOnly=True,topics={
        '/berxel/depth/camera_info':{'type':'sensor_msgs/msg/CameraInfo','count':4}})
    write_manifest(good,pending)
    protected={str(p):p.read_bytes() for segment in excluded for p in segment.rglob('*') if p.is_file()}
    original=original_bytes(good,pending)
    worker=bags.BagCompressionFinalizer(tmp_path,ROBOT,camera_only=True,zstd=zstd,reserve_bytes=0)
    stale=str(excluded[0].relative_to(tmp_path/'days'));worker.errors[stale]='previous non-camera failure'
    assert worker.status()['pendingSegments']==1 and not worker.status()['errors']
    assert worker.run_once(max_segments=1)==1
    assert (good/'archive.json').exists() and worker.status()['pendingSegments']==0 and stale not in worker.errors
    published=json.loads((good/'archive.json').read_text())
    assert published['metadata']['cameraMetadataOnly'] and published['metadata']['losslessSourceBytes']
    assert original_bytes(good,pending)==original
    assert all(Path(name).read_bytes()==raw for name,raw in protected.items())
    assert all(not (segment/'archive.json').exists() and not (segment/'uploaded.json').exists() for segment in excluded)


def test_camera_only_bags_recheck_manifest_after_discovery_and_preserve_source(tmp_path,zstd,monkeypatch):
    segment,pending=make_segment(tmp_path,sensor='Berxel彩色',
        topic='/berxel/color/camera_info',type_name='sensor_msgs/msg/CameraInfo')
    pending['metadata'].update(cameraMetadataOnly=True,topics={
        '/berxel/color/camera_info':{'type':'sensor_msgs/msg/CameraInfo','count':4}})
    write_manifest(segment,pending);original=original_bytes(segment,pending)
    worker=bags.BagCompressionFinalizer(tmp_path,ROBOT,camera_only=True,zstd=zstd,reserve_bytes=0)
    discover=worker._candidates
    def changed():
        candidates=discover();pending['metadata']['topics']['/berxel/points']={'type':'sensor_msgs/msg/PointCloud2','count':1}
        write_manifest(segment,pending);return candidates
    monkeypatch.setattr(worker,'_candidates',changed)
    assert worker.run_once(max_segments=1)==0
    assert original_bytes(segment,pending)==original
    assert not (segment/'archive.json').exists() and not (segment/'compressed-bag').exists() and not (segment/'uploaded.json').exists()
    assert not worker.counters['failedAttempts'] and not worker.counters['finalizedSegments']


def test_camera_only_bags_report_allowed_camera_failure_without_rewriting_source(tmp_path,zstd):
    segment,pending=make_segment(tmp_path,sensor='Berxel深度',
        topic='/berxel/depth/camera_info',type_name='sensor_msgs/msg/CameraInfo')
    pending['metadata'].update(cameraMetadataOnly=True,topics={
        '/berxel/depth/camera_info':{'type':'sensor_msgs/msg/CameraInfo','count':4}})
    pending['metadata']['bagCompressionRequired']=False;write_manifest(segment,pending)
    original=original_bytes(segment,pending)
    worker=bags.BagCompressionFinalizer(tmp_path,ROBOT,camera_only=True,zstd=zstd,reserve_bytes=0)
    assert worker.run_once()==0
    status=worker.status();assert status['pendingSegments']==1 and status['failedAttempts']==1 and status['errors']
    assert original_bytes(segment,pending)==original and not (segment/'archive.json').exists()
