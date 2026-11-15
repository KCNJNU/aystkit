"""Read-only fault evidence, durable bounded offline spool, and ROS log tailing.

This module neither opens devices nor publishes commands. Event timestamps use
receipt wall time; sensor/ROS clocks are retained separately as evidence.
"""
from __future__ import annotations

from collections import OrderedDict
import heapq
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import threading
import time
import uuid
from types import SimpleNamespace

from shared.faults import (MAX_EVENT_BATCH, MAX_EVENTS_BYTES, validate_event,
                           should_record_fault, fault_group_key)

LOG = logging.getLogger(__name__)
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
SECRETS = re.compile(r'''(?i)(bearer\s+|["']?(?:[A-Za-z_]*token|password|secret|api[_-]?key|key)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&]+)''')


def clean_text(value, maximum=2048):
    value = ANSI.sub('', str(value))
    value = SECRETS.sub(lambda m: m.group(1) + '[已隐藏]', value)
    value = re.sub(r'(?i)(https?://)[^/\s@]+:[^/\s@]+@', r'\1[已隐藏]@', value)
    return ''.join(c for c in value if ord(c) >= 32 or c in '\n\r\t')[:maximum]


def default_spool_path(source_id):
    base = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
    safe_source = re.sub(r'[^A-Za-z0-9_.-]', '_', source_id)[:64]
    return base / 'robot-cloud' / ('faults-' + safe_source + '.sqlite3')


def source_timestamp(message):
    # Preserve the actual sensor clock, even reset-to-zero or simulation time.
    stamp = getattr(getattr(message, 'header', None), 'stamp', None)
    if stamp is None:
        return None
    seconds, nanoseconds = getattr(stamp, 'sec', None), getattr(stamp, 'nanosec', None)
    if type(seconds) is not int or type(nanoseconds) is not int or not 0 <= nanoseconds < 1000000000:
        return None
    return seconds + nanoseconds / 1e9


class _RawPointOffsets:
    def __init__(self, raw, start, count, endian):
        self.raw, self.start, self.count = raw, start, count
        self.unpack = struct.Struct(endian + 'I').unpack_from

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        return self.unpack(self.raw, self.start + index * 20)[0]


def custom_message_metadata(raw):
    """Read only CDR1 header/timebase/offsets, without deserializing point objects.

    CDR alignment starts after its four-byte encapsulation. CustomPoint has
    19 data bytes and four-byte array-element alignment (20-byte stride).
    The last point may omit its final alignment byte.
    """
    raw = memoryview(raw)
    if len(raw) < 20 or len(raw) > 8 * 1024 * 1024:
        raise ValueError('invalid CustomMsg size')
    encapsulation = bytes(raw[:2])
    if encapsulation not in (b'\x00\x00', b'\x00\x01'):
        raise ValueError('unsupported CustomMsg CDR encoding')
    endian = '<' if encapsulation == b'\x00\x01' else '>'
    seconds, nanoseconds, frame_length = struct.unpack_from(endian + 'iII', raw, 4)
    if not 0 <= nanoseconds < 1000000000 or not 1 <= frame_length <= 1024:
        raise ValueError('invalid CustomMsg header')
    end = 16 + frame_length
    position = 4 + ((end - 4 + 7) // 8) * 8
    if position + 20 > len(raw) or raw[end - 1] != 0:
        raise ValueError('truncated CustomMsg header')
    timebase, point_num = struct.unpack_from(endian + 'QI', raw, position)
    count = struct.unpack_from(endian + 'I', raw, position + 16)[0]
    start = position + 20
    if count != point_num or count > 200000 or (count and start + (count - 1) * 20 + 19 > len(raw)):
        raise ValueError('invalid CustomMsg point array')
    return SimpleNamespace(timestamp=seconds + nanoseconds / 1e9, timebase=timebase,
                           points=_RawPointOffsets(raw, start, count, endian))


class FaultSpool:
    """SQLite event IDs survive reconnect/restart; only acknowledged IDs leave it."""
    def __init__(self, path, capacity=10000):
        if not 3 <= capacity <= 100000:
            raise ValueError('invalid fault spool capacity')
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock, self.capacity = threading.RLock(), capacity
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA wal_autocheckpoint=64')
        self.db.execute('PRAGMA journal_size_limit=1048576')
        self.db.execute('CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, event TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def add(self, event):
        event = validate_event(event)
        if not should_record_fault(event['level'], event['code'], event['details']):
            return
        with self.lock, self.db:
            count = self.db.execute('SELECT count(*) FROM pending').fetchone()[0]
            if count >= self.capacity:
                # Reserve room for a visible loss record as well as this event.
                removed = count - self.capacity + 2
                self.db.execute('DELETE FROM pending WHERE rowid IN (SELECT rowid FROM pending ORDER BY rowid LIMIT ?)', (removed,))
                previous = self.db.execute("SELECT value FROM metadata WHERE key='dropped'").fetchone()
                total = (int(previous[0]) if previous else 0) + removed
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('dropped',?)", (str(total),))
                marker = dict(id=str(uuid.uuid4()), timestamp=event['timestamp'], level='ERROR',
                              code='FAULT_LOG_OVERFLOW', component='collector',
                              message='离线故障队列已满，最早的未上传日志已被移除',
                              details={'dropped_in_this_overflow': removed, 'dropped_total': total, 'capacity': self.capacity})
                self._insert(marker)
            self._insert(event)

    def _insert(self, event):
        self.db.execute('INSERT OR IGNORE INTO pending VALUES (?,?)',
                        (event['id'], json.dumps(event, ensure_ascii=False, separators=(',', ':'), allow_nan=False)))

    def batch(self):
        with self.lock:
            rows = self.db.execute('SELECT event FROM pending ORDER BY rowid LIMIT ?', (MAX_EVENT_BATCH,)).fetchall()
        result, size = [], 20
        for row in rows:
            encoded = row[0].encode('utf-8')
            # Leave JSON separator overhead and ensure the batch passes shared bounds.
            if result and size + len(encoded) > MAX_EVENTS_BYTES - 1024:
                break
            result.append(json.loads(row[0]))
            size += len(encoded) + 2
        return result

    def ack(self, ids):
        with self.lock, self.db:
            self.db.executemany('DELETE FROM pending WHERE id=?', ((value,) for value in ids))

    def stats(self):
        with self.lock:
            count = self.db.execute('SELECT count(*) FROM pending').fetchone()[0]
            dropped = self.db.execute("SELECT value FROM metadata WHERE key='dropped'").fetchone()
        return {'pending': count, 'capacity': self.capacity, 'dropped_total': int(dropped[0]) if dropped else 0}

    def cursor(self, key, value=None):
        with self.lock:
            if value is None:
                row = self.db.execute('SELECT value FROM metadata WHERE key=?', ('cursor:' + key,)).fetchone()
                return json.loads(row[0]) if row else None
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                ('cursor:' + key, json.dumps(value, separators=(',', ':'))))
                self.db.execute("DELETE FROM metadata WHERE key IN (SELECT key FROM metadata WHERE key LIKE 'cursor:%' ORDER BY rowid DESC LIMIT -1 OFFSET 256)")


class FaultMonitor:
    """Bounded anomaly detectors; no detection is interpreted as a root cause."""
    def __init__(self, spool, clock=time.monotonic, wall_clock=time.time):
        self.spool, self.clock, self.wall_clock = spool, clock, wall_clock
        self.recent = OrderedDict()
        self.streams = {}
        self.context = {}
        self.media_states = {}
        self.action_goals = OrderedDict()
        self.templates = {}
        self.suppressed_times = {}
        self.log_lock = threading.RLock()

    def emit(self, code, component, message, level='WARN', details=None, interval=300):
        # Select before retaining templates or filling the durable upload queue.
        if not should_record_fault(level, code, details):
            return None
        message, component = clean_text(message), clean_text(component, 160)
        key = (level, *fault_group_key(code, component, message))
        now, stamp = self.clock(), self.wall_clock()
        with self.log_lock:
            previous = self.recent.get(key)
            detail = dict(details or {})
            if self.context:
                detail['context'] = dict(self.context)
            # Keep the latest original evidence even when its dynamic numbers
            # change. A repeated issue supplements the existing incident.
            template = dict(timestamp=stamp, level=level, code=code,
                            component=component, message=message, details=detail)
            self.templates[key] = template
            if previous is not None and now - previous[0] < interval:
                self.recent[key] = (previous[0], previous[1] + 1)
                first = self.suppressed_times.get(key, (stamp, None))[0]
                self.suppressed_times[key] = (first, stamp)
                self.recent.move_to_end(key)
                return None
            detail['occurrences'] = 1
            if previous is not None and previous[1]:
                first, last = self.suppressed_times[key]
                detail.update(occurrences=previous[1] + 1,
                              similar_events_suppressed_since_previous=previous[1],
                              first_suppressed_timestamp=first,
                              last_suppressed_timestamp=last)
            event = dict(template, id=str(uuid.uuid4()), details=detail)
            self.spool.add(event)
            self.recent[key] = (now, 0)
            self.suppressed_times.pop(key, None)
            self.recent.move_to_end(key)
            while len(self.recent) > 512:
                removed, _ = self.recent.popitem(last=False)
                self.templates.pop(removed, None)
                self.suppressed_times.pop(removed, None)
            return event

    def observe_media_status(self, key, payload):
        with self.log_lock:
            status = payload.get('status')
            previous = self.media_states.get(key)
            if status == 'AVAILABLE':
                if previous is not None and previous != 'AVAILABLE':
                    self.emit('CAMERA_PREVIEW_RECOVERED', key, '相机预览数据已恢复', 'INFO',
                              {'topic': payload.get('topic'), 'previous_status': previous}, interval=0)
                self.media_states[key] = status
            elif previous == 'AVAILABLE' and status in ('STALE', 'ERROR', 'DISCONNECTED', 'OFFLINE', 'WAITING_UPSTREAM', 'UNAVAILABLE'):
                self.media_states[key] = status
                self.emit('CAMERA_PREVIEW_INTERRUPTED', key, '已运行相机的预览数据中断', 'WARN',
                          {'topic': payload.get('topic'), 'preview_status': status,
                           'reason': clean_text(payload.get('reason', ''), 500),
                           'cause': '预览链路观察；不据此确认相机硬件故障'})

    def observe_action_status(self, topic, message):
        for status in list(getattr(message, 'status_list', ()))[:32]:
            goal = getattr(status, 'goal_info', None)
            raw_id = getattr(getattr(goal, 'goal_id', None), 'uuid', ())
            identity = bytes(raw_id).hex()
            if len(identity) != 32:
                continue
            key = topic, identity
            previous = self.action_goals.get(key)
            self.action_goals[key] = int(status.status)
            self.action_goals.move_to_end(key)
            if int(status.status) == 6 and previous != 6:
                self.emit('NAVIGATION_ABORTED', topic, '导航动作执行失败，动作服务器已终止目标', 'ERROR',
                          {'origin': topic, 'goal_id': identity, 'action_status': 6,
                           'cause': '动作终态仅确认执行失败，具体原因需关联节点日志'})
        while len(self.action_goals) > 256:
            self.action_goals.popitem(last=False)

    def observe_stream(self, topic, timestamp=None, timeout=2.0, timing=False):
        now = self.clock()
        previous = self.streams.get(topic)
        kind = 'IMU' if 'imu' in topic.lower() else 'LIDAR' if 'livox' in topic.lower() else 'STREAM'
        if previous is not None:
            if previous['stale']:
                self.emit('DEVICE_DATA_RECOVERED', topic, '设备数据已恢复', 'INFO',
                          {'topic': topic, 'interruption_s': round(max(0, now - previous['received']), 6)}, interval=0)
            if timing and timestamp is not None and previous['timestamp'] is not None:
                delta = timestamp - previous['timestamp']
                evidence = {'topic': topic, 'previous_source_timestamp': previous['timestamp'],
                            'source_timestamp': timestamp, 'source_delta_s': round(delta, 9),
                            'receive_gap_s': round(max(0, now - previous['received']), 6)}
                if delta < -1e-6:
                    self.emit(kind + '_TIMESTAMP_ROLLBACK', topic, '传感器时间戳回退', 'ERROR', evidence)
                elif delta > (0.25 if kind == 'IMU' else 0.5):
                    self.emit(kind + '_TIMESTAMP_GAP', topic, '传感器采样时间出现断档', 'WARN', evidence)
        self.streams[topic] = {'received': now, 'timestamp': timestamp,
                               'timeout': timeout, 'stale': False}

    def flush_suppressed(self, force=False):
        now = self.clock()
        with self.log_lock:
            for key, (last_emitted, count) in list(self.recent.items()):
                if count and (force or now - last_emitted >= 300):
                    template = self.templates[key]
                    first, last = self.suppressed_times[key]
                    detail = dict(template['details'], occurrences=count,
                                  suppressed_count=count,
                                  first_suppressed_timestamp=first,
                                  last_suppressed_timestamp=last)
                    # Reuse the fault code/message so the server updates one row
                    # instead of creating a separate generic summary record.
                    summary = dict(template, id=str(uuid.uuid4()), timestamp=last, details=detail)
                    self.spool.add(summary)
                    self.recent[key] = (now, 0)
                    self.suppressed_times.pop(key, None)

    def tick(self):
        self.flush_suppressed()
        now = self.clock()
        for topic, stream in list(self.streams.items()):
            gap = max(0, now - stream['received'])
            if not stream['stale'] and gap > stream['timeout']:
                stream['stale'] = True
                self.emit('DEVICE_DATA_INTERRUPTED', topic, '已运行设备的数据接收中断', 'WARN',
                          {'topic': topic, 'receive_gap_s': round(gap, 6), 'timeout_s': stream['timeout'],
                           'last_source_timestamp': stream['timestamp'],
                           'cause': '未确认；采集停止、传输或处理延迟均需排查'})

    def observe_points(self, message, topic='/livox/lidar'):
        points = getattr(message, 'points', ())
        if len(points) < 2:
            return
        # Bounded metadata sample; never copy point payloads or compare offsets
        # across messages, where returning to zero is the normal contract.
        stride = max(1, math.ceil((len(points) - 1) / 1023))
        indices = list(range(0, len(points), stride))
        if indices[-1] != len(points) - 1:
            indices.append(len(points) - 1)
        previous = None
        for index in indices:
            point = points[index]
            offset = point if isinstance(point, int) else getattr(point, 'offset_time', None)
            if not isinstance(offset, int):
                return
            if previous is not None and offset < previous[1]:
                wraps = previous[1] > 3_000_000_000 and offset < 1_000_000_000
                self.emit('LIDAR_POINT_TIME_WRAP' if wraps else 'LIDAR_POINT_TIME_NONMONOTONIC', topic,
                          '激光点相对时间出现数值回绕' if wraps else '激光点相对时间出现倒序', 'ERROR',
                          {'topic': topic, 'point_count': len(points), 'sample_stride': stride,
                           'previous_point_index': previous[0], 'point_index': index,
                           'previous_offset_ns': previous[1], 'offset_ns': offset,
                           'timebase_ns': int(getattr(message, 'timebase', 0)),
                           'cause': '未确认；需核对原始点序与驱动时间转换'})
                return
            previous = index, offset

    def observe_diagnostics(self, message):
        for status in list(getattr(message, 'status', ()))[:32]:
            raw = getattr(status, 'level', 0)
            level = int.from_bytes(raw, 'little') if isinstance(raw, bytes) else int(raw)
            if level <= 0:
                continue
            values = {clean_text(v.key, 100): clean_text(v.value, 200)
                      for v in list(getattr(status, 'values', ()))[:12]}
            if level == 1:
                # A generic diagnostic warning is routine noise, but explicit
                # timing/localization/control failures retain their key type.
                self.observe_log(clean_text(getattr(status, 'name', 'diagnostics'), 160),
                                 clean_text(getattr(status, 'message', '')), 30,
                                 origin='/diagnostics',
                                 extra={'hardware_id': clean_text(getattr(status, 'hardware_id', ''), 100),
                                        'values': values})
                continue
            self.emit('ROS_DIAGNOSTIC_STALE' if level == 3 else 'ROS_DIAGNOSTIC_ERROR',
                      clean_text(getattr(status, 'name', 'diagnostics'), 160) or 'diagnostics',
                      clean_text(getattr(status, 'message', '节点诊断告警')) or '节点诊断告警',
                      'ERROR' if level >= 2 else 'WARN',
                      {'origin': '/diagnostics', 'hardware_id': clean_text(getattr(status, 'hardware_id', ''), 100),
                       'values': values})

    def observe_log(self, component, text, level=30, source_timestamp=None, origin='/rosout', extra=None):
        component, text = clean_text(component, 160) or 'unknown', clean_text(text)
        if not text or component.startswith('robot_cloud_collector'):
            return
        lower, name = text.lower(), component.lower()
        sensor = any(token in name + ' ' + lower for token in ('lidar', 'livox', 'imu', 'fastlio', 'fast_lio', '雷达'))
        navigation = any(token in name for token in ('nav', 'controller', 'coverage', 'follow_path', 'spin', 'behavior', 'localization', 'rtabmap', 'odometry', '定位', '控制器', '导航'))
        code, severity = None, 'FATAL' if level >= 50 else 'ERROR' if level >= 40 else 'WARN'
        if sensor and ('loop back' in lower or 'loopback' in lower or ('timestamp' in lower and any(t in lower for t in ('backward', 'rollback'))) or any(t in lower for t in ('时间戳回退', '时间回退'))):
            code, severity = 'SENSOR_TIME_ROLLBACK_REPORTED', 'ERROR'
        elif sensor and ('not synced' in lower or 'not synchronized' in lower or 'time sync' in lower and ('fail' in lower or 'error' in lower) or '时间同步失败' in lower or '时钟不同步' in lower):
            code, severity = 'SENSOR_TIME_SYNC_REPORTED', 'ERROR'
        elif sensor and ('time' in lower and ('wrap' in lower or 'overflow' in lower) or '点时间回绕' in lower):
            code, severity = 'POINT_TIME_WRAP_REPORTED', 'ERROR'
        elif sensor and any(token in lower for token in ('timestamp gap', 'time gap', 'timestamp discontinuity', '时间断档')):
            code = 'SENSOR_TIME_GAP_REPORTED'
        elif navigation and level >= 30 and any(token in lower for token in ('oscillat', '反复转向', '反复反向', '控制振荡')):
            code = 'CONTROLLER_OSCILLATION'
        elif any(token in lower for token in ('timed out', 'timeout', 'time out', 'time allowance', '执行超时', '导航超时', '动作超时')):
            code = 'NAVIGATION_TIMEOUT' if navigation else 'NODE_TIMEOUT'
        elif navigation and level >= 30 and any(token in lower for token in ('localization lost', 'localization fail', 'lost localization', 'odometry jump', 'invalid odometry', '定位丢失', '定位跳变', '定位失败', '里程计跳变', '定位不稳', '位置跳变')):
            code = 'LOCALIZATION_WARNING'
        elif any(token in name for token in ('localization', 'rtabmap')) and level >= 30 and any(token in lower for token in ('lost', 'jump', 'stale', 'fail', 'invalid')):
            code = 'LOCALIZATION_WARNING'
        elif any(token in lower for token in ('collision', 'obstacle', '障碍')) and any(token in lower for token in ('stop', 'blocked', 'unsafe', 'detected', '停车', '阻塞')):
            code = 'OBSTACLE_STOP_REPORTED'
        elif any(token in lower for token in ('aborting', 'aborted', 'failed to make progress', 'failed to find', 'no valid trajectories', '导航失败', '路径执行失败')):
            code = 'NAVIGATION_FAILURE' if navigation else 'NODE_ERROR'
        elif level >= 40:
            code = 'ROS_ERROR'
        if code:
            if level >= 50:
                severity = 'FATAL'
            details = {'origin': origin, 'raw_message': text, 'source_timestamp': source_timestamp,
                       'cause': '来源日志报告；尚未自动确认根因'}
            details.update(extra or {})
            self.emit(code, component, text, severity, details)

    def update_context(self, system):
        zones = system.get('thermalZones', []) if isinstance(system, dict) else []
        temperatures = [{'name': clean_text(zone.get('name', zone.get('type', '温度')), 64),
                         'temperature_c': zone.get('temperatureC')}
                        for zone in zones[:8] if isinstance(zone, dict)
                        and isinstance(zone.get('temperatureC'), (int, float)) and math.isfinite(zone['temperatureC'])]
        network = []
        for interface in (system.get('network', []) if isinstance(system, dict) else [])[:8]:
            if isinstance(interface, dict):
                network.append({key: interface[key] for key in ('name', 'up', 'speedMbps', 'rxBytesPerSec', 'txBytesPerSec')
                                if key in interface and isinstance(interface[key], (str, int, float, bool))})
        self.context = {'temperatures': temperatures, 'network': network}
        for temp in temperatures:
            if temp['temperature_c'] >= 85:
                self.emit('HOST_TEMPERATURE_HIGH', temp['name'], '工控机温度达到监测阈值', 'WARN',
                          {'temperature_c': temp['temperature_c'], 'threshold_c': 85,
                           'cause': '仅记录温度读数，不据此确认发生降频或数据中断'})


class RosLogTail:
    """Tail only bounded known ROS files; startup history is never re-reported.

Persisted file cursors preserve appended evidence over collector restarts.
New files discovered after startup are read from their beginning, bounded per
poll. Existing uncursored files are registered at EOF on the initial scan.
"""
    def __init__(self, monitor, root=None, file_limit=24, byte_limit=65536):
        self.monitor = monitor
        self.root = Path(root or os.environ.get('ROS_LOG_DIR', str(Path.home() / '.ros/log')))
        self.file_limit, self.byte_limit = file_limit, byte_limit
        self.initial = True
        self.files = {}
        self.last_scan = -float('inf')
        self.started_at = monitor.wall_clock()
        self.turn = 0

    def _limited(self, reason, path=None):
        self.monitor.emit('FAULT_MONITOR_LIMITED', 'ros_log_tail', '部分 ROS 文件日志暂时无法监测', 'WARN',
                          {'log_root': str(self.root), 'log_file': str(path) if path is not None else None,
                           'reason': reason})

    def _candidates(self):
        if self.root.is_symlink():
            self._limited('日志目录是符号链接，已按边界规则跳过')
            return []
        candidates = []
        try:
            with os.scandir(self.root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 2048:
                        self._limited('日志目录超过单次 2048 项发现上限')
                        break
                    path = Path(entry.path)
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False) and entry.name.endswith('.log'):
                            stat = entry.stat(follow_symlinks=False)
                        elif entry.is_dir(follow_symlinks=False):
                            path = path / 'launch.log'
                            if path.is_symlink() or not path.is_file():
                                continue
                            stat = path.stat()
                        else:
                            continue
                        item = stat.st_mtime, str(path), stat
                        if len(candidates) < self.file_limit:
                            heapq.heappush(candidates, item)
                        elif item[:2] > candidates[0][:2]:
                            heapq.heapreplace(candidates, item)
                    except PermissionError:
                        self._limited('没有日志文件或子目录读取权限', path)
                    except OSError:
                        continue  # Files may disappear during normal ROS log rotation.
        except FileNotFoundError:
            return []  # ROS need not have been started yet; scan again later.
        except PermissionError:
            self._limited('没有日志目录读取权限')
        except OSError:
            self._limited('日志目录读取失败')
        return sorted(candidates, reverse=True)

    def poll(self):
        now = self.monitor.clock()
        if now - self.last_scan >= 5:
            active = {}
            for _, path, stat in self._candidates():
                saved = self.files.get(path) or self.monitor.spool.cursor(path)
                identity = [stat.st_dev, stat.st_ino]
                if saved and saved.get('identity') == identity and saved.get('offset', 0) <= stat.st_size:
                    position = saved
                else:
                    # Older logs may re-enter the bounded candidate set. Do not
                    # interpret their prior contents as a fresh fault.
                    position = {'identity': identity, 'offset': stat.st_size if self.initial or stat.st_mtime < self.started_at else 0}
                active[path] = position
                if saved != position or saved is None:
                    self.monitor.spool.cursor(path, position)
            self.files, self.initial, self.last_scan = active, False, now
        remaining = self.byte_limit
        items = list(self.files.items())
        if items:
            turn = self.turn % len(items)
            items = items[turn:] + items[:turn]
            self.turn += 1
        for path, cursor in items:
            if remaining <= 0:
                break
            try:
                stat = os.stat(path, follow_symlinks=False)
                identity = [stat.st_dev, stat.st_ino]
                if identity != cursor['identity'] or stat.st_size < cursor['offset']:
                    # Same-path rotation/truncation starts a new stream.
                    cursor = self.files[path] = {'identity': identity, 'offset': 0}
                if stat.st_size == cursor['offset']:
                    continue
                with open(path, 'rb') as stream:
                    stream.seek(cursor['offset'])
                    chunk = stream.read(min(remaining, 16384))
                remaining -= len(chunk)
                # Process complete lines only. Very long lines are truncated
                # after 8 KiB and consumed to avoid a permanent tail blockage.
                end = chunk.rfind(b'\n') + 1
                if not end and len(chunk) >= 8192:
                    end = len(chunk)
                if not end:
                    continue
                lines = chunk[:end].splitlines(keepends=True)[:128]
                end = sum(len(line) for line in lines)
                for raw in lines:
                    text = raw[:8192].decode('utf-8', 'replace').rstrip('\r\n')
                    severity = 50 if '[FATAL]' in text else 40 if '[ERROR]' in text else 30 if '[WARN]' in text else 20
                    # Plain FAST-LIO stderr has no severity prefix.
                    if severity >= 30 or any(p in text.lower() for p in ('loop back', 'not synced', 'timestamp', 'time wrap')):
                        self.monitor.observe_log(Path(path).stem, text, severity, origin='ros_log_file',
                                                 extra={'log_file': str(Path(path).relative_to(self.root)), 'file_offset': cursor['offset']})
                cursor['offset'] += end
                self.monitor.spool.cursor(path, cursor)
            except PermissionError:
                self._limited('没有日志文件读取权限', path)
            except (OSError, ValueError):
                continue
