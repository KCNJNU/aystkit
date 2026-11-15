"""Drive-axis samples through the durable counter and real isolated Store."""
import uuid

import pytest

from collector.test_metrics import TestMetricSpool, TestMetricTracker
from cloud.backend.store import Store
from shared.protocol import validate_envelope
from tests.test_backend import config
from tests.test_daily_test_collector import odom


def test_counter_queue_to_cloud_respects_manual_pause_and_late_delivery(tmp_path):
    settings=config(str(tmp_path/'cloud'));settings.validate()
    clock=[100.]
    wall=lambda:1700000000.+clock[0]
    store=Store(settings,clock=wall)
    spool=TestMetricSpool(tmp_path/'counter.sqlite3')
    tracker=TestMetricTracker(spool,lambda:clock[0],wall)
    session=str(uuid.uuid4());sequence=[0]

    def sync():
        tracker.apply_control(store.tests.collector_control_state(),wall())

    def pose(x):
        clock[0]+=.1
        tracker.observe(odom(x,stamp=clock[0]))

    def send():
        sequence[0]+=1
        payload=tracker.payload()
        packet=validate_envelope(dict(schema_version=1,robot_id=settings.robot_id,source_id='rk3588',
            session_id=session,sequence=sequence[0],kind='test_metrics',topic='/observer/test_metrics',
            message_timestamp=None,collector_timestamp=wall(),payload=payload),now=wall())
        receipt=store.ingest(packet)
        assert receipt['accepted'] and set(receipt['test_control'])=={'revision','run_id','state','since'}
        spool.ack([s['id'] for s in payload['samples']])
        tracker.apply_control(receipt['test_control'],receipt['server_timestamp'])
        return receipt

    try:
        store.tests.control('start',0,str(uuid.uuid4()),name='隔离测试')
        sync()
        pose(0)
        for i in range(1,11):pose(i*.01)
        store.tests.control('pause',1,str(uuid.uuid4()))
        # Simulate disconnected control feedback: source still believes the
        # old interval is recording. Cloud must clip this late evidence.
        for i in range(1,11):pose(.1+i*.01)
        send()
        paused=store.tests_snapshot()
        assert paused['active']['state']=='paused'
        assert paused['active']['duration_s']==pytest.approx(1.1)
        assert paused['active']['distance_m']==pytest.approx(.1)
        store.tests.control('resume',2,str(uuid.uuid4()))
        sync()
        pose(20.)
        pose(20.01)
        pose(20.02)
        store.tests.control('finish',3,str(uuid.uuid4()))
        send()
        final=store.tests_snapshot()
        assert final['active'] is None
        assert final['runs'][0]['duration_s']==pytest.approx(1.4)
        assert final['runs'][0]['distance_m']==pytest.approx(.12)
        assert final['summary']['distance_m']==pytest.approx(.12)
        assert final['summary']['sessions']==1
        assert not spool.batch()
        assert final['source']['applied_revision']==3  # status reports the sent generation
        send()
        assert store.tests_snapshot()['source']['applied_revision']==4
        store.close()
        store=Store(settings,clock=wall)
        restored=store.tests_snapshot()
        assert restored['runs'][0]['duration_s']==pytest.approx(1.4)
        assert restored['runs'][0]['distance_m']==pytest.approx(.12)
        assert restored['revision']==4
    finally:
        spool.close();store.close()
