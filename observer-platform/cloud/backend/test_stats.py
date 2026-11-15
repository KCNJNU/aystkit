"""Persistent manual test sessions and clipped read-only odometry evidence."""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3
import time
import unicodedata
import uuid

from shared.test_metrics import canonical_uuid, integer

BEIJING = timezone(timedelta(hours=8))
DAY_SECONDS = 86400


class TestConflict(ValueError):
    def __init__(self, message, revision):
        super().__init__(message)
        self.revision = revision


def parse_days(query, *, export=False):
    allowed = {'days', 'format'} if export else {'days'}
    if any(key not in allowed or len(query.getall(key)) != 1 for key in query):
        raise ValueError('invalid test statistics filters')
    value = query.get('days', '30')
    if not value.isascii() or not value.isdigit() or len(value) > 3 or not 1 <= int(value) <= 365:
        raise ValueError('invalid test statistics days')
    return int(value)


def validate_tester_name(value):
    if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 60
            or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp') for c in value)):
        raise ValueError('测试人员姓名需为 1–60 个字，不能包含控制字符')
    return value.strip()


def date_key(stamp):
    return datetime.fromtimestamp(stamp, BEIJING).date().isoformat()


def midnight(stamp):
    return datetime.fromtimestamp(stamp, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _quality(duration, valid, sessions):
    if sessions == 0:
        return 'none'
    if valid <= 1e-9:
        return 'waiting'
    return 'partial' if duration-valid > max(2.0, duration*.05) else 'available'


def _total(duration=0, distance=0, valid=0, sessions=0):
    duration, valid = max(0., float(duration)), max(0., float(valid))
    # A valid stationary measurement is a real zero; missing odometry is null.
    return {'duration_s':duration, 'distance_m':max(0., float(distance)) if valid > 1e-9 else None,
            'valid_seconds':min(duration, valid), 'sessions':sessions,
            'quality':_quality(duration, valid, sessions)}


class TestStats:
    def __init__(self, db, robot_id='robot', *, clock=time.time):
        self.db, self.robot_id, self.clock = db, robot_id, clock
        self._finished_cache = OrderedDict()
        self._finished_cache_limit = 512
        db.executescript('''
          CREATE TABLE IF NOT EXISTS test_control (
            robot_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, run_id TEXT,
            state TEXT NOT NULL, since REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS test_runs (
            id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, name TEXT NOT NULL,
            state TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL,
            created_by TEXT NOT NULL, tester_name TEXT NOT NULL DEFAULT '');
          CREATE INDEX IF NOT EXISTS test_runs_time ON test_runs(robot_id,started_at DESC);
          CREATE UNIQUE INDEX IF NOT EXISTS test_runs_one_active ON test_runs(robot_id)
            WHERE state IN ('running','paused');
          CREATE TABLE IF NOT EXISTS test_intervals (
            run_id TEXT NOT NULL, revision INTEGER NOT NULL, start REAL NOT NULL, end REAL,
            PRIMARY KEY(run_id,revision));
          CREATE TABLE IF NOT EXISTS test_commands (
            robot_id TEXT NOT NULL, command_id TEXT NOT NULL, request TEXT NOT NULL,
            actor TEXT NOT NULL, received_at REAL NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(robot_id,command_id));
          CREATE TABLE IF NOT EXISTS test_metric_receipts (
            robot_id TEXT NOT NULL, source_id TEXT NOT NULL, sample_id TEXT NOT NULL,
            received_at REAL NOT NULL, PRIMARY KEY(robot_id,source_id,sample_id));
          CREATE TABLE IF NOT EXISTS test_samples (
            robot_id TEXT NOT NULL, source_id TEXT NOT NULL, id TEXT NOT NULL,
            run_id TEXT NOT NULL, revision INTEGER NOT NULL, start REAL NOT NULL,
            end REAL NOT NULL, distance_m REAL NOT NULL, valid_seconds REAL NOT NULL,
            received_at REAL NOT NULL, PRIMARY KEY(robot_id,source_id,id));
          CREATE INDEX IF NOT EXISTS test_samples_interval ON test_samples(run_id,revision,start,end);
          CREATE INDEX IF NOT EXISTS test_samples_overlap ON test_samples(robot_id,source_id,run_id,revision,end,start);
          CREATE TABLE IF NOT EXISTS test_source_status (
            robot_id TEXT NOT NULL, source_id TEXT NOT NULL, received_at REAL NOT NULL,
            status TEXT NOT NULL, PRIMARY KEY(robot_id,source_id));
        ''')
        with db:
            # Existing records keep an unknown tester rather than borrowing the
            # authenticated account name. Run metadata never enters the collector.
            if 'tester_name' not in {row[1] for row in db.execute('PRAGMA table_info(test_runs)')}:
                db.execute("ALTER TABLE test_runs ADD COLUMN tester_name TEXT NOT NULL DEFAULT ''")
            db.execute("INSERT OR IGNORE INTO test_control VALUES (?,0,NULL,'idle',?)", (robot_id, clock()))
        self._migrate_accounts()

    def _migrate_accounts(self):
        # Keep collector tables as physical evidence; user history is copied once.
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS account_test_runs (
            id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, name TEXT NOT NULL,
            state TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL,
            created_by TEXT NOT NULL, tester_name TEXT NOT NULL DEFAULT '');
          CREATE INDEX IF NOT EXISTS account_test_runs_owner_time
            ON account_test_runs(robot_id,created_by,started_at DESC);
          CREATE UNIQUE INDEX IF NOT EXISTS account_test_runs_one_active
            ON account_test_runs(robot_id,created_by) WHERE state IN ('running','paused');
          CREATE TABLE IF NOT EXISTS account_test_control (
            robot_id TEXT NOT NULL, actor TEXT NOT NULL, revision INTEGER NOT NULL,
            run_id TEXT, state TEXT NOT NULL, since REAL NOT NULL,
            PRIMARY KEY(robot_id,actor));
          CREATE TABLE IF NOT EXISTS account_test_intervals (
            run_id TEXT NOT NULL, revision INTEGER NOT NULL, start REAL NOT NULL, end REAL,
            PRIMARY KEY(run_id,revision));
          CREATE INDEX IF NOT EXISTS account_test_intervals_time
            ON account_test_intervals(start,end,run_id);
          CREATE TABLE IF NOT EXISTS account_test_commands (
            robot_id TEXT NOT NULL, actor TEXT NOT NULL, command_id TEXT NOT NULL,
            request TEXT NOT NULL, received_at REAL NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(robot_id,actor,command_id));
          CREATE UNIQUE INDEX IF NOT EXISTS account_test_commands_global_id
            ON account_test_commands(robot_id,command_id);
          CREATE TABLE IF NOT EXISTS account_test_migrations (
            robot_id TEXT PRIMARY KEY, migrated_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS account_test_deletions (
            robot_id TEXT NOT NULL, run_id TEXT NOT NULL, deleted_by TEXT NOT NULL,
            deleted_at REAL NOT NULL, PRIMARY KEY(robot_id,run_id));
        """)
        with self.db:
            if self.db.execute('SELECT 1 FROM account_test_migrations WHERE robot_id=?',
                               (self.robot_id,)).fetchone():
                return
            self.db.execute('INSERT INTO account_test_runs '
                'SELECT id,robot_id,name,state,started_at,ended_at,created_by,tester_name '
                'FROM test_runs WHERE robot_id=?', (self.robot_id,))
            self.db.execute('INSERT INTO account_test_intervals '
                'SELECT i.run_id,i.revision,i.start,i.end FROM test_intervals i '
                'JOIN test_runs r ON r.id=i.run_id WHERE r.robot_id=?', (self.robot_id,))
            self.db.execute('INSERT INTO account_test_commands '
                'SELECT robot_id,actor,command_id,request,received_at,revision '
                'FROM test_commands WHERE robot_id=?', (self.robot_id,))
            actors = self.db.execute('SELECT created_by FROM test_runs WHERE robot_id=? '
                'UNION SELECT actor FROM test_commands WHERE robot_id=?',
                (self.robot_id,self.robot_id)).fetchall()
            capture = self.collector_control_state()
            for (actor,) in actors:
                latest = self.db.execute('SELECT COALESCE(MAX(revision),0) FROM account_test_commands '
                    'WHERE robot_id=? AND actor=?', (self.robot_id,actor)).fetchone()[0]
                active = self.db.execute('SELECT id,state,started_at FROM account_test_runs '
                    "WHERE robot_id=? AND created_by=? AND state IN ('running','paused')",
                    (self.robot_id,actor)).fetchone()
                revision,run_id,state,since = latest,None,'idle',self.clock()
                if active:
                    revision=max(latest,capture['revision']);run_id,state,since=active
                    if run_id==capture['run_id']:since=capture['since']
                self.db.execute('INSERT INTO account_test_control VALUES (?,?,?,?,?,?)',
                    (self.robot_id,actor,revision,run_id,state,since))
            self.db.execute('INSERT INTO account_test_migrations VALUES (?,?)',
                            (self.robot_id,self.clock()))

    def control_state(self, *, actor='observer', connection=None):
        db=connection if connection is not None else self.db
        row=db.execute('SELECT revision,run_id,state,since FROM account_test_control '
                       'WHERE robot_id=? AND actor=?',(self.robot_id,actor)).fetchone()
        # No INSERT when a new account opens a page or uses a read-only export.
        return dict(zip(('revision','run_id','state','since'),row)) if row else {
            'revision':0,'run_id':None,'state':'idle','since':self.clock()}

    def _reconcile_capture(self):
        counts=dict(self.db.execute('SELECT state,COUNT(*) FROM account_test_runs '
            "WHERE robot_id=? AND state IN ('running','paused') GROUP BY state",(self.robot_id,)))
        capture=self.collector_control_state();action=None
        if counts.get('running',0):
            if capture['state']=='idle':action='start'
            elif capture['state']=='paused':action='resume'
        elif counts.get('paused',0):
            if capture['state']=='running':action='pause'
        elif capture['state']!='idle':action='finish'
        if action:
            self._capture_control(action,capture['revision'],str(uuid.uuid4()),
                name='账号测试里程采集' if action=='start' else None,actor='__capture__')

    def control(self, action, revision, command_id, *, name=None, tester_name=None, actor='observer'):
        canonical_uuid(command_id);integer(revision)
        if not isinstance(actor,str) or not 1<=len(actor)<=64 or any(ord(c)<32 for c in actor):
            raise ValueError('invalid test account')
        if action not in ('start','pause','resume','finish'):
            raise ValueError('invalid test action')
        if name is not None and action!='start':
            raise ValueError('test name is only valid when starting')
        if name is not None and (not isinstance(name,str) or len(name.strip())>60 or any(ord(c)<32 for c in name)):
            raise ValueError('invalid test name')
        if tester_name is not None and action!='start':
            raise ValueError('测试人员姓名只能在开始测试时登记')
        tester='' if tester_name is None else validate_tester_name(tester_name)
        title=(name or '').strip()
        request_value={'action':action,'revision':revision,'name':title}
        if tester_name is not None:request_value['tester_name']=tester
        request=json.dumps(request_value,sort_keys=True,separators=(',',':'));now=self.clock()
        with self.db:
            other=self.db.execute('SELECT 1 FROM account_test_commands WHERE robot_id=? AND command_id=? AND actor<>? LIMIT 1',
                (self.robot_id,command_id,actor)).fetchone()
            if other:
                raise TestConflict('此操作编号已用于其他账号',self.control_state(actor=actor)['revision'])
            previous=self.db.execute('SELECT request FROM account_test_commands '
                'WHERE robot_id=? AND actor=? AND command_id=?',(self.robot_id,actor,command_id)).fetchone()
            if previous:
                if previous[0]!=request:
                    raise TestConflict('此操作编号已用于其他请求',self.control_state(actor=actor)['revision'])
                return self.control_state(actor=actor)
            current=self.control_state(actor=actor)
            if revision!=current['revision'] or revision>=2**63-1:
                raise TestConflict('测试状态已更新，请刷新后重试',current['revision'])
            state,run_id,updated=current['state'],current['run_id'],revision+1
            self.db.execute("INSERT OR IGNORE INTO account_test_control VALUES (?,?,0,NULL,'idle',?)",
                (self.robot_id,actor,now))
            if action=='start':
                if state!='idle':raise TestConflict('请先结束当前测试',revision)
                run_id=str(uuid.uuid4());state='running'
                title=title or '测试 '+datetime.fromtimestamp(now,BEIJING).strftime('%H:%M')
                self.db.execute("INSERT INTO account_test_runs VALUES (?,?,?,'running',?,NULL,?,?)",
                    (run_id,self.robot_id,title,now,actor,tester))
                self.db.execute('INSERT INTO account_test_intervals VALUES (?,?,?,NULL)',(run_id,updated,now))
            elif action=='pause':
                if state!='running':raise TestConflict('当前测试未在计时',revision)
                state='paused'
                self.db.execute('UPDATE account_test_intervals SET end=MAX(start,?) WHERE run_id=? AND end IS NULL',(now,run_id))
                self.db.execute("UPDATE account_test_runs SET state='paused' WHERE id=? AND created_by=?",(run_id,actor))
            elif action=='resume':
                if state!='paused':raise TestConflict('当前测试未暂停',revision)
                state='running'
                self.db.execute("UPDATE account_test_runs SET state='running' WHERE id=? AND created_by=?",(run_id,actor))
                self.db.execute('INSERT INTO account_test_intervals VALUES (?,?,?,NULL)',(run_id,updated,now))
            else:
                if state not in ('running','paused'):raise TestConflict('当前没有进行中的测试',revision)
                self.db.execute('UPDATE account_test_intervals SET end=MAX(start,?) WHERE run_id=? AND end IS NULL',(now,run_id))
                self.db.execute("UPDATE account_test_runs SET state='finished',ended_at=? WHERE id=? AND created_by=?",(now,run_id,actor))
                state,run_id='idle',None
            changed=self.db.execute('UPDATE account_test_control SET revision=?,run_id=?,state=?,since=? '
                'WHERE robot_id=? AND actor=? AND revision=?',
                (updated,run_id,state,now,self.robot_id,actor,revision)).rowcount
            if changed!=1:
                raise TestConflict('测试状态已更新，请刷新后重试',self.control_state(actor=actor)['revision'])
            self._reconcile_capture()
            self.db.execute('INSERT INTO account_test_commands VALUES (?,?,?,?,?,?)',
                (self.robot_id,actor,command_id,request,now,updated))
            self._finished_cache.clear()
        return self.control_state(actor=actor)

    def _evict_run(self, run_id):
        # Invalidations are safe before commit: a rollback only makes the next
        # read recompute, and never publishes uncommitted measurements.
        for key in list(self._finished_cache):
            if key[0] == run_id:
                del self._finished_cache[key]

    def delete_run(self, run_id, *, administrator):
        """Remove a logical test from statistics; retain physical evidence.

        An active timer is closed atomically with its tombstone. Replaying an
        old control command never recreates a hidden run or stops a newer run.
        """
        canonical_uuid(run_id)
        now = self.clock()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT 1 FROM account_test_deletions WHERE robot_id=? AND run_id=?',
                               (self.robot_id, run_id)).fetchone():
                return True
            row = self.db.execute('SELECT created_by,state FROM account_test_runs WHERE robot_id=? AND id=?',
                                  (self.robot_id, run_id)).fetchone()
            if row is None:
                return False
            owner, state = row
            if state in ('running', 'paused'):
                control = self.control_state(actor=owner)
                if control['run_id'] != run_id or control['revision'] >= 2**63-1:
                    raise TestConflict('测试状态已更新，请刷新后重试', control['revision'])
                self.db.execute('UPDATE account_test_intervals SET end=MAX(start,?) WHERE run_id=? AND end IS NULL',
                                (now, run_id))
                self.db.execute("UPDATE account_test_runs SET state='finished',ended_at=? WHERE robot_id=? AND id=?",
                                (now, self.robot_id, run_id))
                self.db.execute("UPDATE account_test_control SET revision=revision+1,run_id=NULL,state='idle',since=? "
                                'WHERE robot_id=? AND actor=? AND run_id=?',
                                (now, self.robot_id, owner, run_id))
            self.db.execute('INSERT INTO account_test_deletions VALUES (?,?,?,?)',
                            (self.robot_id, run_id, administrator, now))
            self._reconcile_capture()
            self._finished_cache.clear()
        return True

    def collector_control_state(self, *, connection=None):
        db = connection if connection is not None else self.db
        row = db.execute('SELECT revision,run_id,state,since FROM test_control WHERE robot_id=?',
                              (self.robot_id,)).fetchone()
        return dict(zip(('revision','run_id','state','since'), row))

    def _capture_control(self, action, revision, command_id, *, name=None, tester_name=None, actor='observer'):
        canonical_uuid(command_id); integer(revision)
        if action not in ('start','pause','resume','finish'):
            raise ValueError('invalid test action')
        if name is not None and action != 'start':
            raise ValueError('test name is only valid when starting')
        if name is not None and (not isinstance(name,str) or len(name.strip()) > 60 or any(ord(c)<32 for c in name)):
            raise ValueError('invalid test name')
        if tester_name is not None and action != 'start':
            raise ValueError('测试人员姓名只能在开始测试时登记')
        tester = '' if tester_name is None else validate_tester_name(tester_name)
        title = (name or '').strip()
        request_value = {'action':action,'revision':revision,'name':title}
        if tester_name is not None:
            request_value['tester_name'] = tester
        # Preserve legacy command fingerprints when replaying pre-registration
        # operations. The HTTP API requires a tester for every new start.
        request = json.dumps(request_value,sort_keys=True,separators=(',',':'))
        now = self.clock()
        previous = self.db.execute('SELECT request FROM test_commands WHERE robot_id=? AND command_id=?',
                                   (self.robot_id,command_id)).fetchone()
        if previous:
            if previous[0] != request:
                raise TestConflict('此操作编号已用于其他请求', self.collector_control_state()['revision'])
            return self.collector_control_state()
        control = self.collector_control_state()
        if revision != control['revision'] or revision >= 2**63-1:
            raise TestConflict('测试状态已更新，请刷新后重试', control['revision'])
        current = control['state']; run_id = control['run_id']; updated = revision+1
        if action == 'start':
            if current != 'idle':
                raise TestConflict('请先结束当前测试', revision)
            run_id = str(uuid.uuid4()); current = 'running'
            title = title or '测试 '+datetime.fromtimestamp(now,BEIJING).strftime('%H:%M')
            self.db.execute("INSERT INTO test_runs (id,robot_id,name,state,started_at,ended_at,created_by,tester_name) "
                            "VALUES (?,?,?,'running',?,NULL,?,?)",
                            (run_id,self.robot_id,title,now,actor,tester))
            self.db.execute('INSERT INTO test_intervals VALUES (?,?,?,NULL)', (run_id,updated,now))
        elif action == 'pause':
            if current != 'running':
                raise TestConflict('当前测试未在计时', revision)
            current = 'paused'
            self.db.execute('UPDATE test_intervals SET end=MAX(start,?) WHERE run_id=? AND end IS NULL',(now,run_id))
            self.db.execute("UPDATE test_runs SET state='paused' WHERE id=?", (run_id,))
        elif action == 'resume':
            if current != 'paused':
                raise TestConflict('当前测试未暂停',revision)
            current = 'running'
            self.db.execute("UPDATE test_runs SET state='running' WHERE id=?",(run_id,))
            self.db.execute('INSERT INTO test_intervals VALUES (?,?,?,NULL)',(run_id,updated,now))
        else:
            if current not in ('running','paused'):
                raise TestConflict('当前没有进行中的测试',revision)
            self.db.execute('UPDATE test_intervals SET end=MAX(start,?) WHERE run_id=? AND end IS NULL',(now,run_id))
            self.db.execute("UPDATE test_runs SET state='finished',ended_at=? WHERE id=?",(now,run_id))
            current, run_id = 'idle', None
        changed = self.db.execute('UPDATE test_control SET revision=?,run_id=?,state=?,since=? '
                                 'WHERE robot_id=? AND revision=?',
                                 (updated,run_id,current,now,self.robot_id,revision)).rowcount
        if changed != 1:
            raise TestConflict('测试状态已更新，请刷新后重试', self.collector_control_state()['revision'])
        self._evict_run(control['run_id'])
        self._evict_run(run_id)
        self.db.execute('INSERT INTO test_commands VALUES (?,?,?,?,?,?)',
                        (self.robot_id,command_id,request,actor,now,updated))
        return self.collector_control_state()

    def add_batch(self, robot_id, source_id, payload):
        """Caller owns the source checkpoint transaction; every UUID is ACKed once."""
        if robot_id != self.robot_id:
            raise ValueError('test robot identity mismatch')
        now, added = self.clock(), 0
        for sample in payload['samples']:
            fresh = self.db.execute('INSERT OR IGNORE INTO test_metric_receipts VALUES (?,?,?,?)',
                                   (robot_id,source_id,sample['id'],now)).rowcount
            if not fresh:
                continue
            period = self.db.execute('SELECT 1 FROM test_intervals i JOIN test_runs r ON r.id=i.run_id '
                                     'WHERE i.run_id=? AND i.revision=? AND r.robot_id=?',
                                     (sample['run_id'],sample['revision'],robot_id)).fetchone()
            if not period:
                continue
            # Separate UUIDs cannot count the same physical interval twice,
            # including after a collector restart. Backfilled disjoint periods
            # remain accepted regardless of upload order.
            overlap = self.db.execute('SELECT 1 FROM test_samples INDEXED BY test_samples_overlap WHERE robot_id=? AND source_id=? '
                'AND run_id=? AND revision=? AND end>? AND start<? LIMIT 1',
                (robot_id,source_id,sample['run_id'],sample['revision'],sample['start']+1e-6,sample['end']-1e-6)).fetchone()
            if overlap:
                continue
            self.db.execute('INSERT INTO test_samples VALUES (?,?,?,?,?,?,?,?,?,?)',
                (robot_id,source_id,sample['id'],sample['run_id'],sample['revision'],sample['start'],sample['end'],
                 sample['distance_m'],sample['valid_seconds'],now))
            self._finished_cache.clear()
            added += 1
        self.db.execute('INSERT OR REPLACE INTO test_source_status VALUES (?,?,?,?)',
                        (robot_id,source_id,now,json.dumps(payload['status'],separators=(',',':'),allow_nan=False)))
        return added

    def _source(self, source, *, connection=None):
        db = connection if connection is not None else self.db
        source = dict(source or {})
        source_id = source.pop('source_id',None)
        row = db.execute('SELECT received_at,status FROM test_source_status WHERE robot_id=? AND source_id=?',
                              (self.robot_id,source_id)).fetchone() if source_id else None
        status = json.loads(row[1]) if row else {}
        online = bool(source.get('online',False))
        odom = status.get('odom_status','WAITING_UPSTREAM')
        if odom == 'AVAILABLE' and (not online or not row or self.clock()-row[0]>3):
            odom = 'STALE'
        return {'online':online,'odom_status':odom,'applied_revision':status.get('applied_revision',0),
                'last_seen':source.get('last_seen'),'topic':'/odom_nav',
                'last_sample_timestamp':status.get('last_sample_timestamp'),
                'rejected_samples':status.get('rejected_samples',0),'dropped_samples':status.get('dropped_samples',0),
                'run_id':status.get('run_id'),'state':status.get('state','idle')}

    def snapshot(self, days=30, *, actor='observer', read_all=False, source=None, connection=None):
        if type(days) is not int or not 1<=days<=365:
            raise ValueError('invalid test statistics days')
        db=connection if connection is not None else self.db
        now=self.clock();today=midnight(now);cutoff=today-(days-1)*DAY_SECONDS
        control=self.control_state(actor=actor,connection=db)
        scope_sql=(' AND NOT EXISTS (SELECT 1 FROM account_test_deletions d '
                   'WHERE d.robot_id=r.robot_id AND d.run_id=r.id)'
                   + ('' if read_all else ' AND r.created_by=?'))
        scope_args=() if read_all else (actor,)
        rows=db.execute('SELECT r.id,r.name,r.state,r.started_at,r.ended_at,r.tester_name,r.created_by '
            'FROM account_test_runs r WHERE r.robot_id=? AND r.started_at<=?'+scope_sql+
            ' ORDER BY r.started_at DESC,r.id DESC',(self.robot_id,now,*scope_args)).fetchall()
        runs={row[0]:dict(zip(('id','name','state','started_at','ended_at','tester_name','created_by'),row)) for row in rows}
        amounts={run_id:[0.,0.,0.] for run_id in runs}
        by_day={date_key(cutoff+i*DAY_SECONDS):[0.,0.,0.] for i in range(days)}
        day_keys=tuple(by_day)
        owner_days=defaultdict(lambda:[0.,0.,0.]);daily_runs=defaultdict(set)
        # Account time is independent of the shared physical acquisition state.
        for run_id,start,end in db.execute('SELECT i.run_id,i.start,COALESCE(i.end,?) FROM account_test_intervals i '
                'JOIN account_test_runs r ON r.id=i.run_id WHERE r.robot_id=? AND i.start<=?'+scope_sql,
                (now,self.robot_id,now,*scope_args)):
            if run_id not in runs:continue
            end=min(end,now);span=max(0.,end-start);amounts[run_id][0]+=span
            owner=runs[run_id]['created_by'];left=max(start,cutoff)
            while left<end-1e-9:
                boundary=min(end,midnight(left)+DAY_SECONDS);day=date_key(left)
                if day in by_day:
                    by_day[day][0]+=boundary-left;owner_days[(owner,day)][0]+=boundary-left;daily_runs[day].add(run_id)
                left=boundary
            if span==0 and date_key(start) in by_day:daily_runs[date_key(start)].add(run_id)
        # Clip to the original scalar capture interval before intersecting each
        # account's intervals. This preserves old evidence and pause boundaries,
        # including late packets after finish, without replaying physical motion.
        # Consume clipped contributions once. Reusing materialized CTEs and
        # GROUP BY used to spill the full sample history into SQLite's temporary
        # files, exhausting the cloud container's small /tmp during a page read.
        # Only account/day totals are retained here; sample rows stay in a cursor.
        sql="""SELECT r.id,MAX(s.start,p.start,i.start),
            MIN(s.end,COALESCE(p.end,?),COALESCE(i.end,?),?),
            s.distance_m/(s.end-s.start),s.valid_seconds/(s.end-s.start)
          FROM test_samples s
          JOIN test_intervals p ON p.run_id=s.run_id AND p.revision=s.revision
          JOIN account_test_intervals i
            ON i.start<s.end AND (i.end IS NULL OR i.end>s.start)
          JOIN account_test_runs r ON r.id=i.run_id
          WHERE s.robot_id=? AND s.start<=? AND s.valid_seconds>0
            AND r.robot_id=? SCOPE
        """.replace('SCOPE',scope_sql)
        for run_id,start,end,distance_rate,valid_rate in db.execute(sql,
                (now,now,now,self.robot_id,now,self.robot_id,*scope_args)):
            if run_id not in runs or end<=start:continue
            amount=amounts[run_id];span=end-start
            amount[1]+=span*distance_rate;amount[2]+=span*valid_rate
            owner=runs[run_id]['created_by'];left=max(start,cutoff)
            while left<end-1e-9:
                # Beijing uses a fixed UTC+8 offset, so the existing day slots
                # also provide boundaries without converting every sample.
                slot=math.floor((left-cutoff)/DAY_SECONDS)
                boundary=min(end,cutoff+(slot+1)*DAY_SECONDS)
                if 0<=slot<len(day_keys):
                    day=day_keys[slot]
                    distance=(boundary-left)*distance_rate;valid=(boundary-left)*valid_rate
                    by_day[day][1]+=distance;by_day[day][2]+=valid
                    owner_days[(owner,day)][1]+=distance;owner_days[(owner,day)][2]+=valid
                left=boundary
        user_amounts=defaultdict(lambda:[0.,0.,0.,0])
        for run_id,row in runs.items():
            amount=amounts[run_id];row.update(_total(*amount,1));total=user_amounts[row['created_by']]
            for index in range(3):total[index]+=amount[index]
            total[3]+=1
        def day_total(day,owner=None):
            ids={i for i in daily_runs[day] if owner is None or runs[i]['created_by']==owner}
            duration,distance,valid=by_day[day] if owner is None else owner_days[(owner,day)]
            return dict(date=day,tester_names=sorted({runs[i]['tester_name'] for i in ids}),
                        **_total(duration,distance,valid,len(ids)))
        daily=[day_total(day) for day in sorted(by_day)]
        active=runs.get(control['run_id'])
        source_value=self._source(source,connection=db);capture=self.collector_control_state(connection=db)
        source_value.update(collector_revision=capture['revision'],syncing=(
            source_value['applied_revision']!=capture['revision'] or source_value['run_id']!=capture['run_id'] or source_value['state']!=capture['state']))
        if active is not None:active=dict(active,syncing=source_value['syncing'])
        totals=_total(*(sum(value[i] for value in user_amounts.values()) for i in range(3)),
                      sum(value[3] for value in user_amounts.values()))
        output={'server_time':now,'timezone':'Asia/Shanghai','revision':control['revision'],
            'active':active,'summary':daily[-1],'own_summary':day_total(date_key(today),actor),
            'days':daily,'runs':[row for row in runs.values() if row['ended_at'] is None or row['ended_at']>=cutoff],
            'source':source_value,'totals':totals,'viewer':{'username':actor,'can_view_all':bool(read_all)},
            'scope':'all' if read_all else 'own'}
        if read_all:
            users=[]
            for owner in sorted(set(user_amounts)|{actor}):
                own_control=self.control_state(actor=owner,connection=db)
                users.append({'username':owner,'summary':day_total(date_key(today),owner),
                    'total':_total(*user_amounts.get(owner,(0.,0.,0.,0))),
                    'active_state':own_control['state'],
                    'active_run_id':own_control['run_id'],
                    'active_duration_s':runs.get(own_control['run_id'],{}).get('duration_s',0.)})
            output['users']=users
        return output
