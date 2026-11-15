"""Read-only test odometry, with a durable queue independent of live previews."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from shared.test_metrics import (MAX_METRIC_SAMPLES, MAX_SPEED_MPS,
                                 canonical_uuid, validate_test_metrics_payload)
from .faults import source_timestamp

TOPIC = '/odom_nav'
EMPTY_STATUS = dict(applied_revision=0, run_id=None, state='idle',
                    odom_status='WAITING_UPSTREAM', last_sample_timestamp=None,
                    rejected_samples=0, dropped_samples=0)


def default_spool_path(source_id):
    base = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))
    name = re.sub(r'[^A-Za-z0-9_.-]', '_', source_id)[:64]
    return base/'robot-cloud'/('test-metrics-'+name+'.sqlite3')


class TestMetricSpool:
    """Acknowledged IDs alone leave the queue; no history uses LatestQueue."""
    __test__ = False

    def __init__(self, path, capacity=100_000):
        if type(capacity) is not int or not 32 <= capacity <= 200_000:
            raise ValueError('invalid metrics capacity')
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock, self.capacity = threading.RLock(), capacity
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA wal_autocheckpoint=64')
        self.db.execute('PRAGMA journal_size_limit=1048576')
        self.db.execute('CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, sample TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.commit()

    def add(self, sample):
        sample = validate_test_metrics_payload(
            {'samples':[sample], 'status':EMPTY_STATUS},
            now=max(time.time(), sample['end']))['samples'][0]
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM pending WHERE id=?', (sample['id'],)).fetchone():
                return True
            if self.db.execute('SELECT count(*) FROM pending').fetchone()[0] >= self.capacity:
                dropped = self._metadata('dropped') or 0
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('dropped',?)",
                                (json.dumps(dropped+1),))
                return False
            self.db.execute('INSERT INTO pending VALUES (?,?)',
                            (sample['id'], json.dumps(sample, separators=(',', ':'), allow_nan=False)))
            return True

    def batch(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT sample FROM pending ORDER BY rowid LIMIT ?', (MAX_METRIC_SAMPLES,))]

    def ack(self, identities):
        with self.lock, self.db:
            self.db.executemany('DELETE FROM pending WHERE id=?', ((value,) for value in identities))

    def _metadata(self, key):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def metadata(self, key, value=None):
        with self.lock:
            if value is None:
                return self._metadata(key)
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                (key, json.dumps(value, allow_nan=False, separators=(',', ':'))))

    def dropped(self):
        with self.lock:
            return self._metadata('dropped') or 0

    def close(self):
        with self.lock:
            self.db.close()


class TestMetricTracker:
    """Manual recording affects only counters; never publishes robot commands."""
    __test__ = False

    def __init__(self, spool, clock=time.monotonic, wall_clock=time.time, fault_monitor=None):
        self.spool, self.clock, self.wall_clock = spool, clock, wall_clock
        self.fault_monitor = fault_monitor
        self.lock = threading.RLock()
        saved = spool.metadata('control') or {}
        self.control = dict(revision=0, run_id=None, state='idle', since=0)
        try:
            self.control = self._control(saved.get('value', self.control))
        except (TypeError, ValueError):
            pass
        offset = saved.get('offset', 0)
        self.offset = offset if type(offset) in (int, float) and math.isfinite(offset) else 0
        self.server_anchor, self.mono_anchor = wall_clock()+self.offset, clock()
        self.synced = False
        self.point = self.distance_anchor = self.pending = None
        self.last_valid = self.last_sample = None
        self.error = False
        self.rejected = 0

    @staticmethod
    def _control(value):
        if not isinstance(value, dict) or set(value) != {'revision','run_id','state','since'}:
            raise ValueError('invalid test control')
        if type(value['revision']) is not int or value['revision'] < 0:
            raise ValueError('invalid test revision')
        if value['state'] not in ('idle','running','paused'):
            raise ValueError('invalid test state')
        if value['run_id'] is not None:
            canonical_uuid(value['run_id'])
        if ((value['state']=='idle') != (value['run_id'] is None)
                or value['state']!='idle' and value['revision']==0):
            raise ValueError('inconsistent test control')
        if type(value['since']) not in (int, float) or not math.isfinite(value['since']) or value['since']<0:
            raise ValueError('invalid test control time')
        return dict(value)

    def _server_now(self, now=None):
        return self.server_anchor + ((self.clock() if now is None else now)-self.mono_anchor)

    def apply_control(self, control, server_timestamp, round_trip_s=0):
        control = self._control(control)
        if type(server_timestamp) not in (int, float) or not math.isfinite(server_timestamp) or server_timestamp<=0:
            raise ValueError('invalid server time')
        with self.lock:
            if control['revision'] < self.control['revision']:
                return
            changed = control != self.control
            if not self.synced or changed:
                self._flush_locked()
                # Anchor to a server clock and then advance monotonically.
                # Wall-clock changes cannot introduce a jump inside a record.
                correction = min(1.0, max(0.0, round_trip_s))/2
                self.server_anchor = server_timestamp + correction
                self.mono_anchor = self.clock()
                self.offset = self.server_anchor-self.wall_clock()
                self.point = self.distance_anchor = None
                self.control = control
                self.spool.metadata('control', {'value':control,'offset':self.offset})
                self.synced = True

    def _flush_locked(self):
        if self.pending is None:
            return
        sample, self.pending = self.pending, None
        span = sample['end']-sample['start']
        if span <= 0:
            return
        sample['id'] = str(uuid.uuid4())
        sample['valid_seconds'] = span
        try:
            accepted = self.spool.add(sample)
        except ValueError:
            self.rejected += 1
            self.error = True
            return
        if not accepted and self.fault_monitor is not None:
            self.fault_monitor.emit('TEST_METRICS_OVERFLOW', 'test_metrics',
                '测试里程待上传队列已满，部分里程数据未能保存', 'ERROR',
                {'dropped_samples':self.spool.dropped(),'capacity':self.spool.capacity})

    def observe(self, message):
        now = self.clock()
        try:
            frame = message.header.frame_id.lstrip('/')
            child = message.child_frame_id.lstrip('/')
            if (frame, child) != ('odom', 'drive_base_link'):
                raise ValueError('unexpected odometry frame')
            pose = message.pose.pose
            x, y = float(pose.position.x), float(pose.position.y)
            stamp = source_timestamp(message)
            if not math.isfinite(x) or not math.isfinite(y) or stamp is None:
                raise ValueError('invalid odometry')
        except (AttributeError, TypeError, ValueError, OverflowError):
            with self.lock:
                self._flush_locked()
                self.point = self.distance_anchor = None
                self.rejected += 1
                self.error = True
            return
        with self.lock:
            server = self._server_now(now)
            self.last_valid, self.last_sample = now, server
            self.error = False
            if self.control['state'] != 'running':
                self.point = self.distance_anchor = None
                return
            current = (x,y,stamp,now,server)
            previous, self.point = self.point, current
            if previous is None:
                self.distance_anchor = (x,y)
                return
            step = math.hypot(x-previous[0],y-previous[1])
            source_delta, receive_delta = stamp-previous[2], now-previous[3]
            if (not 0<source_delta<=2 or not 0<receive_delta<=2
                    or step>MAX_SPEED_MPS*source_delta+.02):
                self._flush_locked()
                self.distance_anchor = (x,y)
                self.rejected += 1
                self.error = True
                return
            # Preserve the anchor across sub-threshold samples so slow motion
            # accumulates instead of being discarded at every high-rate frame.
            distance = math.hypot(x-self.distance_anchor[0],y-self.distance_anchor[1])
            if distance>=.005:
                self.distance_anchor = (x,y)
            else:
                distance = 0.
            if self.pending is not None and server-self.pending['start']>2:
                self._flush_locked()
            if self.pending is None:
                self.pending = dict(run_id=self.control['run_id'],revision=self.control['revision'],
                                    start=previous[4],end=server,distance_m=distance)
            else:
                self.pending['end'] = server
                self.pending['distance_m'] += distance
            if self.pending['end']-self.pending['start']>=.5:
                self._flush_locked()

    def payload(self):
        with self.lock:
            self._flush_locked()
            status = ('ERROR' if self.error else 'WAITING_UPSTREAM' if self.last_valid is None else
                      'STALE' if self.clock()-self.last_valid>2 else 'AVAILABLE')
            return dict(samples=self.spool.batch(), status=dict(
                applied_revision=self.control['revision'],run_id=self.control['run_id'],
                state=self.control['state'],odom_status=status,last_sample_timestamp=self.last_sample,
                rejected_samples=self.rejected,dropped_samples=self.spool.dropped()))

    def flush(self):
        with self.lock:
            self._flush_locked()
