"""Ordered two-computer shutdown. Checks never stop services or request power.

Execution requires a terminal, a physical stationary confirmation and temporary
sudo authentication. No chassis command is sent. Remote disconnection is never
accepted as proof that a shutdown request was accepted.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import time
from uuid import uuid4

from .archive_capture_session import (CaptureSession, boot_identity, process_identity,
                                      read_environment, status_timestamp)
from .archive_core import atomic_json
from .archive_source_link import ssh_options
from .robot_shutdown_motion import probe_motion
from .robot_shutdown_power import root_command
from .robot_shutdown_state import check_archive_state, check_camera_state

PACKAGE = Path(__file__).resolve().parents[1]
NATIVE = 'robot-native-archive.service'
SOURCE_LINK = 'robot-native-archive-source-link.service'
NONCE = re.compile(r'[0-9a-f]{64}')
BOOT = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class ShutdownError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute():
        raise ShutdownError('状态目录必须是绝对路径')
    for parent in reversed((path, *path.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if not create:
                raise ShutdownError('缺少状态目录：' + str(parent)) from None
            parent.mkdir(mode=0o700)
            info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
            raise ShutdownError('状态目录存在链接或所有者异常：' + str(parent))
        # The warehouse's existing .runtime ancestor is owned by this user
        # and group-writable. The protected control/lock directory itself
        # must remain private; never allow a world-writable ancestor.
        if info.st_mode & (0o022 if parent == path or info.st_uid == 0 else 0o002):
            raise ShutdownError('状态目录允许其他用户写入：' + str(parent))
    if path.stat().st_uid != os.getuid():
        raise ShutdownError('状态目录不属于当前用户')
    return path


def read_json(path, *, optional=False):
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if optional:
            return None
        raise ShutdownError('缺少状态文件：' + path.name) from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > 1024 * 1024):
            raise ShutdownError('状态文件无效：' + path.name)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            value = json.loads(stream.read(1024 * 1024 + 1))
        if not isinstance(value, dict):
            raise ShutdownError('状态文件不是对象：' + path.name)
        return value
    except (ValueError, UnicodeError):
        raise ShutdownError('状态文件格式错误：' + path.name) from None
    finally:
        os.close(fd)


@contextmanager
def file_lock(path, *, timeout=0):
    path = Path(path)
    safe_directory(path.parent, create=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            raise ShutdownError('锁文件无效：' + path.name)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ShutdownError('仍有采集、上传或设备操作：' + path.name) from None
                time.sleep(.2)
        yield
    finally:
        os.close(fd)


class JsonPeer:
    """Bounded binary line protocol; EOF and stderr never become success."""
    def __init__(self, process):
        self.process = process
        self.buffer = b''

    def send(self, value):
        self.process.stdin.write((json.dumps(value, allow_nan=False) + '\n').encode())
        self.process.stdin.flush()

    def receive(self, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if len(line) > 262144:
                    raise ShutdownError('关闭程序响应过大')
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    raise ShutdownError('关闭程序返回了无效响应') from None
                if not isinstance(value, dict):
                    raise ShutdownError('关闭程序返回了无效响应')
                return value
            if len(self.buffer) > 262144:
                raise ShutdownError('关闭程序响应过大')
            left = deadline - time.monotonic()
            if left <= 0:
                raise ShutdownError('等待关闭程序确认超时')
            ready, _, _ = select.select([self.process.stdout.fileno()], [], [], min(left, 1))
            if not ready:
                continue
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                raise ShutdownError('连接已断开，尚未取得正常关机确认；工控机保持运行')
            self.buffer += data

    def close(self):
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            # Stop only the helper/SSH child owned by this invocation, never
            # sensor or algorithm processes. EOF cancels uncommitted helpers.
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                    self.process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass  # A privileged child may no longer be signalable.
        finally:
            self.process.stdout.close()


def transaction_message(phase, nonce, boot_id):
    return dict(phase=phase, nonce=nonce, bootId=boot_id)


def expect(peer, phase, nonce, boot_id=None, *, timeout=30):
    value = peer.receive(timeout)
    if (value.get('phase') != phase or value.get('nonce') != nonce
            or not isinstance(value.get('bootId'), str)
            or BOOT.fullmatch(value['bootId']) is None
            or (boot_id is not None and value['bootId'] != boot_id)):
        detail = value.get('reason')
        raise ShutdownError('关闭阶段未获确认：' + phase
                            + ('（' + detail[:160] + '）' if isinstance(detail, str) else ''))
    return value


def spawn_peer(command, stderr):
    return JsonPeer(subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=stderr, bufsize=0, close_fds=True))


class Host:
    def __init__(self, *, runner=subprocess.run, home=None):
        self.home = Path(home) if home is not None else Path.home()
        self.values = read_environment(self.home / '.config/robot-cloud/archive.env')
        fallback = (self.home / '.local/state/robot-cloud/garage-control'
                    if platform.machine() in ('aarch64', 'arm64') else
                    self.home / 'DBProg/RobotHygienism/.runtime/garage-archive-control')
        self.control = safe_directory(self.values.get('ARCHIVE_CONTROL_DIR', str(fallback)))
        self.state = self.home / '.local/state/robot-cloud'
        self.root = Path(self.values.get('ARCHIVE_ROOT', str(self.home / '.local/share/robot-native-archive')))
        self.runner = runner
        self.boot_id = boot_identity()
        if not self.boot_id or BOOT.fullmatch(self.boot_id) is None:
            raise ShutdownError('无法确认本次系统启动身份')
        # btime has seconds precision; it is deliberately used only for
        # identifying previous-boot metadata, not current-session completion.
        entries = Path('/proc/stat').read_text().splitlines()
        self.boot_time = float(next(line.split()[1] for line in entries if line.startswith('btime ')))

    def command(self, args, timeout=30):
        env = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}',
                   DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
        result = self.runner(args, capture_output=True, text=True, timeout=timeout, env=env)
        if result.returncode:
            raise ShutdownError('命令失败：' + ' '.join(args[:4]) + f'（{result.returncode}）')
        return result.stdout.strip()

    def unit(self, name):
        text = self.command(['/usr/bin/systemctl', '--user', 'show', name,
            '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result',
            '-p', 'MainPID', '-p', 'ExecMainStartTimestampMonotonic'])
        result = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        if not result.get('LoadState') or not result.get('ActiveState'):
            raise ShutdownError('无法确认服务：' + name)
        return result

    def stop_unit(self, name, *, timeout=245):
        before = self.unit(name)
        if before['LoadState'] == 'not-found':
            return False
        if before['ActiveState'] not in ('inactive', 'failed'):
            self.command(['/usr/bin/systemctl', '--user', 'stop', name], timeout=timeout)
        after = self.unit(name)
        if (after['ActiveState'] != 'inactive' or after.get('MainPID') != '0'
                or after.get('Result') not in ('', 'success')):
            raise ShutdownError('服务未正常退出：' + name)
        return before['ActiveState'] not in ('inactive', 'failed')

    def snapshot(self):
        report = read_json(self.control / 'status.json', optional=True) or {}
        ros, store = report.get('ros'), report.get('store')
        ros = ros if isinstance(ros, dict) else {}
        store = store if isinstance(store, dict) else {}
        active = store.get('active')
        upload = report.get('upload')
        upload = upload if isinstance(upload, dict) else {}
        errors = store.get('errors')
        return dict(bootId=self.boot_id, native=self.unit(NATIVE),
                    updatedAt=report.get('updatedAt'), archiveState=report.get('state'),
                    rosActive=ros.get('active'), activeSegments=len(active) if isinstance(active, dict) else None,
                    pendingUpload=upload.get('pendingSegments'),
                    storageErrors=sorted(errors) if isinstance(errors, dict) else [])

    def lock_capture(self, stack, *, notify=True, timeout=150):
        session = CaptureSession(self.control)
        marker = read_json(session.marker, optional=True) or {}
        if (notify and marker.get('state') == 'recording' and marker.get('bootId') == self.boot_id
                and marker.get('processStart')
                and process_identity(marker.get('pid')) == marker['processStart']):
            # Only signal a verified desktop capture owner, never a ROS node.
            session.stop(self.values)
        stack.enter_context(file_lock(self.control / 'desktop-capture.lock', timeout=timeout))
        stack.enter_context(file_lock(self.state / 'start-all-devices.lock', timeout=30))
        # A foreground upload may be interrupted only by its own operator.
        # Do not race its remote-file pruning or cloud acknowledgement.
        safe_directory(self.root)
        stack.enter_context(file_lock(self.root / '.upload-only.lock'))

    def close_gates(self):
        for name in ('garage.json', 'desktop-lease.json'):
            path = self.control / name
            previous = read_json(path, optional=True) or {}
            atomic_json(path, dict(previous, schemaVersion=1, scope='garage', enabled=False,
                                   expiresAt=time.time(), updatedAt=utc_now()))

    def finish_native(self):
        before = self.unit(NATIVE)
        since = time.time()
        stopped_now = self.stop_unit(NATIVE)
        report = read_json(self.control / 'status.json')
        if stopped_now:
            result = check_archive_state(report, now=time.time(), since=since, stopped=True)
        else:
            # A previously stopped, successful unit does not refresh status.
            # Its final report must postdate this boot and the unit's last run.
            stamp = status_timestamp(report.get('updatedAt'))
            try:
                started = self.boot_time + int(before.get('ExecMainStartTimestampMonotonic', '0')) / 1e6
            except ValueError:
                raise ShutdownError('无法确认归档最后运行时间') from None
            if stamp > time.time():
                raise ShutdownError('归档结束状态来自未来')
            result = check_archive_state(report, now=stamp, since=started, stopped=True)
        if result['state'] != 'READY':
            raise ShutdownError('归档保存尚未确认：' + result['reason'])
        self.command(['/usr/bin/sync'], timeout=60)
        return dict(state='locally_saved', warnings=result['warnings'],
                    pendingUpload=report.get('upload', {}).get('pendingSegments'))

    def close_cameras(self):
        # Reuse the existing strict device owner checks. Never run its full
        # --stop path: that would include shared radar sources.
        from deploy.scripts.device_sources import RecordingDeviceSources
        manager = RecordingDeviceSources('jetson')
        result = manager.stop_recording_cameras()
        required = {'usb-cameras', 'berxel', 'fisheye', 'berxel-preview',
                    'robot-cloud-yolo-observer'}
        rows = result.get('devices')
        complete = (isinstance(rows, list) and len(rows) == len(required)
                    and all(isinstance(row, dict) for row in rows)
                    and {row.get('key') for row in rows} == required
                    and all(row.get('state') in ('stopped', 'already_stopped') for row in rows))
        if result.get('ok') is not True or not complete:
            failed = [row.get('key', '?') for row in result.get('devices', [])
                      if row.get('state') not in ('stopped', 'already_stopped')]
            raise ShutdownError('相机未全部正常停止：' + '、'.join(failed))
        owner = manager.load_camera_owner()
        snapshot = owner.SystemOps().snapshot()
        if (snapshot.scan_errors or snapshot.holders
                or not set(owner.CAPTURE_UNITS).issubset(snapshot.units)
                or any(snapshot.units[name].active is not False for name in owner.CAPTURE_UNITS)):
            raise ShutdownError('仍有相机占用或无法确认设备已释放')
        report = read_json(self.control / 'camera-status.json', optional=True)
        # USB holders and managed units are independently checked above. The
        # helper has also verified Berxel/fisheye process ownership and exit.
        if report is None:
            return dict(state='READY', warnings=['camera_archive_status_missing'])
        decision = check_camera_state(report, now=time.time(), boot_time=self.boot_time,
                                      service_active=False, process_count=0)
        if decision['state'] != 'READY':
            raise ShutdownError('相机文件保存尚未确认：' + decision['reason'])
        return decision


def execute_order(local, remote, local_power, *, nonce, local_boot, remote_boot, stack,
                  record=lambda **fields: None, motion=probe_motion, lifecycle=None):
    """Testable ordering boundary. Every failure before remote ACK keeps IPC on."""
    if motion()['state'] == 'MOVING':
        raise ShutdownError('检测到车辆仍在运动，未执行关机')
    local.lock_capture(stack)
    if lifecycle is not None:
        # Registered after the file locks so it runs before their release,
        # including when post-ACK logging or a signal interrupts the caller.
        stack.callback(lambda: hold_until_poweroff() if lifecycle.get('localCommitted') else None)
    local.close_gates()
    local.stop_unit(SOURCE_LINK, timeout=45)
    record(state='sealing')
    remote.send(transaction_message('seal', nonce, remote_boot))
    remote_saved = expect(remote, 'sealed', nonce, remote_boot, timeout=360)
    local_saved = local.finish_native()
    record(state='locally_saved', local=local_saved, remote=remote_saved.get('saved'),
           remoteCamera=remote_saved.get('camera'))
    # Reject motion still observable after draining. Unknown feedback is
    # covered only by the explicit physical confirmation made by the operator.
    if motion()['state'] == 'MOVING':
        raise ShutdownError('检测到车辆运动，采集已保存，未提交关机')
    remote.send(transaction_message('commit', nonce, remote_boot))
    expect(remote, 'shutdown_requested', nonce, remote_boot, timeout=30)
    record(state='jetson_shutdown_requested')
    # Positive ACK proves logind entered shutdown, never physical power loss.
    try:
        remote.send(transaction_message('release', nonce, remote_boot))
    except OSError:
        pass  # A matched positive ACK was already received before disconnect.
    local_power.send(transaction_message('commit', nonce, local_boot))
    expect(local_power, 'shutdown_requested', nonce, local_boot, timeout=30)
    if lifecycle is not None:
        lifecycle['localCommitted'] = True
    record(state='both_shutdown_requested')
    try:
        local_power.send(transaction_message('release', nonce, local_boot))
    except OSError:
        pass


def hold_until_poweroff():
    """Keep startup/capture locks until the OS terminates this process.

    A successful request is irreversible. Closing the terminal or pressing
    Ctrl+C must not reopen collection while logind is stopping the system.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    while True:
        time.sleep(1)


def remote_main(nonce):
    if NONCE.fullmatch(nonce) is None or os.geteuid() == 0:
        return 2
    host = Host()
    boot = host.boot_id
    root_peer = None
    committed = False
    stack = ExitStack()
    reader = JsonPeer(type('InputProcess', (), {'stdout': sys.stdin.buffer})())

    def emit(phase, **fields):
        print(json.dumps(dict(phase=phase, nonce=nonce, bootId=boot, **fields),
                         ensure_ascii=False, allow_nan=False), flush=True)

    def request(timeout=600):
        value = reader.receive(timeout)
        if (value.get('nonce') != nonce or value.get('bootId') != boot
                or set(value) != {'phase', 'nonce', 'bootId'}):
            raise ShutdownError('关闭事务身份不匹配')
        return value['phase']

    try:
        emit('hello', snapshot=host.snapshot())
        phase = request()
        if phase in ('cancel', 'check'):
            emit('checked')
            return 0
        if phase != 'authorize':
            raise ShutdownError('关闭阶段顺序错误')
        root_peer = spawn_peer(root_command(nonce), sys.stderr)
        expect(root_peer, 'ready', nonce, boot)
        emit('authorized')
        if request() != 'seal':
            raise ShutdownError('未收到保存指令')
        host.lock_capture(stack, notify=False)
        host.close_gates()
        # Send camera helper progress to stderr, preserving stdout protocol.
        from contextlib import redirect_stdout
        with redirect_stdout(sys.stderr):
            camera = host.close_cameras()
        saved = host.finish_native()
        emit('sealed', saved=saved, camera=camera)
        if request() != 'commit':
            raise ShutdownError('未收到关机提交指令')
        root_peer.send(transaction_message('commit', nonce, boot))
        expect(root_peer, 'shutdown_requested', nonce, boot)
        committed = True
        emit('shutdown_requested')
        # The root helper independently bounds its delay inhibitor. Neither
        # an absent release nor a broken SSH can be mistaken for a new commit.
        try:
            if request(timeout=1) == 'release':
                root_peer.send(transaction_message('release', nonce, boot))
        except (ShutdownError, BrokenPipeError):
            pass
        return 0
    except (ShutdownError, OSError, ValueError, subprocess.SubprocessError) as error:
        emit('error', reason=str(error)[:200])
        return 2
    finally:
        if committed:
            hold_until_poweroff()
        if root_peer is not None:
            root_peer.close()
        stack.close()


PLAN = '''关闭顺序：
1. 只读检查两台主机连接、权限及车辆速度；现场确认停稳并禁止运动。
2. 输入工控机登录密码，临时授权本次关机；不保存密码或修改 sudoers。
3. 结束采集、关闭相机并等待数据落盘；不单独停止共享雷达/导航进程。
4. Jetson 确认进入正常关机流程后，最后关闭工控机。
未上传文件留在原机，下次开机继续整理和补传。此工具不切整车总电源，也不负责重新开机。'''


def main(argv=None):
    parser = argparse.ArgumentParser(description='先保存采集数据，再依次关闭 Jetson 和工控机')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='仅显示顺序，不连接、不写文件、不关机')
    mode.add_argument('--check', action='store_true', help='只读连接及状态检查，不认证、不停止采集、不关机')
    parser.add_argument('--remote-agent', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.remote_agent is not None:
        if args.check or args.dry_run:
            parser.error('内部模式不能与检查模式组合')
        return remote_main(args.remote_agent)
    print(PLAN, flush=True)
    if args.dry_run:
        print('本次仅预演，没有执行任何操作。')
        return 0
    if os.geteuid() == 0 or platform.machine() not in ('x86_64', 'amd64'):
        print('请在工控机以普通用户运行；不要 sudo 整个脚本。', file=sys.stderr)
        return 2
    if not args.check and not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('实际关机必须在终端由现场人员确认；可先使用 --check 或 --dry-run。', file=sys.stderr)
        return 2
    previous_signals = {}
    if not args.check:
        def cancel(_number, _frame):
            raise KeyboardInterrupt()
        for number in (signal.SIGTERM, signal.SIGHUP):
            previous_signals[number] = signal.signal(number, cancel)
    peer = local_power = None
    report_path = None
    result = dict(schemaVersion=1, state='checking', startedAt=utc_now(),
                  physicalPowerOffConfirmed=False)
    def record(**fields):
        result.update(fields, updatedAt=utc_now())
        if report_path is not None:
            atomic_json(report_path, result)
    try:
        host = Host()
        nonce = uuid4().hex + uuid4().hex
        options, target = ssh_options(host.values)
        remote_entry = ('import sys; from pathlib import Path; '
            'sys.path.insert(0,str(Path.home()/".local/share/robot-cloud")); '
            'from collector.robot_shutdown import remote_main; '
            'raise SystemExit(remote_main(sys.argv[1]))')
        remote_cmd = options + [target, shlex.join(['/usr/bin/python3', '-B', '-u', '-c', remote_entry, nonce])]
        peer = spawn_peer(remote_cmd, sys.stderr)
        hello = expect(peer, 'hello', nonce)
        remote_boot = hello['bootId']
        if remote_boot == host.boot_id:
            raise ShutdownError('远端与本机启动身份相同，拒绝误关本机')
        movement = probe_motion()
        print('车辆状态：' + {'UNKNOWN':'无可信速度反馈，需要现场确认',
                             'STATIONARY':'实际速度反馈持续停稳', 'MOVING':'检测到运动，禁止关机'}[movement['state']], flush=True)
        print('Jetson 私有连接正常；本机和源机数据分别保存在原机。', flush=True)
        if args.check:
            peer.send(transaction_message('check', nonce, remote_boot))
            expect(peer, 'checked', nonce, remote_boot)
            print(json.dumps(dict(local=host.snapshot(), remote=hello['snapshot'], motion=movement,
                                  authorization='实际执行时需要工控机登录密码'), ensure_ascii=False, indent=2))
            print('检查完成；未认证、未停止采集、未提交关机。')
            return 0
        if movement['state'] == 'MOVING':
            raise ShutdownError('车辆仍在运动，请先现场停车并禁止自主/遥控运动')
        print('请确认车辆已物理停稳，并已禁止自主/遥控运动。保存失败会中止关机。', flush=True)
        if input('确认关闭两台电脑，请完整输入「已停稳并关机」：').strip() != '已停稳并关机':
            print('已取消，没有停止采集或关机。')
            return 0
        print('请按 sudo 提示输入工控机登录密码（不显示、不保存）。', flush=True)
        if subprocess.run(['/usr/bin/sudo', '-v'], timeout=120).returncode:
            raise ShutdownError('未取得工控机关机权限；没有停止采集')
        directory = safe_directory(host.state / 'shutdown', create=True)
        report_path = directory / (datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + nonce[:12] + '.json')
        print('本次关机记录：' + str(report_path), flush=True)
        record(nonce=nonce, localBootId=host.boot_id, remoteBootId=remote_boot,
               motion=movement, stationaryConfirmation='physical_operator_confirmation')
        with ExitStack() as stack:
            stack.enter_context(file_lock(host.state / 'computer-shutdown.lock'))
            local_power = spawn_peer(root_command(nonce), sys.stderr)
            expect(local_power, 'ready', nonce, host.boot_id)
            peer.send(transaction_message('authorize', nonce, remote_boot))
            expect(peer, 'authorized', nonce, remote_boot)
            print('权限就绪，正在结束采集并保存；请保持窗口打开。', flush=True)
            execute_order(host, peer, local_power, nonce=nonce, local_boot=host.boot_id,
                          remote_boot=remote_boot, stack=stack, record=record, motion=probe_motion,
                          lifecycle={})
            print('两台电脑已确认进入正常关机流程，正在等待系统关闭。', flush=True)
            hold_until_poweroff()
        return 0
    except (ShutdownError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('关机未全部完成：' + str(error), file=sys.stderr, flush=True)
        print('未上传文件和历史缓存保留；请查看关机记录，不要直接切断总电源。', file=sys.stderr)
        try:
            record(state='incomplete', error=str(error)[:300])
        except OSError:
            pass
        return 2
    except (KeyboardInterrupt, EOFError):
        print('操作已取消；已提交的正常关机无法撤回，尚未提交的主机保持运行。', file=sys.stderr)
        return 130
    finally:
        if local_power is not None:
            local_power.close()
        if peer is not None:
            peer.close()
        for number, handler in previous_signals.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    raise SystemExit(main())
