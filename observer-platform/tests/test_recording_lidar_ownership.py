"""No hardware, ROS participant, network mutation or real driver is used."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from deploy.scripts import device_sources as sources
from tests.test_device_sources import completed, fake_environment
from tests.test_recording_device_sources import manager, lidar_process, forbid_real_operations


def driver_printer(manager,tmp_path,monkeypatch):
    for key in sources.DDS_ENV_KEYS:
        monkeypatch.delenv(key,raising=False)
    config=manager.home/'.config/robot-cloud/archive.env';config.parent.mkdir(parents=True)
    config.write_text('ROS_DOMAIN_ID=216\nRMW_IMPLEMENTATION=rmw_cyclonedds_cpp\n'
        'FASTRTPS_DEFAULT_PROFILES_FILE=/archive-only-reader.xml\n')
    manager.ros_setup=tmp_path/'ros-setup.sh';manager.ros_setup.write_text('# harmless fixture\n')
    (manager.workspace/'install').mkdir(parents=True)
    (manager.workspace/'install/setup.bash').write_text('# harmless fixture\n')
    (manager.workspace/'env.sh').write_text('export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"\n'
        'export ROS_LOCALHOST_ONLY=0\nexport RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"\n'
        'if [[ -n "${MID360_NAV2_DDS_SERVER:-}" && "$MID360_NAV2_DDS_SERVER" != off ]]; then\n'
        ' export ROS_DISCOVERY_SERVER="$MID360_NAV2_DDS_SERVER"\nelse unset ROS_DISCOVERY_SERVER\nfi\n')
    fields=['ROS_DOMAIN_ID','ROS_LOCALHOST_ONLY','RMW_IMPLEMENTATION','ROS_DISCOVERY_SERVER',
        'MID360_NAV2_DDS_SERVER','FASTRTPS_DEFAULT_PROFILES_FILE','FASTDDS_DEFAULT_PROFILES_FILE']
    code='import os,json;print(json.dumps(dict((key,os.environ.get(key)) for key in '+repr(fields)+')))'
    fixture=sources.Source('mid360','unused-printer',(),('/usr/bin/python3','-B','-c',code),'/unused')
    return fixture


def run_printer(command):
    stale=dict(os.environ,ROS_DOMAIN_ID='199',ROS_DISCOVERY_SERVER='stale-manager:11811',
        FASTRTPS_DEFAULT_PROFILES_FILE='/stale-manager.xml',FASTDDS_DEFAULT_PROFILES_FILE='/stale-modern.xml')
    child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=stale)
    output,error=child.communicate(timeout=3)
    assert child.returncode==0,error
    return json.loads(output)


def test_new_lidar_uses_nav_baseline_without_archive_reader_or_manager_settings(manager,tmp_path,monkeypatch):
    fixture=driver_printer(manager,tmp_path,monkeypatch)
    result=run_printer(manager.source_command(fixture))
    assert result==dict(ROS_DOMAIN_ID='0',ROS_LOCALHOST_ONLY='0',RMW_IMPLEMENTATION='rmw_fastrtps_cpp',
        ROS_DISCOVERY_SERVER=None,MID360_NAV2_DDS_SERVER=None,FASTRTPS_DEFAULT_PROFILES_FILE=None,
        FASTDDS_DEFAULT_PROFILES_FILE=None)


def test_explicit_terminal_dds_is_processed_by_the_nav_workspace_environment(manager,tmp_path,monkeypatch):
    fixture=driver_printer(manager,tmp_path,monkeypatch)
    monkeypatch.setenv('ROS_DOMAIN_ID','7')
    monkeypatch.setenv('MID360_NAV2_DDS_SERVER','127.0.0.1:11811')
    monkeypatch.setenv('FASTDDS_DEFAULT_PROFILES_FILE','/explicit-terminal-profile.xml')
    result=run_printer(manager.source_command(fixture))
    assert result['ROS_DOMAIN_ID']=='7' and result['ROS_DISCOVERY_SERVER']=='127.0.0.1:11811'
    assert result['FASTDDS_DEFAULT_PROFILES_FILE']=='/explicit-terminal-profile.xml'
    assert result['FASTRTPS_DEFAULT_PROFILES_FILE'] is None
    assert '216' not in json.dumps(result) and 'archive-only' not in json.dumps(result)


def test_camera_source_keeps_the_existing_archive_reader_transport(manager,tmp_path,monkeypatch):
    driver_printer(manager,tmp_path,monkeypatch)
    command=manager.source_command(sources.SOURCES['berxel'])
    assert 'ROS_DOMAIN_ID=216' in command
    assert 'FASTRTPS_DEFAULT_PROFILES_FILE=/archive-only-reader.xml' in command
    assert sources.RECORDING_ROS_SHELL in command and sources.LIDAR_ROS_SHELL not in command


@pytest.mark.parametrize('parameters,remaps',[
    ({'xfer_format':0,'multi_topic':0},()),
    ({'xfer_format':1},()),
    ({'xfer_format':True,'multi_topic':0},()),
    (None,(('/livox/lidar','/other/lidar'),)),
    (None,(('livox/imu','other/imu'),)),
    (None,(('__ns','/algorithm'),)),
    (None,(('**','/unknown'),)),
])
def test_wrong_or_ambiguous_contract_never_claims_reuse_or_starts_another(manager,parameters,remaps):
    lidar_process(manager,parameters=parameters,remaps=remaps)
    manager.runner=lambda *_a,**_k:pytest.fail('existing driver triggered a system command')
    result=manager.recording_source(sources.RECORDING_MID360)
    assert result['state']=='failed'
    assert (manager.proc_root/'12/cmdline').is_file()


def test_missing_orphan_parameter_file_is_preserved_and_reported(manager):
    _,params,_=lidar_process(manager)
    params.unlink()
    manager.runner=lambda *_a,**_k:pytest.fail('unknown owner triggered a second start')
    result=manager.recording_source(sources.RECORDING_MID360)
    assert result['state']=='failed' and (manager.proc_root/'12/cmdline').is_file()


def test_a_different_real_graph_is_returned_for_idle_recorder_alignment(manager):
    lidar_process(manager,environment=dict(ROS_DOMAIN_ID='214',ROS_DISCOVERY_SERVER='127.0.0.1:11811',
        ROS_LOCALHOST_ONLY='0',RMW_IMPLEMENTATION='rmw_fastrtps_cpp',SECRET_FIXTURE_TOKEN='not-returned'))
    manager.runner=lambda *_a,**_k:pytest.fail('reuse changed the current driver')
    result=manager.recording_source(sources.RECORDING_MID360)
    assert result['state']=='reused' and result['ddsCompatible'] is False
    assert result['dds']['domain']=='214' and result['dds']['discoveryServer']=='127.0.0.1:11811'
    assert result['processStartTicks']=='1012'
    assert 'not-returned' not in json.dumps(result) and 'SECRET_FIXTURE' not in json.dumps(result)


def test_modern_profile_key_and_digest_are_reported_without_profile_contents(manager):
    profile=manager.home/'profile.xml';profile.write_text('<profiles>private-fixture</profiles>')
    lidar_process(manager,environment=dict(FASTDDS_DEFAULT_PROFILES_FILE=str(profile)))
    result=manager.recording_source(sources.RECORDING_MID360)
    assert result['state']=='reused' and result['dds']['profileEnvironmentKey']=='FASTDDS_DEFAULT_PROFILES_FILE'
    assert len(result['dds']['profileSha256'])==64
    assert 'private-fixture' not in json.dumps(result)


def test_inline_cyclone_configuration_is_not_exposed_or_automatically_aligned(manager):
    lidar_process(manager,environment=dict(CYCLONEDDS_URI='<secret-fixture/>'))
    result=manager.recording_source(sources.RECORDING_MID360)
    assert result['state']=='failed' and 'secret-fixture' not in json.dumps(result)


def test_cli_parameter_override_beats_the_launch_file(manager):
    root,_,_=lidar_process(manager)
    args=(root/'cmdline').read_bytes()+b'-p\0xfer_format:=0\0'
    (root/'cmdline').write_bytes(args)
    assert manager.recording_source(sources.RECORDING_MID360)['state']=='failed'


def test_parameters_for_an_unrelated_ros_node_do_not_prove_driver_format(manager):
    _,params,_=lidar_process(manager)
    document=json.loads(params.read_text())
    params.write_text(json.dumps({'unrelated_node':document['/**']}))
    assert manager.recording_source(sources.RECORDING_MID360)['state']=='failed'


def test_unknown_node_scoped_parameter_override_is_not_silently_ignored(manager):
    root,_,_=lidar_process(manager)
    (root/'cmdline').write_bytes((root/'cmdline').read_bytes()+b'-p\0livox_lidar_publisher:xfer_format:=0\0')
    assert manager.recording_source(sources.RECORDING_MID360)['state']=='failed'


def test_blue_sea_topic_remapping_cannot_hide_a_scan_conflict(manager):
    lidar_process(manager,driver='bluesea2_node',parameters=dict(scan_topic='/bluesea_new/scan',local_port=6668),
        remaps=(('/bluesea_new/scan','/scan'),))
    result=manager.recording_source(sources.SOURCES['bluesea'])
    assert result['state']=='failed'


def test_scan_parameter_cli_override_is_used_for_blue_sea_reuse(manager):
    root,_,_=lidar_process(manager,driver='bluesea2_node',parameters=dict(scan_topic='/scan',local_port=6668))
    (root/'cmdline').write_bytes((root/'cmdline').read_bytes()+b'--param\0scan_topic:=/bluesea_new/scan\0')
    result=manager.recording_source(sources.SOURCES['bluesea'])
    assert result['state']=='reused' and result['actualTopics']==['/bluesea_new/scan']


def test_unknown_udp_socket_owner_blocks_start_without_touching_it(manager):
    manager.runner=lambda *_a,**_k:completed('UNCONN 0 0 0.0.0.0:6668 0.0.0.0:*\n')
    with pytest.raises(sources.SourceError,match='占用'):
        manager.require_free_udp_ports([('0.0.0.0',6668)])


def test_mid360_does_not_reject_unrelated_nic_socket(manager):
    manager.runner=lambda *_a,**_k:completed('UNCONN 0 0 10.1.1.1:56301 0.0.0.0:*\n')
    manager.require_free_udp_ports([('192.168.1.100',56301)])


def test_default_wifi_gateway_is_not_used_as_the_lidar_host_address(manager):
    manager.runner=lambda *_a,**_k:completed('[{"dev":"wifi0","prefsrc":"10.1.1.2","gateway":"10.1.1.1"}]')
    with pytest.raises(sources.SourceError,match='直连'):
        manager.lidar_route('192.168.1.135')


def test_blue_sea_ntp_address_is_not_required_when_the_udp_route_is_valid(manager,tmp_path):
    fake_environment(manager,tmp_path)
    prefix=tmp_path/'blue';share=prefix/'share/bluesea2'
    (share/'params').mkdir(parents=True);(share/'launch').mkdir()
    (share/'launch/livox_driver_guard.py').write_text('# installed shared guard')
    original='bluesea_node:\n  ros__parameters:\n    scan_topic: "/scan"\n    lidar_ip: "192.168.158.98"\n    local_port: 6668\n    ntp_ip: "192.168.158.15"\n    ntp_enable: -1\n'
    (share/'params/lds_50c_e_7.yaml').write_text(original)
    manager.package=lambda *_a: manager.prefixes.setdefault('bluesea2',prefix)
    calls=[]
    def runner(command,**_kwargs):
        calls.append(command)
        if command[:5]==['ip','-j','-4','route','get']:
            return completed('[{"dev":"blue0","prefsrc":"192.168.158.100"}]')
        if command[:4]==['ip','-j','-4','address']:
            return completed('[{"ifname":"blue0","addr_info":[{"local":"192.168.158.100","prefixlen":24}]}]')
        if command[0]=='ss':return completed()
        pytest.fail('unexpected system operation '+repr(command))
    manager.runner=runner
    manager.preflight(sources.SOURCES['bluesea'])
    assert manager.params.read_text().replace('/bluesea_new/scan','/scan')==original
    assert all(command[0]!='ping' for command in calls)
    assert (share/'params/lds_50c_e_7.yaml').read_text()==original
