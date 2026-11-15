"""Manual test accounting, evidence clipping and authorization in isolated DBs."""
import copy
import csv
import io
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from multidict import MultiDict

from cloud.backend.app import ACCOUNTS, STORE, create_app
from cloud.backend.store import Store
from cloud.backend.test_stats import TestStats as Stats, TestConflict as Conflict, parse_days
from shared.protocol import ProtocolError, validate_envelope
from shared.test_metrics import validate_test_metrics_payload
from tests.test_backend import config, ORIGIN, PASSWORD, TOKEN, CAMERA_TOKEN
from tests.test_protocol import envelope


def command(): return str(uuid.uuid4())


def status(run_id=None,revision=0,state='idle'):
    return dict(applied_revision=revision,run_id=run_id,state=state,odom_status='AVAILABLE',
                last_sample_timestamp=None,rejected_samples=0,dropped_samples=0)


def sample(run_id,start,end,revision=1,distance=1,valid=None,**updates):
    value=dict(id=command(),run_id=run_id,revision=revision,start=start,end=end,
               distance_m=distance,valid_seconds=end-start if valid is None else valid)
    return dict(value,**updates)


def metrics(samples=(),status_value=None):
    return dict(samples=list(samples),status=status() if status_value is None else status_value)


def packet(payload,sequence=1,session=None):
    value=envelope('heartbeat',sequence=sequence,session=session)
    value.update(kind='test_metrics',topic='/observer/test_metrics',payload=payload)
    return validate_envelope(value)


@pytest.fixture
def store(tmp_path):
    now=[time.time()]
    settings=config(str(tmp_path));settings.validate()
    value=Store(settings,clock=lambda:now[0])
    yield value,now
    value.close()


def start(store,now,name=None):
    control=store.tests.control('start',store.tests.control_state()['revision'],command(),name=name)
    # Evidence is produced for the hidden scalar physical capture; the account
    # run has a separate identity and independently controlled intervals.
    return store.tests.collector_control_state()['run_id'],now[0]


def test_manual_duration_pause_resume_and_finish_without_fake_odometry(store):
    value,now=store;run_id,base=start(value,now)
    now[0]=base+10
    value.tests.control('pause',1,command())
    now[0]=base+100
    snap=value.tests_snapshot()
    assert snap['active']['duration_s']==10 and snap['active']['distance_m'] is None
    assert snap['summary']['quality']=='waiting'
    assert snap['active']['state']=='paused'
    value.tests.control('resume',2,command())
    now[0]=base+105
    value.tests.control('finish',3,command())
    now[0]=base+300
    snap=value.tests_snapshot()
    assert snap['active'] is None and snap['revision']==4
    assert snap['runs'][0]['state']=='finished' and snap['runs'][0]['duration_s']==15
    assert snap['runs'][0]['distance_m'] is None


def test_valid_stationary_measurement_is_zero_but_no_test_is_none(store):
    value,now=store
    assert value.tests_snapshot()['summary']['quality']=='none'
    run_id,base=start(value,now)
    now[0]=base+1
    with value.db:
        value.tests.add_batch('test-robot','rk3588',metrics([sample(run_id,base,base+1,distance=0)]))
    snap=value.tests_snapshot()
    assert snap['summary']['distance_m']==0
    assert snap['summary']['valid_seconds']==1
    assert snap['summary']['quality']=='available'


def test_backfill_duplicate_and_pause_boundary_are_clipped_to_recording_window(store):
    value,now=store;run_id,base=start(value,now)
    now[0]=base+1
    value.tests.control('pause',1,command())
    now[0]=base+100
    evidence=sample(run_id,base+.5,base+1.5,distance=2)
    with value.db:
        assert value.tests.add_batch('test-robot','rk3588',metrics([evidence]))==1
        assert value.tests.add_batch('test-robot','rk3588',metrics([evidence]))==0
    snap=value.tests_snapshot()
    assert snap['summary']['duration_s']==1
    assert snap['summary']['distance_m']==1
    assert snap['summary']['valid_seconds']==.5
    assert value.db.execute('SELECT COUNT(*) FROM test_samples').fetchone()[0]==1
    # A distinct UUID cannot double-count an overlapping physical interval.
    with value.db:
        assert value.tests.add_batch('test-robot','rk3588',metrics([dict(evidence,id=command())]))==0
    assert value.tests_snapshot()['summary']['distance_m']==1


def test_finished_offline_run_accepts_history_but_not_movement_after_end(store):
    value,now=store;run_id,base=start(value,now)
    now[0]=base+2;value.tests.control('finish',1,command())
    now[0]=base+200
    evidence=[sample(run_id,base,base+1),sample(run_id,base+2,base+3)]
    with value.db:value.tests.add_batch('test-robot','rk3588',metrics(evidence))
    snap=value.tests_snapshot()
    assert snap['runs'][0]['distance_m']==1
    assert snap['runs'][0]['valid_seconds']==1
    assert value.db.execute('SELECT COUNT(*) FROM test_samples').fetchone()[0]==2


def test_beijing_midnight_splits_duration_distance_and_unique_session_count(store):
    value,now=store
    now[0]=datetime(2026,10,25,15,59,59,tzinfo=timezone.utc).timestamp()
    run_id,base=start(value,now)
    now[0]=base+2;value.tests.control('pause',1,command())
    with value.db:value.tests.add_batch('test-robot','rk3588',metrics([sample(run_id,base+.5,base+1.5)]))
    snap=value.tests_snapshot(2)
    assert [d['date'] for d in snap['days']]==['2026-10-25','2026-10-26']
    for day in snap['days']:
        assert day['duration_s']==1 and day['distance_m']==.5 and day['valid_seconds']==.5 and day['sessions']==1
    assert snap['runs'][0]['duration_s']==2 and snap['runs'][0]['distance_m']==1


def test_controls_are_idempotent_and_stale_multiterminal_request_conflicts(store):
    value,now=store;identifier=command()
    first=value.tests.control('start',0,identifier,name='固定名称')
    assert value.tests.control('start',0,identifier,name='固定名称')==first
    with pytest.raises(Conflict):value.tests.control('pause',0,command())
    with pytest.raises(Conflict):value.tests.control('start',0,identifier,name='其他名称')
    value.tests.control('finish',1,command())
    assert value.tests.control('start',0,identifier,name='固定名称')['state']=='idle'
    assert value.db.execute('SELECT COUNT(*) FROM test_runs').fetchone()[0]==1


def test_status_only_metrics_and_source_freshness(store):
    value,now=store;run_id,base=start(value,now)
    payload=metrics(status_value=status(run_id,1,'running'))
    reply=value.ingest(packet(payload))
    assert set(reply['test_control'])=={'revision','run_id','state','since'}
    assert reply['metric_count']==0 and reply['test_control']['run_id']==run_id
    snap=value.tests_snapshot();assert snap['source']['online'] is True
    assert snap['source']['odom_status']=='AVAILABLE' and not snap['active']['syncing']
    now[0]+=4
    assert value.tests_snapshot()['source']['odom_status']=='STALE'


def test_metric_receipts_source_checkpoint_and_status_roll_back_together(store):
    value,now=store;run_id,base=start(value,now)
    evidence=sample(run_id,base,base+.5,distance=.2)
    payload=packet(metrics([evidence]))
    value.db.execute("CREATE TRIGGER reject_test_checkpoint BEFORE INSERT ON sources BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.Error):value.ingest(payload)
    for table in ('test_metric_receipts','test_samples','test_source_status','sources'):
        assert value.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    assert value.sources=={}
    value.db.execute('DROP TRIGGER reject_test_checkpoint')
    assert value.ingest(payload)['metric_count']==1


def test_control_transaction_failure_does_not_leave_active_orphan(store):
    value,now=store
    value.db.execute("CREATE TRIGGER reject_test_command BEFORE INSERT ON test_commands BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.Error):value.tests.control('start',0,command())
    assert value.tests.control_state()['revision']==0
    assert value.db.execute('SELECT COUNT(*) FROM test_runs').fetchone()[0]==0
    assert value.db.execute('SELECT COUNT(*) FROM test_intervals').fetchone()[0]==0


def test_quality_partial_unknown_run_ack_and_read_only_snapshot(store):
    value,now=store;run_id,base=start(value,now)
    now[0]=base+20
    with value.db:
        value.tests.add_batch('test-robot','rk3588',metrics([sample(run_id,base,base+1)]))
        assert value.tests.add_batch('test-robot','rk3588',metrics([sample(command(),base,base+1)]))==0
    before=value.db.total_changes
    ro=sqlite3.connect('file:'+str(value.path)+'?mode=ro',uri=True)
    try:
        ro.execute('PRAGMA query_only=ON');ro.execute('BEGIN')
        snap=value.tests_snapshot(connection=ro)
        assert snap['summary']['quality']=='partial'
        assert snap['summary']['distance_m']==1
    finally:ro.close()
    assert value.db.total_changes==before


def test_durable_restart_keeps_running_manual_interval_and_sample_ids(tmp_path):
    settings=config(str(tmp_path));settings.validate();now=[time.time()]
    value=Store(settings,clock=lambda:now[0]);run_id,base=start(value,now)
    evidence=sample(run_id,base,base+1)
    with value.db:value.tests.add_batch('test-robot','rk3588',metrics([evidence]))
    value.close();now[0]+=5
    value=Store(settings,clock=lambda:now[0])
    try:
        assert value.tests_snapshot()['active']['duration_s']==5
        with value.db:assert value.tests.add_batch('test-robot','rk3588',metrics([evidence]))==0
        assert value.tests_snapshot()['summary']['distance_m']==1
    finally:value.close()


@pytest.mark.parametrize('change',[
    {'id':'bad'}, {'run_id':None}, {'revision':True}, {'revision':0}, {'start':float('nan')},
    {'end':float('inf')}, {'end':10}, {'distance_m':-1}, {'distance_m':10},
    {'valid_seconds':2}, {'valid_seconds':True}, {'unexpected':1},
])
def test_metric_validator_rejects_invalid_or_unphysical_samples(change):
    evidence=sample(command(),10,11)
    evidence.update(change)
    with pytest.raises((ValueError,TypeError)):validate_test_metrics_payload(metrics([evidence]))


def test_metric_protocol_strict_topic_status_batch_and_zero_samples():
    valid=packet(metrics());assert valid['kind']=='test_metrics'
    for update in ({'topic':'/wrong'},{'payload':{'samples':[]}}, {'payload':metrics([sample(command(),10,11)]*33)}):
        obj=copy.deepcopy(valid);obj.update(update)
        with pytest.raises(ProtocolError):validate_envelope(obj)
    duplicate=sample(command(),10,11)
    with pytest.raises(ValueError):validate_test_metrics_payload(metrics([duplicate,duplicate]))
    for change in ({'state':'running'}, {'applied_revision':True}, {'rejected_samples':-1}, {'odom_status':'OTHER'}, {'extra':0}):
        payload=metrics();payload['status'].update(change)
        with pytest.raises((ValueError,TypeError)):validate_test_metrics_payload(payload)


def test_statistics_query_days_are_strict_and_bounded():
    assert parse_days(MultiDict())==30
    for query in (MultiDict(days='0'),MultiDict(days='366'),MultiDict(days='1.5'),
                  MultiDict([('days','1'),('days','2')]),MultiDict(extra='1')):
        with pytest.raises(ValueError):parse_days(query)


class DailyStatsAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='daily-stats-api-')
        self.app=create_app(config(self.temp.name));self.client=TestClient(TestServer(self.app),cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()

    async def login(self):
        result=await self.client.post('/api/login',json={'username':'viewer','password':PASSWORD},headers={'Origin':ORIGIN})
        self.assertEqual(result.status,200)

    async def test_anon_pending_and_camera_device_cannot_control_or_view(self):
        for path in ('/api/tests','/api/tests/export?format=json'):
            self.assertEqual((await self.client.get(path,headers={'Authorization':'Bearer '+TOKEN})).status,401)
        self.assertEqual((await self.client.post('/api/tests/control',json={'action':'start','revision':0,'command_id':command()},headers={'Origin':ORIGIN})).status,401)
        response=await self.client.post('/api/register',json={'username':'daily.pending','password':'isolated-daily-password'},headers={'Origin':ORIGIN})
        self.assertEqual(response.status,201)
        for path in ('/api/tests','/api/tests/export?format=json'):
            self.assertEqual((await self.client.get(path)).status,403)
        self.assertEqual((await self.client.post('/api/tests/control',json={'action':'start','revision':0,'command_id':command()},headers={'Origin':ORIGIN})).status,403)
        obj=packet(metrics());obj['source_id']='jetson'
        self.assertEqual((await self.client.post('/api/v1/ingest',json=obj,headers={'Authorization':'Bearer '+CAMERA_TOKEN})).status,403)
        self.assertEqual(self.app[STORE].db.execute('SELECT COUNT(*) FROM test_source_status').fetchone()[0],0)

    async def test_approved_regular_viewer_controls_and_conflicts_are_consistent(self):
        response=await self.client.post('/api/register',json={'username':'daily.viewer','password':'isolated-daily-password'},headers={'Origin':ORIGIN})
        self.assertEqual(response.status,201)
        account=self.app[ACCOUNTS].find('daily.viewer');self.app[ACCOUNTS].approve(account['id'],'viewer')
        body={'action':'start','revision':0,'command_id':command(),'name':'日常测试','tester_name':'王明'}
        self.assertEqual((await self.client.post('/api/tests/control',json=body)).status,403)
        result=await self.client.post('/api/tests/control',json=body,headers={'Origin':ORIGIN})
        self.assertEqual(result.status,200)
        data=await result.json();self.assertEqual(data['revision'],1);self.assertEqual(data['active']['name'],'日常测试')
        self.assertIsNone(data['active']['distance_m'])
        self.assertEqual((await self.client.post('/api/tests/control',json=body,headers={'Origin':ORIGIN})).status,200)
        result=await self.client.post('/api/tests/control',json={'action':'pause','revision':0,'command_id':command()},headers={'Origin':ORIGIN})
        self.assertEqual(result.status,409)
        data=await (await self.client.get('/api/tests')).json()
        self.assertEqual(data['revision'],1);self.assertEqual(len(data['runs']),1)

    async def test_control_json_contract_csrf_and_export_preserve_null(self):
        await self.login()
        body={'action':'start','revision':0,'command_id':command(),'tester_name':'王明'}
        self.assertEqual((await self.client.post('/api/tests/control',json=body,headers={'Origin':'https://other.invalid'})).status,403)
        for value in (dict(body,extra=1),dict(body,revision=True),dict(body,name='x'*61)):
            self.assertEqual((await self.client.post('/api/tests/control',json=value,headers={'Origin':ORIGIN})).status,400)
        duplicate='{"action":"start","action":"pause","revision":0,"command_id":"'+command()+'"}'
        self.assertEqual((await self.client.post('/api/tests/control',data=duplicate,headers={'Origin':ORIGIN,'Content-Type':'application/json'})).status,400)
        self.assertEqual((await self.client.post('/api/tests/control',json=body,headers={'Origin':ORIGIN})).status,200)
        response=await self.client.get('/api/tests?days=2');data=await response.json()
        self.assertEqual(len(data['days']),2);self.assertEqual(data['timezone'],'Asia/Shanghai')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        exported=await (await self.client.get('/api/tests/export?days=2&format=json')).json()
        self.assertIsNone(exported['summary']['distance_m'])
        response=await self.client.get('/api/tests/export?days=2&format=csv')
        raw=await response.read();self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        table=list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
        self.assertEqual(len(table),3);self.assertEqual(table[-1][2],'')
        self.assertIn('attachment;',response.headers['Content-Disposition'])
        self.assertEqual((await self.client.get('/api/tests?days=366')).status,400)

    async def test_primary_receives_control_and_metrics_receipts_are_not_observations(self):
        await self.login();body={'action':'start','revision':0,'command_id':command(),'tester_name':'王明'}
        data=await (await self.client.post('/api/tests/control',json=body,headers={'Origin':ORIGIN})).json()
        obj=envelope('heartbeat');response=await self.client.post('/api/v1/ingest',json=obj,headers={'Authorization':'Bearer '+TOKEN})
        reply=await response.json();self.assertEqual(reply['test_control'],self.app[STORE].tests.collector_control_state())
        self.assertNotEqual(reply['test_control']['run_id'],data['active']['id'])
        self.assertEqual(set(reply['test_control']),{'revision','run_id','state','since'})
        obj.update(sequence=2,kind='test_metrics',topic='/observer/test_metrics',payload=metrics())
        reply=await (await self.client.post('/api/v1/ingest',json=obj,headers={'Authorization':'Bearer '+TOKEN})).json()
        self.assertTrue(reply['accepted']);self.assertEqual(reply['metric_count'],0)
        self.assertIsNone(self.app[STORE].sources[('test-robot','rk3588')]['observation'])


    async def test_start_requires_registered_tester_and_invalid_requests_do_not_create_runs(self):
        await self.login()
        base={'action':'start','revision':0,'command_id':command()}
        invalid=[base,dict(base,tester_name=None),dict(base,tester_name=''),dict(base,tester_name='  '),
                 dict(base,tester_name='名'*61),dict(base,tester_name='王\u202e明'),dict(base,tester_name=42)]
        for body in invalid:
            response=await self.client.post('/api/tests/control',json=body,headers={'Origin':ORIGIN})
            self.assertEqual(response.status,400)
            self.assertIn('测试人员姓名',(await response.json())['error'])
        self.assertEqual(self.app[STORE].tests.control_state()['revision'],0)
        self.assertEqual(self.app[STORE].db.execute('SELECT COUNT(*) FROM test_runs').fetchone()[0],0)

    async def test_registered_tester_survives_api_lifecycle_and_exports(self):
        await self.login()
        body={'action':'start','revision':0,'command_id':command(),'tester_name':'  =1+1  ','name':'路线记录'}
        response=await self.client.post('/api/tests/control',json=body,headers={'Origin':ORIGIN})
        self.assertEqual(response.status,200)
        data=await response.json()
        self.assertEqual(data['active']['tester_name'],'=1+1')
        self.assertEqual(data['summary']['tester_names'],['=1+1'])
        self.assertEqual(self.app[STORE].db.execute('SELECT created_by FROM account_test_runs').fetchone()[0],'viewer')
        changed=await self.client.post('/api/tests/control',json=dict(body,tester_name='其他人'),headers={'Origin':ORIGIN})
        self.assertEqual(changed.status,409)
        for action,revision in [('pause',1),('resume',2),('finish',3)]:
            response=await self.client.post('/api/tests/control',json={'action':action,'revision':revision,'command_id':command()},headers={'Origin':ORIGIN})
            self.assertEqual(response.status,200)
            state=await response.json()
            self.assertEqual(state['runs'][0]['tester_name'],'=1+1')
        exported=await (await self.client.get('/api/tests/export?days=2&format=json')).json()
        self.assertEqual(exported['runs'][0]['tester_name'],'=1+1')
        self.assertEqual(exported['summary']['tester_names'],['=1+1'])
        response=await self.client.get('/api/tests/export?days=2&format=csv')
        table=list(csv.reader(io.StringIO((await response.read()).decode('utf-8-sig'))))
        self.assertEqual(table[0],['日期（北京时间）','测试时长（秒）','测试移动距离（米）','里程有效时间（秒）','测试次数','记录状态','测试人员'])
        self.assertEqual(table[-1][-1],"'=1+1")
        self.assertEqual(table[1][-1],'')
        self.assertEqual(table[-1][2],'')

    async def test_legacy_active_test_can_finish_but_tester_cannot_be_reassigned(self):
        await self.login()
        self.app[STORE].tests.control('start',0,command(),actor='viewer')
        for action,revision in [('pause',1),('resume',2),('finish',3)]:
            body={'action':action,'revision':revision,'command_id':command()}
            refused=await self.client.post('/api/tests/control',json=dict(body,tester_name=None),headers={'Origin':ORIGIN})
            self.assertEqual(refused.status,400)
            response=await self.client.post('/api/tests/control',json=body,headers={'Origin':ORIGIN})
            self.assertEqual(response.status,200)
        snapshot=await (await self.client.get('/api/tests')).json()
        self.assertIsNone(snapshot['active'])
        self.assertEqual(snapshot['runs'][0]['tester_name'],'')
        response=await self.client.get('/api/tests/export?days=1&format=csv')
        table=list(csv.reader(io.StringIO((await response.read()).decode('utf-8-sig'))))
        self.assertEqual(table[-1][-1],'未登记')


def test_readonly_export_uses_one_snapshot_for_control_source_and_measurements(store):
    value,now=store;run_id,base=start(value,now)
    first=packet(metrics(status_value=status(run_id,1,'running')))
    value.ingest(first)
    ro=sqlite3.connect('file:'+str(value.path)+'?mode=ro',uri=True)
    try:
        ro.execute('BEGIN')
        assert ro.execute('SELECT revision FROM test_control').fetchone()[0]==1
        now[0]=base+.5;value.tests.control('pause',1,command())
        second=packet(metrics(status_value=status(run_id,2,'paused')),sequence=2,session=first['session_id'])
        value.ingest(second)
        now[0]=base+1
        old=value.tests_snapshot(connection=ro)
        assert old['revision']==1 and old['active']['state']=='running'
        assert old['active']['duration_s']==1 and old['source']['applied_revision']==1
        current=value.tests_snapshot()
        assert current['revision']==2 and current['active']['state']=='paused'
        assert current['active']['duration_s']==.5 and current['source']['applied_revision']==2
    finally:ro.close()


def test_finished_cache_warm_equals_cold_and_late_backfill_invalidates(store):
    value,now=store;run_id,base=start(value,now)
    now[0]=base+2;value.tests.control('finish',1,command())
    with value.db:value.tests.add_batch('test-robot','rk3588',metrics([sample(run_id,base,base+1)]))
    cold=value.tests_snapshot();warm=value.tests_snapshot()
    assert cold==warm and warm['summary']['distance_m']==1
    with value.db:value.tests.add_batch('test-robot','rk3588',metrics([sample(run_id,base+1,base+2)]))
    assert value.tests_snapshot()['summary']['distance_m']==2


def test_finished_cache_does_not_publish_uncommitted_measurements_after_rollback(store):
    value,now=store;run_id,base=start(value,now)
    now[0]=base+2;value.tests.control('finish',1,command())
    with value.db:value.tests.add_batch('test-robot','rk3588',metrics([sample(run_id,base,base+1)]))
    assert value.tests_snapshot()['summary']['distance_m']==1
    value.db.execute('BEGIN')
    value.tests.add_batch('test-robot','rk3588',metrics([sample(run_id,base+1,base+2)]))
    assert value.tests_snapshot()['summary']['distance_m']==2
    value.db.rollback()
    assert value.tests_snapshot()['summary']['distance_m']==1


def test_readonly_snapshot_bypasses_finished_cache_after_newer_backfill(store):
    value,now=store;run_id,base=start(value,now)
    now[0]=base+2;value.tests.control('finish',1,command())
    with value.db:value.tests.add_batch('test-robot','rk3588',metrics([sample(run_id,base,base+1)]))
    assert value.tests_snapshot()['summary']['distance_m']==1
    ro=sqlite3.connect('file:'+str(value.path)+'?mode=ro',uri=True)
    try:
        ro.execute('BEGIN');assert ro.execute('SELECT COUNT(*) FROM test_samples').fetchone()[0]==1
        with value.db:value.tests.add_batch('test-robot','rk3588',metrics([sample(run_id,base+1,base+2)]))
        assert value.tests_snapshot()['summary']['distance_m']==2
        assert value.tests_snapshot(connection=ro)['summary']['distance_m']==1
    finally:ro.close()


def test_finished_cache_is_bounded_without_deleting_daily_history(store):
    value,now=store
    for index in range(520):
        run_id,base=start(value,now)
        now[0]+= .01
        value.tests.control('finish',value.tests.control_state()['revision'],command())
        now[0]+= .01
    snap=value.tests_snapshot()
    assert len(snap['runs'])==520
    assert len(value.tests._finished_cache)<=512
    assert value.db.execute('SELECT COUNT(*) FROM test_runs').fetchone()[0]==520

