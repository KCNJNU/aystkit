"""Display translations must retain identities and numeric/freshness semantics."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / "cloud" / "frontend"


def test_chinese_display_preserves_device_ids_protocol_fields_and_values():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for display formatter checks")
    script = r"""
      const fs=require('fs'),vm=require('vm');global.window={};
      vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
      const f=window.ObserverFormat;
      const raw={value:0,state:'AVAILABLE',type:'ACTUAL',source:'Page 0x10'};
      const field=f.field({debug:{fields:{speed:raw}}},'speed');
      const unknown='Prototype <img src=x onerror=alert(1)> Camera';
      console.log(JSON.stringify({
        device:f.deviceLabel('Jetson Orin NX'),
        imu:f.deviceLabel('雷达 IMU'),
        odometry:f.deviceLabel('FAST-LIO 里程计'),
        zone:f.deviceLabel('thermal_zone12'),
        wireless:f.networkLabel('wlp1s0'),
        unknown:f.deviceLabel(unknown),escaped:f.safe(f.deviceLabel(unknown)),
        absent:f.deviceLabel(null),zero:f.safe(f.reasonLabel(0)),
        yes:f.safe(f.reasonLabel(true)),no:f.safe(f.reasonLabel(false)),
        age:f.reasonLabel('Last valid telemetry older than 2s'),
        conflict:f.reasonLabel('Contract expects sensor_msgs/msg/Imu; graph reports std_msgs/msg/String'),
        unknownReason:f.reasonLabel('driver-specific error E17'),
        actual:f.uiLabel(raw.type),command:f.uiLabel('COMMAND'),
        source:f.sourceLabel('本机底盘广播 Page 0x10'),
        field,raw,unknownState:f.deviceStateLabel('docking','NEW_STATE_17'),
        waiting:f.displayStatus('WAITING_UPSTREAM'),stale:f.displayStatus('STALE')
      }));
    """
    output = subprocess.run([node, "-e", script, str(FRONTEND / "formatters.js")],
                            text=True, capture_output=True, check=True)
    value = json.loads(output.stdout)
    assert value["device"] == "Jetson Orin NX 边缘计算机"
    assert value["imu"] == "雷达惯性测量单元（IMU）"
    assert "激光惯性" in value["odometry"] and "FAST-LIO" in value["odometry"]
    assert value["zone"] == "温区 12"
    assert value["wireless"] == "无线网卡（wlp1s0）"
    assert value["unknown"] == "Prototype <img src=x onerror=alert(1)> Camera"
    assert "<img" not in value["escaped"] and "&lt;img" in value["escaped"]
    assert value["absent"] == "—"
    assert (value["zero"], value["yes"], value["no"]) == ("0", "是", "否")
    assert value["age"] == "有效遥测已超过 2 秒未更新"
    assert "sensor_msgs/msg/Imu" in value["conflict"]
    assert "std_msgs/msg/String" in value["conflict"]
    assert value["conflict"].startswith("预期消息类型：")
    assert value["unknownReason"] == "driver-specific error E17"
    assert value["actual"] == "实际反馈" and value["command"] == "控制指令"
    assert value["source"] == "本机底盘广播 数据页 0x10"
    assert value["raw"] == {"value": 0, "state": "AVAILABLE", "type": "ACTUAL", "source": "Page 0x10"}
    assert value["field"]["value"] == 0 and value["field"]["type"] == "ACTUAL"
    assert value["unknownState"] == "未知状态（NEW_STATE_17）"
    assert value["waiting"] == "等待数据" and value["stale"] == "数据过期"


def test_live_temperature_and_network_names_have_chinese_context_without_guessing():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for display formatter checks")
    script = r"""
      const fs=require('fs'),vm=require('vm');global.window={};
      vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
      const f=window.ObserverFormat;
      const temperatures=['acpitz','INT3400 Thermal','TCPU','TCPU_PCI','x86_pkg_temp','VendorProbe17'];
      const networks=['Meta','enp2s0','enp3s0','lo','wlxec1ac3074c64','future0'];
      const injection='<img src=x onerror=alert(1)>';
      console.log(JSON.stringify({
        thermal:temperatures.map(v=>[v,f.thermalLabel(v)]),
        network:networks.map(v=>[v,f.networkLabel(v)]),
        thermalNull:f.thermalLabel(null),networkNull:f.networkLabel(undefined),
        zone:f.thermalLabel('thermal_zone3'),
        escape:f.safe(f.thermalLabel(injection)),
        networkEscape:f.safe(f.networkLabel(injection)),
        rmw:f.detailValue('rmw_fastrtps_cpp','rmwImplementation'),
        keys:['usbFront','usbLeft','usbHub3','usbHub2','usbRear','usbPort21'].map(v=>f.deviceLabel(v)),
        topic:f.detailValue('/driver/raw_topic','topic'), frame:f.detailValue('sensor_frame','frameId'),
        zero:f.detailValue(0,'state'),no:f.detailValue(false,'state'),absent:f.detailValue(null,'state'),
        unknown:f.detailValue('NEW_MODE','state'),field:f.detailLabel('vendorMetric'),
        speed:f.unitLabel('Mbps'),rate:f.unitLabel('KB/s'),si:f.unitLabel('rad/s')
      }));
    """
    value = json.loads(subprocess.run([node, "-e", script, str(FRONTEND / "formatters.js")],
                                      text=True, capture_output=True, check=True).stdout)
    for original, translated in value["thermal"] + value["network"]:
        assert original in translated
        assert any("\u3400" <= letter <= "\u9fff" for letter in translated)
    thermal = dict(value["thermal"])
    assert thermal["INT3400 Thermal"] == "英特尔平台温控（INT3400 Thermal）"
    assert thermal["acpitz"] == "固件温区（acpitz）"
    assert thermal["VendorProbe17"] == "温区（VendorProbe17）"
    assert dict(value["network"])["Meta"] == "代理虚拟网卡（Meta）"
    assert dict(value["network"])["future0"] == "网络接口（future0）"
    assert value["thermalNull"] == value["networkNull"] == "—"
    assert value["zone"] == "温区 3"
    assert "<img" not in value["escape"] and "&lt;img" in value["escape"]
    assert "<img" not in value["networkEscape"] and "&lt;img" in value["networkEscape"]
    assert "通信中间件" in value["rmw"] and "rmw_fastrtps_cpp" in value["rmw"]
    assert value["keys"][:4] == ["前视相机", "后视相机", "左视相机", "右视相机"]
    assert all(name.startswith("旧") for name in value["keys"][4:])
    assert value["topic"] == "/driver/raw_topic" and value["frame"] == "sensor_frame"
    assert value["zero"] == 0 and value["no"] is False and value["absent"] is None
    assert value["unknown"] == "未识别状态（NEW_MODE）"
    assert value["field"] == "其他字段（vendorMetric）"
    assert (value["speed"], value["rate"], value["si"]) == ("兆比特/秒", "KiB/秒", "rad/s")
