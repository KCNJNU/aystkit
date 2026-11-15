"""Enrollment must preserve original secrets and fail on conflicting identities."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import shlex
import shutil
from types import SimpleNamespace
from uuid import uuid4
import pytest
from tests.test_backend import config
from tests.test_start_all_devices import startup  # noqa: F401
from collector.robot_profile import literal_environment

spec=importlib.util.spec_from_file_location('fleet_config',Path(__file__).parents[1]/'deploy/scripts/fleet_config.py')
fleet=importlib.util.module_from_spec(spec);spec.loader.exec_module(fleet)


def test_generated_credentials_are_private_unique_and_per_car(tmp_path,capsys):
    for alias in ['car-02','car-03']:
        fleet.generate(SimpleNamespace(observer_id=alias,warehouse_id=str(uuid4()),name=alias,domain=2,
            jetson_host=alias+'-jetson.local',output=tmp_path/alias))
    one=json.loads((tmp_path/'car-02/cloud-entry.json').read_text());two=json.loads((tmp_path/'car-03/cloud-entry.json').read_text())
    assert one['devices'][0]['token']!=two['devices'][0]['token']
    assert one['nativeVideoSocket']!=two['nativeVideoSocket']
    assert one['devices'][0]['token'] not in capsys.readouterr().out
    for item in tmp_path.rglob('*'):
        if item.is_file():assert item.stat().st_mode&0o077==0
    assert json.loads((tmp_path/'car-02/robot.json.template').read_text())['machineId']==''


def test_generated_policy_keeps_ipc_archive_selection_and_bridge_preview(tmp_path,monkeypatch):
    from collector.archive_policy import parse_ros_sensors,camera_only_enabled,training_only_enabled,training_manifest_allowed
    from collector.archive_ros import ArchiveRosAdapter
    from collector.config import Config as CollectorConfig
    from tests.test_archive_ros import FakeNode,Store
    output=tmp_path/'enrollment'
    fleet.generate(SimpleNamespace(observer_id='car-02',warehouse_id=str(uuid4()),name='二号车',domain=2,
        jetson_host='car-02-jetson.local',output=output))
    values=literal_environment(output/'archive.env')
    collector=literal_environment(output/'collector.env')
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    for key,value in {**values,**collector}.items():monkeypatch.setenv(key,value)
    monkeypatch.delenv('ROBOT_PROFILE_FILE',raising=False)
    assert training_only_enabled() and not camera_only_enabled()
    sensors=parse_ros_sensors(values['ARCHIVE_ROS_SENSORS'],camera_only_enabled())
    adapter=ArchiveRosAdapter(Store(),lambda:True,sensors=sensors,image_fps=float(values['ARCHIVE_VIDEO_FPS']))
    node=FakeNode();adapter.bind(node,lambda name:name,lambda spec:{'latched':spec.latched})
    try:
        adapter.reconcile_gate()
        actual={item.topic for item in node.created}
        assert {'/livox/lidar','/livox/imu','/scan','/bluesea_new/scan','/Odometry','/tf'}.issubset(actual)
        assert not actual.intersection({'/berxel/color/image_raw','/berxel/depth/image_raw','/berxel/points',
            '/fisheye_front/image_raw','/diagnostics','/rosout','/chassis_host/telemetry','/coverage/events'})
    finally:assert adapter.close()
    assert CollectorConfig.from_env().berxel_transport=='bridge'
    assert not training_manifest_allowed({'sensor':'Berxel深度','files':['bag/raw.db3'],'metadata':{}})
    assert training_manifest_allowed({'sensor':'原相机前','files':['video.mkv'],'metadata':{'videoReady':True}})
    assert values['ARCHIVE_VIDEO_FPS']=='10'
    assert int(values['ARCHIVE_CAMERA_SEGMENT_BYTES'])==128*1024**2
    assert int(values['ARCHIVE_MIN_FREE_BYTES'])==10*1024**3
    assert int(values['ARCHIVE_MAX_SPOOL_BYTES'])==20*1024**3


@pytest.mark.parametrize('host',['','auto','AUTO','localhost','LOCALHOST','car.localhost','127.0.0.1',
    '127.1','999.1.1.1','0.0.0.0','224.0.0.1','host;echo nope','car..local','-jetson.local','[::1]','::1'])
def test_generator_requires_explicit_remote_jetson_before_creating_files(tmp_path,host):
    output=tmp_path/'enrollment'
    with pytest.raises(ValueError):
        fleet.generate(SimpleNamespace(observer_id='car-02',warehouse_id=str(uuid4()),name='二号车',domain=2,
            jetson_host=host,output=output))
    assert not output.exists()


def test_generated_jetson_identity_matches_video_and_archive_without_old_defaults(tmp_path):
    output=tmp_path/'enrollment'
    fleet.generate(SimpleNamespace(observer_id='car-02',warehouse_id=str(uuid4()),name='二号车',domain=2,
        jetson_host='10.42.0.22',output=output))
    profile=json.loads((output/'robot.json.template').read_text())
    archive=literal_environment(output/'archive.env')
    video=literal_environment(output/'video-tunnel.env.template')
    assert profile['jetsonHost']==archive['ARCHIVE_SOURCE_HOST']==video['VIDEO_SOURCE_HOST']=='10.42.0.22'
    assert archive['ARCHIVE_SOURCE_HOST_KEY_ALIAS']==video['VIDEO_SOURCE_HOST_KEY_ALIAS']=='10.42.0.22'
    assert archive['ARCHIVE_SOURCE_KEY']==archive['ARCHIVE_SOURCE_KNOWN_HOSTS']==''
    assert archive['ARCHIVE_CONTROL_DIR'].endswith('/robots/car-02/garage-control')
    assert archive['ARCHIVE_ROOT'].endswith('/robot-native-archive/car-02')


@pytest.mark.parametrize('cloud_ready', [True, False])
def test_generated_cloud_marker_prevents_local_warehouse_fallback(startup, monkeypatch, cloud_ready):
    module,fake,home=startup
    output=home/'enrollment-car-02'
    fleet.generate(SimpleNamespace(observer_id='car-02',warehouse_id=str(uuid4()),
        name='二号车',domain=2,jetson_host='car-02-jetson.local',output=output))
    marker=output/'warehouse-cloud.json'
    canonical=Path(__file__).parents[1]/'deploy/warehouse/warehouse-cloud.example.json'
    assert json.loads(marker.read_text())==json.loads(canonical.read_text())
    shutil.copy2(marker,home/'.config/robot-cloud/warehouse-cloud.json')
    health=[]
    monkeypatch.setattr(module,'http_ready',lambda url:health.append(url) or cloud_ready)
    if not cloud_ready:
        clock=itertools.count(0,2)
        monkeypatch.setattr(module.time,'monotonic',lambda:next(clock))
    assert module.main(['--all-devices','--ready-timeout','1'])==(0 if cloud_ready else 1)
    assert health==['http://127.0.0.1:13000/ready']
    assert 'robot-warehouse-cloud-api-tunnel.service' in fake.started_units()
    assert 'robot-cloud-dbprog-tunnel.service' not in fake.started_units()
    assert not any(command[0]=='docker' for command in fake.commands())


def test_cloud_merge_is_dry_run_by_default_and_rejects_existing_token_rebind(tmp_path):
    cfg=config(str(tmp_path/'data'));env=tmp_path/'cloud.env'
    values={'ROBOT_ID':cfg.robot_id,'DEVICE_CREDENTIALS_JSON':json.dumps(cfg.device_credentials),
            'VIEWER_USERNAME':cfg.viewer_username,'VIEWER_PASSWORD_HASH':cfg.viewer_password_hash,
            'SESSION_SECRET':cfg.session_secret,'PUBLIC_ORIGIN':'https://fixture.example.test'}
    env.write_text(''.join(k+'='+shlex.quote(v)+'\n' for k,v in values.items()));env.chmod(0o600)
    fleet.generate(SimpleNamespace(observer_id='car-02',warehouse_id=str(uuid4()),name='二号车',domain=2,
        jetson_host='car-02-jetson.local',output=tmp_path/'new'))
    before=env.read_bytes();args=SimpleNamespace(env_file=env,entry=tmp_path/'new/cloud-entry.json',apply=False)
    fleet.merge(args);assert env.read_bytes()==before
    args.apply=True;fleet.merge(args);after=env.read_bytes();assert after!=before
    fleet.merge(args);assert env.read_bytes()==after
    item=json.loads(args.entry.read_text());item['devices'][0]['token']='q'*48;args.entry.write_text(json.dumps(item))
    with pytest.raises(ValueError,match='credential replacement'):fleet.merge(args)
    assert env.read_bytes()==after
