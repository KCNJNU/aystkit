"""Align only an idle archive subscriber with an already verified radar graph.

Sensor publishers and algorithm processes are never restarted or reconfigured.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlsplit

from .archive_core import GarageGate

UNIT = 'robot-native-archive.service'
GRAPH_KEYS = ('ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY', 'RMW_IMPLEMENTATION',
    'ROS_DISCOVERY_SERVER', 'ROS_NAMESPACE', 'FASTRTPS_DEFAULT_PROFILES_FILE',
    'FASTDDS_DEFAULT_PROFILES_FILE', 'CYCLONEDDS_URI', 'MID360_NAV2_DDS_SERVER')


class RecordingGraphError(RuntimeError):
    pass


def _regular(path, limit=262144):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022 or info.st_size > limit):
            raise RecordingGraphError('雷达 DDS 配置文件身份或权限无法确认，未修改现有设备')
        return stream.read(limit + 1)


def _profile(path, digest=None):
    target = Path(path)
    if not target.is_absolute():
        raise RecordingGraphError('雷达 DDS 配置必须为本机绝对路径，未修改现有设备')
    raw = _regular(target)
    actual = hashlib.sha256(raw).hexdigest()
    if digest is not None and actual != digest:
        raise RecordingGraphError('雷达 DDS 配置在检查期间已改变，请重试采集')
    return str(target), actual


def _private_environment(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RecordingGraphError('归档环境文件必须为当前用户的 0600 私有文件')
    return _regular(path, 65536)


def _desired(graph):
    domain = str(graph.get('domain', '0'))
    if not domain.isascii() or not domain.isdecimal() or not 0 <= int(domain) <= 232:
        raise RecordingGraphError('雷达 DDS 域无效，未修改现有设备')
    rmw = graph.get('rmw') or 'rmw_fastrtps_cpp'
    if rmw not in ('rmw_fastrtps_cpp', 'rmw_cyclonedds_cpp'):
        raise RecordingGraphError('雷达使用了未支持的 ROS 中间件，未修改现有设备')
    local = graph.get('localhostOnly', False)
    if type(local) is not bool:
        raise RecordingGraphError('雷达 DDS 本机模式无法确认')
    result = dict(ROS_DOMAIN_ID=str(int(domain)), ROS_LOCALHOST_ONLY='1' if local else '0',
        RMW_IMPLEMENTATION=rmw, ROS_DISCOVERY_SERVER=graph.get('discoveryServer', ''),
        ROS_NAMESPACE=graph.get('namespace', ''), FASTRTPS_DEFAULT_PROFILES_FILE='',
        FASTDDS_DEFAULT_PROFILES_FILE='', CYCLONEDDS_URI='', MID360_NAV2_DDS_SERVER='off')
    profile = graph.get('profilePath', '')
    if profile:
        digest = graph.get('profileSha256')
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
            raise RecordingGraphError('雷达 DDS 配置缺少有效校验值')
        key = graph.get('profileEnvironmentKey') or 'FASTRTPS_DEFAULT_PROFILES_FILE'
        if key not in ('FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_DEFAULT_PROFILES_FILE'):
            raise RecordingGraphError('雷达 DDS 配置来源无法确认')
        result[key], _ = _profile(profile, digest)
    cyclone = graph.get('cycloneUri', '')
    if cyclone:
        parsed = urlsplit(cyclone)
        if parsed.scheme not in ('', 'file') or parsed.netloc or parsed.query or parsed.fragment:
            raise RecordingGraphError('雷达 Cyclone DDS 配置不是受支持的本机文件')
        path, _ = _profile(unquote(parsed.path))
        result['CYCLONEDDS_URI'] = Path(path).as_uri()
    if any(not isinstance(value, str) or len(value) > 4096
           or any(ord(char) < 32 or ord(char) == 127 for char in value) for value in result.values()):
        raise RecordingGraphError('雷达 DDS 配置格式无效')
    return result


def _environment(pid, proc_root):
    directory = Path(proc_root) / str(pid)
    if directory.stat().st_uid != os.getuid():
        raise RecordingGraphError('无法确认归档或雷达进程归属，未修改服务')
    result = {}
    for entry in (directory / 'environ').read_bytes().split(b'\0'):
        key, separator, value = entry.partition(b'=')
        name = key.decode(errors='replace')
        if separator and name in GRAPH_KEYS:
            result[name] = value.decode(errors='strict')
    return result


def _same_graph(actual, desired):
    defaults = dict(ROS_DOMAIN_ID='0', ROS_LOCALHOST_ONLY='0', RMW_IMPLEMENTATION='rmw_fastrtps_cpp')
    def normalized(key, value):
        if key == 'ROS_DOMAIN_ID':
            try:return str(int(value or '0'))
            except ValueError:return 'invalid'
        if key == 'ROS_LOCALHOST_ONLY':return '1' if value.lower() in ('1', 'true') else '0'
        if key == 'ROS_NAMESPACE':return value.strip('/')
        if key == 'CYCLONEDDS_URI' and value:
            parsed = urlsplit(value)
            return unquote(parsed.path)
        return value
    # MID360_NAV2_DDS_SERVER is a launch helper choice, not an active DDS field.
    actual_profile = actual.get('FASTDDS_DEFAULT_PROFILES_FILE') or actual.get('FASTRTPS_DEFAULT_PROFILES_FILE') or ''
    desired_profile = desired.get('FASTDDS_DEFAULT_PROFILES_FILE') or desired.get('FASTRTPS_DEFAULT_PROFILES_FILE') or ''
    return actual_profile == desired_profile and all(normalized(key, actual.get(key, defaults.get(key, '')))
        == normalized(key, desired[key]) for key in GRAPH_KEYS if key not in
            ('MID360_NAV2_DDS_SERVER','FASTRTPS_DEFAULT_PROFILES_FILE','FASTDDS_DEFAULT_PROFILES_FILE'))


def _idle(control, clock, after=None):
    if GarageGate(Path(control) / 'garage.json').enabled():
        raise RecordingGraphError('采集已经开启，未改变归档 DDS 或现有设备')
    value = json.loads(_regular(Path(control) / 'status.json').decode())
    from datetime import datetime
    stamp = datetime.fromisoformat(value.get('updatedAt', '').replace('Z', '+00:00')).timestamp()
    ros, store = value.get('ros') or {}, value.get('store') or {}
    if (not -2 <= clock() - stamp <= 8 or after is not None and stamp < after
            or ros.get('active') is not False or store.get('active') != {}
            or any(type(ros.get(key)) is not int or ros[key] != 0 for key in
                ('queueMessages','queueBytes','inFlightMessages','subscriptionCleanupPending'))):
        raise RecordingGraphError('归档空闲状态尚未确认，未改变现有设备')
    resources = store.get('resourceMonitor')
    if isinstance(resources, dict) and resources.get('state') not in (None, 'READY'):
        raise RecordingGraphError('归档资源检查尚未就绪，请稍后重试')


def _write_env(path, content, *, expected):
    descriptor, temporary = tempfile.mkstemp(prefix='.archive-dds-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        if _private_environment(path) != expected:
            raise RecordingGraphError('归档配置已被其他操作修改，未覆盖新配置')
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def ensure_recording_dds(values, control, startup_report, *, runner=subprocess.run,
                         proc_root='/proc', clock=time.time, sleeper=time.sleep, timeout=30):
    rows = (((startup_report or {}).get('hosts') or {}).get('ipc') or {}).get('devices') or []
    candidates = [row for row in rows if row.get('key') == 'mid360'
        and row.get('state') in ('started','reused') and isinstance(row.get('dds'), dict)]
    if not candidates:
        return dict(state='unchanged', reason='no_verified_driver_graph')
    if len(candidates) != 1:
        raise RecordingGraphError('存在多个雷达图配置，未改变现有设备')
    row = candidates[0]
    pids = row.get('pids') or []
    if len(pids) != 1 or type(pids[0]) is not int or pids[0] <= 0:
        raise RecordingGraphError('雷达进程身份无法确认')
    expected_start = row.get('processStartTicks') or row['dds'].get('processStartTicks')
    if expected_start is None or not str(expected_start).isdecimal():
        raise RecordingGraphError('雷达进程缺少开始时间证明，请重新检查设备')
    desired = _desired(row['dds'])
    cyclone_path = unquote(urlsplit(desired['CYCLONEDDS_URI']).path) if desired['CYCLONEDDS_URI'] else None
    cyclone_sha = _profile(cyclone_path)[1] if cyclone_path else None
    def verify_driver():
        driver = Path(proc_root) / str(pids[0])
        try:
            arguments = (driver / 'cmdline').read_bytes().split(b'\0')
            started = (driver / 'stat').read_text().rsplit(')',1)[1].split()[19]
            if not arguments or Path(os.fsdecode(arguments[0])).name != 'livox_ros_driver2_node' or started != str(expected_start):
                raise RecordingGraphError('雷达进程已改变或重启，请重试采集')
            if _desired(row['dds']) != desired or not _same_graph(_environment(pids[0], proc_root), desired):
                raise RecordingGraphError('雷达 DDS 配置在检查期间已改变，请重试采集')
            if cyclone_path and _profile(cyclone_path)[1] != cyclone_sha:
                raise RecordingGraphError('雷达 Cyclone DDS 配置在检查期间已改变，请重试采集')
            if (driver / 'stat').read_text().rsplit(')',1)[1].split()[19] != started:
                raise RecordingGraphError('雷达进程在检查期间已重启')
        except (FileNotFoundError, ProcessLookupError, IndexError) as error:
            raise RecordingGraphError('雷达进程已退出，未启动第二份驱动') from error
    verify_driver()
    def pid():
        result = runner(['systemctl','--user','show',UNIT,'--property=MainPID','--value'],
            capture_output=True,text=True,timeout=10)
        value = result.stdout.strip()
        if result.returncode or not value.isdecimal() or int(value) <= 0:
            raise RecordingGraphError('归档辅助服务未就绪')
        return int(value)
    archive_pid = pid()
    _idle(control, clock)
    if _same_graph(_environment(archive_pid, proc_root), desired):
        verify_driver()
        return dict(state='unchanged', reason='already_aligned', domain=desired['ROS_DOMAIN_ID'])
    path = Path(values.get('ARCHIVE_ENV_FILE', str(Path.home()/'.config/robot-cloud/archive.env')))
    before = _private_environment(path)
    lines = [line for line in before.decode().splitlines() if line.partition('=')[0].strip() not in desired]
    after = ('\n'.join(lines + [key+'='+shlex.quote(value) for key,value in desired.items()])+'\n').encode()
    print('正在让空闲归档接收器加入现有雷达的 DDS 图；雷达、建图和导航进程保持运行。', flush=True)
    try:
        verify_driver()
        _write_env(path, after, expected=before)
        begin = clock()
        result = runner(['systemctl','--user','restart',UNIT],capture_output=True,text=True,timeout=90)
        if result.returncode:raise RecordingGraphError('归档 DDS 对齐重启失败')
        deadline = clock()+timeout
        while clock() < deadline:
            verify_driver()
            if _private_environment(path) != after:
                raise RecordingGraphError('归档配置已被其他操作修改，未确认对齐')
            try:
                current = pid()
                if not _same_graph(_environment(current, proc_root), desired):
                    raise RecordingGraphError('归档 DDS 配置尚未生效')
                _idle(control, clock, after=begin)
                verify_driver()
                if _private_environment(path) != after:
                    raise RecordingGraphError('归档配置已被其他操作修改，未确认对齐')
                return dict(state='aligned', domain=desired['ROS_DOMAIN_ID'], restartedUnit=UNIT,
                    driverPid=pids[0], driverUnchanged=True)
            except (OSError, ValueError, RecordingGraphError):
                sleeper(.25)
        raise RecordingGraphError('归档 DDS 对齐未获确认，原有设备保持运行')
    except BaseException:
        restored = False
        try:
            if _private_environment(path) == after:
                _write_env(path, before, expected=after)
                restored = True
        except (OSError, RecordingGraphError):pass
        # Revert only our idle helper. Never restart another owner's capture.
        try:
            if restored:
                _idle(control, clock)
                runner(['systemctl','--user','restart',UNIT],capture_output=True,text=True,timeout=90)
        except (OSError, ValueError, RecordingGraphError, subprocess.SubprocessError):pass
        raise
