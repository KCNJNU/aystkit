import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from collector.archive_core import GarageGate
from collector.archive_source_link import REMOTE_SYNC, ssh_options, sync_gate


@pytest.mark.parametrize('mismatch', ['machine', 'archive', 'domain'])
def test_mismatched_profile_blocks_independent_source_link_before_any_remote_write(tmp_path, monkeypatch, mismatch):
    from collector import archive_source_link
    config=tmp_path/'.config/robot-cloud';config.mkdir(parents=True)
    warehouse_id=str(uuid4())
    profile={'schemaVersion':1,'observerId':'car-02','warehouseId':warehouse_id,
        'rosDomainId':2,'machineId':Path('/etc/machine-id').read_text().strip()}
    if mismatch=='machine':profile['machineId']='another-machine'
    target=config/'robot.json';target.write_text(json.dumps(profile));target.chmod(0o600)
    (config/'collector.env').write_text('ROBOT_ID=car-02\nROS_DOMAIN_ID='+('0' if mismatch=='domain' else '2')+'\n')
    (config/'archive.env').write_text('ARCHIVE_ROBOT_ID='+
        (str(uuid4()) if mismatch=='archive' else warehouse_id)+'\n')
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    for key in ('ROBOT_PROFILE_FILE','ROBOT_ID','ARCHIVE_ROBOT_ID','ROS_DOMAIN_ID'):
        monkeypatch.delenv(key,raising=False)
    calls=[]
    def external_work(*args,**kwargs):
        calls.append((args,kwargs))
        raise AssertionError('identity mismatch must stop before SSH or worker creation')
    for name in ('ssh_options','FilePuller','sync_gate'):
        monkeypatch.setattr(archive_source_link,name,external_work)
    monkeypatch.setattr(archive_source_link.threading,'Thread',external_work)
    monkeypatch.setattr(archive_source_link.subprocess,'run',external_work)
    with pytest.raises(ValueError):archive_source_link.main()
    assert calls==[]
    assert not (tmp_path/'.local/state/robot-cloud').exists()


def test_private_source_link_follows_hostname_and_preserves_existing_host_identity():
    options, target = ssh_options({})
    assert target == 'nvidia@nvidia-desktop.local'
    assert 'StrictHostKeyChecking=yes' in options
    assert 'HostKeyAlias=192.168.1.50' in options
    assert 'ForwardAgent=no' in options and '-T' in options
    assert 'ClearAllForwardings=yes' in options
    assert not any('BindInterface' in arg for arg in options)
    with pytest.raises(ValueError):
        ssh_options({'ARCHIVE_SOURCE_HOST': 'host;echo nope'})


def test_gate_sync_acknowledges_exact_revision_and_never_uploads_live_frames(tmp_path):
    gate = GarageGate(tmp_path / 'garage.json')
    gate.set(True)
    captured = []
    def runner(command, **kwargs):
        captured.append((command, kwargs))
        sent = json.loads(kwargs['input'])
        return SimpleNamespace(returncode=0, stdout=json.dumps({'gate': sent, 'effectiveGate': sent,
            'recorder': {'store': {'enabled': False}}, 'camera': {'frames': 2}}).encode(), stderr=b'')
    report = sync_gate(['/usr/bin/ssh'], 'nvidia@host', tmp_path, runner)
    assert report['connected'] and report['gateAcknowledged'] and report['gateEnabled']
    assert report['camera'] == {'frames': 2}
    assert report['recorderGateEnabled'] is False  # separate, potentially delayed runtime evidence
    assert json.loads((tmp_path / 'source.json').read_text())['gateAcknowledged']
    assert len(captured[0][1]['input']) < 1024
    assert b'password' not in captured[0][1]['input']
    gate.set(False)
    assert sync_gate([], 'target', tmp_path, runner)['gateEnabled'] is False


def test_auth_failure_never_claims_source_stopped_or_acknowledged(tmp_path):
    GarageGate(tmp_path / 'garage.json').set(False)
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=255, stdout=b'', stderr=b'Permission denied (publickey,password)')
    report = sync_gate([], 'target', tmp_path, runner)
    assert report == dict(updatedAt=report['updatedAt'], connected=False,
                          error='source_authentication_failed', gateAcknowledged=False)
    assert 'Permission denied' not in (tmp_path / 'source.json').read_text()


def test_desktop_only_capture_sync_keeps_deadline_and_leaves_web_gate_unchanged(tmp_path, monkeypatch):
    from collector.archive_core import atomic_json
    from collector import archive_core
    monkeypatch.setattr(archive_core.time, 'time', lambda: 100)
    GarageGate(tmp_path/'garage.json').set(False)
    original = (tmp_path/'garage.json').read_bytes()
    atomic_json(tmp_path/'desktop-lease.json', dict(schemaVersion=1, scope='garage', enabled=True,
        desktopSession='a'*32, updatedAt='lease-revision', expiresAt=120))
    sent = []
    def runner(command, **kwargs):
        value = json.loads(kwargs['input']); sent.append(value)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'gate': value, 'effectiveGate': value}).encode(), stderr=b'')
    assert sync_gate([], 'target', tmp_path, runner)['gateAcknowledged']
    assert sent[0]['enabled'] and sent[0]['expiresAt'] == 120
    assert sent[0]['desktopSession'] == 'a'*32
    assert (tmp_path/'garage.json').read_bytes() == original

    def wrong_session(command, **kwargs):
        value = json.loads(kwargs['input']); value['desktopSession'] = 'b'*32
        return SimpleNamespace(returncode=0, stdout=json.dumps({'gate': value, 'effectiveGate': value}).encode(), stderr=b'')
    assert not sync_gate([], 'target', tmp_path, wrong_session)['gateAcknowledged']


def test_remote_gate_sync_never_follows_fixed_temporary_link(tmp_path):
    control=tmp_path/'.local/state/robot-cloud/garage-control';control.mkdir(parents=True)
    outside=tmp_path/'outside';outside.write_bytes(b'private untouched bytes')
    (control/'garage.json.link-tmp').symlink_to(outside)
    gate=dict(schemaVersion=1,scope='garage',enabled=True,updatedAt='fixture')
    result=subprocess.run(['/usr/bin/python3','-c',REMOTE_SYNC],input=json.dumps(gate).encode(),
        capture_output=True,env={**os.environ,'HOME':str(tmp_path)},check=True)
    assert json.loads(result.stdout)['gate']==gate
    assert json.loads(result.stdout)['effectiveGate']==gate
    assert json.loads((control/'garage.json').read_text())==gate
    assert outside.read_bytes()==b'private untouched bytes'
    assert not list(control.glob('garage.json.link-tmp-*'))


@pytest.mark.parametrize('mode', ['expired', 'changed', 'missing', 'wrong_expiry', 'malformed_ack'])
def test_echoed_receipt_does_not_acknowledge_an_unapplied_source_gate(tmp_path, monkeypatch, mode):
    from collector import archive_core
    monkeypatch.setattr(archive_core.time, 'time', lambda: 100)
    GarageGate(tmp_path/'garage.json').set(True, expires_at=120)
    def runner(command, **kwargs):
        sent = json.loads(kwargs['input'])
        effective = dict(sent)
        ack = dict(sent)
        if mode == 'expired': effective['expiresAt'] = 99
        elif mode == 'changed': effective['updatedAt'] = 'other-revision'
        elif mode == 'missing': effective = None
        elif mode == 'wrong_expiry': effective['expiresAt'] = 121
        else: ack = []
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {'gate': ack, 'effectiveGate': effective}).encode(), stderr=b'')
    report = sync_gate([], 'target', tmp_path, runner)
    assert report['connected'] is True
    assert report['gateAcknowledged'] is False
    if mode == 'expired': assert report['gateEnabled'] is False
    if mode == 'missing': assert report['gateEnabled'] is None


def test_closed_request_is_not_acknowledged_while_source_local_lease_remains_open(tmp_path):
    GarageGate(tmp_path/'garage.json').set(False)
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({'gate': json.loads(kwargs['input']),
            'effectiveGate': {'schemaVersion': 1, 'scope': 'garage', 'enabled': True}}).encode(), stderr=b'')
    report = sync_gate([], 'target', tmp_path, runner)
    assert report['gateEnabled'] is True and report['gateAcknowledged'] is False


def capture_probe():
    from collector.archive_source_link import REMOTE_CAPTURE_PROBE
    namespace = {'json': json}
    exec(REMOTE_CAPTURE_PROBE, namespace)
    return namespace


def test_camera_probe_is_independent_of_archive_gate_and_requires_live_frames():
    probe = capture_probe()
    status = {'cameras': [
        {'id': ['camera1'], 'frames': 100, 'frame_age_ms': 0},
        {'id': 'unconfigured-camera', 'frames': 100, 'frame_age_ms': 0},
        {'id': 'camera1', 'frames': 20, 'frame_age_ms': 125},
        {'id': 'camera2', 'frames': 3000, 'frame_age_ms': 9000},
        {'id': 'camera3', 'frames': 0, 'frame_age_ms': 0},
        {'id': 'camera4', 'frames': True, 'frame_age_ms': 0},
    ]}
    result = probe['capture_status']({'store': {'enabled': False}}, lambda: status, now=100)
    assert result['cameras']['camera1'] == dict(state='capturing', frames=20, ageS=.125)
    for key in ('camera2', 'camera3', 'camera4'):
        assert result['cameras'][key]['state'] != 'capturing'
    assert result['cameras']['camera4']['frames'] is None
    assert result['cameras']['berxelDepth'] == dict(state='unknown', frames=None, ageS=None)


def test_absent_camera_stream_and_failed_probe_never_reuse_historical_receipt():
    probe = capture_probe()
    unavailable = probe['capture_status'](None, lambda: None, now=100)
    assert len(unavailable['cameras']) == 6
    assert all(item == dict(state='unknown', frames=None, ageS=None)
               for item in unavailable['cameras'].values())
    stopped = probe['capture_status'](None, lambda: {'cameras': [], 'cameraUsage': {
        'usbLeft': {'owner': 'NONE', 'connected': True, 'checkedAgeMs': 100},
        'usbFront': {'owner': 'OTHER', 'checkedAgeMs': 100},
        'usbHub3': {'owner': 'UNKNOWN', 'checkedAgeMs': 100},
        'usbHub2': {'owner': 'NONE', 'checkedAgeMs': 9000},
    }}, now=100)['cameras']
    assert stopped['camera1']['state'] == 'not_capturing'
    assert all(stopped[key]['state'] == 'unknown' for key in ('camera2', 'camera3', 'camera4'))
    assert stopped['camera1']['frames'] is None


def test_depth_probe_uses_only_fresh_actual_image_receipts_not_camera_info():
    probe = capture_probe()
    recorder = {'updatedAt': '1970-01-01T00:01:39+00:00', 'ros': {'topics': {
        '/berxel/color/image_raw': {'state': 'AVAILABLE', 'received': 12, 'ageS': .3},
        '/berxel/depth/image_raw': {'state': 'ERROR', 'received': 20, 'ageS': .2},
        '/berxel/color/camera_info': {'state': 'AVAILABLE', 'received': 1000, 'ageS': 0},
    }}}
    rows = probe['capture_status'](recorder, lambda: None, now=100)['cameras']
    assert rows['berxelColor'] == dict(state='capturing', frames=12, ageS=1.3)
    assert rows['berxelDepth'] == dict(state='capturing', frames=20, ageS=1.2)
    for mode in ('disabled', 'old', 'future', 'stopped'):
        changed = json.loads(json.dumps(recorder))
        if mode == 'disabled':
            for row in changed['ros']['topics'].values(): row['state'] = 'DISABLED'
        elif mode == 'old': changed['updatedAt'] = '1970-01-01T00:00:00+00:00'
        elif mode == 'future': changed['updatedAt'] = '1970-01-01T00:10:00+00:00'
        else: changed['state'] = 'stopped'
        rows = probe['capture_status'](changed, lambda: None, now=100)['cameras']
        assert rows['berxelColor']['state'] == rows['berxelDepth']['state'] == 'unknown'


def test_only_allowlisted_camera_metadata_reaches_source_status(tmp_path):
    GarageGate(tmp_path / 'garage.json').set(False)
    capture = capture_probe()['capture_status'](None, lambda: {'cameras': []}, now=100)
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'gate': json.loads(kwargs['input']), 'effectiveGate': json.loads(kwargs['input']),
            'capture': capture}).encode(), stderr=b'')
    report = sync_gate([], 'target', tmp_path, runner)
    assert report['capture'] == capture
    assert report['gateEnabled'] is False
    assert json.loads((tmp_path / 'source.json').read_text())['capture'] == capture


@pytest.mark.parametrize('status,content_type,body,length,accepted', [
    (200, 'application/json; charset=utf-8', b'{"cameras":[]}', '14', True),
    (302, 'application/json', b'{"cameras":[]}', '14', False),
    (200, 'text/html', b'{"cameras":[]}', '14', False),
    (200, 'application/json', b' ' * 65537, None, False),
    (200, 'application/json', b'{"cameras":[]}', '65537', False),
    (200, 'application/json', b'{"cameras":{}}', None, False),
])
def test_capture_http_probe_has_fixed_read_only_origin_and_bounded_body(monkeypatch, status, content_type, body, length, accepted):
    probe = capture_probe()
    calls = []
    class Response:
        def __init__(self): self.status = status
        def getheader(self, name, default=None):
            return {'Content-Type': content_type, 'Content-Length': length}.get(name, default)
        def read(self, maximum):
            calls.append(('read', maximum))
            return body[:maximum]
    class Connection:
        sock = None
        def __init__(self, host, port, timeout): calls.append(('origin', host, port, timeout))
        def connect(self): pass
        def request(self, method, path, headers): calls.append(('request', method, path, headers))
        def getresponse(self): return Response()
        def close(self): calls.append(('closed',))
    monkeypatch.setenv('HTTP_PROXY', 'http://untrusted.invalid:1234')
    monkeypatch.setattr(probe['http'].client, 'HTTPConnection', Connection)
    value = probe['camera_http_status']()
    assert (value is not None) == accepted
    assert calls[0][:3] == ('origin', '127.0.0.1', 8081)
    assert calls[1][0:3] == ('request', 'GET', '/status')
    assert calls[-1] == ('closed',)
    assert not any('/video' in str(call) or '/stream' in str(call) for call in calls)


@pytest.mark.parametrize('row', [
    {'id': 'camera1'},
    {'id': 'camera1', 'frames': 12},
    {'id': 'camera1', 'frame_age_ms': 100},
    {'id': 'camera1', 'frames': -1, 'frame_age_ms': 100},
    {'id': 'camera1', 'frames': True, 'frame_age_ms': 100},
    {'id': 'camera1', 'frames': '12', 'frame_age_ms': 100},
    {'id': 'camera1', 'frames': 12, 'frame_age_ms': -1},
    {'id': 'camera1', 'frames': 12, 'frame_age_ms': float('nan')},
    {'id': 'camera1', 'frames': 12, 'frame_age_ms': 100, 'error': {}},
])
def test_incomplete_or_invalid_native_evidence_is_unknown(row):
    value = capture_probe()['capture_status'](None, lambda: {'cameras': [row]}, now=100)
    assert value['cameras']['camera1']['state'] == 'unknown'


@pytest.mark.parametrize('row', [
    {'id': 'camera1', 'frames': 0},
    {'id': 'camera1', 'frame_age_ms': 5001},
    {'id': 'camera1', 'error': '相机暂不可用'},
])
def test_explicit_native_stop_evidence_can_report_not_capturing(row):
    value = capture_probe()['capture_status'](None, lambda: {'cameras': [row]}, now=100)
    assert value['cameras']['camera1']['state'] == 'not_capturing'


@pytest.mark.parametrize('usage', [None, [], {}, {'usbLeft': []},
    {'usbLeft': {}}, {'usbLeft': {'owner': 'NONE'}},
    {'usbLeft': {'owner': 'NONE', 'connected': True, 'checkedAgeMs': 6000}},
    {'usbLeft': {'owner': 'NONE', 'connected': True, 'checkedAgeMs': -1}},
    {'usbLeft': {'owner': 'NONE', 'connected': True, 'checkedAgeMs': float('nan')}},
    {'usbLeft': {'owner': 'NONE', 'connected': None, 'checkedAgeMs': 100}},
    {'usbLeft': {'owner': 'WEB', 'connected': True, 'checkedAgeMs': 100}},
])
def test_absent_native_row_without_fresh_known_owner_is_unknown(usage):
    value = capture_probe()['capture_status'](None, lambda: {'cameras': [], 'cameraUsage': usage}, now=100)
    assert value['cameras']['camera1'] == dict(state='unknown', frames=None, ageS=None)


@pytest.mark.parametrize('connected', [True, False])
def test_absent_native_row_with_recent_unoccupied_device_is_not_capturing(connected):
    status = {'cameras': [], 'cameraUsage': {
        'usbLeft': {'owner': 'NONE', 'connected': connected, 'checkedAgeMs': 100}}}
    row = capture_probe()['capture_status'](None, lambda: status, now=100)['cameras']['camera1']
    assert row == dict(state='not_capturing', frames=None, ageS=None)



def depth_report(at=50, frames=123, age=.1):
    return {'schemaVersion': 1, 'reportedMonotonic': at, 'channels': {
        key: {'topic': '/berxel/' + key + '/image_raw',
              'receivedFrames': frames, 'sourceAgeS': age}
        for key in ('color', 'depth')}}


def test_depth_original_receipts_are_visible_with_recording_disabled():
    recorder = {'updatedAt': '1970-01-01T00:01:40+00:00', 'ros': {'active': False, 'topics': {
        '/berxel/color/image_raw': {'state': 'DISABLED', 'received': 0},
        '/berxel/depth/image_raw': {'state': 'DISABLED', 'received': 0}}}}
    rows = capture_probe()['capture_status'](recorder, lambda: None, now=100,
        depth=depth_report(), monotonic_now=50.2)['cameras']
    for key in ('berxelColor', 'berxelDepth'):
        assert rows[key]['state'] == 'capturing'
        assert rows[key]['frames'] == 123
        assert rows[key]['ageS'] == pytest.approx(.3)
    assert recorder['ros']['active'] is False
    assert all(row['received'] == 0 for row in recorder['ros']['topics'].values())


@pytest.mark.parametrize('stamp', [44.9, 50.1, None, True, float('nan')])
def test_stale_future_or_invalid_depth_reports_never_claim_live_capture(stamp):
    rows = capture_probe()['capture_status'](None, lambda: None, now=100,
        depth=depth_report(at=stamp), monotonic_now=50)['cameras']
    assert rows['berxelColor']['state'] == rows['berxelDepth']['state'] == 'unknown'


def test_depth_receipt_age_includes_time_since_health_was_written():
    rows = capture_probe()['capture_status'](None, lambda: None, now=100,
        depth=depth_report(at=46, age=2), monotonic_now=50)['cameras']
    assert rows['berxelColor'] == {'state': 'not_capturing', 'frames': 123, 'ageS': 6}


@pytest.mark.parametrize('patch', [
    {'topic': '/berxel/color/camera_info'}, {'receivedFrames': True},
    {'receivedFrames': -1}, {'receivedFrames': 2**53}, {'sourceAgeS': float('nan')},
])
def test_bad_original_depth_evidence_does_not_become_capture(patch):
    depth = depth_report()
    depth['channels']['color'].update(patch)
    rows = capture_probe()['capture_status'](None, lambda: None, now=100,
        depth=depth, monotonic_now=50)['cameras']
    assert rows['berxelColor']['state'] != 'capturing'
    assert rows['berxelDepth']['state'] == 'capturing'


@pytest.mark.parametrize('mode', ['valid', 'old_boot', 'wrong_process', 'wrong_mode', 'symlink'])
def test_depth_status_file_requires_live_expected_encoder(monkeypatch, tmp_path, mode):
    from pathlib import Path
    import stat
    probe = capture_probe()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    parent = tmp_path / '.local/state/robot-cloud/garage-control'
    parent.mkdir(parents=True, mode=0o700)
    path = parent / 'berxel-capture.json'
    payload = depth_report()
    payload.update(pid=os.getpid(), bootId=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    if mode == 'old_boot': payload['bootId'] = 'old-boot'
    path.write_text(json.dumps(payload));path.chmod(0o644 if mode == 'wrong_mode' else 0o600)
    if mode == 'symlink':
        path.rename(parent / 'actual.json');path.symlink_to(parent / 'actual.json')
    read = Path.read_bytes
    def read_bytes(p):
        if str(p) == '/proc/' + str(os.getpid()) + '/cmdline':
            script = tmp_path / '.local/share/rk3588-observer-camera/jetson_camera_encoder.py'
            if mode == 'wrong_process': script = tmp_path / 'unrelated.py'
            return ('/usr/bin/python3\0-B\0' + str(script) + '\0').encode()
        return read(p)
    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    result = probe['depth_capture_status']()
    assert (result is not None) == (mode == 'valid')
