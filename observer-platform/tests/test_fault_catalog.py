"""Evidence-based categories, readable meanings and matching private exports."""
import csv
import io
import json
import sqlite3
import time
import uuid

import pytest
from multidict import MultiDict

from cloud.backend.fault_catalog import CATEGORIES, CATEGORY_IDS, describe_fault
from cloud.backend.fault_logs import FaultLogs, parse_filters, CSV_HEADER, csv_row
from tests.test_fault_backend import fault, historical_row


@pytest.mark.parametrize('code,component,message,details,category,device', [
    ('IMU_TIMESTAMP_GAP','/livox/imu','sample gap',{},'imu','IMU'),
    ('LIDAR_TIMESTAMP_ROLLBACK','/livox/lidar','rollback',{},'lidar_3d','MID360'),
    ('LIDAR_POINT_TIME_WRAP','unknown','wrap',{},'lidar_3d','待确认'),
    ('DEVICE_DATA_INTERRUPTED','/bluesea_new/scan','stopped',{},'lidar_2d','蓝海'),
    ('DEVICE_DATA_INTERRUPTED','ldlidar_stl19p','stopped',{},'lidar_2d','STL-19P'),
    ('DEVICE_DATA_INTERRUPTED','/scan','stopped',{'topic':'/scan'},'lidar_2d','待确认'),
    ('CAMERA_PREVIEW_INTERRUPTED','usbRear','stopped',{},'camera','后视'),
    ('CAMERA_PREVIEW_INTERRUPTED','usbFront','stopped',{},'camera','后视'),
    ('CAMERA_PREVIEW_INTERRUPTED','usbLeft','stopped',{},'camera','前视'),
    ('CAMERA_PREVIEW_INTERRUPTED','usbRight','stopped',{},'camera','右视'),
    ('CAMERA_PREVIEW_INTERRUPTED','usbHub2','stopped',{},'camera','左视'),
    ('CAMERA_PREVIEW_INTERRUPTED','usbHub3','stopped',{},'camera','右视'),
    ('CAMERA_PREVIEW_INTERRUPTED','camera1','stopped',{},'camera','前视'),
    ('CAMERA_PREVIEW_INTERRUPTED','camera2','stopped',{},'camera','后视'),
    ('CAMERA_PREVIEW_INTERRUPTED','camera3','stopped',{},'camera','右视'),
    ('CAMERA_PREVIEW_INTERRUPTED','camera4','stopped',{},'camera','左视'),
    ('CAMERA_PREVIEW_INTERRUPTED','fisheye','stopped',{},'camera','鱼眼'),
    ('CAMERA_PREVIEW_INTERRUPTED','color','stopped',{'topic':'/observer_camera/color/compressed'},'depth_camera','P100R'),
    ('ROS_DIAGNOSTIC_ERROR','diagnostics','bad clock',{'hardware_id':'berxel-p100r'},'depth_camera','P100R'),
    ('ROS_ERROR','depth_camera_driver','startup failed',{},'depth_camera','待确认'),
    ('ROS_ERROR','camera10','failed',{},'camera','待确认'),
    ('SENSOR_TIME_SYNC_REPORTED','fastlio','IMU and LiDAR not Synced',{},'lidar_imu','同步'),
    ('SENSOR_TIME_ROLLBACK_REPORTED','fastlio','lidar loop back, clear buffer',{},'lidar_3d','激光'),
    ('ROS_ERROR','fastlio','IMU and LiDAR not Synced',{},'lidar_imu','FAST-LIO'),
    ('ROS_ERROR','planner_server','Failed to transform lidar input from /livox/lidar',{},'navigation','导航'),
    ('NAVIGATION_TIMEOUT','lidar_proxy','goal timed out',{},'navigation','导航'),
    ('DEVICE_DATA_INTERRUPTED','/Odometry','stopped',{},'lidar_imu','FAST-LIO'),
    ('ROS_ERROR','chassis_host','serial input failed',{},'chassis','底盘'),
    ('HOST_TEMPERATURE_HIGH','cpu','hot',{},'host','工控机'),
    ('COLLECTOR_CONNECTION_LOST','cloud/collector','lost',{},'network','采集链路'),
    ('TEST_METRICS_OVERFLOW','collector','queue full',{},'system','里程'),
    ('FAULT_LOG_OVERFLOW','collector','queue full',{},'system','故障日志'),
    ('SENSOR_TIME_ROLLBACK_REPORTED','unknown','timestamp rollback in simulation',{},'system','其他'),
    ('UNKNOWN_FAULT','unknown','critical problem',{'context':{'network':[{'name':'eth0'}],'temperatures':[{'name':'cpu'}]}},'system','其他'),
])
def test_direct_evidence_categories_without_context_or_model_guessing(code,component,message,details,category,device):
    result=describe_fault(code,component,message,details)
    assert result['category']==category
    assert device in result['device_label']
    assert result['category_label']==dict(CATEGORIES)[category]
    assert all(isinstance(result[key],str) and result[key] for key in
               ('title','meaning','impact','suggestion'))


@pytest.mark.parametrize('code,word', [
    ('IMU_TIMESTAMP_ROLLBACK','回退'),('LIDAR_TIMESTAMP_GAP','断档'),
    ('LIDAR_POINT_TIME_WRAP','回绕'),('LIDAR_POINT_TIME_NONMONOTONIC','倒序'),
    ('SENSOR_TIME_SYNC_REPORTED','对齐'),('SENSOR_TIME_GAP_REPORTED','断档'),
    ('POINT_TIME_WRAP_REPORTED','回绕'),('DEVICE_DATA_INTERRUPTED','中断'),
    ('CAMERA_PREVIEW_INTERRUPTED','中断'),('NAVIGATION_TIMEOUT','超时'),
    ('NAVIGATION_ABORTED','失败'),('NAVIGATION_FAILURE','受阻'),
    ('LOCALIZATION_WARNING','定位'),('CONTROLLER_OSCILLATION','转向'),
    ('OBSTACLE_STOP_REPORTED','停车'),('ROS_DIAGNOSTIC_STALE','过期'),
    ('ROS_DIAGNOSTIC_ERROR','诊断'),('HOST_TEMPERATURE_HIGH','温度'),
    ('COLLECTOR_CONNECTION_LOST','心跳'),('FAULT_MONITOR_LIMITED','监测'),
    ('FAULT_LOG_OVERFLOW','队列'),('TEST_METRICS_OVERFLOW','队列'),
])
def test_known_failure_types_have_specific_readable_titles(code,word):
    result=describe_fault(code,'source','original report')
    assert word in result['title']
    assert result['title']!='来源节点报告错误'


@pytest.mark.parametrize('message,title', [
    ('Failed to create plan with tolerance 0.5','有效路径规划失败'),
    ('No valid trajectories out of 126','局部轨迹不可用'),
    ("Invalid frame ID 'map' passed to lookupTransform: frame does not exist",'坐标变换不可用'),
    ('Lookup would require extrapolation into the future from frame odom','坐标变换请求时间超前'),
    ('Lookup would require extrapolation into the past from frame odom','坐标变换请求时间过旧'),
    ('Cannot transform input plan from frame map','坐标变换不可用'),
    ('Failed to make progress','导航进展不足'),
    ('Goal is outside current map','规划位置超出地图'),
    ('Connection refused by remote endpoint','数据连接失败'),
    ('Device or resource busy','采集设备被占用'),
    ('std::bad_alloc','内存分配失败'),
])
def test_generic_ros_errors_use_meaningful_patterns(message,title):
    result=describe_fault('ROS_ERROR','planner_server',message)
    assert result['category']=='navigation'
    assert result['title']==title


def test_interpretations_preserve_unknown_causes_and_distinct_overflow_semantics():
    preview=describe_fault('CAMERA_PREVIEW_INTERRUPTED','usbRear','source stopped')
    assert '不等于' in preview['impact']
    obstacle=describe_fault('OBSTACLE_STOP_REPORTED','controller_server','obstacle stop')
    assert '不能自动判定' in obstacle['impact']
    temp=describe_fault('HOST_TEMPERATURE_HIGH','cpu','85C')
    assert '不确认' in temp['impact']
    logs=describe_fault('FAULT_LOG_OVERFLOW','collector','queue full')
    metrics=describe_fault('TEST_METRICS_OVERFLOW','collector','queue full')
    assert '较早记录已被移除' in logs['meaning']
    assert '新里程样本未能保存' in metrics['meaning']
    assert '能力暂时受限' in describe_fault('FAULT_MONITOR_LIMITED','livox_metadata','no type')['meaning']
    for result in (preview,obstacle,temp,logs,metrics):
        assert '关闭避障' not in result['suggestion'] and '放宽容差' not in result['suggestion']
    unknown=describe_fault('UNKNOWN_CODE','unknown','unmatched message')
    assert unknown['category']=='system' and '没有匹配到' in unknown['meaning']


def test_legacy_summaries_and_bounded_invalid_details_are_safe():
    original=describe_fault('IMU_TIMESTAMP_GAP','/livox/imu','gap',{})
    legacy=describe_fault('REPEATED_FAULT_SUMMARY','/livox/imu','summary',
        json.dumps({'original_code':'IMU_TIMESTAMP_GAP','original_message':'gap','suppressed_count':5}))
    assert original==legacy
    for value in ([],None,'{','x'*20000,{'topic':[],'hardware_id':{}}):
        assert describe_fault('ROS_ERROR','unknown','bad',value)['category']=='system'


def test_category_query_and_chinese_search_match_pagination_and_full_readonly_export(tmp_path):
    path=tmp_path/'faults.sqlite3';db=sqlite3.connect(path);now=time.time();logs=FaultLogs(db,clock=lambda:now)
    events=[
        fault('imu source gap',code='IMU_TIMESTAMP_GAP',level='WARN',component='/livox/imu'),
        fault('preview lost',code='CAMERA_PREVIEW_INTERRUPTED',level='WARN',component='usbRear'),
        fault('preview lost',code='CAMERA_PREVIEW_INTERRUPTED',level='WARN',component='color'),
        fault('Failed to create plan',code='ROS_ERROR',component='planner_server'),
        fault('lidar loop back',code='SENSOR_TIME_ROLLBACK_REPORTED',component='fastlio'),
    ]
    with db:logs.add_batch('robot','ipc',events)
    physical_before=db.execute('SELECT rowid,* FROM fault_logs ORDER BY rowid').fetchall()
    camera=logs.query({'category':'camera','limit':1})
    assert camera['total']==1 and camera['items'][0]['device_label']=='后视相机'
    for query,category in [('惯性','imu'),('后视','camera'),('P100R','depth_camera'),('有效路径','navigation')]:
        result=logs.query({'q':query})
        assert result['total']==1 and result['items'][0]['category']==category
    assert logs.query({'category':'camera','q':'P100R'})['total']==0
    assert logs.query({'category':'camera','offset':1})['items']==[]
    summary=logs.summary()
    assert len(summary['categories'])==len(CATEGORIES)==11
    assert sum(c['total'] for c in summary['categories'])==summary['total']==5
    assert sum(c['error'] for c in summary['categories'])==summary['error']
    assert sum(c['warning'] for c in summary['categories'])==summary['warning']
    assert next(c for c in summary['categories'] if c['id']=='host')['total']==0
    reader=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    try:
        reader.execute('BEGIN')
        cursor=logs.select({'category':'depth_camera'},export=True,connection=reader)
        items=[logs._item(row) for row in cursor]
        assert len(items)==1 and items[0]['category']=='depth_camera'
        assert reader.total_changes==0
        exported=list(csv.reader(io.StringIO(csv_row(items[0]).decode())))
        assert len(CSV_HEADER)==len(exported[0])==19
        assert exported[0][6]==items[0]['code'] and exported[0][8]=='preview lost'
        assert exported[0][13:]==[items[0][k] for k in
            ('category_label','device_label','title','meaning','impact','suggestion')]
        cursor.close()
    finally:reader.close()
    assert db.execute('SELECT rowid,* FROM fault_logs ORDER BY rowid').fetchall()==physical_before
    db.close()


def test_legacy_group_category_remains_one_incident_without_changing_original_rows(tmp_path):
    db=sqlite3.connect(tmp_path/'faults.sqlite3');now=time.time();logs=FaultLogs(db,clock=lambda:now)
    first=fault('first gap',code='IMU_TIMESTAMP_GAP',level='WARN',component='/livox/imu')
    second=fault('summary',code='REPEATED_FAULT_SUMMARY',level='WARN',component='/livox/imu',
                 details={'original_code':'IMU_TIMESTAMP_GAP','original_message':'later gap','suppressed_count':7})
    with db:
        historical_row(db,first,received_at=now)
        historical_row(db,second,received_at=now+1)
    before=db.execute('SELECT rowid,* FROM fault_logs ORDER BY rowid').fetchall()
    result=logs.query({'category':'imu','q':'采样时间出现断档'})
    assert result['total']==1 and result['items'][0]['count']==8
    assert result['items'][0]['category']=='imu' and result['items'][0]['title']=='采样时间出现断档'
    assert db.execute('SELECT rowid,* FROM fault_logs ORDER BY rowid').fetchall()==before
    db.close()


def test_category_filter_validation_and_literal_search(tmp_path):
    assert parse_filters(MultiDict(category='imu'))['category']=='imu'
    assert parse_filters(MultiDict(category=''))=={'limit':50,'offset':0}
    for query in (MultiDict(category='unknown'),MultiDict(category='IMU'),MultiDict([('category','imu'),('category','camera')])):
        with pytest.raises(ValueError):parse_filters(query)
    db=sqlite3.connect(tmp_path/'faults.sqlite3');logs=FaultLogs(db)
    with db:logs.add_batch('robot','ipc',[fault('literal 100%_ marker',code='ROS_ERROR')])
    assert logs.query({'q':'%_'})['total']==1
    assert logs.query({'q':"' OR 1=1 --"})['total']==0
    assert CATEGORY_IDS==frozenset(key for key,_ in CATEGORIES)
    db.close()


def test_transform_extrapolation_meaning_distinguishes_future_and_past_without_cause_guessing():
    future=describe_fault('ROS_ERROR','planner_server','Lookup would require extrapolation into the future')
    past=describe_fault('ROS_ERROR','controller_server','Lookup would require extrapolation into the past')
    assert future['category']==past['category']=='navigation'
    assert '晚于' in future['meaning'] and '最新' in future['meaning']
    assert '早于' in past['meaning'] and '最早' in past['meaning']
    assert '不能单独确认' in future['impact']
    assert future['title']!=past['title']
