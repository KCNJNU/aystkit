"""Selection guards use metadata only; no ROS, device or production access."""
import json
from pathlib import Path

import pytest

from collector.archive_policy import (CAMERA_ROS_SENSORS, CAMERA_SENSOR_NAMES,
    allowed_archive_manifest, allowed_manifest_path, camera_only_enabled, parse_ros_sensors)


def camera(sensor='Berxel彩色', topics=None):
    value={'sensor':sensor,'metadata':{}}
    if topics is None and sensor in CAMERA_ROS_SENSORS:topics={'/image':{'type':'sensor_msgs/msg/Image','count':1}}
    if topics is not None:value['metadata']['topics']=topics
    return value


def test_flag_is_explicit_and_invalid_values_fail_closed():
    assert camera_only_enabled({}) is False
    assert camera_only_enabled({'ARCHIVE_CAMERA_ONLY':'0'}) is False
    assert camera_only_enabled({'ARCHIVE_CAMERA_ONLY':'1'}) is True
    for value in ('', 'true', 'yes', ' 1 ', 'all'):
        with pytest.raises(ValueError):camera_only_enabled({'ARCHIVE_CAMERA_ONLY':value})


def test_default_scope_explicit_none_and_known_filtering_are_distinct():
    for raw in (None,'','  '):
        assert parse_ros_sensors(raw) is None
        assert parse_ros_sensors(raw,True)==CAMERA_ROS_SENSORS
    assert parse_ros_sensors('none')==parse_ros_sensors('none',True)==()
    assert parse_ros_sensors('Berxel彩色,雷达MID360,Berxel深度,鱼眼相机,Berxel点云',True)==CAMERA_ROS_SENSORS
    assert parse_ros_sensors('雷达MID360,定位与TF',True)==()
    assert parse_ros_sensors('Berxel彩色,Berxel彩色')==('Berxel彩色',)
    assert parse_ros_sensors('雷达MID360,定位与TF')==('雷达MID360','定位与TF')
    for raw in ('unknown','Berxel彩色,','none,Berxel彩色', 1):
        with pytest.raises(ValueError):parse_ros_sensors(raw,True)


def test_every_current_camera_and_explicit_legacy_label_is_allowed():
    for sensor in CAMERA_SENSOR_NAMES:assert allowed_archive_manifest(camera(sensor),True)
    info={'/berxel/depth/camera_info':{'type':'sensor_msgs/msg/CameraInfo','count':1}}
    assert allowed_archive_manifest(camera('Berxel深度',info),True)
    image={**info,'/berxel/depth/image_raw':{'type':'sensor_msgs/msg/Image','count':1}}
    assert allowed_archive_manifest(camera('Berxel深度',image),True)


def test_non_camera_and_mixed_or_malformed_topics_are_rejected_without_changing_input():
    for sensor in ('雷达MID360','二维雷达scan','Berxel点云','定位与TF','诊断日志','底盘遥测','覆盖任务','鱼眼相机','camera1'):
        assert not allowed_archive_manifest(camera(sensor),True)
        assert allowed_archive_manifest(camera(sensor),False)
    for topics in (None, {}, [], 'invalid', {'/bad':{'type':'sensor_msgs/msg/PointCloud2'}},
                   {'/image':{'type':'sensor_msgs/msg/Image'},'/bad':{'type':'sensor_msgs/msg/Imu'}},
                   {'/unknown':{}}, {'/unknown':{'type':[]}}, {'/image':'sensor_msgs/msg/Image'}):
        value=camera();value['metadata']['topics']=topics;before=json.dumps(value)
        assert not allowed_archive_manifest(value,True)
        assert json.dumps(value)==before
    for value in (None,[],{'sensor':[]},{'sensor':{}},{'sensor':'Berxel彩色','metadata':None}):
        assert not allowed_archive_manifest(value,True)
        assert allowed_archive_manifest(value,False)


def test_path_filter_is_bounded_regular_read_only_and_preserves_legacy_bypass(tmp_path):
    path=tmp_path/'archive.json';path.write_text(json.dumps(camera()))
    before=path.stat().st_mtime_ns;assert allowed_manifest_path(path,True)
    assert path.stat().st_mtime_ns==before
    link=tmp_path/'link.json';link.symlink_to(path)
    assert not allowed_manifest_path(link,True)
    assert not allowed_manifest_path(tmp_path/'missing.json',True)
    path.write_text('{invalid');assert not allowed_manifest_path(path,True)
    path.write_bytes(b' '*(256*1024+1));assert not allowed_manifest_path(path,True)
    fifo=tmp_path/'pipe'
    import os
    os.mkfifo(fifo)
    assert not allowed_manifest_path(fifo,True)
    assert allowed_manifest_path(tmp_path/'missing.json',False)



def test_ros_camera_without_topic_evidence_is_retained_but_not_selected():
    for sensor in CAMERA_ROS_SENSORS:
        manifest={'sensor':sensor,'metadata':{'source':'native-ros-cdr','videoRequired':True}}
        assert not allowed_archive_manifest(manifest,True)
        assert allowed_archive_manifest(manifest,False)
    for sensor in CAMERA_SENSOR_NAMES-set(CAMERA_ROS_SENSORS):
        assert allowed_archive_manifest({'sensor':sensor,'metadata':{'source':'native-camera-jpeg'}},True)
