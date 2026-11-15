"""Start camera sources and inspect existing radar, independently of navigation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time

from .archive_source_link import ssh_options
from .archive_start_transport import (REMOTE_BOOTSTRAP, RemoteStartupCancelled,
                                      RemoteStartupError, run_remote_startup)


HELPER = Path(__file__).resolve().parents[1] / 'deploy/scripts/device_sources.py'
MAX_REPORT_BYTES = 262144
# Inspect the existing radar publishers only; offline hardware must not hold
# camera recording behind a driver startup or first-message wait.
LIDAR_STATUS_TIMEOUT = 8
# Shared-camera shutdown alone may need 105 s to drain and exit. Keep the
# transport alive through that deadline plus SSH and owner/final-state checks.
CAMERA_SHUTDOWN_TIMEOUT = 180
LABELS = {'mid360': 'MID360 雷达与 IMU', 'stl19p': 'STL-19P 二维雷达',
          'bluesea': '蓝海二维雷达', 'berxel': 'Berxel 彩色/深度相机',
          'usb-cameras': '四路 USB 相机', 'fisheye': '鱼眼相机',
          'robot-native-archive': '相机主机归档服务',
          'berxel-preview': 'Berxel 预览编码',
          'berxel-frames': 'Berxel 彩色/深度及网页连续帧检查',
          'robot-cloud-yolo-observer': '相机识别服务'}
STATES = {'started': '已启动', 'reused': '复用现有设备', 'skipped': '未连接或未配置',
          'failed': '启动失败', 'partial': '部分可用', 'inactive': '未运行'}


class DeviceStartupCancelled(RuntimeError):
    pass


class DeviceStartupError(RuntimeError):
    pass


def _terminate(process):
    """Only stop this invocation's helper; sensor systemd units stay running."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=3)


def run_startup(command, *, payload=None, cancelled=lambda: False, timeout=120,
                popen=None):
    """Bound startup and allow cancellation while communicating with SSH."""
    if cancelled():
        raise DeviceStartupCancelled()
    environment = dict(os.environ)
    environment.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    environment.setdefault('DBUS_SESSION_BUS_ADDRESS',
                           f"unix:path={environment['XDG_RUNTIME_DIR']}/bus")
    process = (popen or subprocess.Popen)(command, stdin=subprocess.PIPE if payload is not None
        else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, env=environment)
    deadline = time.monotonic() + timeout
    notice = time.monotonic() + 15
    pending_input = payload
    try:
        while True:
            if cancelled():
                raise DeviceStartupCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeviceStartupError('设备启动检查超时，已启动的设备保持运行')
            try:
                data = pending_input
                pending_input = None
                stdout, stderr = process.communicate(input=data, timeout=min(.25, remaining))
                if len(stdout) > MAX_REPORT_BYTES or len(stderr) > MAX_REPORT_BYTES:
                    raise DeviceStartupError('设备启动报告过大')
                if cancelled():
                    raise DeviceStartupCancelled()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired as error:
                if len(error.output or b'') > MAX_REPORT_BYTES or len(error.stderr or b'') > MAX_REPORT_BYTES:
                    raise DeviceStartupError('设备启动报告过大')
                if time.monotonic() >= notice:
                    print('设备启动检查仍在进行；按 Ctrl+C 可取消后续启动。', flush=True)
                    notice = time.monotonic() + 15
    finally:
        if process.poll() is None:
            _terminate(process)


def _report(result, host):
    try:
        report = json.loads(result.stdout)
        rows = report['devices']
        if (not isinstance(report, dict) or report.get('role') != host
                or report.get('recordingOnly') is not True or not isinstance(rows, list) or not rows):
            raise ValueError()
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('key'), str)
                    or row['key'] in seen or row.get('state') not in STATES):
                raise ValueError()
            seen.add(row['key'])
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise DeviceStartupError('没有收到有效的独立设备启动报告') from None
    # Successful process exit alone never proves that all devices are ready.
    report['ok'] = (result.returncode == 0 and report.get('ok') is True
                    and all(row['state'] in ('started', 'reused') for row in rows))
    return report


def inspect_capture_radar(values, *, cancelled=lambda: False, runner=None):
    """Inspect local publishers only, without camera, SSH or source control."""
    if cancelled():
        raise DeviceStartupCancelled()
    if not HELPER.is_file() or HELPER.is_symlink() or HELPER.stat().st_size > 131072:
        raise DeviceStartupError('缺少只读雷达状态检查程序')
    command = ['/usr/bin/python3', '-B', str(HELPER), 'ipc', '--read-only-sources',
               '--recording-only', '--status']
    result = (runner or run_startup)(command, cancelled=cancelled, timeout=LIDAR_STATUS_TIMEOUT)
    return _report(result, 'ipc')


def start_capture_devices(values, *, cancelled=lambda: False, runner=None):
    """Keep available sources when another source or the private link is down."""
    if not HELPER.is_file() or HELPER.is_symlink() or HELPER.stat().st_size > 131072:
        raise DeviceStartupError('缺少独立设备启动程序')
    code = HELPER.read_bytes()
    reports, failures = {}, []
    for host, label in (('ipc', '本机雷达'), ('jetson', '相机主机')):
        if cancelled():
            raise DeviceStartupCancelled()
        if host == 'ipc':
            print('只读检查现有雷达；不启动驱动，不等待雷达开启或首帧，其他可用设备继续采集。', flush=True)
        else:
            print(f'正在开启或复用{label}；不启动 FAST-LIO、导航或建图。'
                  '保留现有识别进程，不额外开启 YOLO。', flush=True)
        try:
            if host == 'ipc':
                report = inspect_capture_radar(values, cancelled=cancelled, runner=runner)
            else:
                options, target = ssh_options(values)
                camera_args = [host, '--recording-only', '--with-camera-preview']
                if str(values.get('ARCHIVE_START_CAMERA_YOLO', '')).lower() in ('1', 'true', 'yes'):
                    camera_args.append('--with-camera-yolo')
                command = options + [target, shlex.join(
                    ['/usr/bin/python3', '-B', '-c', REMOTE_BOOTSTRAP, *camera_args])]
                try:
                    result = (runner or run_remote_startup)(command, payload=code,
                                                           cancelled=cancelled, timeout=150)
                except RemoteStartupCancelled:
                    raise DeviceStartupCancelled() from None
                except RemoteStartupError as error:
                    raise DeviceStartupError(str(error)) from None
                report = _report(result, host)
            reports[host] = report
            for row in report['devices']:
                detail = row.get('error') or row.get('message') or ''
                print(f"{STATES[row['state']]}：{LABELS.get(row['key'], row['key'])}"
                      + (f'；{detail}' if detail else ''), flush=True)
                if row['state'] not in ('started', 'reused'):
                    failures.append(dict(host=host, key=row['key'], state=row['state']))
            if not report['ok'] and not any(row['host'] == host for row in failures):
                failures.append(dict(host=host, key=host, state='failed'))
        except (OSError, ValueError, DeviceStartupError, subprocess.SubprocessError) as error:
            reports[host] = dict(role=host, ok=False, recordingOnly=True,
                                 devices=[], error=str(error),
                                 **(dict(readOnlySources=True, sourceControl=False) if host == 'ipc' else {}))
            failures.append(dict(host=host, key=host, state='failed'))
            print(f'{label}未全部就绪：{error}；其余可用设备仍会录制。', flush=True)
    return dict(ok=not failures, hosts=reports, incompleteDevices=failures)


def stop_owned_capture_cameras(values, *, ownership=None, timeout=CAMERA_SHUTDOWN_TIMEOUT, runner=None):
    """Finish this capture's auxiliaries; never stop shared sensor sources.

    A successful startup is not exclusive ownership of a hardware stream:
    navigation may have subscribed after capture started. Unknown/legacy leases
    therefore retain everything. The explicit all-device shutdown below is a
    separate operator action and is never used by the desktop capture lifecycle.
    """
    required = {'usb-cameras', 'berxel', 'fisheye', 'berxel-preview', 'robot-cloud-yolo-observer'}
    retained = lambda reason: dict(ok=True, role='jetson', stopOwnedCameras=True, dryRun=False,
        devices=[dict(key=key, state='retained', reason=reason) for key in sorted(required)])
    if ownership is None:
        return retained('no_capture_ownership')
    if (not isinstance(ownership, dict) or ownership.get('schemaVersion') != 1
            or not isinstance(ownership.get('units'), list)):
        raise DeviceStartupError('采集设备归属信息无效；已保留现有相机')
    if not ownership['units']:
        return retained('no_owned_camera_units')
    encoded = json.dumps(ownership, ensure_ascii=True, separators=(',', ':'))
    if len(encoded.encode()) > 16384:
        raise DeviceStartupError('采集设备归属信息过大；已保留现有相机')
    if not HELPER.is_file() or HELPER.is_symlink() or HELPER.stat().st_size > 131072:
        raise DeviceStartupError('缺少采集设备收尾程序；已保留现有相机')
    options, target = ssh_options(values)
    command = options + [target, shlex.join(['/usr/bin/python3', '-B', '-c', REMOTE_BOOTSTRAP,
        'jetson', '--stop-owned-recording-cameras', '--camera-ownership-json', encoded])]
    try:
        result = (runner or run_remote_startup)(command, payload=HELPER.read_bytes(), timeout=timeout,
            notice='正在收尾本次采集的辅助服务；共享相机保持运行。')
    except (RemoteStartupCancelled, RemoteStartupError) as error:
        raise DeviceStartupError('采集辅助服务收尾未获确认：' + str(error)) from None
    try:
        report = json.loads(result.stdout)
        rows = report['devices']
        if (not isinstance(report, dict) or report.get('role') != 'jetson'
                or report.get('stopOwnedCameras') is not True or report.get('dryRun') is not False
                or not isinstance(rows, list) or len(rows) != len(required)
                or {row.get('key') for row in rows if isinstance(row, dict)} != required
                or not all(isinstance(row, dict) and row.get('state') in
                    ('stopped', 'already_stopped', 'retained', 'failed') for row in rows)):
            raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise DeviceStartupError('没有收到完整的采集辅助服务收尾确认；现有相机保持原状') from None
    report['ok'] = (result.returncode == 0 and report.get('ok') is True
        and all(row['state'] in ('stopped', 'already_stopped', 'retained') for row in rows))
    return report


def stop_capture_cameras(values, *, timeout=CAMERA_SHUTDOWN_TIMEOUT, runner=None):
    """Close all known cameras through the private link without stopping ROS."""
    if not HELPER.is_file() or HELPER.is_symlink() or HELPER.stat().st_size > 131072:
        raise DeviceStartupError('缺少独立相机关闭程序，尚未确认相机已关闭')
    options, target = ssh_options(values)
    command = options + [target, shlex.join([
        '/usr/bin/python3', '-B', '-c', REMOTE_BOOTSTRAP,
        'jetson', '--stop-recording-cameras'])]
    def notice(message):
        try:
            print(message, flush=True)
        except (OSError, ValueError):
            pass
    notice('正在关闭全部相机，包括 Berxel 深度相机；雷达、导航和补传继续运行。')
    try:
        result = (runner or run_remote_startup)(command, payload=HELPER.read_bytes(), timeout=timeout,
            notice='相机关闭仍在进行，正在等待设备正常退出。')
    except (RemoteStartupCancelled, RemoteStartupError) as error:
        raise DeviceStartupError('相机关闭未获确认：'+str(error)) from None
    try:
        report = json.loads(result.stdout)
        rows = report['devices']
        required = {'usb-cameras', 'berxel', 'fisheye', 'berxel-preview', 'robot-cloud-yolo-observer'}
        if (not isinstance(report, dict) or report.get('role') != 'jetson'
                or report.get('stopCameras') is not True or report.get('dryRun') is not False
                or not isinstance(rows, list) or len(rows) != len(required)
                or {row.get('key') for row in rows if isinstance(row, dict)} != required
                or not all(isinstance(row, dict) and row.get('state') in
                    ('stopped', 'already_stopped', 'failed', 'external_running') for row in rows)):
            raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise DeviceStartupError('没有收到完整的相机关闭确认，不能宣称相机已全部关闭') from None
    report['ok'] = (result.returncode == 0 and report.get('ok') is True
                    and all(row['state'] in ('stopped', 'already_stopped') for row in rows))
    for row in rows:
        label = LABELS.get(row['key'], row['key'])
        success = row['state'] in ('stopped', 'already_stopped')
        detail = row.get('error') or row.get('message') or ''
        notice(('已关闭：' if success else '尚未确认关闭：')+label+(f'；{detail}' if detail else ''))
    return report
