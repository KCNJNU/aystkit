"""Vehicle directions must agree while physical and transport identities stay fixed."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from cloud.backend.native_video import sanitize_status
from collector.http_camera import CAMERA_SOURCE_PROFILES
from deploy.jetson.yolo_observer import NATIVE
from deploy.scripts import camera_owner as owner, camera_status_gateway as gate


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    'camera1': ('front', 'usbLeft', '2.2.4.2', '前视相机'),
    'camera2': ('rear', 'usbFront', '2.4', '后视相机'),
    'camera3': ('right', 'usbHub3', '2.2.1', '右视相机'),
    'camera4': ('left', 'usbHub2', '2.2.4.1', '左视相机'),
}


def test_corrected_directions_preserve_physical_capture_and_detection_bindings():
    profile = json.loads((ROOT / 'deploy/cameras/jetson-current.json').read_text())
    rows = []
    for row in profile['cameras']:
        identity = row['id']
        direction, key, port, label = EXPECTED[identity]
        assert row['direction'] == direction
        assert row['label'] + '视相机' == label
        assert row['device'].endswith('usb-0:' + port + ':1.0-video-index0')
        assert owner.CAMERA_IDENTITIES[identity][:2] == (key, direction)
        assert gate.IDS[identity] == direction
        assert NATIVE[key] == identity
        assert CAMERA_SOURCE_PROFILES['jetson-hub4'][key] == (identity, label)
        rows.append(dict(id=identity, direction=direction, codec='avc1.640028',
                         width=640, height=480, frames=20, encoded_frames=20))
    # Exercise both sanitizers together, rather than checking isolated constants.
    upstream = gate._cameras(json.dumps({'cameras': rows}).encode())
    result = sanitize_status({'cameras': upstream})['cameras']
    for identity, (direction, key, _, label) in EXPECTED.items():
        assert result[key]['id'] == identity
        assert result[key]['direction'] == direction
        assert result[key]['label'] == label
        assert result[key]['videoUrl'] == '/native-camera/' + key + '/video.mp4'
    # Stale direction metadata must not quietly attach a different stream.
    rows[0]['direction'] = 'rear'
    assert 'usbLeft' not in sanitize_status({'cameras': rows})['cameras']


def test_cards_fps_and_marker_labels_follow_the_same_physical_streams():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for isolated frontend mapping checks')
    program = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const root=process.argv[1], expected=JSON.parse(process.argv[2]);
const app=fs.readFileSync(root+'/app.js','utf8');
const media=fs.readFileSync(root+'/media.js','utf8');
const context={window:{},console,Number,Object,Array,Map,Set,Math,String};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root+'/formatters.js','utf8'),context);
context.F=context.window.ObserverFormat;
context.effectiveStatus=value=>value;
const cards=[];
context.statusCard=(label,state,detail)=>{cards.push({label,state,detail});return '';};
vm.runInContext(app.slice(app.indexOf('  const CAMERA_TRENDS'),app.indexOf('  const PAGE_NAMES'))+
                '\nglobalThis.trends=CAMERA_TRENDS;',context);
vm.runInContext(media.slice(media.indexOf('  const httpCameras'),media.indexOf('  const cameras'))+
                '\nglobalThis.cameraLabels=httpCameras;',context);
vm.runInContext(media.slice(media.indexOf('  function nativeLabel('),media.indexOf('  function verifyNativeUsage(')),context);
vm.runInContext(app.slice(app.indexOf('  function arucoPreviewCards('),app.indexOf('  function renderArucoPreview(')),context);
const markers={status:'AVAILABLE',dictionary:'DICT_5X5_100',ageMs:0,cameras:{}};
Object.entries(expected).forEach(([id,[direction,key,port,label]],index)=>{
  assert.equal(context.F.deviceLabel(key),label,id+' formatter');
  assert.equal(context.cameraLabels[key],label,id+' preview card');
  assert.equal(context.nativeLabel(key),label,id+' native video');
  assert.equal(context.trends[key],'camera'+direction[0].toUpperCase()+direction.slice(1)+'Fps',id+' FPS');
  markers.cameras[key]={status:'AVAILABLE',dictionary:'DICT_5X5_100',ageMs:0,ids:[index+10]};
});
context.arucoPreviewCards(markers);
Object.entries(expected).forEach(([id,[direction,key,port,label]],index)=>{
  const card=cards.find(value=>value.label===label);
  assert(card && card.state==='AVAILABLE' && card.detail==='ID '+(index+10),id+' marker data');
});
console.log('camera labels, native streams, FPS and markers use matching physical cameras');
'''
    result = subprocess.run([node, '-e', program, str(ROOT / 'cloud/frontend'),
                             json.dumps(EXPECTED, ensure_ascii=False)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
