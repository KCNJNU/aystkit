"""Current-deployment UI hides unsupported adapters, never temporarily idle data."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
FRONTEND=ROOT/'cloud/frontend'


def test_supported_pages_and_feedback_are_exposed_without_obsolete_adapters():
    html=(FRONTEND/'index.html').read_text()
    script=(FRONTEND/'app.js').read_text()
    for value in ('page-npu','data-page="npu"','npuUtil','npuFreq','npuTemp',
                  'commandVx','commandWz','指令值,实际值','目标 = 指令'):
        assert value not in html
    for value in ('renderNPU','statuses.NPU','"npu"','TargetRpm','cmdLinear','cmdAngular',
                  'Control Conflict','Control API','statusCard("部署模式"','节点发现 DDS'):
        assert value not in script
    assert '实际线速度' in html and '实际角速度' in html
    assert 'data-chart-series="actualVx"' in html and 'data-chart-series="actualWz"' in html
    assert 'data-chart-series="leftWheel,rightWheel"' in html
    assert 'ArUco 标记识别' in html and 'id="vision-docking-summary"' in html
    assert 'id="page-tests"' in html and 'id="page-logs"' in html
    assert 'id="header-jetson"' in html and 'cloud.online === true' in script


def test_renderers_keep_waiting_interfaces_and_real_feedback_without_commands_or_fake_probes():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for isolated rendering checks')
    code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[2],'utf8'),output={},recorded=[];
const context={window:{},Set,Map,Object,Array,Number,Math,String,Date,performance:{now:()=>1000},console};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const F=context.window.ObserverFormat;context.F=F;
context.state={lastMessageAt:1000,connected:true,nativeMetrics:null,events:[]};
context.disconnectedTooLong=()=>false;context.effectiveStatus=v=>F.normalizeStatus(v);
context.usable=v=>F.normalizeStatus(v)==='AVAILABLE';
context.setHTML=(id,value)=>output[id]=value;context.setText=(id,value)=>output[id]=value;
context.renderEvents=()=>{};context.renderArucoPreview=()=>{};
context.historyStore={record:(name,...rest)=>recorded.push([name,...rest])};
vm.runInContext(source.slice(source.indexOf('  const CAMERA_TRENDS'),source.indexOf('  const PAGE_NAMES')),context);
function load(start,end){vm.runInContext(source.slice(source.indexOf('  function '+start+'('),source.indexOf('  function '+end+'(')),context);}
vm.runInContext(source.slice(source.indexOf('  const RETIRED_SENSOR_KEYS'),source.indexOf('  const SUBPAGES')),context);
load('telemetryOf','disconnectedTooLong');
load('metricText','deriveSTM32Status');
load('deriveSTM32Status','recordEvent');
load('recordHistory','renderHeader');
load('focusMetric','renderOverview');
load('renderOverview','renderEvents');
load('actualFeedbackBlock','arucoPreviewCards');
load('renderRobot','renderSensors');
load('renderSensors','renderSTM32');
load('renderSTM32','renderROS');
load('renderROS','renderSnapshot');
const waiting=(label,topic)=>({label,topic,status:'WAITING_UPSTREAM',publisherCount:0,hz:null,ageS:null,value:null,updateMode:'stream'});
const sensors={
 lidar:waiting('MID360 雷达','/livox/points'),imu:waiting('雷达 IMU','/livox/imu'),
 odometry:waiting('FAST-LIO 里程计','/Odometry'),map:waiting('静态地图','/map'),
 bluesea:waiting('蓝海二维雷达','/bluesea_new/scan'),scan:waiting('二维雷达扫描','/scan'),
 color:waiting('深度相机彩色图','/berxel/color/image_raw'),depth:waiting('深度相机深度图','/berxel/depth/image_raw'),
 localization:waiting('RTAB-Map 定位','/localization_pose'),diagnostics:waiting('节点诊断','/diagnostics'),
 plan:waiting('导航路径','/plan'),localCostmap:waiting('局部代价地图','/local_costmap/costmap'),
 globalCostmap:waiting('全局代价地图','/global_costmap/costmap'),
 amcl:waiting('RETIRED_AMCL_SENTINEL','/amcl_pose'),
 previewDocking:waiting('RETIRED_PREVIEW_SENTINEL','/aruco_docking_preview/state'),
 fisheye:waiting('RETIRED_IMAGE_SENTINEL','/fisheye_front/image_raw'),
 oldBridge:waiting('RETIRED_BRIDGE_SENTINEL','/observer_camera/fisheye/compressed'),
 aliasRetired:waiting('RETIRED_ALIAS_SENTINEL','/amcl_pose')
};
const metadata=(value)=>({value,state:'AVAILABLE',source:'ACTUAL_TEST_FIXTURE',type:'ACTUAL'});
const robot={busVoltage:24,actualVxMps:.2,actualWzRadps:.3,leftWheelMps:.1,rightWheelMps:.2,
 cmdLinear:999,cmdAngular:999,
 frontRollerState:{online:true,faultCode:0,commLost:false,state:'UNSUPPORTED_STATE_SENTINEL',targetRpm:999},
 lift1State:{hallCount:42,direction:1,atMin:false,atMax:true},
 debug:{fields:{busVoltage:metadata(24),actualVxMps:metadata(.2),actualWzRadps:metadata(.3),
  leftWheelMps:metadata(.1),rightWheelMps:metadata(.2),frontRollerStateActualRpm:metadata(123),
  frontRollerStateTargetRpm:{...metadata(999),type:'COMMAND'},lift1State:metadata({hallCount:42})}},
 serialDebug:{validFrameCount:100,checksumErrorCount:2,invalidFrameCount:3,actualOpenDevice:'/dev/test-only',
  baud:115200,portOpen:true,rxActive:true,frameValid:true,validFramesPerSec:10}};
const snap={meta:{timestamp:1700000000},rk3588:{status:'AVAILABLE',network:[],thermalZones:[],
 npu:{status:'AVAILABLE',utilization:{value:77,status:'AVAILABLE'},modelName:'MISLEADING_MODEL_SENTINEL'}},
 cloud:{online:true,status:'AVAILABLE'},robot:{source:'ROS2 /chassis_host/telemetry',
 telemetry:{status:'AVAILABLE',hz:10,ageS:.1,publisherCount:1},data:robot},
 ros2:{status:'WAITING_UPSTREAM',domainId:0,rmwImplementation:'rmw_fastrtps_cpp',localhostOnly:'0',topics:{}},
 devices:{sensors,cameras:{
 usbFront:{status:'WAITING_UPSTREAM',label:'后视相机'},usbLeft:{status:'WAITING_UPSTREAM',label:'前视相机'},
 usbHub2:{status:'WAITING_UPSTREAM',label:'左视相机'},usbHub3:{status:'WAITING_UPSTREAM',label:'右视相机'},
 color:{status:'WAITING_UPSTREAM',label:'深度相机彩色图'},depth:{status:'WAITING_UPSTREAM',label:'深度相机深度图'},
 fisheye:{status:'WAITING_UPSTREAM',label:'RETIRED_FISHEYE_CAMERA_SENTINEL'},
 usbRear:{status:'WAITING_UPSTREAM',label:'RETIRED_REAR_CAMERA_SENTINEL'},
 usbPort21:{status:'WAITING_UPSTREAM',label:'RETIRED_PORT_CAMERA_SENTINEL'}},
 visionDocking:{status:'WAITING_UPSTREAM',targetStatus:'WAITING_UPSTREAM',topics:{},boardPose:null,dockingState:null}},
 pages:{}};
for(const item of Object.values(sensors))snap.ros2.topics[item.topic]=item;
snap.ros2.topics['/aruco_docking/state']=waiting('REAL_DOCKING_SENTINEL','/aruco_docking/state');
snap.ros2.topics['/fisheye_aruco/board_pose']=waiting('REAL_ARUCO_POSE_SENTINEL','/fisheye_aruco/board_pose');
snap.ros2.topics['/fisheye_aruco/detection_status']=waiting('REAL_ARUCO_STATUS_SENTINEL','/fisheye_aruco/detection_status');
const original=JSON.stringify(snap);context.state.snapshot=snap;
context.renderOverview(snap);context.renderRobot(snap);context.renderSensors(snap);
context.renderSTM32(snap);context.renderROS(snap);context.recordHistory(snap);
assert.equal(JSON.stringify(snap),original,'UI filtering must not delete protocol data');
assert.equal((output['overview-status'].match(/class="status-card /g)||[]).length,5);
assert.equal((output['overview-kpis'].match(/class="kpi-card"/g)||[]).length,5);
assert.equal((output['ros-kpis'].match(/class="status-card /g)||[]).length,4);
const all=Object.values(output).join(' ');
for(const forbidden of ['神经网络处理器','MISLEADING_MODEL_SENTINEL','UNSUPPORTED_STATE_SENTINEL',
 'RETIRED_AMCL_SENTINEL','RETIRED_PREVIEW_SENTINEL','RETIRED_ALIAS_SENTINEL',
 'RETIRED_IMAGE_SENTINEL','RETIRED_BRIDGE_SENTINEL','RETIRED_FISHEYE_CAMERA_SENTINEL',
 'RETIRED_REAR_CAMERA_SENTINEL','RETIRED_PORT_CAMERA_SENTINEL','控制冲突检查','未检查','部署模式','节点发现 DDS']){
 assert(!all.includes(forbidden),forbidden);
}
assert(!output['linear-comparison'].includes('999')&&!output['angular-comparison'].includes('999'));
assert(output['linear-comparison'].includes('0.2')&&output['angular-comparison'].includes('0.3'));
assert(output['brush-cards'].includes('123')&&!output['brush-cards'].includes('999'));
assert(output['brush-cards'].includes('在线状态')&&output['brush-cards'].includes('故障码')&&output['brush-cards'].includes('通信丢失'));
assert(!output['brush-cards'].includes('内部状态'));
assert(output['lift-cards'].includes('42')&&output['lift-cards'].includes('最大限位'));
assert(output['stm32-kpis'].includes('校验错误')&&output['stm32-kpis'].includes('无效帧'));
assert(output['serial-diagnostics'].includes('校验错误计数'));
for(const topic of ['/livox/points','/livox/imu','/Odometry','/map','/localization_pose','/diagnostics',
 '/berxel/color/image_raw','/berxel/depth/image_raw','/local_costmap/costmap','/global_costmap/costmap','/plan','/aruco_docking/state',
 '/fisheye_aruco/board_pose','/fisheye_aruco/detection_status']){
 assert(output['topic-health'].includes(topic),topic+' has an interface and must remain while waiting');
}
assert(!output['topic-health'].includes('/amcl_pose')&&!output['topic-health'].includes('/aruco_docking_preview/state'));
assert(!output['topic-health'].includes('/fisheye_front/image_raw')&&!output['topic-health'].includes('/observer_camera/fisheye/compressed'));
for(const label of ['前视相机','后视相机','左视相机','右视相机','深度相机彩色图','深度相机深度图']){
 assert(output['sensor-health'].includes(label),label+' remains even without samples');
}
assert(output['sensor-health'].includes('深度相机')&&output['sensor-health'].includes('定位'));
assert(output['vision-docking-summary'].includes('目标板位姿')&&output['vision-docking-summary'].includes('对接控制器'));
assert(recorded.some(row=>row[0]==='actualVx')&&recorded.some(row=>row[0]==='leftWheel'));
assert(!recorded.some(row=>row[0].startsWith('npu')||row[0].startsWith('command')));
console.log('supported interfaces waiting and actual measurements retained');
'''
    result=subprocess.run([node,'-e',code,str(FRONTEND/'formatters.js'),str(FRONTEND/'app.js')],
                          capture_output=True,text=True)
    assert result.returncode==0,result.stdout+'\n'+result.stderr


def test_old_npu_route_resolves_to_overview_and_known_routes_remain_fixed():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for routing check')
    code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('  const PAGE_NAMES'),end=source.indexOf('  const lastSubpages');
const context={Set,Object,Array};vm.createContext(context);vm.runInContext(source.slice(start,end),context);
context.lastSubpages={system:'performance',robot:'motion',media:'cameras'};
vm.runInContext(source.slice(source.indexOf('  function hasSubpages('),source.indexOf('  function mobileNavigation(')),context);
vm.runInContext(source.slice(source.indexOf('  function resolveRoute('),source.indexOf('  function renderNavigation(')),context);
for(const old of ['npu','npu/performance','npu/../tests']){
 assert.equal(context.resolveRoute(old).route,'overview');
}
for(const route of ['overview','system/environment','robot/motion','robot/actuators','robot/docking',
 'robot/sensors','system/stm32','system/ros','media/cameras','media/clouds','logs','tests']){
 assert.equal(context.resolveRoute(route).route,route);
}
for(const [legacy,canonical] of [['stm32','system/stm32'],['ros','system/ros']]){
 assert.equal(context.resolveRoute(legacy).route,canonical);
 assert.equal(context.resolveRoute(legacy).page,'system');
}
'''
    result=subprocess.run([node,'-e',code,str(FRONTEND/'app.js')],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
