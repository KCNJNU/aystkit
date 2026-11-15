"""Isolated counter, replay and transfer tests; no ROS or physical devices."""
import asyncio
import json
import math
import time
import uuid
from types import SimpleNamespace as NS

import pytest
from aiohttp import web

from collector.config import Config
from collector.test_metrics import TestMetricSpool, TestMetricTracker
from collector.transport import Uploader
from shared.test_metrics import validate_test_metrics_payload


def odom(x=0., y=0., stamp=10., frame='odom', child='drive_base_link'):
    seconds=int(stamp);nanos=round((stamp-seconds)*1e9)
    if nanos==1_000_000_000:seconds,nanos=seconds+1,0
    return NS(header=NS(frame_id=frame,stamp=NS(sec=seconds,nanosec=nanos)),
              child_frame_id=child,pose=NS(pose=NS(position=NS(x=x,y=y,z=0.))))


def control(revision=1,run_id=None,state='running',since=1700000000.):
    return dict(revision=revision,run_id=run_id or str(uuid.uuid4()) if state!='idle' else None,
                state=state,since=since)


def sample(run_id=None,**changes):
    value=dict(id=str(uuid.uuid4()),run_id=run_id or str(uuid.uuid4()),revision=1,
               start=time.time()-1,end=time.time()-.5,distance_m=.1,valid_seconds=.5)
    value['valid_seconds']=value['end']-value['start']
    return dict(value,**changes)


def tracker(tmp_path):
    clock=[100.]
    spool=TestMetricSpool(tmp_path/'metrics.sqlite3')
    value=TestMetricTracker(spool,lambda:clock[0],lambda:1700000000.+clock[0])
    return spool,value,clock


def record(value,clock,x,y=0.,stamp=None,delta=.1):
    clock[0]+=delta
    value.observe(odom(x,y,clock[0] if stamp is None else stamp))


def test_idle_no_distance_and_manual_pause_resume_never_bridges_positions(tmp_path):
    spool,t,c=tracker(tmp_path);run=control()
    record(t,c,0);record(t,c,1)
    assert t.payload()['samples']==[]
    t.apply_control(run,1700000000.+c[0])
    record(t,c,1)
    for i in range(1,11):record(t,c,1+i*.01)
    t.apply_control(dict(run,revision=2,state='paused'),1700000000.+c[0])
    before=sum(s['distance_m'] for s in spool.batch())
    assert before==pytest.approx(.1)
    record(t,c,200)
    t.apply_control(dict(run,revision=3,state='running'),1700000000.+c[0])
    record(t,c,200);record(t,c,200.02)
    rows=t.payload()['samples']
    assert sum(s['distance_m'] for s in rows)==pytest.approx(.12)
    assert {s['revision'] for s in rows}=={1,3}
    assert t.payload()['status']['applied_revision']==3
    spool.close()


def test_curved_path_uses_every_odometry_callback_and_reverse_counts_positive(tmp_path):
    spool,t,c=tracker(tmp_path);t.apply_control(control(),1700000000.+c[0])
    points=[(math.cos(i*.05),math.sin(i*.05)) for i in range(21)]
    for x,y in points:record(t,c,x,y)
    expected=sum(math.hypot(b[0]-a[0],b[1]-a[1]) for a,b in zip(points,points[1:]))
    for x,y in reversed(points[:-1]):record(t,c,x,y)
    rows=t.payload()['samples']
    assert sum(s['distance_m'] for s in rows)==pytest.approx(2*expected)
    assert all(s['distance_m']>=0 for s in rows)
    assert sum(s['valid_seconds'] for s in rows)==pytest.approx(4.)
    validate_test_metrics_payload(t.payload())
    spool.close()


def test_slow_subthreshold_motion_accumulates_without_counting_stationary_jitter(tmp_path):
    spool,t,c=tracker(tmp_path);t.apply_control(control(),1700000000.+c[0])
    record(t,c,0)
    for i in range(20):record(t,c,.001 if i%2 else -.001)
    assert sum(s['distance_m'] for s in t.payload()['samples'])==0
    for i in range(1,21):record(t,c,i*.001)
    assert .015 <= sum(s['distance_m'] for s in t.payload()['samples']) <= .021
    spool.close()


@pytest.mark.parametrize('reason', ['gap','clock_reset','jump','frame','nonfinite'])
def test_invalid_segments_reset_baseline_and_do_not_create_a_bridge(tmp_path,reason):
    spool,t,c=tracker(tmp_path);t.apply_control(control(),1700000000.+c[0])
    record(t,c,0);record(t,c,.01)
    if reason=='gap':record(t,c,100,delta=3)
    elif reason=='clock_reset':record(t,c,100,stamp=1)
    elif reason=='jump':record(t,c,100)
    elif reason=='frame':
        c[0]+=.1;t.observe(odom(100,stamp=c[0],frame='map'))
    else:
        c[0]+=.1;t.observe(odom(float('nan'),stamp=c[0]))
    record(t,c,100,delta=3)
    record(t,c,100.01)
    rows=t.payload()['samples']
    assert sum(s['distance_m'] for s in rows)==pytest.approx(.02)
    assert t.payload()['status']['rejected_samples']>=1
    assert sum(s['valid_seconds'] for s in rows)<.5
    spool.close()


def test_never_started_waiting_then_valid_zero_distance_and_stale_are_distinct(tmp_path):
    spool,t,c=tracker(tmp_path);t.apply_control(control(),1700000000.+c[0])
    assert t.payload()['status']['odom_status']=='WAITING_UPSTREAM'
    assert not t.payload()['samples']
    record(t,c,0);record(t,c,0)
    packet=t.payload()
    assert packet['status']['odom_status']=='AVAILABLE'
    assert packet['samples'][0]['distance_m']==0
    assert packet['samples'][0]['valid_seconds']>0
    c[0]+=3
    assert t.payload()['status']['odom_status']=='STALE'
    spool.close()


def test_restart_preserves_active_control_queue_ids_and_does_not_bridge_restart(tmp_path):
    spool,t,c=tracker(tmp_path);run=control()
    t.apply_control(run,1700000000.+c[0])
    record(t,c,0);record(t,c,.01);t.flush()
    first=spool.batch()[0];spool.close()
    spool=TestMetricSpool(tmp_path/'metrics.sqlite3')
    t=TestMetricTracker(spool,lambda:c[0],lambda:1700000000.+c[0])
    assert t.control==run and spool.batch()[0]==first
    record(t,c,200);record(t,c,200.01)
    assert sum(s['distance_m'] for s in t.payload()['samples'])==pytest.approx(.02)
    spool.ack([first['id']])
    assert len(spool.batch())==1
    spool.close()


def test_monotonic_server_anchor_survives_local_wall_clock_adjustment(tmp_path):
    spool=TestMetricSpool(tmp_path/'metrics.sqlite3');mono=[100.];wall=[1700000000.]
    t=TestMetricTracker(spool,lambda:mono[0],lambda:wall[0])
    t.apply_control(control(),1700000000.)
    t.observe(odom(0,stamp=100.))
    wall[0]+=3600
    mono[0]+=.1;t.observe(odom(.01,stamp=100.1))
    s=t.payload()['samples'][0]
    assert s['end']-s['start']==pytest.approx(.1)
    assert s['end']<1700000001.
    spool.close()


def test_spool_capacity_is_explicit_without_evicting_unacknowledged_samples(tmp_path):
    spool=TestMetricSpool(tmp_path/'metrics.sqlite3',capacity=32)
    rows=[sample() for _ in range(33)]
    for s in rows[:32]:assert spool.add(s)
    assert not spool.add(rows[32])
    assert spool.dropped()==1
    assert [s['id'] for s in spool.batch()]==[s['id'] for s in rows[:32]]
    spool.ack([rows[0]['id']]);assert spool.add(rows[32])
    assert rows[1]['id']==spool.batch()[0]['id']
    spool.close()


def test_control_rejects_bad_input_and_ignores_old_revision(tmp_path):
    spool,t,c=tracker(tmp_path);run=control(revision=5)
    t.apply_control(run,1700000000.)
    t.apply_control(dict(run,revision=4,state='paused'),1700000001.)
    assert t.control==run
    for changes in ({'revision':True},{'run_id':None},{'state':'move'},{'since':float('inf')}):
        with pytest.raises(ValueError):t.apply_control(dict(run,**changes),1700000000.)
    spool.close()


def test_ack_only_sent_metrics_ids_and_reliable_queue_has_separate_slot(tmp_path):
    spool=TestMetricSpool(tmp_path/'metrics.sqlite3');a,b=sample(),sample()
    spool.add(a)
    u=Uploader(Config('r','s',cloud_url='https://isolated.invalid',token='x'*32))
    old_capacity=u.queue.max_keys
    u.enable_test_metrics(spool.ack,lambda *args:None)
    assert u.queue.max_keys==old_capacity+1
    status=dict(applied_revision=0,run_id=None,state='idle',odom_status='WAITING_UPSTREAM',
                last_sample_timestamp=None,rejected_samples=0,dropped_samples=0)
    u.enqueue('test_metrics','/observer/test_metrics',dict(samples=spool.batch(),status=status))
    sent=u.queue.peek();spool.add(b)
    u.enqueue('test_metrics','/observer/test_metrics',dict(samples=spool.batch(),status=status))
    u._ack_test_metrics(sent);u.queue.remove(sent)
    assert spool.batch()==[b] and u.queue.peek() is not None
    cameras=Uploader(Config('r','c',role='cameras',cloud_url='https://isolated.invalid',token='x'*32))
    with pytest.raises(ValueError):cameras.enable_test_metrics(spool.ack,lambda *args:None)
    spool.close()


def test_real_loopback_requires_explicit_metric_ack_and_applies_counter_control(tmp_path):
    async def run():
        spool=TestMetricSpool(tmp_path/'metrics.sqlite3');s=sample();spool.add(s)
        received=[];controls=[];u=None;desired=control()
        async def handler(request):
            packet=await request.json();received.append(packet['payload']['samples'][0]['id'])
            if len(received)==1:return web.json_response({'accepted':False})
            u.stop_event.set()
            return web.json_response({'accepted':True,'server_timestamp':time.time(),'test_control':desired})
        app=web.Application();app.router.add_post('/api/v1/ingest',handler)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start();port=site._server.sockets[0].getsockname()[1]
        u=Uploader(Config('r','s',cloud_url=f'http://127.0.0.1:{port}',allow_insecure_local=True,token='x'*32))
        u.enable_test_metrics(spool.ack,lambda *args:controls.append(args))
        status=dict(applied_revision=0,run_id=None,state='idle',odom_status='WAITING_UPSTREAM',
                    last_sample_timestamp=None,rejected_samples=0,dropped_samples=0)
        u.enqueue('test_metrics','/observer/test_metrics',dict(samples=[s],status=status))
        async def pause(_):await asyncio.sleep(0)
        u._pause=pause
        try:
            await asyncio.wait_for(u.run(),5)
            assert received==[s['id'],s['id']] and not spool.batch()
            assert controls[0][0]==desired and len(controls[0])==3
        finally:
            await runner.cleanup();spool.close()
    asyncio.run(run())


def test_a_slow_valid_delivery_does_not_make_the_previous_chunk_oversized(tmp_path):
    spool,t,c=tracker(tmp_path);t.apply_control(control(),1700000000.+c[0])
    record(t,c,0)
    record(t,c,.04,delta=.4)
    record(t,c,.22,delta=1.8)
    rows=t.payload()['samples']
    assert sum(s['distance_m'] for s in rows)==pytest.approx(.22)
    assert sum(s['valid_seconds'] for s in rows)==pytest.approx(2.2)
    assert all(s['end']-s['start']<=2. for s in rows)
    assert t.payload()['status']['rejected_samples']==0
    spool.close()
