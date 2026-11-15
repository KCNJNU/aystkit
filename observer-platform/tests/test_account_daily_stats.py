"""Independent clocks and shared physical evidence, using isolated SQLite only."""
import json
from pathlib import Path
import sqlite3
import uuid

import pytest

from cloud.backend.test_stats import TestStats as Stats, TestConflict as Conflict


def uid():return str(uuid.uuid4())

def start(stats, actor):
    c=stats.control_state(actor=actor)
    return stats.control('start',c['revision'],uid(),actor=actor,tester_name=actor+'测试员')

def change(stats, actor, action):
    return stats.control(action,stats.control_state(actor=actor)['revision'],uid(),actor=actor)

def evidence(stats, capture, a, b, distance=1):
    item={'id':uid(),'run_id':capture['run_id'],'revision':capture['revision'],
          'start':a,'end':b,'distance_m':distance,'valid_seconds':b-a}
    with stats.db:
        return stats.add_batch('robot','primary',{'samples':[item],'status':{
            'applied_revision':capture['revision'],'run_id':capture['run_id'],'state':capture['state'],
            'odom_status':'AVAILABLE','last_sample_timestamp':b,'rejected_samples':0,'dropped_samples':0}})

@pytest.fixture
def stats():
    now=[1780000000.]
    db=sqlite3.connect(':memory:');value=Stats(db,clock=lambda:now[0])
    yield value,now
    db.close()


def test_overlapping_accounts_keep_independent_time_and_clip_common_evidence(stats):
    s,t=stats;base=t[0];a=start(s,'alice');capture=s.collector_control_state()
    t[0]=base+10;b=start(s,'bob');assert s.collector_control_state()==capture
    t[0]=base+20;change(s,'alice','pause');assert s.collector_control_state()==capture
    t[0]=base+30;change(s,'bob','finish');assert s.collector_control_state()['state']=='paused'
    t[0]=base+40;change(s,'alice','resume');resumed=s.collector_control_state()
    t[0]=base+50;change(s,'alice','finish');assert s.collector_control_state()['state']=='idle'
    for c,left,right in [(capture,9,11),(capture,19,21),(capture,29,31),(resumed,39,41)]:
        assert evidence(s,c,base+left,base+right,2)==1
    alice=s.snapshot(actor='alice');bob=s.snapshot(actor='bob');admin=s.snapshot(actor='observer',read_all=True)
    assert alice['totals']['duration_s']==30 and bob['totals']['duration_s']==20
    assert alice['totals']['distance_m']==4 and bob['totals']['distance_m']==4
    assert {r['created_by'] for r in alice['runs']}=={'alice'}
    assert {r['created_by'] for r in bob['runs']}=={'bob'}
    assert admin['totals']['duration_s']==50 and admin['totals']['distance_m']==8
    assert admin['active'] is None and admin['revision']==0
    assert {r['id'] for r in admin['runs']}=={a['run_id'],b['run_id']}
    assert capture['run_id'] not in {r['id'] for r in admin['runs']}
    assert 'users' not in alice and len(admin['users'])==3


def test_same_uuid_is_account_scoped_and_replay_cannot_change_other_owner(stats):
    s,t=stats;identifier=uid()
    a=s.control('start',0,identifier,actor='alice',tester_name='甲')
    assert s.control('start',0,identifier,actor='alice',tester_name='甲')==a
    with pytest.raises(Conflict):s.control('start',0,identifier,actor='alice',tester_name='乙')
    with pytest.raises(Conflict) as refused:
        s.control('start',0,identifier,actor='bob',tester_name='乙')
    assert refused.value.revision==0 and s.control_state(actor='bob')['state']=='idle'
    b=start(s,'bob')
    assert a['run_id']!=b['run_id']
    t[0]+=12;change(s,'alice','pause');t[0]+=3
    assert s.snapshot(actor='alice')['active']['duration_s']==12
    assert s.snapshot(actor='bob')['active']['duration_s']==15
    assert s.control_state(actor='bob')['revision']==1


def test_failed_account_receipt_rolls_back_personal_and_capture_mutations(stats):
    s,t=stats
    s.db.execute("CREATE TRIGGER reject_account BEFORE INSERT ON account_test_commands BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.Error):start(s,'alice')
    assert s.control_state(actor='alice')['revision']==0
    assert s.collector_control_state()['revision']==0
    for name in ('account_test_runs','account_test_intervals','account_test_commands','test_runs','test_intervals','test_commands'):
        assert s.db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]==0


def test_new_account_readonly_snapshot_never_creates_control_or_leaks_history(stats):
    s,t=stats;start(s,'alice');t[0]+=3
    before=s.db.total_changes
    s.db.execute('PRAGMA query_only=ON')
    own=s.snapshot(actor='bob',connection=s.db)
    admin=s.snapshot(actor='observer',read_all=True,connection=s.db)
    assert own['runs']==[] and own['active'] is None and own['totals']['duration_s']==0
    assert s.db.total_changes==before
    assert admin['totals']['duration_s']==3


def test_source_sync_uses_capture_revision_instead_of_personal_revision(stats):
    s,t=stats;start(s,'alice');capture=s.collector_control_state()
    start(s,'bob');change(s,'bob','pause')
    assert evidence(s,capture,t[0],t[0]+1,0)==1
    t[0]+=1
    snap=s.snapshot(actor='bob',source={'source_id':'primary','online':True})
    assert snap['revision']==2 and snap['source']['collector_revision']==1
    assert snap['source']['syncing'] is False and snap['active']['syncing'] is False


def test_migration_copies_legacy_owner_and_preserves_rows_once(tmp_path):
    db=sqlite3.connect(tmp_path/'history.sqlite3');t=[1780000010.]
    db.executescript("""
      CREATE TABLE test_runs (id TEXT PRIMARY KEY,robot_id TEXT NOT NULL,name TEXT NOT NULL,
        state TEXT NOT NULL,started_at REAL NOT NULL,ended_at REAL,created_by TEXT NOT NULL,
        tester_name TEXT NOT NULL DEFAULT '');
      CREATE TABLE test_control (robot_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,
        run_id TEXT,state TEXT NOT NULL,since REAL NOT NULL);
      CREATE TABLE test_intervals (run_id TEXT NOT NULL,revision INTEGER NOT NULL,start REAL NOT NULL,
        end REAL,PRIMARY KEY(run_id,revision));
      CREATE TABLE test_commands (robot_id TEXT NOT NULL,command_id TEXT NOT NULL,request TEXT NOT NULL,
        actor TEXT NOT NULL,received_at REAL NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(robot_id,command_id));
    """)
    identifier=uid();first={'run_id':uid()};started=t[0]-10
    request=json.dumps({'action':'start','revision':0,'name':'历史'},sort_keys=True,separators=(',',':'))
    with db:
        db.execute("INSERT INTO test_runs VALUES (?,?,'历史','paused',?,NULL,'alice','')",(first['run_id'],'robot',started))
        db.execute("INSERT INTO test_control VALUES ('robot',2,?,'paused',?)",(first['run_id'],t[0]))
        db.execute('INSERT INTO test_intervals VALUES (?,1,?,?)',(first['run_id'],started,t[0]))
        db.execute('INSERT INTO test_commands VALUES (?,?,?,?,?,?)',('robot',identifier,request,'alice',started,1))
    original=[db.execute('SELECT * FROM '+name).fetchall() for name in ('test_runs','test_intervals','test_commands','test_control')]
    s=Stats(db,clock=lambda:t[0])
    assert [db.execute('SELECT * FROM '+name).fetchall() for name in ('test_runs','test_intervals','test_commands','test_control')]==original
    assert s.snapshot(actor='alice')['active']['id']==first['run_id']
    assert s.snapshot(actor='alice')['active']['duration_s']==10
    assert s.snapshot(actor='observer')['runs']==[]
    assert s.control('start',0,identifier,actor='alice',name='历史')['state']=='paused'
    t[0]+=5;start(s,'bob');assert db.execute('SELECT COUNT(*) FROM account_test_runs').fetchone()[0]==2
    s=Stats(db,clock=lambda:t[0]);assert db.execute('SELECT COUNT(*) FROM account_test_runs').fetchone()[0]==2
    assert s.snapshot(actor='alice')['active']['state']=='paused'
    assert s.snapshot(actor='bob')['active']['state']=='running'
    db.close()


def test_all_time_totals_survive_short_history_filter_and_split_midnight(stats):
    s,t=stats;base=t[0];start(s,'alice');t[0]+=10;change(s,'alice','finish')
    t[0]+=3*86400;start(s,'bob');t[0]+=5
    admin=s.snapshot(1,actor='observer',read_all=True)
    assert len(admin['runs'])==1 and admin['runs'][0]['created_by']=='bob'
    assert admin['totals']['duration_s']==15
    assert {x['username']:x['total']['duration_s'] for x in admin['users']}=={'alice':10,'bob':5,'observer':0}


def test_concurrent_same_account_revision_cannot_overwrite_committed_pause(tmp_path):
    path=tmp_path/'concurrent.sqlite3';t=[1780000000.]
    first=sqlite3.connect(path);second=sqlite3.connect(path)
    a=Stats(first,clock=lambda:t[0]);b=Stats(second,clock=lambda:t[0])
    start(a,'alice');t[0]+=5;winner=[False]

    class Gate:
        def __getattr__(self,key):return getattr(first,key)
        def __enter__(self):first.__enter__();return self
        def __exit__(self,*args):return first.__exit__(*args)
        def execute(self,sql,*args):
            if not winner[0] and sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) and 'account_test_' in sql:
                winner[0]=True
                change(b,'alice','pause')
            return first.execute(sql,*args)

    a.db=Gate()
    with pytest.raises(Conflict) as refused:change(a,'alice','pause')
    assert refused.value.revision==2
    assert b.control_state(actor='alice')['state']=='paused'
    assert b.snapshot(actor='alice')['active']['duration_s']==5
    assert second.execute('SELECT COUNT(*) FROM account_test_commands').fetchone()[0]==2
    assert a.collector_control_state()['revision']==2
    first.close();second.close()
