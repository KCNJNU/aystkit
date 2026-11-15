#!/usr/bin/env python3
"""Start or stop the existing sensor sources, without chassis control.

The normal path is intended for the operator's one-click launcher. Status is
read-only; dry-run does not execute commands, import hardware helpers or write.
Process/service checks do not constitute verification of received sensor data.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import importlib.util
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
from concurrent.futures import ThreadPoolExecutor
import stat
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True


class SourceError(RuntimeError):
    pass


class RecordingStartCancelled(BaseException):
    """Cancellation stops subsequent starts without touching running devices."""


@dataclass(frozen=True)
class Source:
    key: str
    driver: str
    packages: tuple
    command: tuple
    topic: str

    @property
    def unit(self):
        return 'robot-device-' + self.key + '.service'


SOURCES = {
    'mid360': Source('mid360', 'livox_ros_driver2_node',
        (('livox_ros_driver2', 'livox_ros_driver2_node'),
         ('fast_lio', 'fastlio_mapping'), ('mid360_nav2', 'navigation_odometry'),
         ('mid360_nav2', 'custommsg_to_pointcloud2')),
        ('/bin/bash', '{workspace}/run_fastlio.sh'), '/livox/lidar'),
    'stl19p': Source('stl19p', 'ldlidar_stl_ros2_node',
        (('ldlidar_stl_ros2', 'ldlidar_stl_ros2_node'),),
        ('/bin/bash', '{workspace}/run_ldrobot_stl19p.sh'), '/scan'),
    'bluesea': Source('bluesea', 'bluesea2_node', (('bluesea2', 'bluesea2_node'),),
        ('ros2', 'launch', 'bluesea2', 'lds_50c_e_7.launch.py',
         'params_file:={bluesea_params}'), '/bluesea_new/scan'),
    'berxel': Source('berxel', 'berxel_camera_ros2',
        (('berxel_camera_ros2', 'berxel_camera_ros2'),),
        ('ros2', 'launch', 'berxel_camera_ros2', 'berxel_p100r.launch.py',
         'use_rviz:=false'), '/berxel/depth/image_raw'),
}
JETSON_SUPPORT = ('robot-native-archive.service',
    'robot-cloud-camera-status-gateway.service', 'robot-cloud-camera-relay.service')
CAMERA_SUPPORT = JETSON_SUPPORT[1:]
YOLO_UNIT = 'robot-cloud-yolo-observer.service'
BERXEL_PREVIEW_UNIT = 'robot-berxel-preview-encoder.service'
SHARED_CAMERA_UNIT = 'robot-cloud-shared-camera-preview.service'
CAMERA_OWNERSHIP_UNITS = frozenset((BERXEL_PREVIEW_UNIT, YOLO_UNIT, SHARED_CAMERA_UNIT,
    SOURCES['berxel'].unit, 'robot-device-fisheye.service'))
SHARED_CAMERA_SOURCE_UNITS = frozenset((SHARED_CAMERA_UNIT, SOURCES['berxel'].unit,
    'robot-device-fisheye.service'))
CAMERA_OWNERSHIP_MAX_BYTES = 16 * 1024
CAMERA_STOP_DEPENDENCIES = ('RequiredBy', 'RequisiteOf', 'BoundBy', 'ConsistsOf',
    'PropagatesStopTo', 'Requires', 'BindsTo', 'PartOf')
CAMERA_OWNERSHIP_KEYS = {
    BERXEL_PREVIEW_UNIT: 'berxel-preview', YOLO_UNIT: YOLO_UNIT.removesuffix('.service'),
    SHARED_CAMERA_UNIT: 'usb-cameras', SOURCES['berxel'].unit: 'berxel',
    'robot-device-fisheye.service': 'fisheye',
}
BERXEL_PREVIEW_ENV = {
    'BERXEL_PREVIEW_MAX_EDGE': '480', 'BERXEL_PREVIEW_JPEG_QUALITY': '45',
    'BERXEL_PREVIEW_IMAGE_FPS': '5', 'BERXEL_PREVIEW_CLOUD_POINTS': '900',
    'BERXEL_PREVIEW_CLOUD_FPS': '2',
}
JETSON_ARCHIVE_ROS_SENSORS = 'Berxel彩色,Berxel深度,Berxel点云,鱼眼相机'
JETSON_ARCHIVE_POLICY = {
    'ARCHIVE_CAMERA_ONLY': '0',
    'ARCHIVE_ROS_SENSORS': JETSON_ARCHIVE_ROS_SENSORS,
}
ROS_SHELL = '''set -e
set +u
source "$1"
source "$2"
source "$3"
shift 3
unset ROS_DISCOVERY_SERVER CYCLONEDDS_URI FASTRTPS_DEFAULT_PROFILES_FILE
export ROS_DOMAIN_ID=0 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export MID360_NAV2_DDS_SERVER=off LDROBOT_STL19P_TOPIC=scan
exec "$@"
'''

RECORDING_ROS_SHELL = '''set -e
set +u
declare -A recording_dds
recording_unset=()
for recording_key in ROS_DOMAIN_ID ROS_LOCALHOST_ONLY RMW_IMPLEMENTATION ROS_DISCOVERY_SERVER CYCLONEDDS_URI FASTRTPS_DEFAULT_PROFILES_FILE FASTDDS_DEFAULT_PROFILES_FILE RMW_FASTRTPS_USE_QOS_FROM_XML RMW_FASTRTPS_PUBLICATION_MODE MID360_NAV2_DDS_SERVER; do
    if [[ -v "$recording_key" ]]; then
        recording_dds[$recording_key]="${!recording_key}"
    else
        recording_unset+=("$recording_key")
    fi
done
source "$1"
source "$2"
source "$3"
shift 3
for recording_key in "${!recording_dds[@]}"; do
    printf -v "$recording_key" '%s' "${recording_dds[$recording_key]}"
    export "$recording_key"
done
for recording_key in "${recording_unset[@]}"; do unset "$recording_key"; done
unset recording_dds recording_key recording_unset
exec "$@"
'''

LIDAR_ROS_SHELL = '''set -e
set +u
source "$1"
source "$2"
source "$3"
shift 3
exec "$@"
'''


def systemd_shell_argv(command):
    """Keep shell variables literal until bash receives the ExecStart argv."""
    command = list(command)
    shell = command.index('/bin/bash')
    if command[shell + 1] != '-c':
        raise SourceError('受管设备启动命令缺少明确的 shell 脚本')
    # systemd 249 expands $NAME in ExecStart before invoking bash. $$ is its
    # documented literal dollar escape; direct preflight commands stay intact.
    command[shell + 2] = command[shell + 2].replace('$', '$$')
    return command


def camera_ownership_payload(value):
    """Validate the entire non-secret lease before any camera action is possible."""
    if isinstance(value, str):
        if len(value.encode('utf-8')) > CAMERA_OWNERSHIP_MAX_BYTES:
            raise SourceError('相机会话归属记录过大，未关闭设备')
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise SourceError('相机归属记录存在重复字段，未关闭设备')
                result[key] = item
            return result
        try:
            value = json.loads(value, object_pairs_hook=unique)
        except (ValueError, RecursionError) as error:
            raise SourceError('相机会话归属记录无效，未关闭设备') from error
    if (not isinstance(value, dict) or type(value.get('schemaVersion')) is not int
            or value['schemaVersion'] != 1 or set(value) - {'schemaVersion', 'units', 'sessionToken'}
            or not isinstance(value.get('units'), list) or len(value['units']) > len(CAMERA_OWNERSHIP_UNITS)):
        raise SourceError('相机会话归属记录无效，未关闭设备')
    if 'sessionToken' in value and (not isinstance(value['sessionToken'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', value['sessionToken'])):
        raise SourceError('相机采集会话身份无效，未关闭设备')
    units, seen = [], set()
    for row in value['units']:
        if (not isinstance(row, dict) or set(row) != {'unit', 'invocationId', 'mainPid',
                'execMainStartTimestampMonotonic'} or not isinstance(row.get('unit'), str)
                or row['unit'] not in CAMERA_OWNERSHIP_UNITS or row['unit'] in seen
                or not isinstance(row.get('invocationId'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', row['invocationId'])
                or int(row['invocationId'], 16) == 0 or type(row.get('mainPid')) is not int
                or not 1 < row['mainPid'] <= 2**31-1
                or not isinstance(row.get('execMainStartTimestampMonotonic'), str)
                or not re.fullmatch(r'[1-9][0-9]{0,19}', row['execMainStartTimestampMonotonic'])):
            raise SourceError('相机服务归属记录无效，未关闭设备')
        seen.add(row['unit'])
        units.append(dict(row))
    return dict(schemaVersion=1, units=units,
                **({'sessionToken': value['sessionToken']} if 'sessionToken' in value else {}))


RECORDING_MID360 = Source('mid360', 'livox_ros_driver2_node',
    (('livox_ros_driver2', 'livox_ros_driver2_node'),),
    ('ros2', 'launch', 'livox_ros_driver2', 'msg_MID360s_launch.py'), '/livox/lidar')
DDS_ENV_KEYS = frozenset(('ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY', 'RMW_IMPLEMENTATION',
    'ROS_DISCOVERY_SERVER', 'CYCLONEDDS_URI', 'FASTRTPS_DEFAULT_PROFILES_FILE',
    'FASTDDS_DEFAULT_PROFILES_FILE', 'RMW_FASTRTPS_USE_QOS_FROM_XML',
    'RMW_FASTRTPS_PUBLICATION_MODE', 'MID360_NAV2_DDS_SERVER','ROS_NAMESPACE'))
FISHEYE_DEVICE = '/dev/v4l/by-id/usb-ZC-20_HDR_Camera_ZCA062260604311-video-index0'
RECORDING_FISHEYE = Source('fisheye', 'usb_cam_node_exe', (('usb_cam', 'usb_cam_node_exe'),),
    ('ros2', 'launch', 'fisheye_camera', 'fisheye_camera.launch.py',
     'video_device:=' + FISHEYE_DEVICE), '/fisheye_front/image_raw')


def exact_processes(proc_root, executable):
    """Match argv[0], never a command substring or an unrelated launch shell."""
    found = []
    for process in Path(proc_root).iterdir():
        if not process.name.isdecimal():
            continue
        try:
            raw = (process / 'cmdline').read_bytes()
            first = raw.split(b'\0', 1)[0].decode(errors='replace')
            if Path(first).name == executable:
                found.append(int(process.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            # An unreadable process might own hardware; fail closed.
            raise SourceError('无法完整检查已有驱动进程') from exc
    return sorted(found)


def exact_python_script_processes(proc_root, script):
    """Match the Python script operand, never arbitrary command text or a basename."""
    found = []
    for process in Path(proc_root).iterdir():
        if not process.name.isdecimal():
            continue
        try:
            args = (process / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            if not re.fullmatch(r'python(?:3(?:\.[0-9]+)?)?', Path(args[0]).name):
                continue
            operands = args[1:]
            while operands and operands[0] in ('-B', '-u', '-s', '-E', '-I', '-S', '-q'):
                operands = operands[1:]
            if operands:
                operand = Path(operands[0])
                if not operand.is_absolute() and operand.name == Path(script).name:
                    operand = (process / 'cwd').resolve(strict=True) / operand
                    operand = operand.resolve(strict=True)
                if str(operand) == str(script):
                    found.append(int(process.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise SourceError('无法完整检查已有相机辅助程序') from exc
    return sorted(found)


def bluesea_parameters(raw):
    """Change the one known scan parameter, preserving all vendor settings."""
    if len(raw.encode()) > 131072:
        raise SourceError('蓝海参数文件过大')
    patched, count = re.subn(r'(?m)^(\s+scan_topic\s*:\s*)[^\r\n]+$',
                            r'\1"/bluesea_new/scan"', raw)
    if count != 1:
        raise SourceError('无法唯一确认蓝海 scan_topic 参数')
    return patched


def existing_bluesea_topic(proc_root, pid):
    """Read only explicit ROS parameter files of the exact running driver."""
    arguments = (Path(proc_root) / str(pid) / 'cmdline').read_bytes().decode().split('\0')
    values = []
    for index, argument in enumerate(arguments[:-1]):
        if argument != '--params-file':
            continue
        path = Path(arguments[index + 1])
        if not path.is_absolute() or path.stat().st_size > 131072:
            raise SourceError('无法确认已运行蓝海雷达的话题配置')
        values += re.findall(r'(?m)^\s+scan_topic\s*:\s*[\'"]?(/[^\s\'"#]+)[\'"]?\s*$', path.read_text())
    return values[-1] if values else None


def status_timestamp(value):
    updated = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if updated.tzinfo is None or updated.utcoffset() is None:
        raise ValueError('status timestamp requires timezone')
    return updated.timestamp()


def process_start_ticks(proc_root, pid):
    raw = (Path(proc_root)/str(pid)/'stat').read_text()
    return raw[raw.rfind(')')+2:].split()[19]


def bounded_config(path, maximum=2*1024*1024):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise SourceError('驱动参数必须是可核验的本地普通文件')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0,os.getuid())
                or info.st_size > maximum):
            raise SourceError('驱动参数文件身份或大小无效')
        with os.fdopen(descriptor,'rb',closefd=False) as stream:
            value=stream.read(maximum+1)
        if len(value)>maximum:
            raise SourceError('驱动参数文件过大')
        return value
    finally:
        os.close(descriptor)


def dds_metadata(environment, namespace=''):
    """Only graph settings leave this helper, never the process environment."""
    try:
        domain=int(environment.get('ROS_DOMAIN_ID') or '0')
        if not 0<=domain<=232:
            raise ValueError()
    except (ValueError,TypeError):
        raise SourceError('已有驱动 ROS_DOMAIN_ID 无效') from None
    local=(environment.get('ROS_LOCALHOST_ONLY') or '0').lower()
    if local not in ('0','1','false','true'):
        raise SourceError('已有驱动 ROS_LOCALHOST_ONLY 无法确认')
    rmw=environment.get('RMW_IMPLEMENTATION') or 'rmw_fastrtps_cpp'
    if not re.fullmatch(r'rmw_[A-Za-z0-9_]+',rmw):
        raise SourceError('已有驱动 RMW 实现无法确认')
    profile_key=next((key for key in ('FASTDDS_DEFAULT_PROFILES_FILE','FASTRTPS_DEFAULT_PROFILES_FILE')
                      if environment.get(key)),None)
    profile=environment.get(profile_key,'') if profile_key else ''
    profile_sha=hashlib.sha256(bounded_config(profile)).hexdigest() if profile else None
    cyclone=environment.get('CYCLONEDDS_URI') or ''
    if cyclone:
        if '<' in cyclone or '>' in cyclone:
            raise SourceError('已有驱动使用内联 Cyclone DDS 配置，无法安全自动对齐')
        bounded_config(cyclone[7:] if cyclone.startswith('file://') else cyclone)
    discovery=environment.get('ROS_DISCOVERY_SERVER') or ''
    if len(discovery)>1024 or any(ord(char)<32 for char in discovery):
        raise SourceError('已有驱动发现服务器配置无效')
    return dict(domain=str(domain),rmw=rmw,discoveryServer=discovery,
        localhostOnly=local in ('1','true'),namespace=namespace,
        profilePath=profile,profileSha256=profile_sha,profileEnvironmentKey=profile_key,
        cycloneUri=cyclone,qosFromXml=environment.get('RMW_FASTRTPS_USE_QOS_FROM_XML') or '',
        publicationMode=environment.get('RMW_FASTRTPS_PUBLICATION_MODE') or '')


def running_driver_runtime(proc_root,pid):
    """Read bounded explicit ROS parameters; missing orphan config fails closed."""
    import yaml
    root=Path(proc_root)/str(pid)
    if root.stat().st_uid!=os.getuid():
        raise SourceError('已有雷达驱动不属于当前用户，保持原状')
    started=process_start_ticks(proc_root,pid)
    raw=(root/'cmdline').read_bytes()
    environment_raw=(root/'environ').read_bytes()
    if len(raw)>131072 or len(environment_raw)>1024*1024:
        raise SourceError('已有雷达进程运行信息过大')
    args=[item.decode() for item in raw.split(b'\0') if item]
    explicit=[args[index+1] for index,argument in enumerate(args[:-1]) if argument in ('-r','--remap')]
    node_names=[item.partition(':=')[2] for item in explicit if item.startswith(('__node:=','__name:='))]
    default_names={'livox_ros_driver2_node':'livox_lidar_publisher','bluesea2_node':'bluesea_node',
                   'ldlidar_stl_ros2_node':'stl19p_lidar'}
    node_name=node_names[-1] if node_names else default_names.get(Path(args[0]).name,'')
    namespaces=[item.partition(':=')[2] for item in explicit if item.startswith('__ns:=')]
    namespace=(namespaces[-1] if namespaces else '').strip('/')
    full_name='/'+('/'.join(item for item in (namespace,node_name) if item))
    parameters,remaps={},[]
    try:
        for index,argument in enumerate(args[:-1]):
            value=args[index+1]
            if argument=='--params-file':
                document=yaml.safe_load(bounded_config(value,131072))
                if not isinstance(document,dict):
                    raise SourceError('已有驱动 ROS 参数格式不正确')
                for selector,node in document.items():
                    if selector not in ('/**',node_name,full_name):
                        continue
                    if isinstance(node,dict) and isinstance(node.get('ros__parameters'),dict):
                        parameters.update(node['ros__parameters'])
            elif argument in ('-p','--param'):
                name,separator,content=value.partition(':=')
                if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.]*',name):
                    raise SourceError('已有驱动 ROS 参数覆盖无法确认')
                parameters[name]=yaml.safe_load(content)
            elif argument in ('-r','--remap'):
                source,separator,target=value.partition(':=')
                if not separator or not source or not target:
                    raise SourceError('已有驱动话题重映射无法确认')
                remaps.append((source,target))
    except yaml.YAMLError:
        raise SourceError('已有驱动参数无法安全解析，保持现有进程') from None
    environment={}
    for item in environment_raw.split(b'\0'):
        key,separator,value=item.partition(b'=')
        name=key.decode(errors='replace')
        if separator and name in DDS_ENV_KEYS:
            environment[name]=value.decode()
    if process_start_ticks(proc_root,pid)!=started:
        raise SourceError('雷达进程身份已变化，请重试状态检查')
    return dict(parameters=parameters,remaps=remaps,namespace=namespace,
                dds=dds_metadata(environment,namespace),processStartTicks=started)


def driver_topic(topic,runtime):
    def resolve(value):
        if any(char in value for char in ('*','?','~',':')):
            raise SourceError('已有驱动使用无法确认的话题重映射，保持原状')
        return value if value.startswith('/') else '/'+('/'.join(
            item for item in (runtime['namespace'],value) if item))
    actual=resolve(topic.lstrip('/'))
    candidates=[]
    for source,target in runtime['remaps']:
        if source in ('__node','__name','__ns'):
            continue
        if resolve(source)==actual:
            candidates.append(resolve(target))
    if len(candidates)>1:
        raise SourceError('已有驱动存在多重话题重映射，无法确认实际话题')
    return candidates[0] if candidates else actual


def storage_has_no_errors(store):
    errors = store.get('errors', {})
    return isinstance(errors, dict) and not any(
        key not in ('sourceTransfer', 'recovery', 'videoFinalization')
        for key in errors)


def archive_video_is_deferred(store):
    """Allow shutdown to pass terminal derivative failures with raw files kept."""
    return (store.get('videoTerminalPending') is True
            or store.get('derivativeTerminalPending') is True)


class DeviceSources:
    def __init__(self, role, *, home=None, workspace=None, runner=None,
                 sleeper=None, proc_root='/proc', usb_root='/sys/bus/usb/devices',
                 ros_setup='/opt/ros/humble/setup.bash', owner_module=None,
                 stl_port='/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_LD0001-if00-port0',
                 berxel_sdk='/opt/berxel'):
        self.role = role
        self.home = Path(home) if home is not None else Path.home()
        self.workspace = Path(workspace) if workspace is not None else self.home / 'code/upper_computer'
        self.runner = runner or subprocess.run
        self.sleeper = sleeper or time.sleep
        self.proc_root, self.usb_root = Path(proc_root), Path(usb_root)
        self.ros_setup = Path(ros_setup)
        self.stl_port, self.berxel_sdk = Path(stl_port), Path(berxel_sdk)
        self.owner_module = owner_module
        self.params = self.home / '.local/state/robot-cloud/device-sources/bluesea.yaml'
        self.prefixes = {}

    @property
    def preview_script(self):
        return self.home / '.local/share/rk3588-observer-camera/jetson_camera_encoder.py'

    @property
    def camera_transport_profile(self):
        return self.preview_script.parent / 'fastdds_camera.xml'

    def camera_transport_command(self, command, *, asynchronous=False):
        environment = ['/usr/bin/env', 'FASTRTPS_DEFAULT_PROFILES_FILE=' + str(self.camera_transport_profile),
                       'RMW_FASTRTPS_USE_QOS_FROM_XML=0']
        if asynchronous:
            environment.append('RMW_FASTRTPS_PUBLICATION_MODE=ASYNCHRONOUS')
        return environment + command

    def camera_transport_preflight(self):
        """Read an already deployed private profile; never create or repair it."""
        profile = self.camera_transport_profile
        try:
            directories = [self.home]
            for part in profile.parent.relative_to(self.home).parts:
                directories.append(directories[-1] / part)
            for current in directories:
                info = current.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise SourceError('深度相机 DDS 配置目录不能是符号链接或非目录')
            directory = profile.parent.lstat()
            if directory.st_uid != os.getuid() or directory.st_mode & 0o022:
                raise SourceError('深度相机 DDS 配置目录需归当前用户所有，且不可被组或其他用户写入')
            info = profile.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise SourceError('深度相机 DDS 配置必须是当前用户所有的普通非符号链接文件，权限为0600')
            if not 1 <= info.st_size <= 16384:
                raise SourceError('深度相机 DDS 配置为空或过大')
        except OSError as error:
            raise SourceError('缺少或无法读取已部署的深度相机 DDS 配置：' + str(profile)) from error

    def preview_start_command(self):
        return ['systemd-run', '--user', '--unit=' + BERXEL_PREVIEW_UNIT, '--collect',
                '--property=Restart=no', '--property=KillMode=control-group',
                '--property=TimeoutStopSec=15', '--property=UMask=0077',
                '--working-directory=' + str(self.workspace),
                *['--setenv=' + key + '=' + value for key, value in BERXEL_PREVIEW_ENV.items()],
                '--', *systemd_shell_argv(self.ros_command(self.camera_transport_command(
                    ['/usr/bin/python3', '-B', str(self.preview_script)])))]

    def preview_state(self):
        result = self.run(['systemctl', '--user', 'show', BERXEL_PREVIEW_UNIT,
            '-p', 'LoadState', '-p', 'ActiveState', '-p', 'Result', '-p', 'MainPID', '-p', 'ControlGroup'])
        state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if not state.get('LoadState'):
            raise SourceError('无法读取深度预览编码器服务状态')
        return state

    def verified_preview_unit(self, state, pids):
        """Only the exact script in this unit's cgroup may be stopped or claimed."""
        return self.verified_script_unit(state, pids, BERXEL_PREVIEW_UNIT)

    def verified_script_unit(self, state, pids, unit):
        """A script match must also belong to the same managed process group."""
        pid, group = int(state.get('MainPID', '0')), state.get('ControlGroup', '')
        if pids != [pid] or not group.endswith('/' + unit):
            return False
        process = self.proc_root / str(pid)
        try:
            groups = [line.split(':', 2)[-1] for line in (process / 'cgroup').read_text().splitlines()]
            return process.stat().st_uid == os.getuid() and any(
                value == group or value.startswith(group + '/') for value in groups)
        except OSError:
            return False

    def preview_preflight(self):
        for path in (self.ros_setup, self.workspace / 'install/setup.bash', self.workspace / 'env.sh'):
            if not path.is_file():
                raise SourceError('缺少运行环境：' + str(path))
        for name in ('jetson_camera_encoder.py', 'camera_codec.py', 'point_codec.py'):
            path = self.preview_script.parent / name
            if path.is_symlink() or not path.is_file():
                raise SourceError('缺少已部署的深度预览编码器：' + name)
            info = path.stat()
            if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o002:
                raise SourceError('深度预览编码器所有者或权限不正确：' + name)
        self.camera_transport_preflight()

    def berxel_preview(self, status=False):
        result = dict(key='berxel-preview', unit=BERXEL_PREVIEW_UNIT, dataVerified=False)
        try:
            pids = exact_python_script_processes(self.proc_root, self.preview_script)
            if len(pids) > 1:
                raise SourceError('已有多个深度预览编码器，未启动重复实例')
            state = self.preview_state()
            active = state['ActiveState'] in ('active', 'activating', 'reloading', 'deactivating')
            if active and not self.verified_preview_unit(state, pids):
                raise SourceError('同名深度预览服务身份无法确认，保持运行并拒绝重复启动')
            if pids:
                return dict(result, state='reused', pids=pids,
                    message='复用已有深度预览编码器；网页帧接收仍需核对')
            if status:
                return dict(result, state='inactive', serviceState=state['ActiveState'],
                    message='深度预览编码器未运行，网页无法收到彩色、深度和点云预览')
            self.preview_preflight()
            if exact_python_script_processes(self.proc_root, self.preview_script):
                return self.berxel_preview(status=True)
            if self.run(self.preview_start_command()).returncode:
                raise SourceError('深度预览编码器服务启动失败')
            for _ in range(6):
                self.sleeper(.5)
                state = self.preview_state()
                pids = exact_python_script_processes(self.proc_root, self.preview_script)
                if state['ActiveState'] == 'active' and self.verified_preview_unit(state, pids):
                    return dict(result, state='started', pids=pids,
                        message='只读深度预览编码器已启动；原始采集参数不变，网页帧接收仍需核对')
                if state['ActiveState'] in ('failed', 'inactive'):
                    break
            raise SourceError('深度预览编码器未就绪，请检查 ' + BERXEL_PREVIEW_UNIT)
        except (SourceError, OSError, ValueError, KeyError, TypeError) as exc:
            return dict(result, state='failed', error=str(exc))

    def stop_berxel_preview(self):
        result = dict(key='berxel-preview', unit=BERXEL_PREVIEW_UNIT, dataVerified=False)
        try:
            pids = exact_python_script_processes(self.proc_root, self.preview_script)
            state = self.preview_state()
            active = state['ActiveState'] not in ('inactive', 'failed') and state['LoadState'] != 'not-found'
            stopped = 'already_stopped'
            if active:
                if not self.verified_preview_unit(state, pids):
                    raise SourceError('深度预览服务身份无法确认，未停止未知程序')
                if self.run(['systemctl', '--user', 'stop', BERXEL_PREVIEW_UNIT], timeout=30).returncode:
                    raise SourceError('深度预览编码器关闭失败')
                after = self.preview_state()
                if (after['LoadState'] != 'not-found' and after['ActiveState'] not in ('inactive', 'failed')
                        or after.get('Result', 'success') not in ('', 'success')):
                    raise SourceError('深度预览编码器尚未正常退出')
                stopped = 'stopped'
            remaining = exact_python_script_processes(self.proc_root, self.preview_script)
            if remaining:
                return dict(result, state='external_running', pids=remaining,
                    message='其他入口启动的深度预览编码器仍运行，已保留，请到原终端关闭')
            return dict(result, state=stopped)
        except (SourceError, OSError, ValueError, KeyError, TypeError) as exc:
            return dict(result, state='failed', error=str(exc))

    def berxel_frames(self):
        """Read existing lightweight encoder status; never touch the capture owner."""
        result = dict(key='berxel-frames', dataVerified=False)
        try:
            self.preview_preflight()
            command = self.ros_command(['/usr/bin/python3', '-B', str(self.preview_script),
                '--health-probe', '--health-timeout', '15'])
            try:
                completed = self.run(command, timeout=20)
            except SourceError as error:
                if isinstance(error.__cause__, subprocess.TimeoutExpired):
                    return dict(result, state='failed', errorCode='BERXEL_FRAME_PROBE_TIMEOUT',
                        timeoutSeconds=20, error='深度相机真实帧检查超时：15秒观察及 ROS 节点清理未在20秒内完成；'
                        '可能存在 ROS/DDS 发现或退出阻塞，尚未确认连续出图。现有驱动保持运行。')
                raise
            if not completed.stdout.strip() or len(completed.stdout) > 32768:
                raise SourceError('深度帧检查未返回有效状态；驱动进程存在不表示正在出图')
            report = json.loads(completed.stdout.strip().splitlines()[-1])
            if not isinstance(report, dict):
                raise SourceError('深度帧检查状态格式错误')
            if completed.returncode or report.get('ok') is not True or report.get('dataVerified') is not True:
                message = report.get('error')
                raise SourceError(message[:500] if isinstance(message, str) and message else
                                  '深度相机尚未连续收到并输出彩色及深度帧')
            duration, count = report.get('sampleWindowSeconds'), report.get('reports')
            if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                    or not 3.0 <= duration <= 30 or type(count) is not int or count < 4):
                raise SourceError('深度帧观察窗口不足，未确认连续出图')
            channels = report.get('channels')
            if not isinstance(channels, dict) or set(channels) != {'color', 'depth'}:
                raise SourceError('深度帧检查缺少彩色或深度结果')
            for row in channels.values():
                if (not isinstance(row, dict) or any(type(row.get(key)) is not int or row[key] < 1
                        for key in ('receivedFrames', 'publishedFrames', 'receivedDelta', 'publishedDelta'))
                        or any(not isinstance(row.get(key), (int, float)) or isinstance(row[key], bool)
                            or not 0 <= row[key] <= 1 for key in ('sourceAgeS', 'previewAgeS'))):
                    raise SourceError('深度帧计数或帧龄不满足连续出图条件')
            return dict(result, state='reused', dataVerified=True,
                        sampleWindowSeconds=duration, reports=count, channels=channels,
                        message='已连续确认彩色与深度原始帧、网页预览帧增长；未启动或重启任何设备')
        except (SourceError, OSError, ValueError, KeyError, TypeError) as exc:
            return dict(result, state='failed', error=str(exc))

    def run(self, command, *, timeout=15):
        env = os.environ.copy()
        env.setdefault('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))
        env.setdefault('DBUS_SESSION_BUS_ADDRESS',
                       'unix:path=/run/user/' + str(os.getuid()) + '/bus')
        try:
            return self.runner(command, capture_output=True, text=True,
                               timeout=timeout, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceError('系统命令无法完成：' + Path(command[0]).name) from exc

    def ros_command(self, command):
        return ['/bin/bash', '-c', ROS_SHELL, 'robot-device-source',
                str(self.ros_setup), str(self.workspace / 'install/setup.bash'),
                str(self.workspace / 'env.sh'), *command]

    def source_command(self, source):
        command = [part.format(workspace=self.workspace,
            bluesea_params=self.params) for part in source.command]
        if source.key == 'berxel':
            # Apply after sourcing the ROS/workspace shell. Large image/cloud
            # writes must not block the camera SDK callback on a slow DDS reader.
            command = self.camera_transport_command(command, asynchronous=True)
        return self.ros_command(command)

    def start_command(self, source):
        return ['systemd-run', '--user', '--unit=' + source.unit, '--collect',
            '--property=Restart=no', '--property=KillMode=control-group',
            '--property=TimeoutStopSec=15', '--property=UMask=0077',
            '--working-directory=' + str(self.workspace), '--',
            *systemd_shell_argv(self.source_command(source))]

    def unit_state(self, unit):
        result = self.run(['systemctl', '--user', 'show', unit,
            '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result'])
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if not values.get('LoadState'):
            raise SourceError('无法读取用户服务状态：' + unit)
        return values

    def restore_jetson_archive_policy(self):
        """Restore the original Jetson-side sensor split before starting support.

        The industrial PC archives its local radar/telemetry topics.  Jetson
        owns the USB cameras and P100R, so its archive must accept the Berxel
        image, depth, point-cloud and optional fisheye topics.  This updates
        only the two policy keys and leaves upload credentials and paths alone.
        """
        if self.role != 'jetson':
            return False
        path = self.home / '.config/robot-cloud/archive.env'
        if not path.exists():
            # A missing EnvironmentFile is handled by the service preflight;
            # do not create a file that could omit deployment credentials.
            return False
        if path.is_symlink():
            raise SourceError('Jetson 归档环境文件不能是符号链接')
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > 65536):
            raise SourceError('Jetson 归档环境文件身份或大小不安全')
        raw = path.read_text(encoding='utf-8')
        lines, seen = [], set()
        for line in raw.splitlines():
            key, separator, _ = line.partition('=')
            if separator and key in JETSON_ARCHIVE_POLICY:
                if key not in seen:
                    lines.append(key + '=' + JETSON_ARCHIVE_POLICY[key])
                    seen.add(key)
                continue
            lines.append(line)
        for key, value in JETSON_ARCHIVE_POLICY.items():
            if key not in seen:
                lines.append(key + '=' + value)
        updated = ('\n'.join(lines) + '\n').encode('utf-8')
        if updated == raw.encode('utf-8'):
            return False
        backup_dir = self.home / '.local/state/robot-cloud/archive-policy-backup'
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_dir / 'archive.env.before-all-sensors'
        if not backup.exists():
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    descriptor = None
                    stream.write(raw.encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        descriptor, temporary = tempfile.mkstemp(prefix='.archive-', dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                descriptor = None
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return True

    def stop_unit(self, name):
        """Stop one exact known unit; missing/inactive units are already stopped."""
        allowed = {source.unit for source in SOURCES.values()} | set(JETSON_SUPPORT) | {YOLO_UNIT, RECORDING_FISHEYE.unit}
        if name not in allowed:
            raise SourceError('服务不在设备停止清单中')
        before = self.unit_state(name)
        if before.get('LoadState') == 'not-found' or before.get('ActiveState') in ('inactive', 'failed'):
            return 'already_stopped'
        if before.get('LoadState') != 'loaded':
            raise SourceError('无法确认已部署服务：' + name)
        if self.run(['systemctl', '--user', 'stop', name],
                    timeout=220 if name == 'robot-native-archive.service' else 45).returncode:
            raise SourceError('服务关闭失败：' + name)
        after = self.unit_state(name)
        if after.get('LoadState') != 'not-found' and after.get('ActiveState') not in ('inactive', 'failed'):
            raise SourceError('服务仍在运行：' + name)
        if after.get('Result') not in (None, '', 'success'):
            raise SourceError('服务退出异常，需检查日志：' + name + ' (' + after['Result'] + ')')
        return 'stopped'

    def stop_source(self, source):
        result = dict(key=source.key, unit=source.unit, topic=source.topic, dataVerified=False)
        try:
            state = self.stop_unit(source.unit)
            drivers = [source.driver]
            if source.key == 'mid360':
                drivers.append('fastlio_mapping')
            remaining = sorted({pid for driver in drivers for pid in exact_processes(self.proc_root, driver)})
            if remaining:
                return dict(result, state='external_running', pids=remaining,
                            message='仍有其他入口启动的驱动，已保留；请到原终端关闭')
            return dict(result, state=state)
        except (SourceError, OSError, ValueError) as exc:
            return dict(result, state='failed', error=str(exc))

    def drain_archive(self, *, after, attempts=360):
        """Allow the 15-second idle segment finalization and fresh status update."""
        path = self.home / '.local/state/robot-cloud/garage-control/status.json'
        for attempt in range(attempts):
            try:
                if path.is_symlink() or path.stat().st_size > 262144:
                    raise ValueError('archive status file')
                value = json.loads(path.read_text())
                updated = status_timestamp(value['updatedAt'])
                ros, store = value['ros'], value['store']
                now = time.time()
                if (after < updated <= now and now - updated <= 8
                        and all(type(ros.get(field)) is int and ros[field] == 0
                                for field in ('queueMessages', 'queueBytes'))
                        and type(ros.get('inFlightMessages', 0)) is int
                        and ros.get('inFlightMessages', 0) == 0
                        and all(ros.get(field) is None or (type(ros[field]) is str and ros[field] == '')
                                for field in ('tickError', 'gateError'))
                        and store.get('active') == {} and storage_has_no_errors(store)
                        and type(store.get('videoPendingSegments', 0)) is int
                        and ((store.get('videoPendingSegments', 0) == 0
                              and store.get('videoFinalizing', False) is False)
                             or archive_video_is_deferred(store))):
                    return True
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
            if attempt + 1 < attempts:
                self.sleeper(.5)
        return False

    def stop_support(self, name, *, after=None, sources_stopped=True):
        result = dict(key=name.removesuffix('.service'), unit=name, dataVerified=False)
        try:
            if name == 'robot-native-archive.service':
                current = self.unit_state(name)
                active = current.get('LoadState') == 'loaded' and current.get('ActiveState') not in ('inactive', 'failed')
                if active:
                    if not sources_stopped:
                        raise SourceError('采集源尚未全部关闭，保留归档服务继续保存数据')
                    if not self.drain_archive(after=after if after is not None else time.time()):
                        raise SourceError('尚未确认原始数据队列和活跃片段均已收尾，归档服务继续运行；请稍后重试')
            return dict(result, state=self.stop_unit(name))
        except (SourceError, OSError, ValueError) as exc:
            return dict(result, state='failed', error=str(exc))

    def camera_archive_finalized(self, *, after):
        """The tap publishes its final status before the shared process exits."""
        path = self.home / '.local/state/robot-cloud/garage-control/camera-status.json'
        try:
            if path.is_symlink() or path.stat().st_size > 262144:
                return False
            report = json.loads(path.read_text())
            updated = status_timestamp(report['updatedAt'])
            sink, store, now = report['sink'], report['store'], time.time()
            return (after < updated <= now and now - updated <= 8
                    and all(type(sink.get(field)) is int and sink[field] == 0
                            for field in ('queueFrames', 'queueBytes'))
                    and sink.get('writerRunning') is False
                    and sink.get('errors', {}) == {}
                    and store.get('active') == {} and storage_has_no_errors(store))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return False

    def package(self, package, executable):
        if package not in self.prefixes:
            result = self.run(self.ros_command(['ros2', 'pkg', 'prefix', package]))
            value = result.stdout.strip()
            if result.returncode or not value or '\n' in value or not Path(value).is_absolute():
                raise SourceError('未安装 ROS 包：' + package)
            self.prefixes[package] = Path(value)
        prefix = self.prefixes[package]
        binary = prefix / 'lib' / package / executable
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise SourceError('驱动未编译或不可执行：' + package + '/' + executable)
        return prefix

    def ping(self, destination, host=None):
        try:
            ipaddress.IPv4Address(destination)
            if host is not None:
                ipaddress.IPv4Address(host)
        except ValueError as exc:
            raise SourceError('雷达地址配置无效') from exc
        command = ['ping', '-n', '-c', '1', '-W', '1']
        if host:
            command += ['-I', host]
        if self.run([*command, destination]).returncode:
            raise SourceError('雷达不可达：' + destination)

    def private_params(self, raw):
        """Publish a 0600 copy, refusing symlink directories and targets."""
        self.private_text(self.params,bluesea_parameters(raw))

    def private_text(self,path,content):
        directory = path.parent
        current = self.home
        for part in directory.relative_to(self.home).parts:
            current = current / part
            if current.is_symlink():
                raise SourceError('运行参数目录不能是符号链接')
            current.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SourceError('运行参数目录必须归当前用户所有且权限为700')
        if path.is_symlink():
            raise SourceError('运行参数文件不能是符号链接')
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise SourceError('已有运行参数文件身份不安全')
        descriptor, name = tempfile.mkstemp(prefix='bluesea-', suffix='.tmp', dir=directory)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def preflight(self, source):
        if source.key == 'berxel':
            self.camera_transport_preflight()
        for path in (self.ros_setup, self.workspace / 'install/setup.bash', self.workspace / 'env.sh'):
            if not path.is_file():
                raise SourceError('缺少运行环境：' + str(path))
        for package, executable in source.packages:
            self.package(package, executable)
        if source.key in ('mid360', 'stl19p'):
            if not (self.workspace / ('run_fastlio.sh' if source.key == 'mid360'
                                      else 'run_ldrobot_stl19p.sh')).is_file():
                raise SourceError('缺少现有设备启动脚本')
        if source.key == 'mid360':
            config = self.prefixes['livox_ros_driver2'] / 'share/livox_ros_driver2/config/MID360s_config.json'
            if config.stat().st_size > 131072:
                raise SourceError('MID360 配置过大')
            data = json.loads(config.read_text())
            addresses = [item['ip'] for item in data['lidar_configs']]
            if len(addresses) != 1:
                raise SourceError('需要唯一明确的 MID360 地址')
            self.ping(addresses[0])
        elif source.key == 'stl19p':
            port = self.stl_port
            if not port.exists() or not os.access(port, os.R_OK | os.W_OK):
                raise SourceError('STL-19P 串口缺失或无读写权限')
            owner = self.run(['fuser', '--', str(port)])
            if owner.returncode == 0:
                raise SourceError('STL-19P 串口已被其他程序占用，未启动第二份驱动')
            if owner.returncode != 1:
                raise SourceError('无法检查 STL-19P 串口占用')
        elif source.key == 'bluesea':
            prefix = self.prefixes['bluesea2']
            guard=prefix/'share/bluesea2/launch/livox_driver_guard.py'
            if guard.is_symlink() or not guard.is_file():
                raise SourceError('蓝海共享占用保护尚未安装，未启动第二份驱动')
            path = prefix / 'share/bluesea2/params/lds_50c_e_7.yaml'
            if path.stat().st_size > 131072:
                raise SourceError('蓝海参数文件过大')
            raw = path.read_text()
            def field(key):
                values = re.findall(r'(?m)^\s+' + key + r'\s*:\s*"([0-9.]+)"\s*$', raw)
                if len(values) != 1:
                    raise SourceError('无法确认蓝海网络配置')
                return values[0]
            # ntp_ip is an optional sensor clock setting, not the UDP bind
            # address. The SDK listens on INADDR_ANY:local_port.
            self.lidar_route(field('lidar_ip'))
            ports=re.findall(r'(?m)^\s+local_port\s*:\s*(\d+)\s*$',raw)
            if len(ports)!=1:
                raise SourceError('无法确认蓝海 UDP 接收端口')
            self.require_free_udp_ports([('0.0.0.0',int(ports[0]))])
            self.private_params(raw)
        elif source.key == 'berxel':
            if not self.berxel_sdk.is_dir():
                raise SourceError('缺少 Berxel SDK：/opt/berxel')
            connected = False
            for device in self.usb_root.iterdir():
                try:
                    connected |= ((device / 'idVendor').read_text().strip().lower(),
                                  (device / 'idProduct').read_text().strip().lower()) == ('0603', '0009')
                except FileNotFoundError:
                    continue
            if not connected:
                raise SourceError('未连接 Berxel P100R USB 设备（0603:0009）')

    def lidar_route(self,destination):
        ipaddress.IPv4Address(destination)
        result=self.run(['ip','-j','-4','route','get',destination])
        if result.returncode:
            raise SourceError('没有可用的雷达接收路由，请检查网线与网口地址')
        routes=json.loads(result.stdout)
        if result.returncode or not isinstance(routes,list) or len(routes)!=1:
            raise SourceError('没有唯一可核验的雷达路由，未修改网卡')
        route=routes[0]
        local=route.get('prefsrc') or route.get('src')
        if not local or route.get('gateway') or not route.get('dev'):
            raise SourceError('雷达路由未指向本机直连网口，请检查网线与网口地址')
        inventory=self.run(['ip','-j','-4','address','show'])
        if inventory.returncode:
            raise SourceError('无法读取本机雷达网口地址，未修改网卡')
        entries=json.loads(inventory.stdout)
        valid=any(item.get('ifname')==route['dev'] and address.get('local')==local
            and ipaddress.IPv4Address(destination) in ipaddress.IPv4Network(
                str(local)+'/'+str(address.get('prefixlen',32)),strict=False)
            for item in entries for address in item.get('addr_info',[]))
        if inventory.returncode or not valid:
            raise SourceError('雷达出口地址不是本机同网段地址，未生成错误的接收配置')
        return local

    def require_free_udp_ports(self,endpoints):
        result=self.run(['ss','-H','-lun'])
        if result.returncode:
            raise SourceError('无法核验 UDP 接收端口占用，保持现有设备')
        for line in result.stdout.splitlines():
            fields=line.split()
            if len(fields)<5:
                raise SourceError('无法解析 UDP 接收端口占用，保持现有设备')
            address,separator,port=fields[3].rpartition(':')
            address=address.strip('[]')
            if not separator or not port.isdecimal():
                raise SourceError('无法解析 UDP 接收地址，保持现有设备')
            if any(int(port)==wanted and (host=='0.0.0.0' or address in
                    (host,'0.0.0.0','::','*')) for host,wanted in endpoints):
                raise SourceError('雷达 UDP 接收端口已被其他程序占用，未启动第二份驱动')

    def source(self, source, status=False):
        result = dict(key=source.key, unit=source.unit, topic=source.topic, dataVerified=False)
        try:
            processes = exact_processes(self.proc_root, source.driver)
            if source.key in ('stl19p', 'bluesea'):
                blue = exact_processes(self.proc_root, SOURCES['bluesea'].driver)
                if any(existing_bluesea_topic(self.proc_root, pid) != '/bluesea_new/scan' for pid in blue):
                    raise SourceError('已有蓝海驱动未明确使用 /bluesea_new/scan；保持现有程序不动，请人工关闭旧实例后重试')
            if len(processes) > 1:
                raise SourceError('已有多个同类驱动，请先人工检查；未启动重复实例')
            if source.key == 'mid360':
                mapping = exact_processes(self.proc_root, 'fastlio_mapping')
                if len(mapping) > 1 or (mapping and not processes):
                    raise SourceError('FAST-LIO 已运行但雷达链不完整，未启动重复定位进程')
            if processes:
                if source.key == 'mid360' and not exact_processes(self.proc_root, 'fastlio_mapping'):
                    return dict(result, state='partial', pids=processes,
                                error='MID360 驱动已运行但 FAST-LIO 未运行；未重复启动雷达')
                return dict(result, state='reused', pids=processes,
                            message='复用现有驱动；尚未验证原始数据')
            unit = self.unit_state(source.unit)
            if status:
                return dict(result, state='inactive', serviceState=unit['ActiveState'])
            if unit['ActiveState'] in ('active', 'activating', 'reloading', 'deactivating'):
                raise SourceError('同名服务已在运行但未检测到目标驱动；未重复启动')
            self.preflight(source)
            # Hardware/package checks take time. Recheck immediately before
            # starting so a manually launched driver is never knowingly duplicated.
            if exact_processes(self.proc_root, source.driver):
                return self.source(source, status=True)
            if source.key == 'mid360' and exact_processes(self.proc_root, 'fastlio_mapping'):
                raise SourceError('检查期间 FAST-LIO 已启动，未重复启动定位进程')
            if self.run(self.start_command(source)).returncode:
                raise SourceError('设备服务启动失败')
            for _ in range(6):
                self.sleeper(.5)
                unit = self.unit_state(source.unit)
                processes = exact_processes(self.proc_root, source.driver)
                complete = (source.key != 'mid360' or
                            len(exact_processes(self.proc_root, 'fastlio_mapping')) == 1)
                if unit['ActiveState'] == 'active' and len(processes) == 1 and complete:
                    return dict(result, state='started', pids=processes,
                                message='服务与驱动进程已启动；尚未验证原始数据')
                if unit['ActiveState'] in ('failed', 'inactive'):
                    break
            raise SourceError('启动后未同时确认服务和目标驱动，请查看 journalctl --user -u ' + source.unit)
        except (SourceError, OSError, ValueError, KeyError, TypeError) as exc:
            message = str(exc)
            # Optional auxiliary lidars must not prevent MID360, cameras or
            # archive services from starting when their physical input or
            # package is absent. Keep the reason visible as SKIPPED.
            if ((source.key == 'stl19p' and '串口缺失或无读写权限' in message) or
                    (source.key == 'bluesea' and '未安装 ROS 包：bluesea2' in message)):
                return dict(result, state='skipped', error=message)
            return dict(result, state='failed', error=message)

    def support(self, name, status=False):
        result = dict(key=name.removesuffix('.service'), unit=name, dataVerified=False)
        try:
            current = self.unit_state(name)
            if current['LoadState'] != 'loaded':
                raise SourceError('缺少已部署服务：' + name)
            if current['ActiveState'] in ('active', 'activating', 'reloading', 'deactivating'):
                return dict(result, state='reused')
            if status:
                return dict(result, state='inactive', serviceState=current['ActiveState'])
            if self.run(['systemctl', '--user', 'start', name]).returncode:
                raise SourceError('已部署服务启动失败：' + name)
            self.sleeper(.5)
            if self.unit_state(name)['ActiveState'] != 'active':
                raise SourceError('服务启动后未处于 active：' + name)
            return dict(result, state='started')
        except SourceError as exc:
            return dict(result, state='failed', error=str(exc))

    def load_camera_owner(self):
        if self.owner_module is not None:
            return self.owner_module
        path = self.home / '.local/share/robot-cloud/camera_owner.py'
        if not path.is_file() or path.is_symlink():
            raise SourceError('缺少已部署的相机占用检查 helper')
        info = path.stat()
        if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise SourceError('相机占用检查 helper 权限不安全')
        spec = importlib.util.spec_from_file_location('_robot_device_camera_owner', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def camera(self, status=False, *, before_start=None):
        result = dict(key='usb-cameras', dataVerified=False)
        try:
            owner = self.load_camera_owner()
            ops = owner.SystemOps()
            engine = owner.Engine(ops)
            def verified(current, state, message):
                available = tuple(getattr(current, 'available', ()))
                absent = tuple(getattr(current, 'missing_cameras', ()))
                details = dict(dataVerified=True, dataVerifiedFor=list(available),
                               missingCameras=list(absent))
                if absent:
                    labels = {'camera1': '前', 'camera2': '后', 'camera3': '右', 'camera4': '左'}
                    if not available or not all(identity in labels for identity in (*available, *absent)):
                        raise SourceError('无法确认部分相机的实际连接状态')
                    message = ('已运行：' + '、'.join(labels[identity] for identity in available)
                               + '；未连接：' + '、'.join(labels[identity] for identity in absent)
                               + '。未连接的相机接回后，请关闭再重新开启设备。')
                    state = 'partial'
                return dict(result, state=state, message=message, **details)
            def inspect(current):
                engine.preflight(current, True)
                active = any(current.units.get(name, owner.Unit(name)).active
                             for name in owner.CAPTURE_UNITS)
                if active:
                    if not current.healthy:
                        if getattr(current, 'reconnect_required', ()):
                            raise SourceError('相机已重新接入，需关闭后重新开启；现有采集保持运行')
                        raise SourceError('已有相机采集服务未就绪，保持运行并拒绝启动第二份')
                    return verified(current, 'reused', '复用现有健康相机采集')
                return None
            if status:
                current = ops.snapshot()
                return inspect(current) or dict(result, state='inactive')
            with ops.lock():
                reused = inspect(ops.snapshot())
                if reused:
                    return reused
                if ops.settings.capture_config != owner.SHARED_CONFIG:
                    raise SourceError('缺少已核对的共享相机配置')
                ops.validate_start_target()
                if before_start is not None:
                    before_start()
                if ops.command(['systemctl', '--user', 'start', owner.SHARED_UNIT]).returncode:
                    raise SourceError('共享相机服务启动失败')
                current = ops.wait_healthy()
                engine.preflight(current, True)
                if not current.healthy:
                    raise SourceError('相机服务尚未收到有效原始帧')
                return verified(current, 'started', '已有共享相机采集已启动并通过帧健康检查')
        except Exception as exc:
            # Existing ownership helper has its own typed failure family. Never
            # stop/kill an owner, even when this start attempt fails health.
            return dict(result, state='failed', error=str(exc))

    def stop_camera(self):
        """Only stop the verified shared unit, never other capture owners."""
        result = dict(key='usb-cameras', dataVerified=False)
        try:
            owner = self.load_camera_owner()
            ops = owner.SystemOps()
            with ops.lock():
                current = ops.snapshot()
                unit = current.units.get(owner.SHARED_UNIT)
                if current.scan_errors or unit is None:
                    raise SourceError('无法确认共享相机服务身份，保持现有程序运行')
                state = 'already_stopped'
                if unit.active:
                    if not unit.verified:
                        raise SourceError('共享相机服务身份发生变化，未停止未知程序')
                    execution = ops.command(['systemctl', '--user', 'show', owner.SHARED_UNIT,
                        '-p', 'ExecStart', '--value'])
                    if execution.returncode or not execution.stdout.strip():
                        raise SourceError('无法确认相机原始归档是否启用，保持现有程序运行')
                    native_tap = any(arg == '--archive-config' or arg.startswith('--archive-config=')
                                     for arg in shlex.split(execution.stdout))
                    environment = ops.command(['systemctl', '--user', 'show', owner.SHARED_UNIT,
                        '-p', 'Environment', '--value'])
                    if environment.returncode:
                        raise SourceError('无法确认相机归档环境，保持现有程序运行')
                    native_tap = native_tap or any(
                        key == 'NATIVE_CAMERA_ARCHIVE_CONFIG' and bool(value)
                        for item in shlex.split(environment.stdout)
                        for key, separator, value in (item.partition('='),) if separator)
                    stopping_at = time.time()
                    if ops.command(['systemctl', '--user', 'stop', owner.SHARED_UNIT], timeout=105).returncode:
                        raise SourceError('共享相机服务关闭失败')
                    current = ops.snapshot()
                    unit = current.units.get(owner.SHARED_UNIT)
                    if unit is None or unit.active:
                        raise SourceError('共享相机服务尚未退出')
                    completion = ops.command(['systemctl', '--user', 'show', owner.SHARED_UNIT,
                        '-p', 'Result', '--value'])
                    if completion.returncode or completion.stdout.strip() not in ('', 'success'):
                        raise SourceError('相机服务退出异常，未确认本次采集正常收尾')
                    if native_tap and not self.camera_archive_finalized(after=stopping_at):
                        raise SourceError('相机已退出，但原始帧归档收尾未获确认；保留归档辅助服务，请检查 camera-status.json')
                    state = 'stopped'
                elif getattr(unit, 'load_state', 'loaded') == 'loaded':
                    completion = ops.command(['systemctl', '--user', 'show', owner.SHARED_UNIT,
                        '-p', 'Result', '--value'])
                    if completion.returncode or completion.stdout.strip() not in ('', 'success'):
                        raise SourceError('相机上次退出异常，未确认原始采集已收尾；请检查 camera-status.json')
                others = any(current.units.get(name, owner.Unit(name)).active
                             for name in owner.CAPTURE_UNITS if name != owner.SHARED_UNIT)
                if current.scan_errors or others or current.holders:
                    return dict(result, state='external_running',
                                message='仍有其他相机采集程序或无法完整确认占用，已保留；请到原终端关闭')
                return dict(result, state=state,
                            message='共享相机程序已退出；已保存文件保留' if state == 'stopped' else '共享相机程序已关闭')
        except Exception as exc:
            return dict(result, state='failed', error=str(exc))

    def stop(self, *, dry_run=False, no_camera_source=False, cameras_only=False):
        names = ('mid360', 'stl19p', 'bluesea') if self.role == 'ipc' else ('berxel',)
        support = CAMERA_SUPPORT if cameras_only else JETSON_SUPPORT
        if self.role == 'jetson' and no_camera_source:
            return dict(ok=True, role=self.role, stop=True, dryRun=dry_run,
                        devices=[dict(key=key, state='skipped', message='已选择保留 Jetson 相机及其归档辅助服务')
                                 for key in ('usb-cameras', 'berxel', 'berxel-preview', *support, YOLO_UNIT)],
                        dataVerified=False)
        if dry_run:
            rows = []
            if self.role == 'jetson' and not no_camera_source:
                rows.append(dict(key='berxel-preview', unit=BERXEL_PREVIEW_UNIT, state='planned',
                    message='仅停止身份已核对的只读编码器，原始归档收尾另行确认',
                    command=['systemctl', '--user', 'stop', BERXEL_PREVIEW_UNIT]))
                rows.append(dict(key='usb-cameras', state='planned',
                    message='核对共享相机身份后正常停止；其他程序不处理'))
            if not (self.role == 'jetson' and no_camera_source):
                rows += [dict(key=name, unit=SOURCES[name].unit, state='planned',
                    command=['systemctl', '--user', 'stop', SOURCES[name].unit]) for name in names]
            if self.role == 'jetson':
                rows += [dict(key=name, state='planned',
                    message='数据源关闭且活跃片段收尾后才停止；异常则保留所有辅助服务',
                    command=['systemctl', '--user', 'stop', name]) for name in (*support, YOLO_UNIT)]
            return dict(ok=True, role=self.role, stop=True, dryRun=True, devices=rows,
                        dataVerified=False, message='仅预演关闭顺序，未执行命令、读取设备或写文件')
        rows = []
        if self.role == 'jetson':
            rows.append(self.stop_berxel_preview())
            rows.append(self.stop_camera())
        rows += [self.stop_source(SOURCES[name]) for name in names]
        if self.role == 'jetson':
            clean = all(row['state'] in ('stopped', 'already_stopped') for row in rows)
            if clean and not cameras_only:
                archive = self.stop_support('robot-native-archive.service', after=time.time())
                rows.append(archive)
                clean = archive['state'] in ('stopped', 'already_stopped')
            elif not clean and not cameras_only:
                rows.append(dict(key='robot-native-archive', state='failed',
                    error='采集源尚未全部关闭，保留归档和相机辅助服务'))
            if clean:
                rows += [self.stop_support(name) for name in (*CAMERA_SUPPORT, YOLO_UNIT)]
            else:
                rows += [dict(key=name.removesuffix('.service'), state='skipped',
                    message='采集关闭或收尾未完成，辅助服务保持运行') for name in (*CAMERA_SUPPORT, YOLO_UNIT)]
        return dict(ok=all(row['state'] in ('stopped', 'already_stopped', 'skipped') for row in rows),
                    role=self.role, stop=True, dryRun=False, devices=rows, dataVerified=False,
                    message='只停止固定托管服务；原始文件和地库开关保留，未执行关机')

    def execute(self, *, status=False, dry_run=False, no_camera_source=False, stop=False, cameras_only=False):
        if cameras_only and self.role != 'jetson':
            raise SourceError('仅相机模式只适用于 Jetson 相机端')
        if stop:
            return self.stop(dry_run=dry_run, no_camera_source=no_camera_source, cameras_only=cameras_only)
        names = ('mid360', 'stl19p', 'bluesea') if self.role == 'ipc' else ('berxel',)
        support = CAMERA_SUPPORT if cameras_only else JETSON_SUPPORT
        if dry_run:
            rows = [dict(key=name, unit=SOURCES[name].unit, state='planned',
                         command=self.start_command(SOURCES[name]), dataVerified=False)
                    for name in names if not (no_camera_source and self.role == 'jetson')]
            if self.role == 'jetson':
                rows = [dict(key=name, state='planned', command=['systemctl', '--user', 'start', name])
                        for name in support] + rows
                if not no_camera_source:
                    rows.append(dict(key='berxel-preview', unit=BERXEL_PREVIEW_UNIT, state='planned',
                        command=self.preview_start_command(), dataVerified=False))
                    rows.append(dict(key='usb-cameras', state='planned',
                        message='在已部署的占用检查锁内，安全复用或启动共享相机；不停止任何程序'))
                    rows.append(dict(key=YOLO_UNIT.removesuffix('.service'), state='planned',
                        command=['systemctl', '--user', 'start', YOLO_UNIT],
                        message='相机检查完成后启动现有 YOLO/ArUco 只读推理服务'))
                    rows.append(dict(key='berxel-frames', state='planned',
                        message='只读订阅轻量状态，确认至少三秒连续彩色／深度原始帧及预览帧'))
            return dict(ok=True, role=self.role, dryRun=True, devices=rows,
                        dataVerified=False, message='仅预演，未执行命令、读取设备或写文件')
        rows = []
        archive_policy_changed = False
        if self.role == 'jetson':
            if not cameras_only and not status:
                archive_policy_changed = self.restore_jetson_archive_policy()
                if archive_policy_changed:
                    archive_state = self.unit_state('robot-native-archive.service')
                    if archive_state.get('ActiveState') not in ('inactive', 'failed'):
                        # Do not hold the SSH session while a recorder drains
                        # its active segment.  The service manager queues the
                        # restart; the source/driver checks continue separately.
                        if self.run(['systemctl', '--user', 'restart', '--no-block',
                                     'robot-native-archive.service'], timeout=15).returncode:
                            raise SourceError('全传感器归档策略已更新，但归档服务重启失败')
            rows += [self.support(name, status) for name in support]
        if no_camera_source and self.role == 'jetson':
            rows += [dict(key=name, state='skipped') for name in ('berxel', 'berxel-preview', 'usb-cameras')]
        else:
            rows += [self.source(SOURCES[name], status) for name in names]
            if self.role == 'jetson':
                rows.append(self.berxel_preview(status))
                rows.append(self.camera(status))
                rows.append(self.support(YOLO_UNIT, status))
                berxel_ready = all(any(row['key'] == key and row['state'] in ('started', 'reused')
                                       for row in rows) for key in ('berxel', 'berxel-preview'))
                rows.append(self.berxel_frames() if berxel_ready else dict(key='berxel-frames',
                    state='failed', dataVerified=False,
                    error='深度驱动或预览编码器未就绪，尚未验证连续真实帧'))
        return dict(ok=all(row['state'] in ('started', 'reused', 'skipped')
                           or (row['key'] == 'usb-cameras' and row['state'] == 'partial'
                               and row.get('dataVerified') is True and row.get('dataVerifiedFor')
                               and row.get('missingCameras')) for row in rows),
                    role=self.role, status=status, dryRun=False, devices=rows,
                    dataVerified=False,
                    message='驱动/服务检查与数据实收验收分开；地库采集开关保持原值')


class RecordingDeviceSources(DeviceSources):
    """Start only missing acquisition sources; never stop or change a pipeline."""

    def __init__(self, *args, cancelled=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancelled = cancelled or (lambda: False)
        self._camera_start_attempts = {}
        self._camera_ownership = {}
        self._read_only_sources = False

    def check_cancelled(self):
        if self.cancelled():
            raise RecordingStartCancelled()

    def run(self, command, **kwargs):
        self.check_cancelled()
        if self._read_only_sources:
            allowed = [
                ['systemctl', '--user', 'show', source.unit,
                 '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result']
                for source in (RECORDING_MID360, SOURCES['stl19p'], SOURCES['bluesea'])
            ]
            if command not in allowed:
                raise SourceError('只读雷达检查不允许启动、关闭或修改设备')
        unit = None
        if command and command[0] == 'systemd-run':
            unit = next((arg.removeprefix('--unit=') for arg in command
                         if arg.startswith('--unit=')), None)
        elif command[:3] == ['systemctl', '--user', 'start'] and len(command) == 4:
            unit = command[3]
        started = time.monotonic_ns() // 1000
        result = super().run(command, **kwargs)
        if not result.returncode and unit in CAMERA_OWNERSHIP_UNITS:
            self._camera_start_attempts[unit] = started
        return result

    def camera_unit_identity(self, unit):
        if unit not in CAMERA_OWNERSHIP_UNITS:
            raise SourceError('服务不在相机归属清单中')
        result = self.run(['systemctl', '--user', 'show', unit,
            '--property=LoadState,ActiveState,InvocationID,MainPID,ExecMainStartTimestampMonotonic,'
            + ','.join(CAMERA_STOP_DEPENDENCIES)])
        if result.returncode or len(result.stdout) > 4096:
            raise SourceError('无法确认相机服务本次运行身份')
        state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        return state

    def remember_camera_ownership(self):
        """A reused source is never a start attempt and therefore never claimed."""
        for unit, started in list(self._camera_start_attempts.items()):
            if unit in self._camera_ownership:
                continue
            try:
                state = self.camera_unit_identity(unit)
                if state.get('ActiveState') != 'active':
                    continue
                row = dict(unit=unit, invocationId=state.get('InvocationID'),
                    mainPid=int(state.get('MainPID', '0')),
                    execMainStartTimestampMonotonic=state.get('ExecMainStartTimestampMonotonic'))
                camera_ownership_payload(dict(schemaVersion=1, units=[row]))
                # Never claim an invocation which predates our successful start.
                if int(row['execMainStartTimestampMonotonic']) < started:
                    continue
                self._camera_ownership[unit] = row
            except (SourceError, OSError, ValueError, TypeError):
                # Missing evidence only retains a camera; it must not fabricate
                # ownership or turn a valid acquisition into a failed start.
                continue
            finally:
                self._camera_start_attempts.pop(unit, None)

    def camera_ownership(self):
        return dict(schemaVersion=1, units=list(self._camera_ownership.values()))

    def camera_archive_http_status(self):
        """Read bounded local metadata, without opening any frame or device."""
        # 8081 is the filtered observability gateway; only the original shared
        # process on 8080 exposes the attached archive_tap metadata.
        connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=.8)
        try:
            connection.request('GET', '/status', headers={'Connection': 'close'})
            response = connection.getresponse()
            if (response.status != 200 or response.getheader('Content-Type', '').split(';', 1)[0]
                    != 'application/json'):
                raise SourceError('相机归档接入状态不可读')
            raw = response.read(262145)
            if len(raw) > 262144:
                raise SourceError('相机归档接入状态过大')
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get('cameras'), list):
                raise SourceError('相机归档接入状态格式无效')
            return value
        finally:
            connection.close()

    def camera_archive_ready(self, available, *, after=None):
        """Confirm an attached tap and live writer even before the gate opens."""
        path = self.home / '.local/state/robot-cloud/garage-control/camera-status.json'
        report = json.loads(bounded_config(path, 262144))
        now, updated = time.time(), status_timestamp(report['updatedAt'])
        store, sink = report['store'], report['sink']
        if (not -2 <= now-updated <= 8 or after is not None and updated < after-2
                or report.get('state') == 'stop_failed'
                or not isinstance(store, dict) or not isinstance(sink, dict)
                or sink.get('writerRunning') is not True or sink.get('errors', {}) != {}):
            raise SourceError('相机画面正常，但原生录制状态过期或写盘线程未就绪')
        resources = store.get('resourceMonitor')
        if isinstance(resources, dict) and resources.get('ready') is not True:
            raise SourceError('相机原生录制的存储空间检查尚未就绪：' + str(resources.get('state')))
        native = self.camera_archive_http_status()
        rows = {row.get('id'): row for row in native['cameras']
                if isinstance(row, dict) and isinstance(row.get('id'), str)}
        required = available or ('camera1', 'camera2', 'camera3', 'camera4')
        for identity in required:
            tap = rows.get(identity, {}).get('archive_tap')
            if (not isinstance(tap, dict) or tap.get('error') != ''
                    or any(type(tap.get(key)) is not int or tap[key] < 0
                           for key in ('submitted', 'rejected', 'sampled'))):
                raise SourceError('相机画面正常，但 ' + identity + ' 未确认接入原生录制')
        return dict(archiveReady=True, archiveStatusUpdatedAt=report['updatedAt'],
                    archiveCameraIds=list(required))

    def camera(self, status=False, *, before_start=None, require_archive=True):
        before = time.time()
        started = None
        def preparing():
            nonlocal started
            if before_start is not None:
                before_start()
            started = time.monotonic_ns() // 1000
        result = super().camera(status, before_start=preparing)
        if (not status and started is not None and result.get('state') in ('started', 'partial')
                and result.get('dataVerified') is True):
            self._camera_start_attempts[SHARED_CAMERA_UNIT] = started
            self.remember_camera_ownership()
        if not require_archive:
            return result
        if result.get('state') not in ('started', 'reused', 'partial') or result.get('dataVerified') is not True:
            return result
        # Pure preview tools retain the base implementation. A recording entry
        # must separately prove the original JPEG tap; source FPS proves neither
        # a configured archive nor a running writer. Never restart a live owner.
        last_error = None
        for attempt in range(1 if status else 8):
            self.check_cancelled()
            try:
                evidence = self.camera_archive_ready(result.get('dataVerifiedFor'),
                    after=before if result['state'] == 'started' else None)
                return {**result, **evidence}
            except (SourceError, OSError, ValueError, TypeError, KeyError,
                    http.client.HTTPException) as error:
                last_error = error
                if not status and attempt < 7:
                    self.sleeper(.5)
        return {**result, 'state': 'failed', 'archiveReady': False,
            'errorCode': 'CAMERA_ARCHIVE_NOT_READY',
            'error': '相机原始画面已收到，但录制链路未就绪；未重启或停止相机：' + str(last_error)}

    @property
    def yolo_script(self):
        return self.home / '.local/share/robot-cloud/yolo_observer.py'

    def yolo_state(self):
        result = self.run(['systemctl', '--user', 'show', YOLO_UNIT,
            '-p', 'LoadState', '-p', 'ActiveState', '-p', 'MainPID', '-p', 'ControlGroup'])
        state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if not state.get('LoadState'):
            raise SourceError('无法读取已有 YOLO/ArUco 服务状态')
        return state

    def camera_yolo(self, status=False):
        """Reuse the one verified inference owner; never launch a second GPU model."""
        result = dict(key=YOLO_UNIT.removesuffix('.service'), unit=YOLO_UNIT, dataVerified=False)
        try:
            pids = exact_python_script_processes(self.proc_root, self.yolo_script)
            if len(pids) > 1:
                raise SourceError('已有多个 YOLO/ArUco 进程，保持运行且不启动第二份')
            state = self.yolo_state()
            if state['LoadState'] != 'loaded':
                raise SourceError('缺少已部署服务：' + YOLO_UNIT)
            if state['ActiveState'] in ('active', 'activating', 'reloading', 'deactivating'):
                if not self.verified_script_unit(state, pids, YOLO_UNIT):
                    raise SourceError('YOLO/ArUco 服务的脚本、进程或控制组身份无法确认，保持现状且不重启')
                return dict(result, state='reused', pids=pids,
                    message='复用已有只读 YOLO/ArUco；模型及限帧配置不变，检测输出仍需核对')
            if pids:
                raise SourceError('已有其他入口运行相同 YOLO/ArUco 脚本，已保留且不启动第二份，请在原入口核对')
            if status:
                return dict(result, state='inactive', serviceState=state['ActiveState'])
            self.check_cancelled()
            if exact_python_script_processes(self.proc_root, self.yolo_script):
                return self.camera_yolo(status=True)
            if self.run(['systemctl', '--user', 'start', YOLO_UNIT]).returncode:
                raise SourceError('已有 YOLO/ArUco 服务启动失败，请检查本地模型及服务日志；未下载模型或更改推理配置')
            for _ in range(6):
                self.sleeper(.5)
                self.check_cancelled()
                pids = exact_python_script_processes(self.proc_root, self.yolo_script)
                state = self.yolo_state()
                if state['ActiveState'] == 'active' and self.verified_script_unit(state, pids, YOLO_UNIT):
                    return dict(result, state='started', pids=pids,
                        message='已有只读 YOLO/ArUco 服务已启动；模型及限帧配置不变，检测输出仍需核对')
                if state['ActiveState'] in ('inactive', 'failed'):
                    break
            raise SourceError('未确认 YOLO/ArUco 进程身份，保留现有状态；请检查本地模型及服务日志')
        except (SourceError, OSError, ValueError) as error:
            return dict(result, state='failed', error=str(error))

    def dds_environment(self):
        """Read only non-secret DDS settings as data, not a sourced shell file."""
        keys = DDS_ENV_KEYS
        values = {'ROS_DOMAIN_ID': '0', 'ROS_LOCALHOST_ONLY': '0',
                  'RMW_IMPLEMENTATION': 'rmw_fastrtps_cpp'}
        path = self.home / '.config/robot-cloud/archive.env'
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise SourceError('归档 DDS 配置文件身份无效')
            for line in path.read_text().splitlines():
                key, separator, raw = line.strip().partition('=')
                if not separator or key not in keys:
                    continue
                fields = shlex.split(raw, comments=True)
                if len(fields) > 1:
                    raise SourceError('归档 DDS 配置格式不正确')
                values[key] = fields[0] if fields else ''
        values.update({key: os.environ[key] for key in keys if key in os.environ})
        return values

    def source_command(self,source):
        command=[part.format(workspace=self.workspace,bluesea_params=self.params) for part in source.command]
        if source.key=='mid360' and getattr(self,'mid360_config',None):
            command.append('user_config_path:='+str(self.mid360_config))
        if source.key=='berxel':
            command=self.camera_transport_command(command,asynchronous=True)
        if source.key in ('mid360','stl19p','bluesea'):
            return self.driver_ros_command(command)
        return self.ros_command(command)

    def driver_ros_command(self,command):
        # A newly owned hardware publisher follows exactly the workspace/Nav
        # environment, not a recorder's historical reader/transport profile.
        # systemd's user manager may retain stale settings from another shell.
        values={'ROS_DOMAIN_ID':'0','ROS_LOCALHOST_ONLY':'0','RMW_IMPLEMENTATION':'rmw_fastrtps_cpp'}
        values.update({key:os.environ[key] for key in DDS_ENV_KEYS if key in os.environ})
        return ['/usr/bin/env',*[f'--unset={key}' for key in sorted(DDS_ENV_KEYS)],
            *[key+'='+value for key,value in sorted(values.items())],
            '/bin/bash','-c',LIDAR_ROS_SHELL,'robot-lidar-source',
            str(self.ros_setup),str(self.workspace/'install/setup.bash'),
            str(self.workspace/'env.sh'),*command]

    def lidar_runtime_report(self,source,pid):
        if source.key not in ('mid360','bluesea','stl19p'):
            return {}
        runtime=running_driver_runtime(self.proc_root,pid)
        parameters=runtime['parameters']
        if source.key=='mid360':
            if type(parameters.get('xfer_format')) is not int or parameters['xfer_format']!=1:
                raise SourceError('已有 MID360 不是 xfer_format=1 CustomMsg，保持原驱动且未启动第二份')
            if type(parameters.get('multi_topic')) is not int or parameters['multi_topic']!=0:
                raise SourceError('已有 MID360 话题模式无法确认，保持原驱动')
            # Also require the hardware configuration to remain verifiable.
            config=parameters.get('user_config_path')
            if not isinstance(config,str):
                raise SourceError('已有 MID360 缺少可核验的硬件配置，保持原驱动')
            json.loads(bounded_config(config,131072))
            topics=[driver_topic(topic,runtime) for topic in ('livox/lidar','livox/imu')]
            if topics!=['/livox/lidar','/livox/imu']:
                raise SourceError('已有 MID360 实际话题不是 /livox/lidar 与 /livox/imu，保持原驱动')
        else:
            key='scan_topic' if source.key=='bluesea' else 'topic_name'
            if not isinstance(parameters.get(key),str):
                raise SourceError('已有二维雷达缺少明确的输出话题参数，保持现有进程')
            topics=[driver_topic(parameters[key],runtime)]
            if topics!=[source.topic]:
                raise SourceError('已有二维雷达实际话题与归档话题不同，保持现有进程')
        expected=dds_metadata(self.dds_environment())
        return dict(dds=runtime['dds'],ddsCompatible=runtime['dds']==expected,
            processStartTicks=runtime['processStartTicks'],actualTopics=topics,
            **({'xferFormat':1} if source.key=='mid360' else {}))

    @property
    def camera_transport_profile(self):
        configured = self.dds_environment().get('FASTRTPS_DEFAULT_PROFILES_FILE')
        if configured:
            path = Path(configured)
            if not path.is_absolute():
                raise SourceError('归档 DDS 配置路径必须为绝对路径')
            return path
        return super().camera_transport_profile

    def ros_command(self, command):
        # Explicit exec environment also survives systemd-run, whose service
        # manager does not inherit the SSH helper's arbitrary environment.
        return ['/usr/bin/env', *[key + '=' + value for key, value in sorted(self.dds_environment().items())],
                '/bin/bash', '-c', RECORDING_ROS_SHELL, 'robot-recording-source',
                str(self.ros_setup), str(self.workspace / 'install/setup.bash'),
                str(self.workspace / 'env.sh'), *command]

    def preflight(self, source):
        if source.key != 'mid360':
            return super().preflight(source)
        for path in (self.ros_setup, self.workspace / 'install/setup.bash', self.workspace / 'env.sh'):
            if not path.is_file():
                raise SourceError('缺少运行环境：' + str(path))
        prefix = self.package('livox_ros_driver2', 'livox_ros_driver2_node')
        launch = prefix / 'share/livox_ros_driver2/launch_ROS2/msg_MID360s_launch.py'
        guard = launch.with_name('livox_driver_guard.py')
        if (not launch.is_file() or guard.is_symlink() or not guard.is_file()
                or not os.access(guard, os.R_OK)):
            raise SourceError('缺少 MID360 独立驱动启动文件或共享占用保护；未启动定位算法')
        config = prefix / 'share/livox_ros_driver2/config/MID360s_config.json'
        if config.stat().st_size > 131072:
            raise SourceError('MID360 配置过大')
        data = json.loads(config.read_text())
        addresses = [item['ip'] for item in data['lidar_configs']]
        if len(addresses) != 1:
            raise SourceError('需要唯一明确的 MID360 地址')
        local=self.lidar_route(addresses[0])
        hosts=(data.get('Mid360s') or {}).get('host_net_info')
        if not isinstance(hosts,list) or not hosts or not all(isinstance(host,dict) for host in hosts):
            raise SourceError('MID360 缺少明确的 host_net_info 接收配置')
        endpoints=[]
        for host in hosts:
            host['host_ip']=local
            ports=[value for key,value in host.items() if key.endswith('_port')]
            if not ports or any(type(port) is not int or not 0<port<65536 for port in ports):
                raise SourceError('MID360 UDP 接收端口配置无效')
            endpoints.extend((local,port) for port in ports)
        self.require_free_udp_ports(endpoints)
        path=self.params.with_name('MID360s-recording.json')
        self.private_text(path,json.dumps(data,indent=2)+'\n')
        self.mid360_config=path

    def recording_source(self, source, status=False):
        result = dict(key=source.key, unit=source.unit, topic=source.topic, dataVerified=False)
        try:
            self.check_cancelled()
            pids = exact_processes(self.proc_root, source.driver)
            if len(pids) > 1:
                raise SourceError('已有多个同类驱动，保持现有程序并拒绝重复启动')
            if source.key=='stl19p':
                for blue in exact_processes(self.proc_root,SOURCES['bluesea'].driver):
                    self.lidar_runtime_report(SOURCES['bluesea'],blue)
            if pids:
                return dict(result, state='reused', pids=pids,**self.lidar_runtime_report(source,pids[0]),
                    message='复用已有驱动，不要求或启动 FAST-LIO；数据实收由本次归档另行核验')
            unit = self.unit_state(source.unit)
            if status:
                return dict(result, state='inactive', serviceState=unit['ActiveState'])
            if unit['ActiveState'] not in ('inactive', 'failed'):
                raise SourceError('受管服务已在运行但未确认驱动，保持原状且不启动第二份')
            self.preflight(source)
            self.check_cancelled()
            # A navigation/algorithm launch may have acquired the driver while
            # preflight ran. The MID360 launch also has its lifetime owner guard.
            if exact_processes(self.proc_root, source.driver):
                return self.recording_source(source, status=True)
            if self.run(self.start_command(source)).returncode:
                raise SourceError('设备驱动启动失败')
            for _ in range(6):
                self.sleeper(.5)
                self.check_cancelled()
                pids = exact_processes(self.proc_root, source.driver)
                unit = self.unit_state(source.unit)
                if len(pids) == 1 and unit['ActiveState'] == 'active':
                    return dict(result, state='started', pids=pids,**self.lidar_runtime_report(source,pids[0]),
                        message='仅设备驱动已启动；数据实收由本次归档另行核验')
                if unit['ActiveState'] in ('inactive', 'failed'):
                    break
            raise SourceError('未确认驱动就绪；保留已启动服务，请查看设备日志')
        except (SourceError, OSError, ValueError, KeyError, TypeError) as error:
            return dict(result, state='failed', error=str(error))

    def fisheye_identity(self):
        info = Path(FISHEYE_DEVICE).stat()
        if not stat.S_ISCHR(info.st_mode):
            raise SourceError('固定鱼眼映射不是视频设备，未尝试其他 video 节点')
        return info.st_rdev

    def fisheye_holder(self, device_id):
        evidence = self.run(['fuser', '--', FISHEYE_DEVICE])
        if evidence.returncode not in (0, 1):
            raise SourceError('无法检查鱼眼相机占用，保持现有程序')
        holders = {int(value) for value in evidence.stdout.split() if value.isdecimal()}
        if evidence.returncode == 0 and not holders:
            raise SourceError('鱼眼相机占用无法确认，未启动第二份')
        if not holders:
            return None
        candidates = set(exact_processes(self.proc_root, RECORDING_FISHEYE.driver))
        if len(holders) != 1 or not holders <= candidates:
            raise SourceError('鱼眼相机被其他程序占用，保持现有程序')
        pid = next(iter(holders))
        process = self.proc_root / str(pid)
        argv = (process / 'cmdline').read_bytes().decode(errors='replace').split('\0')
        if '__ns:=/fisheye_front' not in argv:
            raise SourceError('已有 usb_cam 的鱼眼话题身份无法确认，保持原状')
        if not any(stat.S_ISCHR(info.st_mode) and info.st_rdev == device_id
                   for descriptor in (process / 'fd').iterdir()
                   for info in [descriptor.stat()]):
            raise SourceError('已有鱼眼进程的设备句柄无法确认，保持原状')
        return pid

    def fisheye(self, status=False):
        source = RECORDING_FISHEYE
        result = dict(key=source.key, unit=source.unit, topic=source.topic, dataVerified=False)
        try:
            try:
                device_id = self.fisheye_identity()
            except FileNotFoundError:
                return dict(result, state='skipped', message='固定 by-id 鱼眼相机未连接，未猜测其他 video 设备')
            owner = self.load_camera_owner()
            ops = owner.SystemOps()
            with ops.lock():
                # The same audited camera-owner lock serializes UVC/fisheye
                # starts; never open a four-camera device as the fisheye.
                nodes, _missing = ops.camera_nodes()
                if device_id in nodes:
                    raise SourceError('鱼眼与四路共享 UVC 映射重叠，未启动')
                pid = self.fisheye_holder(device_id)
                if pid is not None:
                    return dict(result, state='reused', pids=[pid], message='复用持有固定鱼眼设备及正确话题的相机进程')
                unit = self.unit_state(source.unit)
                if status:
                    return dict(result, state='inactive', serviceState=unit['ActiveState'])
                if unit['ActiveState'] not in ('inactive', 'failed'):
                    raise SourceError('鱼眼服务已在运行但未确认设备句柄，未重复启动')
                for path in (self.ros_setup, self.workspace / 'install/setup.bash', self.workspace / 'env.sh'):
                    if not path.is_file():
                        raise SourceError('缺少鱼眼 ROS 运行环境')
                self.package('usb_cam', 'usb_cam_node_exe')
                package = self.run(self.ros_command(['ros2', 'pkg', 'prefix', 'fisheye_camera']))
                prefix = Path(package.stdout.strip())
                if (package.returncode or not prefix.is_absolute()
                        or not (prefix / 'share/fisheye_camera/launch/fisheye_camera.launch.py').is_file()):
                    raise SourceError('缺少独立鱼眼相机包，未启动检测或控制算法')
                self.check_cancelled()
                if self.fisheye_holder(device_id) is not None:
                    return dict(result, state='reused', message='启动前检测到鱼眼已由其他入口启动，已复用')
                if self.run(self.start_command(source)).returncode:
                    raise SourceError('独立鱼眼驱动启动失败')
                for _ in range(6):
                    self.sleeper(.5)
                    self.check_cancelled()
                    pid = self.fisheye_holder(device_id)
                    if pid is not None:
                        return dict(result, state='started', pids=[pid], message='仅鱼眼驱动已启动，未启动 ArUco/控制算法')
                raise SourceError('未确认鱼眼设备句柄，已启动服务保持原状')
        except Exception as error:
            return dict(result, state='failed', error=str(error))

    def stop_recording_cameras(self, *, dry_run=False):
        """Close camera acquisition and its encoders, keeping ROS/archive alive."""
        if self.role != 'jetson':
            raise SourceError('仅在相机主机关闭相机，未操作本机雷达')
        actions = [('berxel-preview', self.stop_berxel_preview),
                   (YOLO_UNIT.removesuffix('.service'), lambda: self.stop_support(YOLO_UNIT)),
                   ('usb-cameras', self.stop_camera),
                   ('berxel', lambda: self.stop_source(SOURCES['berxel'])),
                   ('fisheye', lambda: self.stop_source(RECORDING_FISHEYE))]
        if dry_run:
            rows = [dict(key=key, state='planned', message='仅关闭相机及依赖的编码/识别服务')
                    for key, _ in actions]
        else:
            # Independent systemd jobs may drain concurrently. No command here
            # targets a radar, navigation process, recorder or upload transport.
            def close(action):
                key, operation = action
                print('正在关闭相机：'+key, file=sys.stderr, flush=True)
                try:
                    return operation()
                except Exception as error:
                    return dict(key=key, state='failed', error=str(error))
            with ThreadPoolExecutor(max_workers=len(actions)) as pool:
                rows = list(pool.map(close, actions))
        return dict(ok=all(row['state'] in ('stopped', 'already_stopped', 'planned') for row in rows),
                    role=self.role, stopCameras=True, recordingOnly=True, dryRun=dry_run,
                    devices=rows, dataVerified=False,
                    message='仅关闭相机及其预览/识别，归档补传、雷达、导航和建图保持运行')

    def shared_capture_enabled(self):
        """Unknown gate state retains sources, including another desktop lease."""
        control = self.home / '.local/state/robot-cloud/garage-control'
        gate = json.loads(bounded_config(control / 'garage.json', 16384))
        if (not isinstance(gate, dict) or gate.get('schemaVersion') != 1
                or gate.get('scope') != 'garage' or type(gate.get('enabled')) is not bool):
            raise SourceError('共享地库采集状态无法确认，已保留相机')
        if gate['enabled']:
            return True
        lease = control / 'desktop-lease.json'
        if lease.exists() or lease.is_symlink():
            value = json.loads(bounded_config(lease, 16384))
            if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
                raise SourceError('其他采集会话状态无法确认，已保留相机')
            if value['enabled']:
                expiry = value.get('expiresAt')
                if type(expiry) not in (int, float) or not math.isfinite(expiry):
                    raise SourceError('其他采集会话有效期无法确认，已保留相机')
                if expiry > time.time():
                    return True
        return False

    def stop_owned_recording_cameras(self, ownership, *, dry_run=False):
        """Release this run's optional UI workers, preserving shared sensor sources.

        A created unit is not exclusive ownership: navigation or a calibration
        task may have subscribed after collection started. Discovery queries
        cannot prove their absence, so all hardware camera sources stay alive.
        """
        if self.role != 'jetson':
            raise SourceError('仅在相机主机核对本次相机归属')
        payload = camera_ownership_payload(ownership)
        operations = {
            BERXEL_PREVIEW_UNIT: self.stop_berxel_preview,
            YOLO_UNIT: lambda: self.stop_support(YOLO_UNIT),
        }
        rows = []
        leases = {row['unit']: row for row in payload['units']}
        for unit, key in CAMERA_OWNERSHIP_KEYS.items():
            row = dict(key=key, unit=unit, dataVerified=False)
            lease = leases.get(unit)
            if lease is None:
                rows.append(dict(row, state='retained', reason='not_created_by_capture'))
                continue
            if unit in SHARED_CAMERA_SOURCE_UNITS:
                rows.append(dict(row, state='retained', reason='shared_source_may_be_in_use',
                    message='保留共享相机源，避免中断随后接入的导航、建图或标定'))
                continue
            if dry_run:
                rows.append(dict(row, state='retained', reason='dry_run'))
                continue
            try:
                if self.shared_capture_enabled():
                    rows.append(dict(row, state='retained', reason='shared_capture_enabled'))
                    continue
                state = self.camera_unit_identity(unit)
                if state.get('LoadState') == 'not-found' or state.get('ActiveState') in ('inactive', 'failed'):
                    rows.append(dict(row, state='already_stopped'))
                    continue
                actual = dict(unit=unit, invocationId=state.get('InvocationID'),
                    mainPid=int(state.get('MainPID', '0')),
                    execMainStartTimestampMonotonic=state.get('ExecMainStartTimestampMonotonic'))
                if state.get('ActiveState') != 'active' or actual != lease:
                    rows.append(dict(row, state='retained', reason='camera_identity_changed'))
                    continue
                dependencies_clear = (all(state.get(key) == '' for key in CAMERA_STOP_DEPENDENCIES
                    if key != 'Requires') and isinstance(state.get('Requires'), str)
                    and set(state['Requires'].split()) <= {'app.slice', 'basic.target'})
                if not dependencies_clear:
                    rows.append(dict(row, state='retained', reason='camera_stop_dependencies_unconfirmed'))
                    continue
                # Recheck the shared gate immediately before the exact stop.
                if self.shared_capture_enabled():
                    rows.append(dict(row, state='retained', reason='shared_capture_enabled'))
                    continue
                stopped = operations[unit]()
                if stopped.get('state') == 'external_running':
                    stopped = dict(stopped, state='retained', reason='external_running')
                rows.append({**row, **stopped})
            except (SourceError, OSError, ValueError, TypeError) as error:
                rows.append(dict(row, state='retained', reason='camera_ownership_unconfirmed',
                                 message=str(error)))
        return dict(ok=all(row['state'] in ('stopped', 'already_stopped', 'retained') for row in rows),
            role=self.role, stopOwnedCameras=True, recordingOnly=True, dryRun=dry_run,
            devices=rows, dataVerified=False,
            message='采集已结束；共享相机源及无法确认独占的设备保持运行')

    def preview_only(self, *, status=False, dry_run=False):
        """Open existing camera sources and previews without enabling recording."""
        if self.role != 'jetson':
            raise SourceError('仅相机预览模式只适用于 Jetson 相机端')
        if dry_run:
            rows = [dict(key=unit.removesuffix('.service'), unit=unit, state='planned',
                         command=['systemctl', '--user', 'start', unit], dataVerified=False)
                    for unit in CAMERA_SUPPORT]
            rows += [dict(key='berxel', state='planned', dataVerified=False,
                          command=self.start_command(SOURCES['berxel'])),
                     dict(key='berxel-preview', state='planned', dataVerified=False,
                          command=self.preview_start_command()),
                     dict(key='berxel-frames', state='planned', dataVerified=False,
                          message='只读确认连续彩色、深度及网页预览帧'),
                     dict(key='usb-cameras', state='planned', dataVerified=False,
                          message='在现有占用锁内复用健康共享相机或启动一份受管实例'),
                     dict(key=YOLO_UNIT.removesuffix('.service'), state='planned', dataVerified=False,
                          command=['systemctl', '--user', 'start', YOLO_UNIT],
                          message='共享相机通过健康检查后复用或启动现有只读识别服务'),
                     dict(key='fisheye', state='planned', dataVerified=False,
                          message='仅固定 by-id 鱼眼连接且占用检查通过时启动，未连接则跳过')]
            return dict(ok=True, role=self.role, camerasOnly=True, dryRun=True, status=status,
                        partial=False, cancelled=False, devices=rows, dataVerified=False,
                        message='仅预演相机、预览与识别；不启动归档、雷达或修改保存开关')

        rows, interrupted = [], False

        def source_ready(key):
            return any(row['key'] == key and row['state'] in ('started', 'reused') for row in rows)

        def verified_partial_camera(row):
            return (row['key'] == 'usb-cameras' and row['state'] == 'partial'
                    and row.get('dataVerified') is True and bool(row.get('dataVerifiedFor'))
                    and bool(row.get('missingCameras')))

        def berxel():
            row = self.recording_source(SOURCES['berxel'], status)
            if row['state'] in ('started', 'reused'):
                row = dict(row, message='Berxel 驱动正在运行；随后检查彩色与深度连续画面')
            return row

        def usb_cameras():
            row = self.camera(status, before_start=self.check_cancelled, require_archive=False)
            if row['state'] in ('started', 'reused') and row.get('dataVerified') is True:
                row = dict(row, message='共享相机取流已通过健康检查')
            return row

        def preview():
            return self.berxel_preview(status) if source_ready('berxel') else dict(
                key='berxel-preview', state='skipped', dataVerified=False,
                message='Berxel 驱动未就绪，未启动预览；其他相机继续检查')

        def frames():
            ready = source_ready('berxel') and source_ready('berxel-preview')
            return self.berxel_frames() if ready else dict(key='berxel-frames', state='skipped',
                dataVerified=False, message='驱动或预览未就绪，尚未确认连续图像')

        def yolo():
            ready = any(row['key'] == 'usb-cameras' and row.get('dataVerified') is True
                        and (row['state'] in ('started', 'reused') or verified_partial_camera(row))
                        for row in rows)
            return self.camera_yolo(status) if ready else dict(
                key=YOLO_UNIT.removesuffix('.service'), state='skipped', dataVerified=False,
                message='共享相机未通过健康检查，未启动识别；其他相机继续检查')

        actions = [(unit.removesuffix('.service'), lambda unit=unit: self.support(unit, status))
                   for unit in CAMERA_SUPPORT]
        actions += [('berxel', berxel),
                    ('berxel-preview', preview), ('berxel-frames', frames),
                    ('usb-cameras', usb_cameras),
                    (YOLO_UNIT.removesuffix('.service'), yolo),
                    ('fisheye', lambda: self.fisheye(status))]
        try:
            for key, action in actions:
                self.check_cancelled()
                print('相机设备：' + key, file=sys.stderr, flush=True)
                try:
                    rows.append(action())
                except Exception as error:
                    rows.append(dict(key=key, state='failed', dataVerified=False, error=str(error)))
        except RecordingStartCancelled:
            interrupted = True
            done = {row['key'] for row in rows}
            rows += [dict(key=key, state='failed', dataVerified=False,
                          error='启动请求已撤销，后续相机未启动，已有设备保持运行')
                     for key, _action in actions if key not in done]
        available = [row['key'] for row in rows
                     if row['state'] in ('started', 'reused') or verified_partial_camera(row)]
        complete = not interrupted and all(
            row['state'] in ('started', 'reused') or verified_partial_camera(row)
            or (row['key'] == 'fisheye' and row['state'] == 'skipped') for row in rows)
        partial = any(verified_partial_camera(row) for row in rows) or bool(available and not complete)
        return dict(ok=complete, role=self.role, camerasOnly=True, dryRun=False, status=status,
                    partial=partial, cancelled=interrupted, available=available,
                    devices=rows, dataVerified=False,
                    message='仅开启或复用相机、预览与只读识别；不启动归档、雷达或修改保存开关。'
                            '未连接或失败的相机见逐项报告，其余相机保持运行。'
                            '已有录制按原保存开关继续，未由本入口停止。')

    def inspect_recording_sources(self):
        """Lock this invocation to read-only local radar status, even without --status."""
        if self.role != 'ipc':
            raise SourceError('只读雷达检查仅适用于 ipc')
        self._read_only_sources = True
        result = self.recording_only(status=True)
        return dict(result, readOnlySources=True, sourceControl=False)

    def recording_only(self, *, status=False, dry_run=False, no_camera_source=False,
                       with_camera_preview=False, with_camera_yolo=False):
        if with_camera_preview and (self.role != 'jetson' or no_camera_source):
            raise SourceError('相机预览需使用 jetson 采集模式，且不能跳过相机数据源')
        if with_camera_yolo and (self.role != 'jetson' or no_camera_source):
            raise SourceError('相机识别需使用 jetson 采集模式，且不能跳过相机数据源')
        names = (RECORDING_MID360, SOURCES['stl19p'], SOURCES['bluesea']) if self.role == 'ipc' else (SOURCES['berxel'],)
        if dry_run:
            rows = [dict(key=source.key, state='planned', command=self.start_command(source), dataVerified=False)
                    for source in names if not (self.role == 'jetson' and no_camera_source)]
            if self.role == 'jetson':
                rows += [dict(key='robot-native-archive', state='planned',
                    command=['systemctl', '--user', 'start', 'robot-native-archive.service'])]
                if not no_camera_source:
                    if with_camera_preview:
                        rows += [dict(key='berxel-preview', state='planned',
                            command=self.preview_start_command(), dataVerified=False),
                                 dict(key='berxel-frames', state='planned', dataVerified=False,
                            message='只读检查连续彩色、深度及网页预览帧，不重启设备')]
                    rows.append(dict(key='usb-cameras', state='planned', message='复用健康共享 UVC 或在既有占用锁内启动'))
                    if with_camera_yolo:
                        rows.append(dict(key=YOLO_UNIT.removesuffix('.service'), state='planned',
                            command=['systemctl', '--user', 'start', YOLO_UNIT], dataVerified=False,
                            message='共享相机可用后恢复现有只读 YOLO/ArUco 服务，复用原模型及限帧配置'))
                    rows.append(dict(key='fisheye', state='planned', message='仅固定 by-id 映射与占用检查均通过时启动'))
            return dict(ok=True, role=self.role, recordingOnly=True, dryRun=True,
                partial=False, devices=rows, dataVerified=False, cameraOwnership=self.camera_ownership())
        rows, interrupted = [], False
        actions = [(source.key, lambda source=source: self.recording_source(source, status)) for source in names]
        if self.role == 'jetson':
            actions = [('robot-native-archive', lambda: self.support('robot-native-archive.service', status))] + actions
            if no_camera_source:
                actions = actions[:1]
            else:
                if with_camera_preview:
                    def preview():
                        ready = any(row['key'] == 'berxel' and row['state'] in ('started', 'reused')
                                    for row in rows)
                        return self.berxel_preview(status) if ready else dict(key='berxel-preview',
                            state='skipped', dataVerified=False,
                            message='Berxel 驱动未就绪，未启动网页预览；其他数据源可继续录制')
                    def frames():
                        ready = all(any(row['key'] == key and row['state'] in ('started', 'reused')
                                        for row in rows) for key in ('berxel', 'berxel-preview'))
                        return self.berxel_frames() if ready else dict(key='berxel-frames',
                            state='skipped', dataVerified=False,
                            message='驱动或预览未就绪，尚未确认网页连续出图；原始录制独立进行')
                    actions += [('berxel-preview', preview), ('berxel-frames', frames)]
                actions.append(('usb-cameras', lambda: self.camera(status, before_start=self.check_cancelled)))
                if with_camera_yolo:
                    def yolo():
                        ready = any(row['key'] == 'usb-cameras'
                            and (row['state'] in ('started', 'reused')
                                 or (row['state'] == 'partial' and row.get('dataVerified') is True
                                     and row.get('dataVerifiedFor'))) for row in rows)
                        return self.camera_yolo(status) if ready else dict(
                            key=YOLO_UNIT.removesuffix('.service'), state='skipped', dataVerified=False,
                            message='共享相机未就绪，未启动 YOLO/ArUco；其他数据源可继续录制')
                    actions.append((YOLO_UNIT.removesuffix('.service'), yolo))
                actions.append(('fisheye', lambda: self.fisheye(status)))
        try:
            for key, action in actions:
                self.check_cancelled()
                print('采集设备：' + key, file=sys.stderr, flush=True)
                rows.append(action())
                if self.role == 'jetson' and not status:
                    self.remember_camera_ownership()
        except RecordingStartCancelled:
            interrupted = True
            done = {row['key'] for row in rows}
            rows += [dict(key=key, state='failed', dataVerified=False,
                          error='启动请求已撤销，后续设备未启动，已有设备保持运行')
                     for key, _action in actions if key not in done]
        complete = all(row['state'] in ('started', 'reused') for row in rows) and not interrupted
        available = [row['key'] for row in rows if row['state'] in ('started', 'reused', 'partial')]
        return dict(ok=complete, role=self.role, recordingOnly=True, status=status, dryRun=False,
            cancelled=interrupted, partial=bool(available and not complete), available=available,
            devices=rows, dataVerified=False, cameraOwnership=self.camera_ownership(),
            message=('仅补齐采集驱动及源端归档；未操作导航、网页开关或重启已有设备。'
                     + ('按请求恢复只读相机预览；预览失败不停止原始录制。' if with_camera_preview else '')
                     + ('按请求恢复现有只读 YOLO/ArUco 服务；识别失败不停止原始录制。' if with_camera_yolo else '')
                     + '部分源离线时其余源可继续记录。'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('ipc', 'jetson'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--status', action='store_true')
    mode.add_argument('--stop', action='store_true')
    mode.add_argument('--stop-recording-cameras', action='store_true', help='结束桌面采集后仅关闭全部相机及其编码/识别')
    mode.add_argument('--stop-owned-recording-cameras', action='store_true',
                        help='仅收尾本次启动的专属预览；共享相机源保持运行')
    parser.add_argument('--camera-ownership-json', help='本次启动报告中的相机归属记录，最大16KiB')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-camera-source', action='store_true')
    parser.add_argument('--cameras-only', action='store_true', help='Jetson 相机/识别模式，不启动或关闭原生归档')
    parser.add_argument('--recording-only', action='store_true', help='仅补齐采集设备，复用已有驱动；默认不启动定位、识别或预览')
    parser.add_argument('--read-only-sources', action='store_true',
                        help='仅用于 ipc --recording-only，只读检查雷达，禁止启动或关闭数据源')
    parser.add_argument('--with-camera-preview', action='store_true',
                        help='Jetson 独立采集时额外开启只读低负载相机预览，不启动定位算法')
    parser.add_argument('--with-camera-yolo', action='store_true',
                        help='Jetson 独立采集时恢复现有只读 YOLO/ArUco 服务，复用已部署模型和限帧配置')
    args = parser.parse_args(argv)
    if args.read_only_sources:
        if (args.role != 'ipc' or not args.recording_only or args.stop
                or args.stop_recording_cameras or args.stop_owned_recording_cameras
                or args.camera_ownership_json is not None or args.dry_run
                or args.no_camera_source or args.cameras_only
                or args.with_camera_preview or args.with_camera_yolo):
            parser.error('--read-only-sources 仅能用于 ipc --recording-only [--status]，不能组合设备或相机操作')
        args.status = True
    if args.status and args.dry_run:
        parser.error('--status 和 --dry-run 不能同时使用')
    if args.cameras_only and args.role != 'jetson':
        parser.error('--cameras-only 仅适用于 jetson')
    if args.stop_recording_cameras and (args.role != 'jetson' or args.recording_only or args.cameras_only or args.no_camera_source):
        parser.error('--stop-recording-cameras 仅能单独用于 jetson')
    if args.stop_owned_recording_cameras and (args.role != 'jetson' or args.recording_only
            or args.cameras_only or args.no_camera_source):
        parser.error('--stop-owned-recording-cameras 仅能单独用于 jetson')
    if bool(args.camera_ownership_json is not None) != bool(args.stop_owned_recording_cameras):
        parser.error('--camera-ownership-json 需与 --stop-owned-recording-cameras 同时使用')
    if args.recording_only and (args.stop or args.cameras_only):
        parser.error('--recording-only 不允许关闭设备或同时指定 --cameras-only')
    if args.with_camera_preview and (args.role != 'jetson' or not args.recording_only or args.no_camera_source):
        parser.error('--with-camera-preview 仅能用于 jetson --recording-only，且不能跳过相机数据源')
    if args.with_camera_yolo and (args.role != 'jetson' or not args.recording_only or args.no_camera_source):
        parser.error('--with-camera-yolo 仅能用于 jetson --recording-only，且不能跳过相机数据源')
    if args.stop_owned_recording_cameras:
        try:
            result = RecordingDeviceSources(args.role).stop_owned_recording_cameras(
                args.camera_ownership_json, dry_run=args.dry_run)
        except SourceError as error:
            result = dict(ok=False, role=args.role, stopOwnedCameras=True,
                          devices=[], dataVerified=False, error=str(error))
    elif args.stop_recording_cameras:
        result = RecordingDeviceSources(args.role).stop_recording_cameras(dry_run=args.dry_run)
    elif args.recording_only or (args.cameras_only and not args.stop and not args.no_camera_source):
        def cancel(_signal, _frame):
            raise RecordingStartCancelled()
        previous = {number: signal.signal(number, cancel) for number in (signal.SIGTERM, signal.SIGINT)}
        try:
            manager = RecordingDeviceSources(args.role)
            result = (manager.inspect_recording_sources() if args.read_only_sources else
                      manager.preview_only(status=args.status, dry_run=args.dry_run)
                      if args.cameras_only else manager.recording_only(status=args.status,
                          dry_run=args.dry_run, no_camera_source=args.no_camera_source,
                          with_camera_preview=args.with_camera_preview, with_camera_yolo=args.with_camera_yolo))
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    else:
        result = DeviceSources(args.role).execute(status=args.status, dry_run=args.dry_run,
                                                no_camera_source=args.no_camera_source, stop=args.stop,
                                                cameras_only=args.cameras_only)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
