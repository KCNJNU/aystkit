"""Final recorder status must reflect local persistence, without ROS hardware."""
import fcntl
import json
import signal
import sys
from types import SimpleNamespace

import pytest

from collector import archive_main
from tests.test_archive_core import store as raw_store
from tests.test_archive_ros import bound


def drained_ros():
    return dict(workerAlive=False, queueMessages=0, queueBytes=0,
                inFlightMessages=0, subscriptionCleanupPending=0,
                tickError=None, gateError=None, topics={})


class Adapter:
    def __init__(self, *, closes=True, error=None, status=None):
        self.closes, self.error = closes, error
        self.status = drained_ros() if status is None else status
        self.calls = []

    def close(self, timeout):
        self.calls.append(timeout)
        if self.error:
            raise self.error
        return self.closes

    def snapshot(self):
        return self.status


class Store:
    def __init__(self, *, close_error=None, snapshot_error=None, status=None):
        self.close_error, self.snapshot_error = close_error, snapshot_error
        self.status = dict(active={}, errors={}) if status is None else status
        self.calls = []

    def close(self):
        self.calls.append('close')
        if self.close_error:
            raise self.close_error

    def snapshot(self):
        self.calls.append('snapshot')
        if self.snapshot_error:
            raise self.snapshot_error
        return self.status


@pytest.mark.parametrize('adapter', [Adapter(closes=False), Adapter(error=OSError('fixture'))])
def test_incomplete_writer_drain_never_touches_possibly_locked_store(adapter):
    store = Store(close_error=AssertionError('store may still be locked'),
                  snapshot_error=AssertionError('snapshot may still block'))
    report = archive_main.finish_archive(adapter, store)
    assert report['state'] == 'stop_failed'
    assert report['store'] is None
    assert report['errors']['rosClose']
    assert adapter.calls == [20]
    assert store.calls == []


@pytest.mark.parametrize('field,value', [
    ('workerAlive', True), ('queueMessages', 1), ('queueBytes', 20),
    ('inFlightMessages', 1), ('subscriptionCleanupPending', 1),
])
def test_claimed_closed_writer_must_confirm_no_pending_work_before_store_close(field, value):
    ros = drained_ros()
    ros[field] = value
    store = Store()
    report = archive_main.finish_archive(Adapter(status=ros), store)
    assert report['state'] == 'stop_failed'
    assert store.calls == []


@pytest.mark.parametrize('store', [
    Store(close_error=OSError('fixture disk error')),
    Store(snapshot_error=OSError('fixture status error')),
    Store(status=dict(active={'radar': {'messages': 1}}, errors={})),
    Store(status=dict(active={}, errors={'radar': 'finalization failed'})),
])
def test_store_finalization_failure_is_reported_instead_of_stopped(store):
    report = archive_main.finish_archive(Adapter(), store)
    assert report['state'] == 'stop_failed'
    assert report['errors']
    assert store.calls == ['close', 'snapshot']


@pytest.mark.parametrize('topic', [
    dict(error='writer:OSError', reasons={}),
    dict(error=None, reasons={'writer_error': 1}),
    dict(error=None, reasons={'writer_backoff': 1}),
    dict(error=None, reasons={'store_rejected': 1}),
])
def test_topic_write_loss_is_not_reported_as_saved_even_after_queue_drains(topic):
    ros = drained_ros()
    ros['topics']['/raw'] = topic
    store = Store()
    report = archive_main.finish_archive(Adapter(status=ros), store)
    assert report['state'] == 'stop_failed'
    assert 'topic:/raw' in report['errors']
    assert store.calls == ['close', 'snapshot']


def test_single_accepted_raw_message_is_finalized_without_waiting_for_segment_duration(tmp_path):
    store = raw_store(tmp_path / 'raw', idle_seconds=99999)
    adapter, node = bound(store, lambda: True)
    adapter.reconcile_gate()
    original = b'\x00one-native-message\xff'
    next(item for item in node.created if item.topic == '/livox/lidar').callback(original)
    report = archive_main.finish_archive(adapter, store)
    assert report['state'] == 'stopped'
    assert report['errors'] == {}
    assert report['store']['active'] == {}
    manifests = list(store.root.rglob('archive.json'))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest['metadata']['receivedMessages'] == 1
    assert manifest['metadata']['closeReason'] == 'shutdown'
    assert next(store.root.rglob('bag_0.db3')).read_bytes() == original


@pytest.mark.parametrize('finished', [False, True])
def test_shutdown_waits_for_sealed_camera_video_before_claiming_stopped(finished):
    store = Store()

    class Video:
        def drain(self, timeout):
            assert store.calls == ['close', 'snapshot']
            assert timeout == 180
            return finished

        def snapshot(self):
            return {'pendingSegments': 0 if finished else 1, 'running': not finished, 'errors': {}}

    report = archive_main.finish_archive(Adapter(), store, Video())
    assert report['state'] == ('stopped' if finished else 'stop_failed')
    assert report['video']['pendingSegments'] == (0 if finished else 1)
    assert ('videoFinalize' in report['errors']) is not finished


def test_video_worker_finishes_single_camera_frame_outside_store_writer(tmp_path):
    from collector.archive_camera import NativeCameraStore
    from tests.test_archive_camera import jpeg
    import shutil
    if not shutil.which('ffprobe'):
        pytest.skip('ffprobe required for the real video container check')
    root = tmp_path/'camera'
    store = NativeCameraStore(root, '00000000-0000-0000-0000-000000000001', gate=lambda: True,
                              min_free_bytes=0, max_spool_bytes=10**8)
    raw = jpeg()
    assert store.record('camera1', raw, sequence=1, timestamp_ns=1_000_000_000,
                        wall_time=1_000_000_000)
    worker = archive_main.VideoWorker(root, store.robot_id, reserve_bytes=0)
    worker.thread.start()
    try:
        report = archive_main.finish_archive(None, store, worker)
        assert report['state'] == 'stopped'
        manifests = list(root.rglob('archive.json'))
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text())
        assert manifest['files'] == ['video.mkv']
        assert manifest['metadata']['receivedFrames'] == 1
        assert manifest['metadata']['reencoded'] is False
    finally:
        worker.close()


def test_archive_worker_drains_new_native_bag_and_reports_combined_state(tmp_path):
    import shutil
    from tests.test_archive_bag_compress import make_segment,ROBOT
    if not shutil.which('zstd'):
        pytest.skip('zstd required for the real compressed-bag check')
    segment,_ = make_segment(tmp_path)
    worker = archive_main.VideoWorker(tmp_path,ROBOT,reserve_bytes=0)
    before = worker.snapshot()
    assert before['pendingSegments'] == 1 and before['bag']['pendingSegments'] == 1
    assert before['video']['pendingSegments'] == 0
    worker.thread.start()
    try:
        assert worker.drain(timeout=10)
        state = worker.snapshot()
        assert state['pendingSegments'] == 0 and not state['running'] and not state['errors']
        assert state['finalizedSegments'] == 1 and state['bag']['finalizedSegments'] == 1
        assert json.loads((segment/'archive.json').read_text())['metadata']['bagCompressionReady']
    finally:
        worker.close()


def test_failed_bag_is_visible_to_stop_and_status_without_stalling_video_passes(tmp_path,monkeypatch):
    import threading
    from collector import archive_video,archive_bag_compress
    calls = []
    video_passed = threading.Event()
    worker_holder = {}
    class Finalizer:
        def __init__(self,root,robot_id,**options):
            assert 'ffmpeg' in options
        def status(self):
            return dict(pendingSegments=0,running=False,errors={})
        def run_once(self,max_segments):
            assert max_segments == 1;calls.append('video');video_passed.set()
    class Bags:
        def __init__(self,root,robot_id,**options):
            assert options == {'reserve_bytes':0,'timeout_s':1}
        def status(self):
            return dict(pendingSegments=1,running=False,errors={'segment':'retry needed'})
        def run_once(self,max_segments):
            assert max_segments == 1;calls.append('bag')
            assert video_passed.wait(1)
            worker_holder['worker'].stopping.set()
            raise OSError('fixture failure')
    monkeypatch.setattr(archive_video,'VideoFinalizer',Finalizer)
    monkeypatch.setattr(archive_bag_compress,'BagCompressionFinalizer',Bags)
    worker = archive_main.VideoWorker(tmp_path,'fixture',reserve_bytes=0,timeout_s=1,ffmpeg='fixture-ffmpeg')
    worker_holder['worker'] = worker
    worker.thread.start()
    worker.thread.join(timeout=2)
    worker.close()
    state = worker.snapshot()
    assert {'video','bag'}.issubset(calls)
    assert state['pendingSegments'] == 1 and state['errors']['bag:segment'] == 'retry needed'
    assert state['errors']['worker:bag'] == 'OSError'
    assert not worker.drain(timeout=.01)
    status = archive_main.status_with_transfer(Store(),tmp_path,worker)
    assert status['videoPendingSegments'] == 1 and 'videoFinalization' in status['errors']


def test_long_video_does_not_starve_bags_and_stop_waits_for_both_lanes(tmp_path,monkeypatch):
    import threading
    from collector import archive_video, archive_bag_compress
    video_started, release_video, bags_done = (threading.Event() for _ in range(3))
    class Videos:
        def __init__(self,*args,**kwargs):
            self.pending = 1;self.running = False
        def status(self):
            return dict(pendingSegments=self.pending,running=self.running,errors={})
        def run_once(self,max_segments):
            assert max_segments == 1
            if not self.pending:return 0
            self.running = True;video_started.set()
            assert release_video.wait(3)
            self.pending = 0;self.running = False
            return 1
    class Bags:
        def __init__(self,*args,**kwargs):self.pending = 8
        def status(self):
            return dict(pendingSegments=self.pending,running=False,errors={})
        def run_once(self,max_segments):
            assert max_segments == 1
            if not self.pending:return 0
            assert video_started.wait(1)
            self.pending -= 1
            if not self.pending:bags_done.set()
            return 1
    monkeypatch.setattr(archive_video,'VideoFinalizer',Videos)
    monkeypatch.setattr(archive_bag_compress,'BagCompressionFinalizer',Bags)
    worker = archive_main.VideoWorker(tmp_path,'fixture')
    worker.thread.start()
    try:
        assert video_started.wait(1) and bags_done.wait(1)
        state = worker.snapshot()
        assert state['workerAlive'] and state['videoWorkerAlive'] and state['bagWorkerAlive']
        assert state['bag']['pendingSegments'] == 0 and state['video']['pendingSegments'] == 1
        assert state['running'] and not worker.drain(timeout=.02)
        release_video.set()
        assert worker.drain(timeout=1)
        assert worker.snapshot()['pendingSegments'] == 0
    finally:
        release_video.set();worker.close()
    state = worker.snapshot()
    assert not state['workerAlive'] and not state['bagWorkerAlive'] and not state['videoWorkerAlive']


def test_drain_rejects_unavailable_bag_lane_with_pending_work(tmp_path,monkeypatch):
    import threading
    from collector import archive_bag_compress
    class Bags:
        def __init__(self,*args,**kwargs):pass
        def status(self):return dict(pendingSegments=1,running=False,errors={})
        def run_once(self,max_segments):return 0
    monkeypatch.setattr(archive_bag_compress,'BagCompressionFinalizer',Bags)
    worker = archive_main.VideoWorker(tmp_path,'fixture')
    worker.bag_thread = threading.Thread(target=lambda: None, name='fixture-unavailable-bag')
    worker.thread.start()
    try:
        assert worker.workers_started.wait(1)
        worker.bag_thread.join(timeout=1)
        assert not worker.snapshot()['workerAlive']
        assert worker.snapshot()['videoWorkerAlive'] and not worker.drain(timeout=.1)
    finally:
        worker.close()


@pytest.mark.parametrize('closes,exit_code,state', [(False, 1, 'stop_failed'), (True, 0, 'stopped')])
@pytest.mark.parametrize('camera_limit', [None, 128 * 1024**2])
@pytest.mark.parametrize('counts_close', [True, False])
def test_main_publishes_honest_final_state_exits_and_releases_runtime_resources(
        tmp_path, monkeypatch, closes, exit_code, state, camera_limit, counts_close):
    from collector import archive_ros

    control = tmp_path / 'control'
    monkeypatch.setenv('ARCHIVE_CONTROL_DIR', str(control))
    monkeypatch.setenv('ARCHIVE_ROOT', str(tmp_path / 'raw'))
    monkeypatch.setenv('ARCHIVE_UPLOAD_DISABLED', '0')
    if camera_limit is None:
        monkeypatch.delenv('ARCHIVE_CAMERA_SEGMENT_BYTES', raising=False)
    else:
        monkeypatch.setenv('ARCHIVE_CAMERA_SEGMENT_BYTES', str(camera_limit))
    monkeypatch.setattr(archive_main.ArchiveUploadConfig, 'from_environment',
                        lambda *_: SimpleNamespace(robot_id='00000000-0000-0000-0000-000000000001'))
    handlers, events = {}, []
    monkeypatch.setattr(archive_main.signal, 'signal', lambda number, callback: handlers.update({number: callback}))
    store, adapter = Store(), Adapter(closes=closes)
    store_options = {}
    def create_store(*args, **kwargs):
        store_options.update(kwargs)
        return store
    monkeypatch.setattr(archive_main, 'ArchiveStore', create_store)
    node = SimpleNamespace(destroy_node=lambda: events.append('node-destroyed'))
    monkeypatch.setattr(archive_ros, 'build_archive_node', lambda *args, **kwargs: (node, adapter))
    running = [True]

    def ros_shutdown():
        running[0] = False
        events.append('ros-stopped')

    no_ros_handlers = object()
    def ros_init(**kwargs):
        assert kwargs['signal_handler_options'] is no_ros_handlers
        events.append('ros-started')
    ros = SimpleNamespace(init=ros_init,
        ok=lambda: running[0], shutdown=ros_shutdown,
        spin_once=lambda *args, **kwargs: handlers[signal.SIGTERM]())
    monkeypatch.setitem(sys.modules, 'rclpy', ros)
    monkeypatch.setitem(sys.modules, 'rclpy.signals',
                        SimpleNamespace(SignalHandlerOptions=SimpleNamespace(NO=no_ros_handlers)))
    uploader = SimpleNamespace(
        thread=SimpleNamespace(start=lambda: events.append('upload-started'),
                               join=lambda **kwargs: events.append('upload-joined')),
        snapshot=lambda: dict(pendingSegments=1, lastError='offline'))
    monkeypatch.setattr(archive_main, 'UploadWorker', lambda *args, **kwargs: uploader)
    original_counts_factory = archive_main._upload_snapshot_factory
    def count_factory(*args, **kwargs):
        getter = original_counts_factory(*args, **kwargs)
        original_close = getter.close
        def close_counts():
            result = original_close()
            return result if counts_close else False
        getter.close = close_counts
        return getter
    monkeypatch.setattr(archive_main, '_upload_snapshot_factory', count_factory)

    assert archive_main.main([]) == (exit_code if counts_close else 1)
    report = json.loads((control / 'status.json').read_text())
    assert report['state'] == (state if counts_close else 'stop_failed')
    if not counts_close:
        assert report['errors']['uploadStatusCleanup'] == 'RuntimeError'
    assert report['updatedAt']
    expected_camera_limit = 512 * 1024**2 if camera_limit is None else camera_limit
    assert store_options['resource_isolate_inventory'] is True
    assert store_options['camera_segment_bytes'] == expected_camera_limit
    assert 'segment_bytes' not in store_options  # Non-image bags keep their existing limit.
    assert report['recordingPolicy']['cameraSegmentBytes'] == expected_camera_limit
    assert report['upload']['pendingSegments'] == 1  # Durable local data does not wait for upload.
    assert {'node-destroyed', 'ros-stopped', 'upload-joined'}.issubset(events)
    if not closes:
        assert 'close' not in store.calls
        assert report['store'] is None
    with (control / 'recorder.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize('value', ['0', '-1', str(512 * 1024**2 + 1), '', '1.5', '1e6',
                                  'true', '1_024', '１２８', None, 128, True])
def test_camera_segment_environment_rejects_invalid_limits(value):
    with pytest.raises(ValueError, match='ARCHIVE_CAMERA_SEGMENT_BYTES'):
        archive_main._camera_segment_limit({'ARCHIVE_CAMERA_SEGMENT_BYTES': value})


@pytest.mark.parametrize('limit', [1, 128 * 1024**2, 512 * 1024**2])
def test_camera_segment_environment_accepts_store_boundaries(limit):
    assert archive_main._camera_segment_limit({'ARCHIVE_CAMERA_SEGMENT_BYTES': str(limit)}) == limit
    assert archive_main._camera_segment_limit({}) == 512 * 1024**2


def test_camera_segment_environment_rotates_without_losing_accepted_images(tmp_path):
    limit = archive_main._camera_segment_limit({'ARCHIVE_CAMERA_SEGMENT_BYTES': '6'})
    archive = raw_store(tmp_path, segment_bytes=4, camera_segment_bytes=limit)
    images = [b'one', b'two', b'end']
    for index, data in enumerate(images):
        assert archive.record('Berxel深度', '/berxel/depth/image_raw',
                              'sensor_msgs/msg/Image', data, index + 1, 100 + index)
    archive.close()
    pending = [json.loads(path.read_text()) for path in tmp_path.rglob('video-pending.json')]
    assert sorted(row['metadata']['receivedFrames'] for row in pending) == [1, 2]
    assert sum(row['metadata']['receivedFrames'] for row in pending) == len(images)
    assert sorted(path.read_bytes() for path in tmp_path.rglob('bag_0.db3')) == [b'end', b'onetwo']
    assert archive.camera_segment_bytes == limit and archive.segment_bytes == 4


def test_periodic_video_status_preserves_unknown_discovery_and_stop_keeps_fresh_queries(tmp_path, monkeypatch):
    from collector import archive_video, archive_bag_compress
    calls = []
    class Finalizer:
        def __init__(self, *_args, **_options):pass
        def status(self, *, cached=False):
            calls.append(cached)
            return dict(pendingSegments=None if cached else 0,
                discoveryReady=not cached, terminalPendingSegments=0,
                finalizedSegments=0, failedAttempts=0, busySegments=0,
                running=False, errors={})
    monkeypatch.setattr(archive_video, 'VideoFinalizer', Finalizer)
    monkeypatch.setattr(archive_bag_compress, 'BagCompressionFinalizer', Finalizer)
    worker = archive_main.VideoWorker(tmp_path, 'fixture')
    cached = worker.snapshot(cached=True)
    assert cached['pendingSegments'] is None and cached['discoveryReady'] is False
    assert archive_main._terminal_video_pending(cached) is False
    report = archive_main.status_with_transfer(Store(), tmp_path, worker, cached=True)
    assert report['videoPendingSegments'] is None and report['videoTerminalPending'] is False
    assert calls == [True, True, True, True]
    assert worker.snapshot()['pendingSegments'] == 0
    assert calls[-2:] == [False, False]



def camera_policy_manifest(root, sensor, *, completed=False):
    directory = root/'days'/'2026-10-30'/sensor/'fixture'/'segment'
    directory.mkdir(parents=True)
    (directory/'archive.json').write_text(json.dumps({'schemaVersion':1,'sensor':sensor,
        'metadata':{'scope':'garage'},'files':['video.mkv']}))
    (directory/'video.mkv').write_bytes(b'fixture source retained')
    if completed:(directory/'uploaded.json').write_text('{}')
    return directory


def test_camera_only_uploader_skips_old_non_camera_pending_without_ack(tmp_path,monkeypatch):
    import threading
    stopping=threading.Event();seen=[]
    excluded=camera_policy_manifest(tmp_path,'Berxel点云')
    camera=camera_policy_manifest(tmp_path,'原相机前')
    class Client:
        def upload_segment(self,path):
            seen.append(path);stopping.set();return {'state':'complete'}
    monkeypatch.setattr(archive_main,'ArchiveUploadClient',lambda cfg:Client())
    worker=archive_main.UploadWorker(tmp_path,None,stopping,camera_only=True)
    worker.run()
    assert seen==[camera] and not (excluded/'uploaded.json').exists()
    assert (excluded/'video.mkv').read_bytes()==b'fixture source retained'
    assert worker.snapshot()['storagePolicy']=='camera-only'
    assert worker.snapshot()['skippedSegments']==1 and worker.snapshot()['pendingSegments']==0


def test_camera_only_uploader_does_not_prune_excluded_history(tmp_path,monkeypatch):
    import threading
    from collector import archive_retention
    stopping=threading.Event();pruned=[]
    excluded=camera_policy_manifest(tmp_path,'雷达MID360',completed=True)
    camera=camera_policy_manifest(tmp_path,'原相机后',completed=True)
    monkeypatch.setattr(archive_main,'ArchiveUploadClient',lambda cfg:object())
    def prune(path,client,**kwargs):pruned.append(path);stopping.set()
    monkeypatch.setattr(archive_retention,'prune_confirmed_copy',prune)
    worker=archive_main.UploadWorker(tmp_path,None,stopping,camera_only=True)
    worker.run()
    assert pruned==[camera] and worker.snapshot()['completedSegments']==1
    assert (excluded/'uploaded.json').read_text()=='{}'
    assert (excluded/'video.mkv').read_bytes()==b'fixture source retained'


def test_video_worker_forwards_camera_selection_to_both_finalizer_lanes(tmp_path,monkeypatch):
    from collector import archive_video,archive_bag_compress
    calls=[]
    class Videos:
        def __init__(self,*args,**kwargs):calls.append(('video',kwargs))
    class Bags:
        def __init__(self,*args,**kwargs):calls.append(('bag',kwargs))
    monkeypatch.setattr(archive_video,'VideoFinalizer',Videos)
    monkeypatch.setattr(archive_bag_compress,'BagCompressionFinalizer',Bags)
    archive_main.VideoWorker(tmp_path,'fixture',camera_only=True,reserve_bytes=123,timeout_s=45)
    assert [kind for kind,_ in calls]==['video','bag']
    assert all(values['camera_only'] is True for _,values in calls)


@pytest.mark.parametrize('kind', ['video', 'bag'])
def test_finalizer_shares_throttled_discovery_and_removes_completed_pending(tmp_path, monkeypatch, kind):
    """Frequent status/run calls must not repeatedly walk an idle archive tree."""
    from collector import archive_video, archive_bag_compress
    module = archive_video if kind == 'video' else archive_bag_compress
    worker_type = module.VideoFinalizer if kind == 'video' else module.BagCompressionFinalizer
    worker = worker_type(tmp_path, 'fixture')
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    discovered = []
    discover = worker._candidates
    monkeypatch.setattr(worker, '_candidates', lambda: discovered.append(clock[0]) or discover())
    for _ in range(20):
        assert worker.status()['pendingSegments'] == 0
        assert worker.run_once(max_segments=1) == 0
    assert discovered == [100.0]
    segment = tmp_path/'days/2026-11-02/sensor/hour/new'
    segment.mkdir(parents=True)
    (segment/(kind+'-pending.json')).write_text(json.dumps({'id': 'new', 'robotId': 'fixture'}))
    assert worker.status()['pendingSegments'] == 0
    clock[0] += 5.01
    assert worker.status()['pendingSegments'] == 1
    monkeypatch.setattr(module, 'finalize_pending',
                        lambda path, **_: (path/'archive.json').write_text('{}'))
    assert worker.run_once(max_segments=1) == 1
    assert worker.status()['pendingSegments'] == 0
    assert len(discovered) == 2


def test_drain_refreshes_just_sealed_capture_and_wakes_both_idle_lanes(tmp_path, monkeypatch):
    """Stopping after idle must process new seals without a five-second wait."""
    import threading
    from collector import archive_video, archive_bag_compress
    monkeypatch.setattr(archive_video, 'finalize_pending',
                        lambda path, **_: (path/'archive.json').write_text('{}'))
    monkeypatch.setattr(archive_bag_compress, 'finalize_pending',
                        lambda path, **_: (path/'archive.json').write_text('{}'))
    worker = archive_main.VideoWorker(tmp_path, 'fixture')
    assert worker.snapshot()['pendingSegments'] == 0
    idle = []
    for finalizer in (worker.finalizer, worker.bag_finalizer):
        event = threading.Event()
        run_once = finalizer.run_once
        def observed(*, max_segments, original=run_once, event=event):
            result = original(max_segments=max_segments)
            event.set()
            return result
        monkeypatch.setattr(finalizer, 'run_once', observed)
        idle.append(event)
    worker.thread.start()
    try:
        assert all(event.wait(1) for event in idle)
        for kind in ('video', 'bag'):
            segment = tmp_path/f'days/2026-11-02/sensor/hour/{kind}'
            segment.mkdir(parents=True)
            (segment/(kind+'-pending.json')).write_text(json.dumps({'id': kind, 'robotId': 'fixture'}))
        assert worker.drain(timeout=1)
        assert worker.snapshot()['finalizedSegments'] == 2
    finally:
        worker.close()


def test_terminal_input_cannot_hide_new_unattempted_segment_during_stop(tmp_path, monkeypatch):
    from collector import archive_video
    worker = archive_main.VideoWorker(tmp_path, 'fixture')
    bad = tmp_path/'days/2026-11-02/sensor/hour/bad'
    bad.mkdir(parents=True)
    (bad/'video-pending.json').write_text(json.dumps({'id': 'bad', 'robotId': 'fixture'}))
    attempts = []
    def finalize(path, **_):
        attempts.append(path)
        if path == bad:
            raise archive_video.VideoFinalizeError('video_source_file_invalid')
        (path/'archive.json').write_text('{}')
    monkeypatch.setattr(archive_video, 'finalize_pending', finalize)
    assert worker.finalizer.run_once(max_segments=1) == 0
    assert archive_main._terminal_video_pending(worker.snapshot())
    fresh = bad.with_name('fresh')
    fresh.mkdir()
    (fresh/'video-pending.json').write_text(json.dumps({'id': 'fresh', 'robotId': 'fixture'}))
    # finish_archive performs this refresh after closing the raw store.
    worker.refresh_pending()
    state = worker.snapshot()
    assert state['pendingSegments'] == 2 and state['terminalPendingSegments'] == 1
    assert not archive_main._terminal_video_pending(state)
    assert worker.finalizer.run_once(max_segments=1) == 1
    assert attempts == [bad, fresh]  # The permanent error stays visible, without retrying.
    assert archive_main._terminal_video_pending(worker.snapshot())


@pytest.mark.parametrize('kind', ['video', 'bag'])
def test_concurrent_status_calls_share_one_candidate_scan(tmp_path, monkeypatch, kind):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from collector import archive_video, archive_bag_compress
    worker_type = archive_video.VideoFinalizer if kind == 'video' else archive_bag_compress.BagCompressionFinalizer
    worker = worker_type(tmp_path, 'fixture')
    started, release = threading.Event(), threading.Event()
    scans = []
    def discover():
        scans.append(1)
        started.set()
        assert release.wait(2)
        return []
    monkeypatch.setattr(worker, '_candidates', discover)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker.status) for _ in range(4)]
        assert started.wait(1)
        release.set()
        assert all(future.result(timeout=2)['pendingSegments'] == 0 for future in futures)
    assert len(scans) == 1
