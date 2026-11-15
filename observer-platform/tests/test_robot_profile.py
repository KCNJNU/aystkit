import json
from pathlib import Path
from uuid import uuid4
import pytest
from collector.robot_profile import validate_current_profile

@pytest.fixture
def vehicle(tmp_path):
    root=tmp_path/'.config/robot-cloud';root.mkdir(parents=True)
    rid=str(uuid4());profile={'schemaVersion':1,'observerId':'robot-002','warehouseId':rid,'rosDomainId':12,'machineId':'new-machine','name':'二号车'}
    target=root/'robot.json';target.write_text(json.dumps(profile));target.chmod(0o600)
    (root/'collector.env').write_text('ROBOT_ID=robot-002\nROS_DOMAIN_ID=12\n')
    (root/'archive.env').write_text('ARCHIVE_ROBOT_ID='+rid+'\n')
    return tmp_path,root,profile

def check(vehicle,**kw):
    home,_,_=vehicle
    return validate_current_profile(home=home,environ={},machine_id='new-machine',**kw)

def test_absent_profile_keeps_legacy_host_compatible(tmp_path):
    assert validate_current_profile(home=tmp_path,environ={}) is None

def test_valid_profile_and_explicit_vehicle_identity(vehicle):
    assert check(vehicle)['observerId']=='robot-002'

@pytest.mark.parametrize('fault',['machine','observer','archive','domain','permission','foreign-upload','jetson','socket'])
def test_copied_or_conflicting_profile_fails_before_devices_or_upload(vehicle,fault):
    _,root,p=vehicle
    if fault=='machine':p['machineId']='old-machine'
    elif fault=='observer':(root/'collector.env').write_text('ROBOT_ID=old-car\nROS_DOMAIN_ID=12\n')
    elif fault=='archive':(root/'archive.env').write_text('ARCHIVE_ROBOT_ID='+str(uuid4())+'\n')
    elif fault=='domain':(root/'collector.env').write_text('ROBOT_ID=robot-002\nROS_DOMAIN_ID=0\n')
    elif fault=='foreign-upload':
        upload=root/'archive-upload.json';upload.write_text(json.dumps({'robotId':str(uuid4())}));upload.chmod(0o600)
        (root/'archive.env').write_text('ARCHIVE_ROBOT_ID='+p['warehouseId']+'\nARCHIVE_UPLOAD_CONFIG='+str(upload)+'\n')
    elif fault=='jetson':p['jetsonHost']='new-jetson.local'
    elif fault=='socket':p['cloudSocketDir']='/var/lib/robot-video-tunnel/preview'
    target=root/'robot.json';target.write_text(json.dumps(p))
    if fault=='permission':target.chmod(0o644)
    with pytest.raises(ValueError):check(vehicle)
