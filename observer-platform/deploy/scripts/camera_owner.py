#!/usr/bin/env python3
"""Explicit camera capture handoff; not Linux permissions or robot control.

Only the fixed preview units are managed. Unknown camera users are reported,
never signalled. The status and dry-run paths do not create locks or state.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time
import urllib.request
import uuid

NEW_WEB_UNIT = 'robot-camera-web-owner.service'
SHARED_UNIT = 'robot-cloud-shared-camera-preview.service'
CAPTURE_UNITS = (NEW_WEB_UNIT, 'robot-cloud-lowres-preview.service', 'yolo-native-direct-preview.service', SHARED_UNIT)
RELAY_UNIT = 'robot-cloud-camera-relay.service'
KNOWN_UNITS = CAPTURE_UNITS + (RELAY_UNIT,)
PORTS = ('2.4', '2.3', '2.1.2', '2.1.3')
LAUNCHER = '/home/nvidia/code/upper_computer/run_camera_native_web.sh'
WORKING_DIRECTORY = '/home/nvidia/code/upper_computer'
SHARED_PROGRAM = WORKING_DIRECTORY + '/install/yolo_camera/lib/yolo_camera/camera_native_web.py'
SHARED_CONFIG = '/home/nvidia/.config/robot-cloud/cameras.json'
KEY_PORTS = dict(zip(('usbFront', 'usbLeft', 'usbHub2', 'usbHub3'), PORTS))
# The usb* keys are stable transport aliases, not physical direction names.
# Keep IDs/USB bindings intact when correcting the vehicle's direction labels.
CAMERA_IDENTITIES = {'camera1': ('usbLeft', 'front', '5187'),
                     'camera2': ('usbFront', 'rear', '5187'),
                     'camera3': ('usbHub3', 'right', 'd584'),
                     'camera4': ('usbHub2', 'left', 'd584')}
SHARED_ARGS = ['/usr/bin/python3', '-B', SHARED_PROGRAM, '--config', SHARED_CONFIG,
               '--local-window', '--web-max-side', '640', '--web-fps', '10',
               '--web-bitrate', '350000']


def trusted_exec_start(name, raw):
    """Admit one exact non-shell interpreter invocation, never path substrings."""
    if (not isinstance(raw, str) or name not in KNOWN_UNITS or raw.count('argv[]=') != 1 or raw.count('{') != 1 or raw.count('}') != 1
            or not raw.strip().startswith('{') or not raw.strip().endswith('}')):
        return False
    match = re.search(r'\{\s*path=([^;]+?)\s*;\s*argv\[\]=(.*?)\s*;\s*(?=\}|(?:ignore_errors|flags|start_time|stop_time|pid|code|status)=)', raw)
    if not match:
        return False
    try:
        argv = shlex.split(match.group(2))
    except ValueError:
        return False
    if not argv or match.group(1).strip() != argv[0]:
        return False
    if name == SHARED_UNIT:
        return argv == SHARED_ARGS
    if name in (NEW_WEB_UNIT, 'yolo-native-direct-preview.service'):
        if len(argv) != 6 or argv[:2] not in (['/bin/bash', LAUNCHER], ['/usr/bin/bash', LAUNCHER]):
            return False
        options = dict(zip(argv[2::2], argv[3::2]))
        if set(options) != {'--host', '--allow-client'}:
            return False
        if name == NEW_WEB_UNIT and any(address != '127.0.0.1' for address in options.values()):
            return False
        try:
            return all('%' not in address and ipaddress.ip_address(address) for address in options.values())
        except ValueError:
            return False
    if argv[0] != '/usr/bin/python3':
        return False
    index = 2 if len(argv) > 1 and argv[1] == '-B' else 1
    expected = '/home/nvidia/.local/share/robot-cloud/' + (
        'usb_camera_preview.py' if name in CAPTURE_UNITS else 'http_camera_relay.py')
    if len(argv) <= index or argv[index] != expected:
        return False
    options = argv[index + 1:]
    if name != RELAY_UNIT:
        return not options
    seen = set()
    while options:
        option = options.pop(0)
        if option not in ('--keep-alive', '--preview-size') or option in seen:
            return False
        seen.add(option)
        if option == '--preview-size' and (not options or options.pop(0) not in ('320x240', '640x480')):
            return False
    return True


class OwnerError(RuntimeError):
    pass


class CameraIdentityError(OSError):
    """A present device is untrusted, rather than a safely absent camera."""


class StartFailure(OwnerError):
    def __init__(self, message, receipt=None):
        super().__init__(message)
        self.receipt = receipt


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        return None


@dataclass
class Unit:
    name: str
    active: bool = False
    load_state: str = 'loaded'
    main_pid: int = 0
    cgroup: str = ''
    verified: bool = True


@dataclass
class Holder:
    pid: int
    uid: int
    nodes: tuple = ()
    unit: str = None
    unreadable: bool = False


@dataclass
class Snapshot:
    units: dict = field(default_factory=dict)
    holders: tuple = ()
    missing: tuple = ()
    healthy: bool = False
    scan_errors: tuple = ()
    partial_visibility: bool = True
    available: tuple = ()
    missing_cameras: tuple = ()
    reconnect_required: tuple = ()


@dataclass
class StartReceipt:
    unit: str = NEW_WEB_UNIT
    run_id: str = ''


@dataclass
class Settings:
    private_dir: str = '/home/nvidia/.local/state/robot-cloud/camera-owner'
    proc_root: str = '/proc'
    sys_video_root: str = '/sys/class/video4linux'
    dev_by_path_root: str = '/dev/v4l/by-path'
    unit_timeout: float = 12
    health_timeout: float = 15
    capture_config: str | None = field(default_factory=lambda:
        SHARED_CONFIG if os.path.lexists(SHARED_CONFIG) else None)


def camera_ports(settings):
    """Use an explicit audited capture configuration; never guess USB order."""
    if settings.capture_config is None:
        return dict(KEY_PORTS)
    path = Path(settings.capture_config)
    if not path.is_absolute() or '..' in path.parts:
        raise OwnerError('相机映射路径必须是规范绝对路径')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022 or info.st_size > 16384):
            raise OwnerError('相机映射身份或权限不安全')
        raw = os.read(fd, 16385)
    finally:
        os.close(fd)
    data = json.loads(raw)
    rows = data.get('cameras') if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 4:
        raise OwnerError('需要四路明确的相机映射')
    ports, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            raise OwnerError('相机映射身份无效')
        identity = row['id']
        if identity not in CAMERA_IDENTITIES or identity in seen:
            raise OwnerError('相机映射身份无效或重复')
        key, direction, _ = CAMERA_IDENTITIES[identity]
        device = row.get('device')
        match = re.fullmatch(r'/dev/v4l/by-path/platform-3610000\.usb-usb-0:'
                            r'([1-9][0-9]*(?:\.[1-9][0-9]*){0,7}):1\.0-video-index0',
                            device if isinstance(device, str) else '')
        if row.get('direction') != direction or row.get('enabled', True) is not True or not match:
            raise OwnerError('相机朝向或 USB 路径无效')
        seen.add(identity)
        ports[key] = match.group(1)
    if len(set(ports.values())) != 4:
        raise OwnerError('不得将同一设备配置为多个相机')
    return {key: ports[key] for key in KEY_PORTS}


def camera_usb_parent(settings, key, port):
    """Inspect sysfs only, including VID/PID so a depth camera cannot substitute."""
    anchor = Path(settings.dev_by_path_root) / (
        'platform-3610000.usb-usb-0:' + port + ':1.0-video-index0')
    # Only a missing configured entry is an optional disconnected camera.
    # Broken links or unreadable metadata remain failed identity checks.
    anchor.lstat()
    try:
        device = anchor.resolve(strict=True)
        if not re.fullmatch(r'video[0-9]+', device.name):
            raise CameraIdentityError('Configured camera device node is invalid')
        physical = (Path(settings.sys_video_root) / device.name / 'device').resolve(strict=True).parent
        expected_product = '5187' if key in ('usbFront', 'usbLeft') else 'd584'
        if not re.fullmatch(r'[0-9]+-' + re.escape(port), physical.name):
            raise CameraIdentityError('Configured camera USB topology does not match')
        if ((physical / 'idVendor').read_text().strip().lower(),
                (physical / 'idProduct').read_text().strip().lower()) != ('0bda', expected_product):
            raise CameraIdentityError('Configured camera USB identity does not match')
    except OSError as exc:
        raise CameraIdentityError('Configured camera USB identity cannot be verified') from exc
    return physical


class Engine:
    def __init__(self, ops):
        self.ops = ops
        self.current = None

    def preflight(self, current, web):
        if current.scan_errors or any(unit.active and not unit.verified for unit in current.units.values()):
            raise OwnerError('无法确认相机服务身份，请先检查服务状态')
        if not web:
            return
        if current.missing and not current.available:
            raise OwnerError('没有可启动的已连接相机，保持现有程序不动')
        for holder in current.holders:
            unit = current.units.get(holder.unit)
            if (holder.uid != self.ops.uid or holder.unreadable or holder.unit not in CAPTURE_UNITS
                    or unit is None or not unit.active or not unit.verified):
                raise OwnerError('相机被 PID ' + str(holder.pid) + ' 占用，请手动关闭标定／其他程序后再切换网页')

    def result(self, action, current, dry_run, reused=False):
        return {'ok': True, 'action': action, 'reused': reused, 'dryRun': dry_run,
            'partial': bool(current.missing), 'dataVerifiedFor': list(current.available) if current.healthy else [],
            'snapshot': asdict(current), 'plannedStops': [name for name in KNOWN_UNITS
                if current.units.get(name, Unit(name)).active and not reused],
            'plannedStart': action == 'web' and not reused}

    def execute(self, action, dry_run=False):
        if action not in ('status', 'web', 'peer'):
            raise OwnerError('仅支持 status / web / peer')
        current = self.ops.snapshot()
        self.current = current
        if action == 'status':
            result = self.result(action, current, dry_run)
            result.update(plannedStops=[], plannedStart=False)
            return result
        self.preflight(current, action == 'web')
        reused = action == 'web' and current.healthy and any(
            current.units.get(name, Unit(name)).active for name in CAPTURE_UNITS)
        if dry_run or reused:
            return self.result(action, current, dry_run, reused)
        with self.ops.lock():
            current = self.ops.snapshot()
            self.current = current
            self.preflight(current, action == 'web')
            if action == 'web' and current.healthy and any(
                    current.units.get(name, Unit(name)).active for name in CAPTURE_UNITS):
                return self.result(action, current, False, True)
            validate_start = getattr(self.ops, 'validate_start_target', None)
            if action == 'web' and validate_start is not None:
                validate_start()  # Do not stop an existing owner for an invalid replacement.
            names = [name for name in KNOWN_UNITS if current.units.get(name, Unit(name)).active]
            for name in names:
                self.ops.stop_unit(name)
            if not self.ops.wait_stopped(names):
                raise OwnerError('已知预览服务未退出，未启动第二个采集程序')
            current = self.ops.snapshot()
            self.current = current
            if action == 'peer':
                if any(unit.active for unit in current.units.values()):
                    raise OwnerError('已知预览重新出现，交接未完成')
                self.ops.save_state(action, current)
                return self.result(action, current, False)
            self.preflight(current, True)  # A GUI can start without our advisory lock.
            receipt = None
            try:
                receipt = self.ops.start_web()
                current = self.ops.wait_healthy()
                self.current = current
                self.preflight(current, True)
                if not current.healthy:
                    raise OwnerError('原生预览尚未收到有效相机数据')
            except OwnerError as exc:
                receipt = receipt or (exc.receipt if isinstance(exc, StartFailure) else None)
                if receipt is not None:
                    self.ops.stop_new(receipt)
                raise
            self.ops.save_state(action, current)
            return self.result(action, current, False)


class SystemOps:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        self.uid = os.getuid()
        self.directory_fd = None
        self._health_frames = None
        self._checked_ports = None
        self._reconnect_required = ()

    def command(self, args, timeout=None):
        env = os.environ.copy()
        env.setdefault('XDG_RUNTIME_DIR', '/run/user/' + str(self.uid))
        env.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/run/user/' + str(self.uid) + '/bus')
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout or self.settings.unit_timeout,
                              env=env, check=False)

    def unit(self, name):
        if name not in KNOWN_UNITS:
            raise OwnerError('服务不在预览白名单中')
        result = self.command(['systemctl', '--user', 'show', name, '-p', 'LoadState', '-p', 'ActiveState',
            '-p', 'MainPID', '-p', 'ControlGroup', '-p', 'ExecStart', '-p', 'Description', '-p', 'InvocationID'])
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if not values.get('LoadState'):
            raise OwnerError('无法读取用户预览服务状态')
        active = values.get('ActiveState') in ('active', 'activating', 'reloading', 'deactivating')
        pid = int(values.get('MainPID', '0'))
        cgroup = values.get('ControlGroup', '')
        verified = not active or (trusted_exec_start(name, values.get('ExecStart', '')) and cgroup.endswith('/' + name))
        if active and pid:
            try:
                process = Path(self.settings.proc_root, str(pid))
                groups = [line.split(':', 2)[-1] for line in (process / 'cgroup').read_text().splitlines()]
                verified = verified and process.stat().st_uid == self.uid and any(
                    group == cgroup or group.startswith(cgroup + '/') for group in groups)
            except OSError:
                verified = False
        return Unit(name, active, values['LoadState'], pid, cgroup, verified), values

    def camera_nodes(self):
        nodes, missing, usb_paths = {}, [], []
        ports = camera_ports(self.settings)
        for key, port in ports.items():
            try:
                usb_paths.append(camera_usb_parent(self.settings, key, port))
            except FileNotFoundError:
                missing.append(port)
        # Include every video node on these same physical USB devices, not just index0.
        for entry in Path(self.settings.sys_video_root).glob('video*'):
            try:
                interface = (entry / 'device').resolve(strict=True)
                if not any(interface.parent == parent for parent in usb_paths):
                    continue
                major, minor = map(int, (entry / 'dev').read_text().strip().split(':'))
                nodes[os.makedev(major, minor)] = '/dev/' + entry.name
            except (OSError, ValueError):
                continue
        if usb_paths and not nodes:
            raise OwnerError('无法确认相机的 sysfs 设备节点')
        self._checked_ports = ports
        return nodes, missing

    def holders(self, nodes, units):
        found = {}
        for process in Path(self.settings.proc_root).iterdir():
            if not process.name.isdigit():
                continue
            try:
                uid = process.stat().st_uid
                held = set()
                for fd in (process / 'fd').iterdir():
                    try:
                        info = fd.stat()
                        if stat.S_ISCHR(info.st_mode) and info.st_rdev in nodes:
                            held.add(nodes[info.st_rdev])
                    except OSError:
                        continue
                if not held:
                    continue
                groups = [line.split(':', 2)[-1] for line in (process / 'cgroup').read_text().splitlines()]
                matched = next((unit.name for unit in units.values() if unit.active and unit.verified and unit.cgroup
                    and any(group == unit.cgroup or group.startswith(unit.cgroup + '/') for group in groups)), None)
                found[int(process.name)] = Holder(int(process.name), uid, tuple(sorted(held)), matched)
            except (OSError, ValueError):
                continue  # Unrelated unreadable system processes are not evidence of camera use.
        if nodes:
            try:
                evidence = self.command(['fuser', '--', *sorted(set(nodes.values()))], timeout=4)
                pids = {int(value) for value in evidence.stdout.split() if value.isdigit()}
                for pid in pids - found.keys():
                    try:
                        uid = Path(self.settings.proc_root, str(pid)).stat().st_uid
                    except OSError:
                        uid = -1
                    found[pid] = Holder(pid, uid, tuple(sorted(nodes.values())), unreadable=True)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Visibility is explicitly partial; never claim all other UIDs were inspected.
        return tuple(found[pid] for pid in sorted(found))

    def healthy(self, available=None):
        """Validate advancement for every physically verified connected camera."""
        self._reconnect_required = ()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open('http://127.0.0.1:8080/status', timeout=2) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                self._health_frames = None
                return False
            cameras = json.loads(raw).get('cameras', [])
            expected = {identity: (direction, *((2592, 1944) if product == '5187' else (1920, 1080)))
                        for identity, (_, direction, product) in CAMERA_IDENTITIES.items()}
            required = set(expected) if available is None else set(available)
            if not required or not required <= set(expected):
                self._health_frames = None
                return False
            seen, frames = set(), {}
            for camera in cameras:
                identity, age = camera.get('id'), camera.get('frame_age_ms')
                if identity not in expected or identity in seen:
                    self._health_frames = None
                    return False
                direction, width, height = expected[identity]
                seen.add(identity)
                if camera.get('direction') != direction:
                    self._health_frames = None
                    return False
                if identity not in required:
                    if (camera.get('connected') is not False or camera.get('state') != 'disconnected'
                            or type(camera.get('frames')) is not int or camera['frames'] != 0
                            or type(camera.get('encoded_frames')) is not int or camera['encoded_frames'] != 0
                            or camera.get('frame_age_ms') is not None or camera.get('codec') is not None):
                        self._health_frames = None
                        return False
                    continue
                if camera.get('connected') is False:
                    if camera.get('state') == 'disconnected':
                        self._reconnect_required = (identity,)
                    self._health_frames = None
                    return False
                size = (camera.get('width'), camera.get('height'))
                native = size == (width, height)
                scaled = (size == ((640, 480) if identity in ('camera1', 'camera2') else (640, 360))
                          and camera.get('requested_fps') == 10
                          and (camera.get('capture_width'), camera.get('capture_height')) == (width, height))
                count, encoded = camera.get('frames'), camera.get('encoded_frames')
                if (camera.get('error') or camera.get('video_error')
                    or camera.get('direction') != direction or not (native or scaled)
                    or type(count) is not int or count <= 0 or type(encoded) is not int or encoded <= 0
                    or not isinstance(age, (int, float)) or isinstance(age, bool) or not math.isfinite(age) or not 0 <= age < 2000
                    or not re.fullmatch(r'avc1\.[0-9a-fA-F]{6}', str(camera.get('codec', '')))):
                    self._health_frames = None
                    return False
                frames[identity] = (count, encoded)
            complete = seen == set(expected) and set(frames) == required
            before, self._health_frames = self._health_frames, frames if complete else None
            if self._health_frames is not None and before is None:
                # Establish real advancement on the first check as well, so an
                # already-running healthy shared preview is reused immediately.
                time.sleep(.15)
                return self.healthy(available)
            return (complete and before is not None and set(before) == required
                    and all(frames[key][0] > before[key][0] and frames[key][1] > before[key][1]
                            for key in required))
        except (OSError, ValueError, TypeError, AttributeError):
            self._health_frames = None
            return False

    def snapshot(self):
        units, errors = {}, []
        for name in KNOWN_UNITS:
            try:
                units[name] = self.unit(name)[0]
            except (OwnerError, OSError, subprocess.TimeoutExpired, ValueError):
                errors.append('service status unavailable: ' + name)
        try:
            nodes, missing = self.camera_nodes()
            holders = self.holders(nodes, units)
            ports = self._checked_ports
            available = tuple(identity for identity, (key, _, _) in CAMERA_IDENTITIES.items()
                              if ports[key] not in missing)
            absent = tuple(identity for identity in CAMERA_IDENTITIES if identity not in available)
        except (OwnerError, OSError, ValueError):
            missing, holders = (), ()
            available, absent = (), ()
            errors.append('camera device inspection unavailable')
        healthy = self.healthy(available) if not errors else False
        return Snapshot(units, holders, tuple(missing), healthy, tuple(errors), True,
                        available, absent, self._reconnect_required if not errors else ())

    @contextmanager
    def lock(self):
        directory = Path(self.settings.private_dir)
        if not directory.is_absolute() or '..' in directory.parts:
            raise OwnerError('私有锁目录必须是规范绝对路径')
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        lock_fd = None
        try:
            for index, component in enumerate(directory.parts[1:]):
                try:
                    next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_fd
            info = os.fstat(descriptor)
            if info.st_uid != self.uid or stat.S_IMODE(info.st_mode) != 0o700:
                raise OwnerError('私有锁目录权限不安全')
            lock_fd = os.open('owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=descriptor)
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise OwnerError('锁文件身份或权限不安全')
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OwnerError('已有相机切换正在执行') from exc
            self.directory_fd = descriptor
            yield
        except OSError as exc:
            raise OwnerError('无法安全取得相机切换锁') from exc
        finally:
            self.directory_fd = None
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(descriptor)

    def stop_unit(self, name):
        current, _ = self.unit(name)
        if not current.verified:
            raise OwnerError('服务身份已变化，拒绝停止')
        if current.active and self.command(['systemctl', '--user', 'stop', name]).returncode:
            raise OwnerError('预览服务停止失败')

    def wait_stopped(self, names):
        deadline = time.monotonic() + self.settings.unit_timeout
        while time.monotonic() < deadline:
            if all(not self.unit(name)[0].active for name in names):
                return True
            time.sleep(.2)
        return False

    def validate_start_target(self):
        if self.settings.capture_config is not None:
            if self.settings.capture_config != SHARED_CONFIG:
                raise OwnerError('启动共享预览必须使用固定的已核对配置')
            camera_ports(self.settings)
            current, values = self.unit(SHARED_UNIT)
            if (current.load_state != 'loaded'
                    or not trusted_exec_start(SHARED_UNIT, values.get('ExecStart', ''))):
                raise OwnerError('共享预览服务缺失或身份变化，未启动')

    def start_web(self):
        if self.settings.capture_config is not None:
            self.validate_start_target()
            current, _ = self.unit(SHARED_UNIT)
            if current.active:
                raise OwnerError('共享预览已被其他启动操作占用，未重复启动')
            receipt = None
            try:
                result = self.command(['systemctl', '--user', 'start', SHARED_UNIT])
                started, values = self.unit(SHARED_UNIT)
                invocation = values.get('InvocationID', '')
                if re.fullmatch(r'[0-9a-f]{32}', invocation):
                    receipt = StartReceipt(unit=SHARED_UNIT, run_id=invocation)
                if result.returncode or not started.active or not started.verified or receipt is None:
                    raise OwnerError('共享预览启动失败')
                return receipt
            except (OwnerError, OSError, subprocess.TimeoutExpired) as exc:
                raise StartFailure('共享预览未成功启动', receipt) from exc
        receipt = StartReceipt(run_id=uuid.uuid4().hex)
        description = 'Robot camera web owner ' + receipt.run_id
        try:
            result = self.command(['systemd-run', '--user', '--collect', '--unit=' + NEW_WEB_UNIT,
                '--description=' + description, '--working-directory=' + WORKING_DIRECTORY,
                '--property=Restart=on-failure', '--property=KillMode=control-group',
                '/bin/bash', LAUNCHER, '--host', '127.0.0.1', '--allow-client', '127.0.0.1'])
            if result.returncode:
                raise OwnerError('原生预览启动失败')
            return receipt
        except (OwnerError, OSError, subprocess.TimeoutExpired) as exc:
            try:
                _, values = self.unit(NEW_WEB_UNIT)
                created = values.get('Description') == description
            except (OwnerError, OSError, subprocess.TimeoutExpired):
                created = False
            raise StartFailure('原生预览未成功启动', receipt if created else None) from exc

    def wait_healthy(self):
        deadline = time.monotonic() + self.settings.health_timeout
        while time.monotonic() < deadline:
            current = self.snapshot()
            if current.healthy:
                return current
            time.sleep(.3)
        raise OwnerError('原生预览启动后未收到有效数据')

    def stop_new(self, receipt):
        if receipt.unit == SHARED_UNIT:
            current, values = self.unit(SHARED_UNIT)
            if (not re.fullmatch(r'[0-9a-f]{32}', receipt.run_id)
                    or values.get('InvocationID') != receipt.run_id or not current.verified
                    or not trusted_exec_start(SHARED_UNIT, values.get('ExecStart', ''))):
                raise OwnerError('本轮共享预览已被替换，拒绝清理其他来源')
            self.stop_unit(SHARED_UNIT)
            return
        if receipt.unit != NEW_WEB_UNIT or not receipt.run_id:
            raise OwnerError('无本轮启动凭据，拒绝清理')
        _, values = self.unit(NEW_WEB_UNIT)
        if values.get('Description') != 'Robot camera web owner ' + receipt.run_id:
            raise OwnerError('本轮服务已被替换，拒绝清理其他来源')
        self.stop_unit(NEW_WEB_UNIT)

    def save_state(self, action, current):
        if self.directory_fd is None:
            raise OwnerError('必须持有切换锁才能记录状态')
        try:
            previous = os.stat('state.json', dir_fd=self.directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(previous.st_mode) or previous.st_uid != self.uid or previous.st_nlink != 1 or stat.S_IMODE(previous.st_mode) != 0o600:
                raise OwnerError('已有状态文件身份或权限不安全')
        except FileNotFoundError:
            pass
        filename = 'state-' + uuid.uuid4().hex + '.tmp'
        descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                             dir_fd=self.directory_fd)
        try:
            value = json.dumps({'requestedMode': action, 'timestamp': time.time(), 'snapshot': asdict(current)}, ensure_ascii=False).encode()
            remaining = memoryview(value)
            while remaining:
                written = os.write(descriptor, remaining)
                if not written:
                    raise OwnerError('未能完整记录切换状态')
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(filename, 'state.json', src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
        os.fsync(self.directory_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description='切换已知网页相机采集与标定独占；不修改设备权限')
    parser.add_argument('action', choices=('status', 'web', 'peer'))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--capture-config', default=Settings().capture_config, help='已核对的四路采集配置 JSON')
    args = parser.parse_args(argv)
    engine = Engine(SystemOps(Settings(capture_config=args.capture_config)))
    try:
        result = engine.execute(args.action, args.dry_run)
    except OwnerError as exc:
        result = {'ok': False, 'action': args.action, 'error': str(exc)}
        if engine.current is not None:
            result['snapshot'] = asdict(engine.current)
        print(json.dumps(result, ensure_ascii=False))
        return 1
    except (OSError, subprocess.TimeoutExpired, ValueError):
        print(json.dumps({'ok': False, 'action': args.action, 'error': '相机切换未完成，请先检查设备占用与已知预览服务状态'}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
