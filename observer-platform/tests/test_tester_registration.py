"""Tester registration persists separately from accounts and respects old records."""
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from cloud.backend.test_stats import TestStats as Stats, TestConflict as Conflict, validate_tester_name
from tests.test_daily_stats_backend import command, store


@pytest.mark.parametrize('value', ['', '  ', '名'*61, 42, False, [], {}, '\t王明', '王\n明',
    '王\x7f明', '王\x85明', '王\u202e明', '王\ud800明', '王\u2028明', '王\u2029明'])
def test_tester_name_rejects_empty_nonstring_long_and_hidden_control_values(store, value):
    value_store, now = store
    with pytest.raises(ValueError, match='测试人员姓名'):
        value_store.tests.control('start',0,command(),tester_name=value)
    assert value_store.tests.control_state()['revision']==0
    assert value_store.db.execute('SELECT COUNT(*) FROM test_runs').fetchone()[0]==0


def test_unicode_limit_is_codepoints_and_whitespace_normalizes():
    assert validate_tester_name('  王明  ')=='王明'
    assert validate_tester_name('\U00020000'*60)=='\U00020000'*60
    with pytest.raises(ValueError): validate_tester_name('\U00020000'*61)


def test_tester_is_not_actor_and_survives_pause_resume_finish_and_replay(store):
    value, now=store;identifier=command()
    first=value.tests.control('start',0,identifier,name='路线一',tester_name='  张三  ',actor='observer')
    run=first['run_id']
    with pytest.raises(Conflict) as refused:
        value.tests.control('start',0,identifier,name='路线一',tester_name='张三',actor='other.viewer')
    assert refused.value.revision==0 and value.tests.control_state(actor='other.viewer')['run_id'] is None
    with pytest.raises(Conflict):
        value.tests.control('start',0,identifier,name='路线一',tester_name='李四')
    assert value.db.execute('SELECT tester_name,created_by FROM account_test_runs WHERE id=?',(run,)).fetchone()==('张三','observer')
    assert value.tests_snapshot()['active']['tester_name']=='张三'
    now[0]+=2;value.tests.control('pause',1,command())
    now[0]+=3;value.tests.control('resume',2,command())
    now[0]+=4;value.tests.control('finish',3,command())
    snap=value.tests_snapshot()
    assert snap['active'] is None
    assert snap['runs'][0]['tester_name']=='张三'
    assert snap['runs'][0]['duration_s']==6
    assert snap['summary']['tester_names']==['张三']
    assert value.tests.control('start',0,identifier,name='路线一',tester_name='张三')['state']=='idle'
    for action in ['pause','resume','finish']:
        with pytest.raises(ValueError,match='只能在开始'):
            value.tests.control(action,4,command(),tester_name='李四')


def test_migrate_legacy_active_run_preserves_data_and_command_then_new_name_persists(tmp_path):
    path=tmp_path/'legacy.sqlite3';now=[time.time()];run=command();identifier=command()
    db=sqlite3.connect(path)
    db.executescript('''
      CREATE TABLE test_runs (id TEXT PRIMARY KEY, robot_id TEXT NOT NULL,name TEXT NOT NULL,
        state TEXT NOT NULL, started_at REAL NOT NULL,ended_at REAL,created_by TEXT NOT NULL);
      CREATE TABLE test_control (robot_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, run_id TEXT,
        state TEXT NOT NULL,since REAL NOT NULL);
      CREATE TABLE test_intervals (run_id TEXT NOT NULL,revision INTEGER NOT NULL,start REAL NOT NULL,
        end REAL,PRIMARY KEY(run_id,revision));
      CREATE TABLE test_commands (robot_id TEXT NOT NULL,command_id TEXT NOT NULL,request TEXT NOT NULL,
        actor TEXT NOT NULL,received_at REAL NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(robot_id,command_id));
    ''')
    old=(run,'robot','历史测试','running',now[0]-10,None,'old.viewer')
    request=json.dumps({'action':'start','revision':0,'name':'历史测试'},sort_keys=True,separators=(',',':'))
    with db:
        db.execute('INSERT INTO test_runs VALUES (?,?,?,?,?,?,?)',old)
        db.execute("INSERT INTO test_control VALUES ('robot',1,?,'running',?)",(run,now[0]-10))
        db.execute('INSERT INTO test_intervals VALUES (?,1,?,NULL)',(run,now[0]-10))
        db.execute('INSERT INTO test_commands VALUES (?,?,?,?,?,?)',('robot',identifier,request,'old.viewer',now[0]-10,1))
    stats=Stats(db,clock=lambda:now[0])
    assert db.execute('SELECT id,robot_id,name,state,started_at,ended_at,created_by FROM test_runs').fetchone()==old
    assert stats.snapshot(actor='old.viewer')['active']['tester_name']==''
    assert stats.snapshot(actor='old.viewer')['summary']['tester_names']==['']
    assert stats.control('start',0,identifier,name='历史测试',actor='old.viewer')['run_id']==run
    stats=Stats(db,clock=lambda:now[0])
    assert [row[1] for row in db.execute('PRAGMA table_info(test_runs)')].count('tester_name')==1
    stats.control('finish',1,command(),actor='old.viewer')
    stats.control('start',2,command(),tester_name='李四',actor='old.viewer')
    db.close()
    db=sqlite3.connect(path)
    try:
        restarted=Stats(db,clock=lambda:now[0])
        assert restarted.snapshot(actor='old.viewer')['active']['tester_name']=='李四'
        assert restarted.snapshot(actor='old.viewer')['summary']['tester_names']==['','李四']
        assert db.execute('SELECT request FROM test_commands WHERE command_id=?',(identifier,)).fetchone()[0]==request
    finally: db.close()


def test_daily_tester_list_uses_recording_days_and_deduplicates(store):
    value,now=store
    now[0]=datetime(2026,10,27,15,59,59,tzinfo=timezone.utc).timestamp()
    assert value.tests_snapshot()['summary']['tester_names']==[]
    value.tests.control('start',0,command(),tester_name='张三')
    now[0]+=2;value.tests.control('finish',1,command())
    value.tests.control('start',2,command(),tester_name='李四')
    now[0]+=2;value.tests.control('finish',3,command())
    value.tests.control('start',4,command(),tester_name='张三')
    now[0]+=2;value.tests.control('finish',5,command())
    # Existing internal callers/legacy records can still have unknown names.
    value.tests.control('start',6,command())
    now[0]+=1;value.tests.control('finish',7,command())
    snapshot=value.tests_snapshot(3)
    assert [row['tester_names'] for row in snapshot['days']]==[[],['张三'],['','张三','李四']]
    assert snapshot['days'][1]['duration_s']==1
    assert snapshot['summary']['duration_s']==6
    assert snapshot['summary']['sessions']==4
    assert len(snapshot['runs'])==4
    assert snapshot['summary']['distance_m'] is None
