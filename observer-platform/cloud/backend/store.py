"""Latest-state SQLite checkpoints and bounded in-memory media, no ROS imports."""
from __future__ import annotations

import copy
import json
import math
import sqlite3
import time
from pathlib import Path

from shared.protocol import CAMERA_KEYS, CLOUD_KEYS, MAP_KEYS, MEDIA_KEYS, decode_media
from .fault_logs import FaultLogs
from .test_stats import TestStats
from .test_issues import TestIssues

ARUCO_PREVIEW_TOPIC = '/observer_camera/aruco_detections'
ARUCO_TTL_MS = 5000

VISUAL_GROUPS = {
    'clouds': frozenset({'lidar', 'registered', 'depthPoints'}),
    'scans': frozenset({'bluesea', 'scan'}),
    'maps': frozenset({'map', 'localCostmap', 'globalCostmap'}),
}


class ReplayError(ValueError):
    def __init__(self, reason, expected_sequence):
        super().__init__(reason.replace('_', ' '))
        self.reason, self.expected_sequence = reason, expected_sequence


def _age_tree(value, elapsed, stale=False, upstream_limit=2, path=(), _aruco_preview=False):
    """Accumulate real upstream age, propagating each object's own stale boundary."""
    if isinstance(value, list):
        return [_age_tree(v, elapsed, stale, upstream_limit, path, _aruco_preview) for v in value]
    if not isinstance(value, dict):
        return value
    own_stale = stale
    # Preserve original sensor semantics; event/latched data is valid while connected.
    topic = value.get('topic')
    aruco_preview = (_aruco_preview or topic == ARUCO_PREVIEW_TOPIC
                     or path in (('devices', 'visionDocking', 'arucoPreview'),
                                 ('devices', 'visionDocking', 'topics', 'arucoPreview'),
                                 ('ros2', 'topics', ARUCO_PREVIEW_TOPIC)))
    static_event = value.get('updateMode') == 'event' or topic in ('/map', '/plan') or (path and path[-1] in ('map', 'plan'))
    limit = (ARUCO_TTL_MS / 1000 if aruco_preview else
             5 if topic in ('/local_costmap/costmap', '/global_costmap/costmap') or (path and path[-1] in ('localCostmap', 'globalCostmap')) else
             3 if topic == '/diagnostics' or (path and path[-1] == 'diagnostics') else upstream_limit)
    result = {}
    for key in ('ageS', 'ageMs', 'lastPageAgeS', 'lastPageAgeMs'):
        age = value.get(key)
        if isinstance(age, (int, float)) and not isinstance(age, bool) and math.isfinite(age):
            amount = elapsed * (1000 if key.endswith('Ms') else 1)
            result[key] = max(0, age) + amount
            boundary = limit * (1000 if key.endswith('Ms') else 1)
            expired = result[key] >= boundary if aruco_preview else result[key] > boundary
            if not static_event and expired:
                own_stale = True
    for key, item in value.items():
        if key in result:
            continue
        if key in ('status', 'state') and item in ('AVAILABLE', 'ONLINE') and own_stale:
            result[key] = 'STALE'
        else:
            result[key] = _age_tree(item, elapsed, own_stale, upstream_limit, path + (key,), aruco_preview)
    return result


class Store:
    def __init__(self, config, clock=time.time):
        self.config, self.clock = config, clock
        Path(config.data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = Path(config.data_dir) / 'latest.sqlite3'
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA wal_autocheckpoint=256')
        self.db.execute('PRAGMA journal_size_limit=4194304')
        self.db.execute('''CREATE TABLE IF NOT EXISTS sources (
            robot_id TEXT NOT NULL, source_id TEXT NOT NULL, session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL, collector_timestamp REAL NOT NULL,
            session_start REAL NOT NULL, retired TEXT NOT NULL, last_seen REAL NOT NULL,
            observation TEXT, observation_received REAL, observation_timestamp REAL,
            observation_session TEXT,
            observation_actual REAL,
            PRIMARY KEY(robot_id,source_id))''')
        if 'observation_session' not in {row[1] for row in self.db.execute('PRAGMA table_info(sources)')}:
            self.db.execute('ALTER TABLE sources ADD COLUMN observation_session TEXT')
        if 'observation_actual' not in {row[1] for row in self.db.execute('PRAGMA table_info(sources)')}:
            self.db.execute('ALTER TABLE sources ADD COLUMN observation_actual REAL')
        self.db.commit()
        self.faults = FaultLogs(self.db, clock=self.clock)
        self.tests = TestStats(self.db, config.robot_id, clock=self.clock)
        self.test_issues = TestIssues(self.db, config.robot_id, clock=self.clock)
        self._fault_online = {}
        self.sources = {}
        permitted = {(d.robot_id, d.source_id) for d in config.device_credentials}
        # Source identities removed from operator configuration do not accumulate
        # stale observation blobs; the database remains latest-state only.
        with self.db:
            for robot, source in self.db.execute('SELECT robot_id,source_id FROM sources').fetchall():
                if (robot, source) not in permitted:
                    self.db.execute('DELETE FROM sources WHERE robot_id=? AND source_id=?', (robot, source))
        for row in self.db.execute('SELECT * FROM sources'):
            robot, source, session, seq, collector, start, retired, seen, observation, received, timestamp, observation_session, actual = row
            if any(d.robot_id == robot and d.source_id == source for d in config.device_credentials):
                self.sources[(robot, source)] = dict(session_id=session, sequence=seq,
                    collector_timestamp=collector, session_start=start, retired=json.loads(retired),
                    last_seen=seen, observation=json.loads(observation) if observation else None,
                    observation_received=received, observation_timestamp=timestamp,
                    observation_session=observation_session, observation_actual=actual)
        self.media = {}  # Fixed allowlisted keys; one immutable frame each.
        self.started = self.clock()
        self.primary_source = next(d.source_id for d in config.device_credentials if d.role == 'primary')

    def close(self):
        self.db.close()

    def ready(self):
        return self.db.execute('SELECT 1').fetchone()[0] == 1

    def ingest(self, envelope):
        identity = (envelope['robot_id'], envelope['source_id'])
        previous = self.sources.get(identity)
        now, session, sequence = self.clock(), envelope['session_id'], envelope['sequence']
        timestamp = envelope['collector_timestamp']
        if previous:
            if session == previous['session_id']:
                if sequence <= previous['sequence']:
                    raise ReplayError('duplicate' if sequence == previous['sequence'] else 'out_of_order', previous['sequence'] + 1)
                if timestamp < previous['session_start']:
                    raise ReplayError('old_timestamp', previous['sequence'] + 1)
                retired, start = previous['retired'], previous['session_start']
            else:
                if session in previous['retired']:
                    raise ReplayError('retired_session', previous['sequence'] + 1)
                if sequence != 1:
                    raise ReplayError('new_session_sequence', 1)
                if timestamp <= previous['collector_timestamp']:
                    raise ReplayError('old_timestamp', 1)
                retired = (previous['retired'] + [previous['session_id']])[-64:]
                start = timestamp
        else:
            if sequence != 1:
                raise ReplayError('new_session_sequence', 1)
            retired, start = [], timestamp
        value = dict(session_id=session, sequence=sequence,
            collector_timestamp=max(timestamp, previous['collector_timestamp'] if previous else timestamp),
            session_start=start, retired=retired, last_seen=now,
            observation=previous['observation'] if previous else None,
            observation_received=previous['observation_received'] if previous else None,
            observation_timestamp=previous['observation_timestamp'] if previous else None,
            observation_session=previous['observation_session'] if previous else None,
            observation_actual=previous['observation_actual'] if previous else None)
        if envelope['kind'] == 'observation':
            sample_timestamp = envelope['message_timestamp'] if envelope['message_timestamp'] is not None else timestamp
            if previous and previous['observation_timestamp'] is not None and sample_timestamp < previous['observation_timestamp']:
                raise ReplayError('old_timestamp', previous['sequence'] + 1)
            value.update(observation=envelope['payload'], observation_received=now,
                         observation_timestamp=sample_timestamp, observation_session=session,
                         observation_actual=envelope['message_timestamp'])
        media = None
        if envelope['kind'] == 'media':
            payload = envelope['payload']
            media = dict(metadata={k: v for k, v in payload.items() if k != 'data'},
                binary=decode_media(payload), source_id=envelope['source_id'], received=now,
                collector_timestamp=timestamp, message_timestamp=envelope['message_timestamp'],
                session_id=session, upload_sequence=sequence)
        event_count = None
        metric_count = None
        with self.db:
            if envelope['kind'] == 'events':
                event_count = self.faults.add_batch(identity[0], identity[1], envelope['payload']['events'])
            if envelope['kind'] == 'test_metrics':
                if identity[1] != self.primary_source:
                    raise ValueError('test metrics require the primary source')
                metric_count = self.tests.add_batch(identity[0], identity[1], envelope['payload'])
            self.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity[0], identity[1], session, sequence, value['collector_timestamp'], start,
                 json.dumps(retired, separators=(',', ':')), now,
                 json.dumps(value['observation'], ensure_ascii=False, separators=(',', ':'), allow_nan=False) if value['observation'] is not None else None,
                 value['observation_received'], value['observation_timestamp'], value['observation_session'], value['observation_actual']))
        self.sources[identity] = value
        if media is not None:
            self.media[envelope['payload']['key']] = media
        return {'accepted': True, 'sequence': sequence, 'server_timestamp': now,
                **({'event_count': event_count} if event_count is not None else {}),
                **({'metric_count': metric_count} if metric_count is not None else {}),
                **({'test_control':self.tests.collector_control_state()} if identity[1] == self.primary_source else {})}

    def tests_snapshot(self, days=30, *, actor='observer', read_all=False, connection=None):
        db = connection if connection is not None else self.db
        source = db.execute('SELECT last_seen,collector_timestamp FROM sources WHERE robot_id=? AND source_id=?',
                            (self.config.robot_id,self.primary_source)).fetchone()
        age = max(0,self.clock()-min(source)) if source else None
        return self.tests.snapshot(days, actor=actor, read_all=read_all, source={
            'source_id':self.primary_source, 'online':source is not None and age<=self.config.heartbeat_timeout_s,
            'last_seen':source[0] if source else None}, connection=connection)

    def source_online(self, source_id):
        source = self.sources.get((self.config.robot_id, source_id))
        age = max(0, self.clock() - min(source['last_seen'], source['collector_timestamp'])) if source else None
        return bool(source and age <= self.config.heartbeat_timeout_s)

    def poll_faults(self):
        """Observe the primary uplink even with no browsers connected."""
        import uuid
        source_id = self.primary_source
        source = self.sources.get((self.config.robot_id, source_id))
        if source is None:
            return
        online = self.source_online(source_id)
        previous = self._fault_online.get(source_id)
        if online and previous is not False:
            self._fault_online[source_id] = online
            return
        if not online and previous is False:
            return
        stamp = self.clock()
        code = 'COLLECTOR_CONNECTION_RESTORED' if online else 'COLLECTOR_CONNECTION_LOST'
        # A server restart cannot duplicate the same observed outage.
        identity = f"{self.config.robot_id}/{source_id}/{source['session_id']}/{source['last_seen']}/{code}"
        event = {'id': str(uuid.uuid5(uuid.NAMESPACE_URL, identity)), 'timestamp': stamp,
                 'level': 'INFO' if online else 'ERROR', 'code': code,
                 'component': 'cloud/collector',
                 'message': '观测采集端连接恢复' if online else '观测采集端心跳超时',
                 'details': {'last_seen': source['last_seen'], 'timeout_s': self.config.heartbeat_timeout_s,
                             'evidence': 'server_heartbeat',
                             'note': '仅说明云端未收到采集心跳，具体网络或主机原因待排查'}}
        with self.db:
            self.faults.add_batch(self.config.robot_id, source_id, [event])
        self._fault_online[source_id] = online

    def cloud_status(self):
        source = self.sources.get((self.config.robot_id, self.primary_source))
        now = self.clock()
        observation = source.get('observation_timestamp') if source else None
        received = source.get('observation_received') if source else None
        age = max(0, now - min(observation, received)) if observation is not None and received is not None else None
        online = self.source_online(self.primary_source)
        status = ('WAITING_UPSTREAM' if not source else 'OFFLINE' if not online else
                  'WAITING_UPSTREAM' if observation is None else
                  'STALE' if source['observation_session'] != source['session_id'] or age > self.config.observation_timeout_s else 'AVAILABLE')
        return dict(robotId=self.config.robot_id, sourceId=self.primary_source, online=online,
            status=status, lastSeen=source['last_seen'] if source else None,
            lastObservation=observation, ageS=age, lastReceived=received,
            actualTimestamp=source['observation_actual'] if source else None)

    def _media_item(self, key):
        return self._media_entry_item(key, self.media.get(key), self.clock())

    def _media_entry_item(self, key, entry, now):
        kind = 'image' if key in CAMERA_KEYS else 'map' if key in MAP_KEYS else 'cloud'
        if not entry:
            return dict(key=key, kind=kind, status='WAITING_UPSTREAM', ageS=None, hz=None)
        elapsed = max(0, now - entry['received'])
        result = copy.deepcopy(entry['metadata'])
        # ageS describes the frame at collection, before queuing/upload. Add that
        # entire delay rather than taking its maximum with source age. Anchor
        # every lower bound at receipt, then age it locally: a slightly future
        # source clock must never subtract from known age or freeze its growth.
        upload_delay = max(0, entry['received'] - entry['collector_timestamp'])
        ages = [upload_delay]
        if entry['message_timestamp'] is not None:
            ages.append(max(0, entry['received'] - entry['message_timestamp']))
        if result.get('received_timestamp') is not None:
            ages.append(max(0, entry['received'] - result['received_timestamp']))
        original = result.get('ageS')
        if isinstance(original, (int, float)) and not isinstance(original, bool):
            ages.append(max(0, original) + upload_delay)
        result['ageS'] = max(ages) + elapsed
        connected = self.source_online(entry['source_id']) and self.source_online(self.primary_source)
        owner = self.sources.get((self.config.robot_id, entry['source_id']))
        connected = connected and owner is not None and owner['session_id'] == entry['session_id']
        stale = not connected or (key != 'map' and result['ageS'] > (5 if key in MAP_KEYS else self.config.upstream_timeout_s))
        if stale and result['status'] in ('AVAILABLE', 'ONLINE'):
            result['status'] = 'STALE'
        if key in CLOUD_KEYS:
            # A display-only preview identity includes its authority as well as
            # its session. Repeated GETs never create a new frame or lease.
            result['frameIdentity'] = entry['source_id'] + ':' + self._frame_identity(entry)
            result['ttlS'] = self.config.upstream_timeout_s
            result['sourceAgeS'] = max(0, original) if isinstance(original, (int, float)) and not isinstance(original, bool) else None
            result['uploadAgeS'] = upload_delay + elapsed
        if key in CAMERA_KEYS:
            result['previewUrl'] = f'/camera/{key}.jpg'
            result['sequence'] = result.get('frameSeq', result.get('sequence', 0))
            result['frameIdentity'] = self._frame_identity(entry)
        if result['status'] != 'AVAILABLE':
            result.pop('points', None)
        return result

    def visuals(self, keys=None):
        allowed = MAP_KEYS | CLOUD_KEYS
        selected = allowed if keys is None else frozenset(keys)
        if not selected <= allowed:
            raise ValueError('Unknown visual key')
        # One response uses one clock instant; filtering precedes copying the
        # potentially large arrays of unrelated sensors.
        now = self.clock()
        return {key: self._media_entry_item(key, self.media.get(key), now) for key in sorted(selected)}

    def image(self, key):
        frame = self.image_frame(key)
        return frame['binary'] if frame else None

    def image_frame(self, key):
        """One synchronous read binds freshness, pixels and identity to one entry."""
        entry = self.media.get(key)
        item = self._media_entry_item(key, entry, self.clock())
        if item['status'] != 'AVAILABLE':
            return None
        return dict(binary=entry['binary'], metadata=item, identity=self._frame_identity(entry))

    @staticmethod
    def _frame_identity(entry):
        metadata = entry['metadata']
        sequence = next((metadata[key] for key in ('frameSeq', 'sequence')
                         if metadata.get(key) is not None), entry['upload_sequence'])
        # Per-frame identity is opaque to old viewers, but integer fallback also
        # lets newer viewers order snapshots against in-flight JPEG responses.
        return f"{entry['session_id']}:{sequence}"

    def image_sequence(self, key):
        entry = self.media.get(key)
        return self._frame_identity(entry) if entry is not None else None

    def snapshot(self):
        now, cloud = self.clock(), self.cloud_status()
        source = self.sources.get((self.config.robot_id, self.primary_source))
        observation = source.get('observation') if source else None
        if observation is None:
            observation = {k: {'status': 'WAITING_UPSTREAM'} for k in ('system', 'robot', 'devices', 'ros2')}
            observation.update(pages={}, errors={})
        elapsed = max(0, now - min(source['observation_received'], source['observation_timestamp'])) if source and source.get('observation_timestamp') is not None else 0
        stale = cloud['status'] in ('OFFLINE', 'STALE')
        dto = _age_tree(observation, elapsed, stale, self.config.upstream_timeout_s)
        result = dict(meta={'type': 'rk3588_observer_snapshot', 'schemaVersion': 1, 'timestamp': now,
                            'observationTimestamp': cloud['lastObservation'], 'uptimeS': max(0, now - self.started),
                            'actualTimestamp': cloud['actualTimestamp'], 'lastReceived': cloud['lastReceived'],
                            'readOnly': True, 'snapshotIntervalS': 1.0},
                      robot=dto['robot'], devices=dto['devices'], rk3588=dto['system'],
                      ros2=dto['ros2'], pages=dto['pages'], errors=dto['errors'], cloud=cloud)
        result['devices']['cameras'] = {key: self._media_item(key) for key in sorted(CAMERA_KEYS)}
        return result
