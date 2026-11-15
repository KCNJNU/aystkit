"""Grouped operator history preserves exact source timestamps and legacy evidence."""
import json
import sqlite3
import time
import unittest
import uuid

from multidict import MultiDict

from cloud.backend.app import STORE
from cloud.backend.fault_logs import parse_filters
from tests import test_fault_backend as fixtures
from tests.test_fault_backend import batch, fault, historical_row


class FaultGroupStorageTests(unittest.TestCase):
    setUp = fixtures.FaultStorageTests.setUp
    tearDown = fixtures.FaultStorageTests.tearDown

    def add(self, events, *, robot='test-robot', source='rk3588'):
        with self.store.db:
            return self.store.faults.add_batch(robot, source, events)

    def groups(self, **filters):
        return self.store.faults.query(dict(grouped=True, **filters))

    def test_grouping_spans_all_incidents_before_page_and_preserves_defaults(self):
        base = self.now[0]
        for index in range(4):
            self.now[0] = base + index * 700
            self.add([fault('Timed out at %s seconds' % index, timestamp=self.now[0],
                            code='NAVIGATION_TIMEOUT', level='WARN')])
        self.assertEqual(self.store.faults.query({})['total'], 4)
        self.assertEqual(len(list(self.store.faults.select({}, export=True))), 4)
        data = self.groups(limit=1)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['count'], 4)
        self.assertEqual((data['items'][0]['timestamp'], data['items'][0]['last_timestamp']), (base, base + 2100))
        self.assertRegex(data['items'][0]['group_id'], r'^[a-f0-9]{64}$')
        self.assertEqual(self.groups(limit=1, offset=1)['items'], [])
        summary = self.store.faults.summary({'grouped': True})
        self.assertEqual((summary['total'], summary['warning']), (1, 1))

    def test_exact_times_survive_ingest_merging_restart_dedup_and_pagination(self):
        base = self.now[0]
        events = [fault('delta %.1f' % index, timestamp=base + index) for index in range(3)]
        self.add(events)
        self.add(events)
        row = self.groups()['items'][0]
        page = self.store.faults.timestamps(row['group_id'], {'limit': 2})
        self.assertEqual((page['total'],page['occurrence_count']), (3,3))
        self.assertEqual([item['timestamp'] for item in page['items']], [base + 2,base + 1])
        self.assertEqual({item['kind'] for item in page['items']}, {'event'})
        self.assertEqual([item['timestamp'] for item in self.store.faults.timestamps(row['group_id'], {'offset':2})['items']], [base])
        self.store.db.commit()
        from cloud.backend.fault_logs import FaultLogs
        reopened = FaultLogs(self.store.db, clock=lambda: self.now[0])
        self.assertEqual(reopened.timestamps(row['group_id'], {})['total'],3)
        self.assertEqual(reopened.query({'grouped':True})['items'][0]['group_id'],row['group_id'])

    def test_filters_apply_to_actual_timestamps_and_original_messages_before_grouping(self):
        base = self.now[0]
        self.add([fault('first evidence', timestamp=base),fault('second evidence', timestamp=base+1),
                  fault('first evidence', timestamp=base+2)])
        # Different generic error messages remain distinct groups.
        all_groups = self.groups()
        self.assertEqual(all_groups['total'], 2)
        data = self.groups(q='first evidence', start=base+1, end=base+3)
        self.assertEqual((data['total'],data['items'][0]['count']), (1,1))
        self.assertEqual(data['items'][0]['timestamp'],base+2)
        history = self.store.faults.timestamps(data['items'][0]['group_id'],{'q':'first evidence','start':base+1,'end':base+3})
        self.assertEqual((history['total'],history['occurrence_count']), (1,1))
        self.assertEqual(history['items'][0]['timestamp'],base+2)
        self.assertIsNone(self.store.faults.timestamps(data['items'][0]['group_id'],{'start':base+9}))

    def test_group_identity_retains_robot_source_level_component_and_device(self):
        event = fault('stale',code='CAMERA_PREVIEW_INTERRUPTED',component='camera',level='WARN',details={'device_id':'left'})
        self.add([event])
        self.add([dict(event,id=str(uuid.uuid4()))],source='jetson')
        self.add([dict(event,id=str(uuid.uuid4()))],robot='other-robot')
        self.add([dict(event,id=str(uuid.uuid4()),level='ERROR')])
        self.add([dict(event,id=str(uuid.uuid4()),component='other-camera')])
        self.add([dict(event,id=str(uuid.uuid4()),details={'device_id':'right'})])
        self.assertEqual(self.groups()['total'],6)

    def test_generic_status_codes_do_not_collapse_distinct_failures(self):
        self.add([fault('device error code=11 at 5.2 seconds',code='ROS_ERROR'),
                  fault('device error code=12 at 7.4 seconds',code='ROS_ERROR'),
                  fault('device error code=11 at 9.3 seconds',code='ROS_ERROR')])
        self.assertEqual(sorted(item['count'] for item in self.groups()['items']),[1,2])

    def test_legacy_ranges_and_reported_summaries_are_not_invented_exact_times(self):
        base = self.now[0]
        old = fault('same fault',timestamp=base)
        with self.store.db:
            historical_row(self.store.db,old,received_at=base,last_timestamp=base+100,count=7)
        self.add([fault('same fault',timestamp=base+200,details={'occurrences':4})])
        row = self.groups()['items'][0]
        self.assertEqual(row['count'],11)
        result = self.store.faults.timestamps(row['group_id'],{})
        self.assertEqual(result['total'],2)
        self.assertEqual(result['occurrence_count'],11)
        newest,oldest = result['items']
        self.assertEqual((newest['kind'],newest['count'],newest['timestamp'],newest['last_timestamp']),
                         ('reported_summary',4,base+200,base+200))
        self.assertEqual((oldest['kind'],oldest['count'],oldest['timestamp'],oldest['last_timestamp']),
                         ('historical_summary',7,base,base+100))

    def test_occurrences_are_atomic_with_ingest_checkpoint(self):
        self.store.db.execute("CREATE TRIGGER reject_group_checkpoint BEFORE INSERT ON sources BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.Error):
            self.store.ingest(batch([fault()]))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_occurrences').fetchone()[0],0)
        self.assertEqual(self.groups()['total'],0)
        self.store.db.execute('DROP TRIGGER reject_group_checkpoint')
        self.store.ingest(batch([fault()]))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_occurrences').fetchone()[0],1)

    def test_timestamp_cap_keeps_honest_aggregate_and_retention_removes_orphans(self):
        self.store.faults.max_occurrences = 2
        base = self.now[0]
        self.add([fault('same fault',timestamp=base+index) for index in range(4)])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_occurrences').fetchone()[0],2)
        row = self.groups()['items'][0]
        self.assertEqual(row['count'],4)
        history = self.store.faults.timestamps(row['group_id'],{})
        self.assertEqual((history['total'],history['occurrence_count']),(3,4))
        fallback = [item for item in history['items'] if item['kind']=='historical_summary']
        self.assertEqual(len(fallback),1)
        self.assertEqual(fallback[0]['count'],2)
        self.now[0] += 31*86400
        with self.store.db:
            self.store.faults.prune(force=True)
        self.assertEqual(self.groups()['total'],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_occurrences').fetchone()[0],0)

    def test_legacy_summary_canonical_code_and_lower_bound_survive(self):
        base = self.now[0]
        with self.store.db:
            historical_row(self.store.db,fault('summary',timestamp=base,code='REPEATED_FAULT_SUMMARY',
                details={'original_code':'NAVIGATION_TIMEOUT','original_message':'Goal failed','suppressed_count':8}),count=3)
        row = self.groups(code='NAVIGATION_TIMEOUT')['items'][0]
        self.assertEqual(row['count'],8)
        self.assertTrue(row['details']['legacy_count_is_lower_bound'])
        history = self.store.faults.timestamps(row['group_id'],{})
        self.assertEqual(history['items'][0]['kind'],'historical_summary')
        self.assertTrue(history['items'][0]['details']['legacy_count_is_lower_bound'])

    def test_summary_and_normal_codes_keep_evidence_in_both_arrival_orders(self):
        for reverse in (False,True):
            component = 'planner-reverse-%s' % reverse
            summary = fault('summary',code='REPEATED_FAULT_SUMMARY',component=component,level='WARN',
                details={'original_code':'NAVIGATION_TIMEOUT','original_message':'timeout','occurrences':4})
            normal = fault('timeout',code='NAVIGATION_TIMEOUT',component=component,level='WARN',details={})
            self.add([normal,summary] if reverse else [summary,normal])
            row = self.groups(q=component)['items'][0]
            self.assertEqual((row['code'],row['count']),('NAVIGATION_TIMEOUT',5))
            history = self.store.faults.timestamps(row['group_id'],{})
            self.assertEqual((history['total'],history['occurrence_count']),(2,5))
            self.assertEqual(sorted(item['kind'] for item in history['items']),['event','reported_summary'])

    def test_replay_does_not_duplicate_when_noise_evicted_receipt(self):
        self.store.faults.max_receipts = 1
        event = fault('original timestamp')
        self.add([event])
        self.add([fault('original timestamp',timestamp=event['timestamp']+1)])
        self.add([fault('filtered noise',level='WARN',code='ROS_WARNING')])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_receipts').fetchone()[0],1)
        self.assertEqual(self.add([event]),0)
        row = self.groups()['items'][0]
        self.assertEqual(row['count'],2)
        self.assertEqual(self.store.faults.timestamps(row['group_id'],{})['total'],2)

    def test_lower_bound_flag_propagates_from_older_entry_to_latest_evidence(self):
        base = self.now[0]
        with self.store.db:
            historical_row(self.store.db,fault('summary',timestamp=base,level='WARN',code='REPEATED_FAULT_SUMMARY',
                details={'original_code':'NAVIGATION_TIMEOUT','original_message':'timeout','suppressed_count':8}),count=3)
        self.add([fault('timeout',timestamp=base+1,level='WARN',code='NAVIGATION_TIMEOUT')])
        row = self.groups()['items'][0]
        self.assertEqual(row['count'],9)
        self.assertTrue(row['details']['legacy_count_is_lower_bound'])


class FaultGroupAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FaultAPITests.asyncSetUp
    asyncTearDown = fixtures.FaultAPITests.asyncTearDown
    login = fixtures.FaultAPITests.login

    async def test_grouped_endpoints_authorization_validation_and_filtered_history(self):
        paths = ('/api/fault-logs?grouped=1','/api/fault-logs/summary?grouped=1',
                 '/api/fault-logs/groups/'+'a'*64+'/timestamps')
        for path in paths:
            self.assertEqual((await self.client.get(path)).status,401)
        await self.login()
        store = self.app[STORE]
        base = time.time()-50
        with store.db:
            store.faults.add_batch(store.config.robot_id,store.primary_source,
                                  [fault('same fault',timestamp=base+index) for index in range(3)])
        page = await (await self.client.get(paths[0])).json()
        self.assertEqual(page['total'],1)
        group_id = page['items'][0]['group_id']
        self.assertEqual((await (await self.client.get(paths[1])).json())['total'],1)
        path = '/api/fault-logs/groups/'+group_id+'/timestamps'
        response = await self.client.get(path+'?limit=1&offset=1')
        self.assertEqual(response.status,200)
        result = await response.json()
        self.assertEqual((result['total'],result['occurrence_count']),(3,3))
        self.assertEqual(result['items'][0]['timestamp'],base+1)
        response = await self.client.get(path+'?start='+str(base+1)+'&end='+str(base+1))
        self.assertEqual((await response.json())['occurrence_count'],1)
        self.assertEqual((await self.client.get('/api/fault-logs/groups/'+'b'*64+'/timestamps')).status,404)
        for query in ('grouped=0','grouped=true','grouped=1&grouped=1'):
            self.assertEqual((await self.client.get('/api/fault-logs?'+query)).status,400)
        for suffix in ('BAD/timestamps', 'A'*64+'/timestamps'):
            self.assertEqual((await self.client.get('/api/fault-logs/groups/'+suffix)).status,400)
        for offset in (20050,120000):
            response = await self.client.get(path+'?offset='+str(offset))
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['items'],[])
        self.assertEqual((await self.client.get('/api/fault-logs?grouped=1&offset=20050')).status,400)
        for query in ('limit=201','offset=-1','offset=120001','start=nan','grouped=1'):
            self.assertEqual((await self.client.get(path+'?'+query)).status,400)
        self.assertEqual((await self.client.get('/api/fault-logs/export?grouped=1')).status,400)
        self.assertEqual((await self.client.get('/api/fault-logs/summary?level=ERROR')).status,400)
