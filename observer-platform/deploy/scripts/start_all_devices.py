#!/usr/bin/env python3
"""Camera-first manual startup; explicit full-stack mode and legacy safe shutdown."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

WAREHOUSE_SERVICES = ('postgres', 'redis', 'minio', 'backend', 'worker', 'web')
CLOUD_WAREHOUSE_UNIT = 'robot-warehouse-cloud-api-tunnel.service'
CLOUD_WAREHOUSE_API = 'http://127.0.0.1:13000/api'
LEGACY_WAREHOUSE_TUNNEL = 'robot-cloud-dbprog-tunnel.service'
LOCAL_UNITS = (
    'robot-native-archive.service',
    'robot-native-archive-source-link.service',
    'robot-cloud-collector.service',
    'robot-cloud-chassis-telemetry.service',
    'robot-cloud-dbprog-tunnel.service',
)
CAMERA_IDS = ('usbFront', 'usbLeft', 'usbHub2', 'usbHub3')
TUNNEL_UNITS = tuple(f'robot-cloud-native-{side}-tunnel@{key}.service'
                     for key in CAMERA_IDS for side in ('source', 'cloud'))
CAMERA_LOCAL_UNITS = ('robot-cloud-collector.service',) + TUNNEL_UNITS
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
from shared.preview_health import read_preview_health


class StartupError(RuntimeError):
    pass


def cloud_warehouse_config():
    """A private cutover marker disables every legacy warehouse start path.

    Invalid markers fail closed. They must never silently select the old
    writable database after a cloud migration.
    """
    path = Path.home() / '.config/robot-cloud/warehouse-cloud.json'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StartupError('云数仓迁移标记无法安全读取；未回退旧仓') from error
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > 16384):
            raise ValueError('private marker required')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            value = json.loads(stream.read(16385))
        if (not isinstance(value, dict) or type(value.get('schemaVersion')) is not int
                or value.get('schemaVersion') != 1 or value.get('mode') != 'cloud'
                or value.get('apiUrl') != CLOUD_WAREHOUSE_API
                or value.get('publicUrl') != 'https://aystkit.cn/dbprog/'
                or value.get('unit') != CLOUD_WAREHOUSE_UNIT):
            raise ValueError('unsupported cloud marker')
        return value
    except (OSError, ValueError, TypeError) as error:
        raise StartupError('云数仓迁移标记无效；未启动或停止旧仓，请核对私有配置') from error
    finally:
        os.close(descriptor)


class Runner:
    def run(self, args, **kwargs):
        env = dict(os.environ)
        env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
        env.setdefault('DBUS_SESSION_BUS_ADDRESS', f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
        return subprocess.run(args, text=True, capture_output=True,
                              timeout=kwargs.pop('timeout', 30), env=env, **kwargs)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_ready(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def connection_options(path):
    """Parse systemd-style KEY=value connection fields without evaluating shell."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, raw = line.partition('=')
        if not sep or key.strip() not in {
            'VIDEO_SOURCE_HOST', 'VIDEO_SOURCE_NAME', 'VIDEO_SOURCE_USER',
            'VIDEO_SOURCE_KEY', 'VIDEO_SOURCE_KNOWN_HOSTS', 'VIDEO_SOURCE_HOST_KEY_ALIAS',
        }:
            continue
        parts = shlex.split(raw, comments=True)
        if len(parts) != 1:
            raise StartupError('Jetson 连接配置格式不正确')
        values[key.strip()] = parts[0]
    host = values.get('VIDEO_SOURCE_HOST', 'auto')
    if host == 'auto':
        host = values.get('VIDEO_SOURCE_NAME', 'nvidia-desktop.local')
    user = values.get('VIDEO_SOURCE_USER', 'nvidia')
    alias = values.get('VIDEO_SOURCE_HOST_KEY_ALIAS', '192.168.1.50')
    if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', v) for v in (host, user, alias)):
        raise StartupError('Jetson 主机或用户配置不正确')
    key = values.get('VIDEO_SOURCE_KEY', str(Path.home() / '.ssh/id_ed25519'))
    known = values.get('VIDEO_SOURCE_KNOWN_HOSTS', str(Path.home() / '.ssh/known_hosts_jetson_video'))
    if not all(Path(v).is_absolute() and Path(v).is_file() for v in (key, known)):
        raise StartupError('Jetson SSH 密钥或已验证主机文件缺失')
    return ['ssh', '-4', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes',
            '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no',
            '-o', 'KbdInteractiveAuthentication=no', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'HostKeyAlias=' + alias,
            '-o', 'UserKnownHostsFile=' + known, '-o', 'ClearAllForwardings=yes',
            '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'ConnectTimeout=8',
            '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
            '-i', key, user + '@' + host]


@contextmanager
def startup_lock(*, wait=False, on_wait=None):
    directory = Path.home() / '.local/state/robot-cloud'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise StartupError('启动状态目录所有者或权限不正确')
    fd = os.open(directory / 'start-all-devices.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise StartupError('启动锁文件不正确')
        next_notice = 0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    raise StartupError('已有一次启动或关闭正在执行，请等待完成') from None
                if on_wait is not None and time.monotonic() >= next_notice:
                    on_wait('关闭请求已接收；等待当前设备操作结束后自动继续，请保留此窗口。')
                    next_notice = time.monotonic() + 15
                time.sleep(.5)
        yield
    finally:
        os.close(fd)


class Launcher:
    def __init__(self, args, runner=None):
        self.args = args
        self.runner = runner or Runner()
        self.failures = []
        self.partial_devices = set()
        self.depth_source_ready = False
        self.db = Path(os.environ.get('DBPROG_DIR', str(Path.home() / 'DBProg/RobotHygienism')))

    def say(self, message):
        print(message, flush=True)

    def check(self, args, **kwargs):
        result = self.runner.run(args, **kwargs)
        if result.returncode:
            raise StartupError('命令失败：' + shlex.join(args[:5]) + f'（退出码 {result.returncode}）')
        return result

    def step(self, label, action):
        verb = '检查' if self.args.status else '关闭' if self.args.stop else '启动'
        self.say(f'正在{verb}：{label}')
        try:
            action()
        except (StartupError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.failures.append(label)
            self.say(f'失败：{label}：{exc}')

    def warehouse(self):
        cloud = cloud_warehouse_config()
        if cloud is not None:
            self.unit(CLOUD_WAREHOUSE_UNIT)
            deadline = time.monotonic() + (0 if self.args.status else self.args.ready_timeout)
            while not http_ready(CLOUD_WAREHOUSE_API.removesuffix('/api') + '/ready'):
                if time.monotonic() >= deadline:
                    raise StartupError('云数仓 API 暂不可达；本机采集缓存保留，旧仓保持停用')
                time.sleep(1)
            self.say('就绪：云数仓 API；历史数据与账号由云端提供')
            return
        if not (self.db / 'docker-compose.yml').is_file() or not (self.db / '.env').is_file():
            raise StartupError('本机数仓尚未部署，缺少 docker-compose.yml 或 .env')
        compose = ['docker', 'compose', '--project-directory', str(self.db),
                   '--file', str(self.db / 'docker-compose.yml'), '--env-file', str(self.db / '.env')]
        existing = self.check(compose + ['ps', '--all', '--services']).stdout.split()
        missing = set(WAREHOUSE_SERVICES) - set(existing)
        if missing:
            raise StartupError('缺少已部署容器：' + '、'.join(sorted(missing)))
        if not self.args.status:
            # Preserve the deployed container image/config, volumes and migrated schema.
            self.check(compose + ['start', *WAREHOUSE_SERVICES], timeout=120)
        running = self.check(compose + ['ps', '--status', 'running', '--services']).stdout.split()
        missing = set(WAREHOUSE_SERVICES) - set(running)
        if missing:
            raise StartupError('容器未运行：' + '、'.join(sorted(missing)))
        deadline = time.monotonic() + (0 if self.args.status else self.args.ready_timeout)
        urls = ('http://127.0.0.1:3000/ready', 'http://127.0.0.1:5173/healthz',
                'http://127.0.0.1:5173/nest/ready')
        refreshed_proxy = False
        while True:
            api_ok, web_ok, proxy_ok = (http_ready(url) for url in urls)
            if api_ok and web_ok and proxy_ok:
                break
            if api_ok and web_ok and not proxy_ok and not self.args.status and not refreshed_proxy:
                # Docker can assign a different IP after restart. Nginx's
                # existing upstream resolution then needs a graceful reload.
                self.check(compose + ['exec', '-T', 'web', 'nginx', '-t'])
                self.check(compose + ['exec', '-T', 'web', 'nginx', '-s', 'reload'])
                refreshed_proxy = True
            if time.monotonic() >= deadline:
                raise StartupError('数仓 API/Web 尚未就绪；查看 docker compose logs --tail 100 backend worker web')
            time.sleep(1)
        self.say('就绪：本机数仓 API、Web、对象存储及后台容器')

    def unit(self, unit):
        values = self.check(['systemctl', '--user', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState']).stdout
        state = dict(line.split('=', 1) for line in values.splitlines() if '=' in line)
        if state.get('LoadState') != 'loaded':
            raise StartupError(f'缺少服务 {unit}')
        if state.get('ActiveState') != 'active':
            if self.args.status:
                raise StartupError(f'{unit} 当前未运行')
            self.check(['systemctl', '--user', 'start', unit])
        self.check(['systemctl', '--user', 'is-active', '--quiet', unit])
        self.say(f'运行：{unit}（数据状态以页面为准）')

    def report_sources(self, result, expected=()):
        if self.args.stop and not result.stdout.strip():
            raise StartupError('没有收到设备关闭确认；保留必要数据服务')
        if result.stdout.strip():
            if len(result.stdout) > 262144:
                raise StartupError('设备状态报告过大')
            report = json.loads(result.stdout)
            if not isinstance(report, dict) or not isinstance(report.get('devices'), list):
                raise StartupError('设备状态报告格式错误')
            for row in report['devices']:
                if (not isinstance(row, dict) or not isinstance(row.get('key'), str)
                        or row.get('state') not in ('reused', 'started', 'inactive', 'failed', 'partial', 'skipped',
                                                   'stopped', 'already_stopped', 'external_running')):
                    raise StartupError('设备状态条目格式错误')
            labels = {'mid360': 'MID360/IMU/FAST-LIO', 'stl19p': 'STL-19P 二维雷达',
                      'bluesea': '蓝海二维雷达', 'berxel': 'Berxel 彩色/深度相机',
                      'berxel-preview': '深度相机网页预览转发',
                      'berxel-frames': '深度相机连续帧检查',
                      'usb-cameras': '四路 USB 原相机',
                      'fisheye': '鱼眼相机（已连接且已配置时）',
                      'robot-cloud-yolo-observer': 'YOLO 与 ArUco 识别',
                      'robot-cloud-camera-status-gateway': '相机状态与识别网关',
                      'robot-cloud-camera-relay': '相机网页预览转发'}
            states = {'reused': '已运行', 'started': '已启动', 'inactive': '未运行',
                      'failed': '失败', 'partial': '部分运行', 'skipped': '已跳过',
                      'stopped': '已关闭', 'already_stopped': '已处于关闭状态',
                      'external_running': '其他程序仍在运行'}
            for row in report['devices']:
                key = row['key']
                detail = row.get('error') or row.get('message') or ''
                self.say(f"{states.get(row['state'], row['state'])}：{labels.get(key, key)}"
                         + (f"：{detail}" if detail else ''))
                if row['state'] == 'partial':
                    self.partial_devices.add(labels.get(key, key))
                if (not self.args.stop and key == 'berxel-frames'
                        and row['state'] in ('started', 'reused') and row.get('dataVerified') is True):
                    self.depth_source_ready = True
            if self.args.stop and (report.get('ok') is not True or report.get('stop') is not True
                    or not report['devices']
                    or not set(expected).issubset({row['key'] for row in report['devices']})
                    or any(row['state'] not in ('stopped', 'already_stopped') for row in report['devices'])):
                raise StartupError('未收到全部受管设备的关闭确认；保留必要数据服务')
        if result.returncode:
            state = '未完成关闭' if self.args.stop else '未就绪'
            raise StartupError(f'部分设备{state}（退出码 {result.returncode}）；查看上方设备报告')

    def local_sources(self):
        args = [sys.executable, '-B', str(SCRIPT_DIR / 'device_sources.py'), 'ipc']
        if self.args.status:
            args.append('--status')
        if self.args.stop:
            args.append('--stop')
        self.report_sources(self.runner.run(args, timeout=180 if self.args.stop else 100),
                            expected=('mid360', 'stl19p', 'bluesea'))

    def jetson(self):
        options = connection_options(Path.home() / '.config/robot-cloud/video-tunnel.env')
        args = ['python3', '-B', '-', 'jetson']
        if self.args.status:
            args.append('--status')
        if self.args.stop:
            args.append('--stop')
        if self.args.cameras_only:
            args.append('--cameras-only')
        if self.args.no_camera_source or self.args.no_device_sources:
            args.append('--no-camera-source')
        # Send reviewed helper code over the existing pinned SSH connection.
        # Nothing is installed, no new port is forwarded, no credentials copied.
        result = self.runner.run(options + [shlex.join(args)],
                                 input=(SCRIPT_DIR / 'device_sources.py').read_text(), timeout=360 if self.args.stop else 75)
        if result.returncode and not result.stdout.strip():
            raise StartupError('Jetson 连接失败；确认已开机、同一网络且现有 SSH 授权可用')
        expected = ('usb-cameras', 'berxel', 'berxel-preview', 'robot-cloud-yolo-observer',
                    'robot-cloud-camera-status-gateway', 'robot-cloud-camera-relay')
        if not self.args.cameras_only:
            expected += ('robot-native-archive',)
        self.report_sources(result, expected=expected)

    def preview_receiver_ready(self, seconds):
        """Verify frames in the running collector, not in a fresh DDS probe."""
        unit = 'robot-cloud-collector.service'
        values = self.check(['systemctl', '--user', 'show', unit,
                             '-p', 'LoadState', '-p', 'ActiveState', '-p', 'MainPID']).stdout
        state = dict(line.split('=', 1) for line in values.splitlines() if '=' in line)
        pid = state.get('MainPID', '')
        if (state.get('LoadState') != 'loaded' or state.get('ActiveState') != 'active'
                or not pid.isdecimal() or int(pid) < 1):
            raise StartupError('工控机网页接收服务未运行，无法检查深度画面')
        path = Path.home() / '.local/state/robot-cloud/preview-receiver.json'
        deadline, first = time.monotonic() + seconds, None
        while True:
            try:
                report = read_preview_health(path, int(pid))
                channels = report['channels']
                report_age = max(0, time.monotonic() - report['monotonic'])
                if all(row['status'] == 'AVAILABLE' and row['frameSeq'] > 0
                       and row['ageS'] is not None and row['ageS'] + report_age <= 2
                       for row in channels.values()):
                    if first is None:
                        first = report
                    elif (report['monotonic'] - first['monotonic'] >= 1
                          and all(channels[key]['frameSeq'] > first['channels'][key]['frameSeq']
                                  for key in ('color', 'depth'))):
                        return True
                else:
                    first = None
            except (OSError, ValueError):
                first = None
            if time.monotonic() >= deadline:
                return False
            time.sleep(.5)

    def preview_receiver(self):
        if self.preview_receiver_ready(4 if self.args.status else 8):
            self.say('正常：工控机已连续收到深度相机彩色与深度画面')
            return
        if self.args.status:
            raise StartupError('Jetson 已连续出图，但工控机网页接收未确认；重新执行一键开启相机可恢复接收')
        # This action is reached only after the source's real-frame check passes.
        # Rebuild the observation participant once when it started before the
        # camera and failed to rediscover it. Drivers and archive writers stay up.
        self.say('深度相机已出图，正在恢复工控机网页接收服务…')
        self.check(['systemctl', '--user', 'restart', 'robot-cloud-collector.service'], timeout=45)
        if not self.preview_receiver_ready(20):
            raise StartupError('网页接收恢复后仍未确认连续彩色/深度画面，请检查接收服务日志')
        self.say('已恢复：工控机连续收到彩色与深度新帧')

    def garage_status(self):
        if cloud_warehouse_config() is not None:
            self.say('本机采集由桌面采集脚本控制；云数仓负责保存数据。')
            return
        try:
            gate = json.loads((self.db / '.runtime/garage-archive-control/garage.json').read_text())
            enabled = gate.get('scope') == 'garage' and gate.get('enabled') is True
            self.say('地库采集开关：' + ('开启，设备出数据后按原设置保存。' if enabled else '关闭；进入地库后在数仓页面开启。'))
        except (OSError, ValueError, AttributeError):
            self.say('地库采集开关状态未知，请到数仓系统设置确认。')

    def run(self):
        cloud = cloud_warehouse_config()
        camera_only = self.args.cameras_only
        units = CAMERA_LOCAL_UNITS if camera_only else LOCAL_UNITS + TUNNEL_UNITS
        if cloud is not None:
            units = tuple(CLOUD_WAREHOUSE_UNIT if unit == LEGACY_WAREHOUSE_TUNNEL else unit
                          for unit in units)
        if self.args.no_device_sources or self.args.no_chassis_telemetry:
            units = tuple(unit for unit in units if unit != 'robot-cloud-chassis-telemetry.service')
        if self.args.dry_run:
            self.say('预演：不连接设备、不执行系统命令、不写入设置。')
            self.say('范围：仅相机与网页识别。' if camera_only else '范围：全部设备与数据服务。')
            if not camera_only and not self.args.no_warehouse:
                if cloud is not None:
                    self.say('云数仓：仅确保受限 API 隧道与云 API 健康；旧仓容器保持停用。')
                else:
                    self.say('本机数仓：仅 start 六个已部署容器：' + ' '.join(WAREHOUSE_SERVICES))
            if not camera_only and not self.args.no_device_sources:
                self.say('工控机设备：MID360/IMU/FAST-LIO、STL-19P /scan、蓝海 /bluesea_new/scan')
            if not camera_only and self.args.no_chassis_telemetry:
                self.say('底盘串口：跳过 robot-cloud-chassis-telemetry.service，不占用串口')
            self.say('本机服务：' + ' '.join(units))
            self.say('Jetson：相机状态网关、只读 relay' + ('' if camera_only else '、原生归档'))
            if not self.args.no_camera_source and not self.args.no_device_sources:
                self.say('Jetson 设备：四路 USB 原相机、Berxel 彩色/深度/点云及只读网页预览转发')
                if camera_only:
                    self.say('另检查固定设备映射已配置的鱼眼相机；未连接或未配置会明确跳过。')
                self.say('相机就绪检查后：robot-cloud-yolo-observer.service（YOLO 与 ArUco）')
            self.say('保留现有设备运行状态与地库开关；不会执行行驶任务。')
            return 0
        self.say('范围：仅相机与网页识别。' if camera_only else '范围：全部设备与数据服务。')
        if not camera_only and not self.args.no_warehouse:
            self.step('DBProg 数仓', self.warehouse)
        for unit in units:
            self.step(unit, lambda unit=unit: self.unit(unit))
        if not camera_only and not self.args.no_device_sources:
            self.step('工控机雷达与定位数据源', self.local_sources)
        self.step('Jetson 相机与识别' if camera_only else 'Jetson 相机、识别与归档', self.jetson)
        if self.depth_source_ready and not (self.args.no_camera_source or self.args.no_device_sources):
            self.step('工控机深度画面接收', self.preview_receiver)
        if not camera_only:
            self.garage_status()
        if self.failures:
            self.say('部分项目未就绪：' + '、'.join(self.failures) + '。其余已运行项目继续工作。')
            return 1
        if self.partial_devices:
            detail = '。未连接相机没有画面；详见上方报告。' if camera_only else '。未连接相机没有画面，也不会产生采集文件；详见上方报告。'
            self.say('检查完成，以下设备仅部分可用：' + '、'.join(sorted(self.partial_devices)) + detail)
        else:
            self.say('状态检查通过。' if self.args.status else '启动检查完成；请在观测页面核对实时数据。')
        self.say('网页：https://aystkit.cn/#media/cameras' if camera_only else
                 '网页：https://aystkit.cn  数仓：https://aystkit.cn/dbprog/system-settings')
        return 0


class Shutdown(Launcher):
    """Stop known sources before archive writers; keep storage on any failure."""

    def unit_state(self, unit):
        result = self.runner.run(['systemctl', '--user', 'show', unit,
                                  '-p', 'LoadState', '-p', 'ActiveState', '-p', 'Result'])
        state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if not state.get('LoadState'):
            raise StartupError(f'无法确认服务状态：{unit}')
        return state

    def unit_stop(self, unit):
        state = self.unit_state(unit)
        if state['LoadState'] == 'not-found' or state.get('ActiveState') in ('inactive', 'failed'):
            self.say(f'已处于关闭状态：{unit}')
            return
        self.check(['systemctl', '--user', 'stop', unit],
                   timeout=220 if unit == 'robot-native-archive.service' else 45)
        state = self.unit_state(unit)
        if state['LoadState'] != 'not-found' and (state.get('ActiveState') != 'inactive'
                                                 or state.get('Result', 'success') not in ('', 'success')):
            raise StartupError(f'{unit} 未正常退出；请检查 journalctl --user -u {unit}')
        self.say(f'已关闭：{unit}')

    def wait_archive_idle(self):
        unit = self.unit_state('robot-native-archive.service')
        if unit['LoadState'] == 'not-found' or unit.get('ActiveState') in ('inactive', 'failed'):
            self.say('本机归档已停止；已落盘但未入仓的片段留待下次恢复。')
            return
        since = time.time()
        deadline = time.monotonic() + 180
        path = self.db / '.runtime/garage-archive-control/status.json'
        self.say('等待原始数据片段收尾及相机视频封装完成…')
        while time.monotonic() < deadline:
            try:
                if path.is_symlink() or path.stat().st_size > 262144:
                    raise ValueError('status file')
                report = json.loads(path.read_text())
                updated = datetime.fromisoformat(report['updatedAt'].replace('Z', '+00:00'))
                if updated.tzinfo is None:
                    raise ValueError('status timezone')
                stamp = updated.astimezone(timezone.utc).timestamp()
                ros, store = report['ros'], report['store']
                if (since <= stamp <= time.time() and time.time() - stamp <= 8
                        and all(type(ros.get(field)) is int and ros[field] == 0
                                for field in ('queueMessages', 'queueBytes'))
                        and type(ros.get('inFlightMessages', 0)) is int
                        and ros.get('inFlightMessages', 0) == 0
                        and store.get('active') == {} and not ros.get('tickError')
                        and not ros.get('gateError')
                        and type(store.get('videoPendingSegments', 0)) is int
                        and ((store.get('videoPendingSegments', 0) == 0
                              and store.get('videoFinalizing', False) is False)
                             or (store.get('videoTerminalPending') is True
                                 or store.get('derivativeTerminalPending') is True
                                 ))
                        and not {k: v for k, v in store.get('errors', {}).items()
                                  if k not in ('sourceTransfer', 'recovery', 'videoFinalization')}):
                    self.say('本机原始数据队列已清空，活动片段已收尾。')
                    return
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            time.sleep(1)
        raise StartupError('未确认原始数据收尾完成；保留归档、同步和数仓，稍后再次关闭')

    def stop_warehouse(self):
        if cloud_warehouse_config() is not None:
            self.say('云数仓及其上传隧道保持运行，未操作云端或旧仓容器。')
            return
        if not (self.db / 'docker-compose.yml').is_file() or not (self.db / '.env').is_file():
            raise StartupError('数仓部署配置缺失，无法确认要关闭的容器')
        compose = ['docker', 'compose', '--project-directory', str(self.db),
                   '--file', str(self.db / 'docker-compose.yml'), '--env-file', str(self.db / '.env')]
        existing = set(self.check(compose + ['ps', '--all', '--services']).stdout.split())
        missing = set(WAREHOUSE_SERVICES) - existing
        if missing:
            raise StartupError('数仓部署不完整：' + '、'.join(sorted(missing)))
        for group in (('web', 'worker', 'backend'), ('minio', 'redis', 'postgres')):
            self.check(compose + ['stop', '--timeout', '60', *group], timeout=130)
            running = set(self.check(compose + ['ps', '--status', 'running', '--services']).stdout.split())
            if running.intersection(group):
                raise StartupError('部分数仓容器仍在运行；保留后续存储服务')
        self.say('本机数仓已关闭，数据库和原始文件保留；再次一键启动后恢复访问与补传。')

    def run(self):
        cloud = cloud_warehouse_config()
        if self.args.dry_run:
            self.say('关闭预演：不连接设备、不执行系统命令、不写入设置。')
            self.say('1. 关闭 Jetson 受管相机、深度相机；确认数据收尾后关闭源端归档、relay 和网关。')
            if self.args.no_chassis_telemetry:
                self.say('2. 关闭本机受管雷达，底盘串口只读遥测保持不接管，等待原始数据片段收尾。')
            else:
                self.say('2. 关闭本机受管雷达与底盘只读遥测，等待原始数据片段收尾。')
            self.say('3. 关闭本机归档、文件同步、观测和视频隧道。'
                     if cloud is not None else '3. 关闭本机归档、文件同步、观测和既有数仓/视频隧道。')
            if not self.args.no_warehouse:
                self.say('4. 云数仓及上传隧道保持运行；旧仓容器保持停用。'
                         if cloud is not None else
                         '4. 关闭数仓 Web、Worker、API，再关闭对象存储、Redis 和 PostgreSQL；保留数据卷。')
            self.say('地库开关保持原值；失败时保留必要数据服务；未完成入仓的本地片段下次启动补传。')
            return 0
        self.step('Jetson 设备与源端归档', self.jetson)
        self.step('工控机受管雷达', self.local_sources)
        if not self.args.no_chassis_telemetry:
            self.step('底盘只读遥测', lambda: self.unit_stop('robot-cloud-chassis-telemetry.service'))
        if not self.failures:
            self.step('本机原始数据收尾', self.wait_archive_idle)
        if self.failures:
            self.say('未全部关闭：' + '、'.join(self.failures) + '。归档、同步、隧道和数仓保持现状，请处理后重试。')
            return 1
        # Recorder shutdown closes files; sync can then stop with durable
        # pending segments retained on both machines for the next startup.
        for unit in ('robot-native-archive.service', 'robot-native-archive-source-link.service',
                     'robot-cloud-collector.service', *reversed(TUNNEL_UNITS),
                     'robot-cloud-dbprog-tunnel.service'):
            if cloud is not None and unit in (LEGACY_WAREHOUSE_TUNNEL, CLOUD_WAREHOUSE_UNIT):
                continue
            self.step(unit, lambda unit=unit: self.unit_stop(unit))
            if self.failures:
                self.say('关闭未完成；保留剩余服务与数仓，请检查后重试。')
                return 1
        if not self.args.no_warehouse:
            self.step('DBProg 数仓', self.stop_warehouse)
        if self.failures:
            return 1
        self.say('本次管理范围内的设备与服务已关闭，地库开关保持原值。')
        self.say('未完成入仓的片段保留本地，下次一键启动自动恢复补传。电脑仍保持开机。')
        return 0


class CameraShutdown(Shutdown):
    """Explicit camera-only stop leaves shared collection/storage services alone."""

    def run(self):
        if self.args.dry_run:
            self.say('相机关闭预演：不连接设备、不执行系统命令、不写入设置。')
            self.say('关闭受管 USB/P100R 相机、深度预览、YOLO/ArUco、相机网关/relay 和视频隧道。')
            self.say('保留工控机观测采集、雷达、底盘、归档与数仓当前状态。')
            return 0
        self.say('范围：仅关闭相机与视频链路；其他设备和数据服务保持现状。')
        self.step('Jetson 相机、预览与识别', self.jetson)
        if self.failures:
            self.say('相机关闭未获完整确认，保留视频隧道及其他服务，请检查后重试。')
            return 1
        for unit in reversed(TUNNEL_UNITS):
            self.step(unit, lambda unit=unit: self.unit_stop(unit))
            if self.failures:
                self.say('视频隧道关闭未完成，其余服务保持现状。')
                return 1
        self.say('受管相机与视频链路已关闭；工控机采集、其他设备和数仓保持现状。')
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='启动/检查设备、归档与网页服务；--all-devices 操作完整设备范围。')
    modes = parser.add_mutually_exclusive_group()
    scopes = parser.add_mutually_exclusive_group()
    scopes.add_argument('--cameras-only', action='store_true', help='仅相机/识别/视频链路；启动与状态的默认范围，也可与 --stop 合用')
    scopes.add_argument('--all-devices', action='store_true', help='全部设备、数仓与归档；保留原完整启动/状态检查')
    parser.add_argument('--dry-run', action='store_true', help='只列出动作，可与 --stop 合用，不连接设备')
    modes.add_argument('--status', action='store_true', help='只检查当前状态，不启动服务')
    modes.add_argument('--stop', action='store_true', help='收尾数据后关闭受管设备和数据服务，保留所有数据')
    parser.add_argument('--no-camera-source', action='store_true', help='跳过 Jetson USB/深度相机启动')
    parser.add_argument('--no-device-sources', action='store_true', help='仅恢复数仓/观测/归档/隧道，跳过硬件数据源')
    parser.add_argument('--no-chassis-telemetry', action='store_true', help='启动其他设备但跳过底盘串口只读遥测服务，不占用底盘串口')
    parser.add_argument('--no-warehouse', action='store_true', help='跳过本机数仓容器')
    parser.add_argument('--ready-timeout', default=os.environ.get('START_ALL_READY_TIMEOUT', '60'),
                        help='数仓就绪等待秒数，1–300，默认60')
    args = parser.parse_args(argv)
    if args.status and args.dry_run:
        parser.error('--status 与 --dry-run 不能同时使用')
    if args.stop and (args.no_camera_source or args.no_device_sources):
        parser.error('--stop 需先关闭受管数据源，不能跳过相机或设备源；可用 --no-warehouse 保留数仓')
    if not re.fullmatch(r'[0-9]{1,3}', str(args.ready_timeout)) or not 1 <= int(args.ready_timeout) <= 300:
        parser.error('--ready-timeout 需为 1–300 的整数')
    args.ready_timeout = int(args.ready_timeout)
    # Legacy --stop remains a full, ordered shutdown. Camera-only stop is explicit.
    args.cameras_only = args.cameras_only or (not args.all_devices and not args.stop)
    if not (args.status or args.stop or args.dry_run):
        try:
            from collector.robot_profile import validate_current_profile
            validate_current_profile()
        except (OSError, ValueError, KeyError, TypeError):
            print('机器人身份配置核对失败；未启动设备，请运行机器人接入检查。', file=sys.stderr)
            return 2
    launcher = (CameraShutdown(args) if args.cameras_only else Shutdown(args)) if args.stop else Launcher(args)
    try:
        if args.dry_run or args.status:
            return launcher.run()
        if args.stop:
            with startup_lock(wait=True, on_wait=launcher.say):
                return launcher.run()
        with startup_lock():
            return launcher.run()
    except (StartupError, OSError) as exc:
        print(f'无法完成设备操作：{exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('设备操作已中断；请用 --status 检查当前状态。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
