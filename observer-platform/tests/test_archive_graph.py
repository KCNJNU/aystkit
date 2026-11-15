"""Isolated DDS alignment: fake procfs and runner, never ROS/devices/systemd."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
from types import SimpleNamespace
import time

import pytest

from collector import archive_graph
from collector.archive_graph import GRAPH_KEYS, RecordingGraphError, UNIT, ensure_recording_dds


class GraphFixture:
    def __init__(self, root):
        self.root, self.proc, self.control = root, root/'proc', root/'control'
        self.proc.mkdir(); self.control.mkdir()
        self.env = root/'archive.env'
        self.before = ("# preserve this comment\nARCHIVE_UPLOAD_CONFIG='/private/fixture config.json'\n"
                       "ARCHIVE_OTHER_SECRET='fixture-secret-only'\nROS_DOMAIN_ID=0\n"
                       "UNRELATED_SETTING=keep-me\n").encode()
        self.env.write_bytes(self.before); self.env.chmod(0o600)
        self.now = 1_793_635_200.0
        self.calls, self.restarts = [], 0
        self.archive_pid = 201
        self.graph = dict(domain='17', rmw='rmw_fastrtps_cpp', localhostOnly=False,
            discoveryServer='', namespace='')
        self.report = {'hosts': {'ipc': {'devices': [dict(key='mid360', state='reused', pids=[101],
            processStartTicks='10001', dds=self.graph)]}}}
        self.process(101, dict(ROS_DOMAIN_ID='17'), ticks='10001', executable='livox_ros_driver2_node')
        self.process(self.archive_pid, dict(ROS_DOMAIN_ID='0'), ticks='20001')
        self.status()
        self.gate(False)
        self.on_restart = self.on_sleep = None

    def process(self, pid, environment, *, ticks='30001', executable='python3'):
        path = self.proc/str(pid); path.mkdir(exist_ok=True)
        (path/'cmdline').write_bytes(('/fixture/'+executable).encode()+b'\0')
        (path/'stat').write_text(str(pid)+' ('+executable+') '+ ' '.join(['S']+['0']*18+[ticks,'0'])+'\n')
        (path/'environ').write_bytes(b'\0'.join((key+'='+value).encode() for key,value in environment.items())+b'\0')

    def status(self, **changes):
        value = dict(updatedAt=datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            ros=dict(active=False, queueMessages=0, queueBytes=0,
                inFlightMessages=0, subscriptionCleanupPending=0),
            store=dict(active={}, resourceMonitor=dict(state='READY',ready=True,workerAlive=True)))
        for key, replacement in changes.items():
            if isinstance(replacement, dict):
                value[key].update(replacement)
            else:
                value[key] = replacement
        (self.control/'status.json').write_text(json.dumps(value))
        (self.control/'status.json').chmod(0o600)

    def gate(self, enabled):
        (self.control/'garage.json').write_text(json.dumps(dict(schemaVersion=1, scope='garage', enabled=enabled)))

    def configured_graph(self):
        result = {}
        for line in self.env.read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator and key in GRAPH_KEYS:
                decoded = shlex.split(value)
                result[key] = decoded[0] if decoded else ''
        return result

    def restart_archive(self):
        self.archive_pid += 1
        self.now += .1
        self.process(self.archive_pid, self.configured_graph(), ticks=str(20000+self.archive_pid))
        self.status()

    def runner(self, command, **options):
        self.calls.append((list(command), options))
        assert command in (
            ['systemctl','--user','show',UNIT,'--property=MainPID','--value'],
            ['systemctl','--user','restart',UNIT],
        ), 'alignment may only inspect/restart its exact archive unit'
        assert options.get('capture_output') is True and options.get('text') is True
        assert 0 < options.get('timeout', 0) <= 90
        if command[2] == 'show':
            return SimpleNamespace(returncode=0,stdout=str(self.archive_pid)+'\n',stderr='')
        self.restarts += 1
        if self.on_restart is not None:
            result = self.on_restart(self.restarts)
            if result is not None:
                return result
        self.restart_archive()
        return SimpleNamespace(returncode=0,stdout='',stderr='')

    def sleep(self, delay):
        self.now += delay
        if self.on_sleep is not None:
            self.on_sleep()

    def align(self, **options):
        return ensure_recording_dds({'ARCHIVE_ENV_FILE': str(self.env)}, self.control, self.report,
            runner=self.runner, proc_root=self.proc, clock=lambda:self.now,
            sleeper=self.sleep, timeout=1, **options)

    def assert_no_mutation(self):
        assert self.env.read_bytes() == self.before
        assert self.restarts == 0


@pytest.fixture
def graph(tmp_path):
    return GraphFixture(tmp_path)


def test_already_aligned_has_no_configuration_or_service_side_effects(graph):
    graph.process(graph.archive_pid, dict(ROS_DOMAIN_ID='017'), ticks='20001')
    result = graph.align()
    assert result['state'] == 'unchanged' and result['reason'] == 'already_aligned'
    graph.assert_no_mutation()
    assert all(command[2] == 'show' for command,_ in graph.calls)


def test_only_a_verified_driver_report_can_authorize_alignment(graph):
    graph.report = {'hosts': {'ipc': {'devices': [dict(key='mid360', state='failed', dds=graph.graph, pids=[101])]}}}
    assert graph.align() == dict(state='unchanged',reason='no_verified_driver_graph')
    graph.assert_no_mutation()
    assert graph.calls == []


def test_changes_only_idle_recorder_and_preserves_other_settings_and_secrets(graph):
    driver_before = {path.name:path.read_bytes() for path in (graph.proc/'101').iterdir()}
    old_pid = graph.archive_pid
    result = graph.align()
    assert result['state'] == 'aligned' and result['domain'] == '17'
    assert result['restartedUnit'] == UNIT and result['driverUnchanged'] is True
    assert graph.archive_pid != old_pid and graph.restarts == 1
    content = graph.env.read_text()
    assert "ARCHIVE_OTHER_SECRET='fixture-secret-only'" in content
    assert "ARCHIVE_UPLOAD_CONFIG='/private/fixture config.json'" in content
    assert '# preserve this comment\n' in content and 'UNRELATED_SETTING=keep-me\n' in content
    assert content.count('ROS_DOMAIN_ID=') == 1
    assert graph.configured_graph()['ROS_DOMAIN_ID'] == '17'
    assert graph.env.stat().st_mode & 0o777 == 0o600
    assert {path.name:path.read_bytes() for path in (graph.proc/'101').iterdir()} == driver_before


@pytest.mark.parametrize('busy', ('gate','desktop-lease','active-ros','active-store','queueMessages',
    'queueBytes','inFlightMessages','subscriptionCleanupPending'))
def test_capture_or_any_undrained_queue_refuses_service_restart(graph,busy):
    if busy == 'gate':
        graph.gate(True)
    elif busy == 'desktop-lease':
        (graph.control/'desktop-lease.json').write_text(json.dumps(dict(schemaVersion=1,scope='garage',
            enabled=True,desktopSession='a'*32,expiresAt=time.time()+20)))
    elif busy == 'active-ros':
        graph.status(ros={'active':True})
    elif busy == 'active-store':
        graph.status(store={'active':{'雷达MID360':{'messages':1}}})
    else:
        graph.status(ros={busy:1})
    with pytest.raises(RecordingGraphError):
        graph.align()
    graph.assert_no_mutation()


@pytest.mark.parametrize('state', ('PENDING','STALE','ERROR','CLOSED'))
@pytest.mark.parametrize('already_aligned',(False,True))
def test_resource_probe_not_ready_is_never_a_ready_graph(graph,state,already_aligned):
    graph.status(store={'resourceMonitor':{'state':state,'ready':False}})
    if already_aligned:
        graph.process(graph.archive_pid,dict(ROS_DOMAIN_ID='17'))
    with pytest.raises(RecordingGraphError,match='资源'):
        graph.align()
    graph.assert_no_mutation()


@pytest.mark.parametrize('status_change', (
    {'updatedAt':'1970-01-01T00:00:00+00:00'},
    {'ros':{'queueMessages':False}}, {'ros':{'active':None}}, {'store':{'active':None}},
))
def test_old_or_incomplete_idle_status_cannot_authorize_alignment(graph,status_change):
    graph.status(**status_change)
    with pytest.raises(RecordingGraphError):
        graph.align()
    graph.assert_no_mutation()


def test_driver_pid_reuse_is_rejected_before_touching_recorder(graph):
    graph.process(101,dict(ROS_DOMAIN_ID='17'),ticks='10002',executable='livox_ros_driver2_node')
    with pytest.raises(RecordingGraphError,match='重启'):
        graph.align()
    graph.assert_no_mutation()
    assert graph.calls == []


def test_driver_identity_requires_the_helpers_start_time_proof(graph):
    graph.report['hosts']['ipc']['devices'][0].pop('processStartTicks')
    with pytest.raises(RecordingGraphError):
        graph.align()
    graph.assert_no_mutation()


def test_changed_driver_environment_is_rejected_before_touching_recorder(graph):
    graph.process(101,dict(ROS_DOMAIN_ID='18'),ticks='10001',executable='livox_ros_driver2_node')
    with pytest.raises(RecordingGraphError,match='改变'):
        graph.align()
    graph.assert_no_mutation()


@pytest.mark.parametrize('mode',(0o622,0o644,0o660,0o666))
def test_mutable_env_permissions_refuse_alignment(graph,mode):
    graph.env.chmod(mode)
    with pytest.raises(RecordingGraphError,match='0600|权限'):
        graph.align()
    graph.assert_no_mutation()


def test_symlink_environment_is_never_replaced(graph,tmp_path):
    original = tmp_path/'private-env';graph.env.rename(original)
    graph.env.symlink_to(original)
    with pytest.raises((RecordingGraphError,OSError)):
        graph.align()
    assert graph.env.is_symlink() and original.read_bytes() == graph.before
    assert graph.restarts == 0


def profile_graph(graph,tmp_path):
    profile = tmp_path/'dds.xml';profile.write_text('<profiles/>');profile.chmod(0o644)
    graph.graph.update(profilePath=str(profile),profileSha256=hashlib.sha256(profile.read_bytes()).hexdigest(),
        profileEnvironmentKey='FASTRTPS_DEFAULT_PROFILES_FILE')
    graph.process(101,dict(ROS_DOMAIN_ID='17',FASTRTPS_DEFAULT_PROFILES_FILE=str(profile)),
        ticks='10001',executable='livox_ros_driver2_node')
    return profile


def test_profile_sha_change_is_rejected_before_touching_recorder(graph,tmp_path):
    profile = profile_graph(graph,tmp_path)
    profile.write_text('<changed/>')
    with pytest.raises(RecordingGraphError,match='改变'):
        graph.align()
    graph.assert_no_mutation()


def test_profile_with_valid_identity_is_retained_and_works_for_alignment(graph,tmp_path):
    profile = profile_graph(graph,tmp_path)
    result = graph.align()
    assert result['state'] == 'aligned'
    assert graph.configured_graph()['FASTRTPS_DEFAULT_PROFILES_FILE'] == str(profile)
    assert profile.read_text() == '<profiles/>'


def test_restart_failure_rolls_back_bytes_and_restarts_only_idle_archive(graph):
    def fail_first(attempt):
        if attempt == 1:
            return SimpleNamespace(returncode=1,stdout='',stderr='fixture restart failure')
    graph.on_restart = fail_first
    with pytest.raises(RecordingGraphError,match='重启失败'):
        graph.align()
    assert graph.env.read_bytes() == graph.before
    assert graph.restarts == 2
    assert graph.configured_graph()['ROS_DOMAIN_ID'] == '0'


def test_new_service_environment_mismatch_times_out_then_restores_config(graph):
    def wrong_environment(attempt):
        if attempt == 1:
            graph.restart_archive()
            graph.process(graph.archive_pid,dict(ROS_DOMAIN_ID='99'))
            return SimpleNamespace(returncode=0,stdout='',stderr='')
    graph.on_restart = wrong_environment
    with pytest.raises(RecordingGraphError,match='确认'):
        graph.align()
    assert graph.env.read_bytes() == graph.before and graph.restarts == 2


def test_gate_opened_during_restart_cannot_trigger_a_rollback_restart(graph):
    def gate_opens(attempt):
        if attempt == 1:
            graph.restart_archive();graph.gate(True)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
    graph.on_restart = gate_opens
    with pytest.raises(RecordingGraphError):
        graph.align()
    assert graph.env.read_bytes() == graph.before and graph.restarts == 1


def test_driver_reuse_during_recorder_restart_never_claims_driver_unchanged(graph):
    def driver_restarts(attempt):
        if attempt == 1:
            graph.process(101,dict(ROS_DOMAIN_ID='17'),ticks='10002',executable='livox_ros_driver2_node')
    graph.on_restart = driver_restarts
    with pytest.raises(RecordingGraphError):
        graph.align()
    assert graph.env.read_bytes() == graph.before


def test_profile_changed_during_restart_never_claims_graph_is_aligned(graph,tmp_path):
    profile = profile_graph(graph,tmp_path)
    def profile_changes(attempt):
        if attempt == 1:
            profile.write_text('<changed-after-preflight/>')
    graph.on_restart = profile_changes
    with pytest.raises(RecordingGraphError):
        graph.align()
    assert graph.env.read_bytes() == graph.before


def test_write_failure_after_atomic_replace_restores_original_configuration(graph,monkeypatch):
    original = archive_graph._write_env
    failed = []
    def failing_write(*args,**kwargs):
        original(*args,**kwargs)
        if not failed:
            failed.append(True)
            raise OSError('isolated directory fsync failure after replace')
    monkeypatch.setattr(archive_graph,'_write_env',failing_write)
    with pytest.raises((RecordingGraphError,OSError)):
        graph.align()
    assert graph.env.read_bytes() == graph.before


def test_concurrent_environment_edit_is_not_overwritten_by_alignment(graph,monkeypatch):
    original = archive_graph._regular
    concurrent = graph.before+b'NEW_SETTING=preserve-concurrent-editor\n'
    changed = []
    def concurrent_read(path,*args,**kwargs):
        raw = original(path,*args,**kwargs)
        if Path(path) == graph.env and not changed:
            changed.append(True)
            graph.env.write_bytes(concurrent)
        return raw
    monkeypatch.setattr(archive_graph,'_regular',concurrent_read)
    with pytest.raises(RecordingGraphError):
        graph.align()
    assert graph.env.read_bytes() == concurrent
    assert graph.restarts == 0


def test_pending_resources_after_restart_are_polled_until_really_ready(graph):
    polled = []
    def pending(attempt):
        if attempt == 1:
            graph.restart_archive()
            graph.status(store={'resourceMonitor':{'state':'PENDING','ready':False}})
            return SimpleNamespace(returncode=0,stdout='',stderr='')
    def become_ready():
        polled.append(True)
        graph.status()
    graph.on_restart, graph.on_sleep = pending, become_ready
    result = graph.align()
    assert result['state'] == 'aligned' and polled and graph.restarts == 1


def test_active_already_aligned_recorder_is_never_borrowed_for_a_new_lease(graph):
    graph.process(graph.archive_pid,dict(ROS_DOMAIN_ID='17'))
    graph.gate(True)
    with pytest.raises(RecordingGraphError):
        graph.align()
    graph.assert_no_mutation()


def test_rollback_does_not_overwrite_an_edit_made_while_restart_was_running(graph):
    concurrent = []
    def editor(attempt):
        if attempt == 1:
            content = graph.env.read_bytes()+b'NEW_SETTING=keep-other-operators-work\n'
            graph.env.write_bytes(content); concurrent.append(content)
            return SimpleNamespace(returncode=1,stdout='',stderr='fixture failure')
    graph.on_restart = editor
    with pytest.raises(RecordingGraphError):
        graph.align()
    assert concurrent and graph.env.read_bytes() == concurrent[0]
    assert graph.restarts == 1, 'a concurrent environment edit cannot authorize another service restart'


def test_driver_environment_change_during_restart_invalidates_final_confirmation(graph):
    def different_graph(attempt):
        if attempt == 1:
            graph.process(101,dict(ROS_DOMAIN_ID='18'),ticks='10001',executable='livox_ros_driver2_node')
    graph.on_restart = different_graph
    with pytest.raises(RecordingGraphError):
        graph.align()
    assert graph.env.read_bytes() == graph.before


def test_cyclone_profile_changed_during_restart_is_not_treated_as_same_graph(graph,tmp_path):
    profile = tmp_path/'cyclone.xml';profile.write_text('<CycloneDDS/>');profile.chmod(0o644)
    graph.graph.update(rmw='rmw_cyclonedds_cpp',cycloneUri=profile.as_uri())
    graph.process(101,dict(ROS_DOMAIN_ID='17',RMW_IMPLEMENTATION='rmw_cyclonedds_cpp',
        CYCLONEDDS_URI=profile.as_uri()),ticks='10001',executable='livox_ros_driver2_node')
    def changed(attempt):
        if attempt == 1:
            profile.write_text('<CycloneDDS><Domain/></CycloneDDS>')
    graph.on_restart = changed
    with pytest.raises(RecordingGraphError):
        graph.align()
    assert graph.env.read_bytes() == graph.before


def test_different_owner_proc_directory_cannot_authorize_reconfiguration(graph,monkeypatch):
    uid = archive_graph.os.getuid()
    monkeypatch.setattr(archive_graph.os,'getuid',lambda:uid+1)
    with pytest.raises(RecordingGraphError,match='归属'):
        graph.align()
    graph.assert_no_mutation()
