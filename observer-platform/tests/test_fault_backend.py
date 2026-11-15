"""Fault persistence/authorization/export tests; all events use isolated databases."""
import copy
import csv
import io
import json
import sqlite3
import tempfile
import time
import unittest
import uuid

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from cloud.backend.app import ACCOUNTS, STORE, create_app
from cloud.backend.fault_logs import FaultLogs, MERGE_WINDOW_SECONDS, csv_cell
from cloud.backend.store import ReplayError, Store
from shared.protocol import validate_envelope
from shared.faults import KEY_WARNING_CODES, fault_group_key, should_record_fault
from tests.test_backend import config, ORIGIN, PASSWORD, TOKEN, CAMERA_TOKEN
from tests.test_protocol import envelope


def fault(message='雷达时间回退', timestamp=None, **fields):
    return dict({'id': str(uuid.uuid4()), 'timestamp': timestamp or time.time(), 'level': 'ERROR',
                 'code': 'LIDAR_TIME_BACKWARD', 'component': '/livox/lidar',
                 'message': message, 'details': {'evidence': 'header_stamp', 'delta_s': -0.5}}, **fields)


def batch(events, sequence=1, session=None):
    result = envelope('heartbeat', sequence=sequence, session=session)
    result.update(kind='events', topic='/observer/events', payload={'events': events})
    return validate_envelope(result)


def historical_row(db, event, *, robot='test-robot', source='rk3588', received_at=None,
                   last_timestamp=None, count=1):
    """Seed a pre-upgrade row with its distinct legacy message fingerprint."""
    received_at = event['timestamp'] if received_at is None else received_at
    last_timestamp = event['timestamp'] if last_timestamp is None else last_timestamp
    db.execute('INSERT INTO fault_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (event['id'], robot, source, event['timestamp'], last_timestamp, received_at,
                event['level'], event['code'], event['component'], event['message'],
                json.dumps(event['details']), count, 'legacy-' + event['id']))
    return event['id']


class FaultStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='observer-fault-storage-')
        self.now = [time.time()]
        settings = config(self.temp.name)
        settings.validate()
        self.store = Store(settings, clock=lambda: self.now[0])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_persists_deduplicates_and_aggregates_after_restart(self):
        event = fault()
        obj = batch([event])
        self.assertEqual(self.store.ingest(obj)['event_count'], 1)
        with self.assertRaises(ReplayError):
            self.store.ingest(obj)
        obj['sequence'] = 2
        self.assertEqual(self.store.ingest(obj)['event_count'], 0)
        event2 = dict(event, id=str(uuid.uuid4()), timestamp=event['timestamp'] + 2)
        obj.update(sequence=3, payload={'events': [event2]})
        self.store.ingest(obj)
        rows = self.store.faults.query({})
        self.assertEqual(rows['total'], 1)
        self.assertEqual(rows['items'][0]['count'], 2)
        self.assertEqual(rows['items'][0]['timestamp'], event['timestamp'])
        self.assertEqual(rows['items'][0]['last_timestamp'], event2['timestamp'])
        self.store.close()
        settings = config(self.temp.name)
        settings.validate()
        self.store = Store(settings, clock=lambda: self.now[0])
        obj.update(sequence=4, payload={'events': [event]})
        self.assertEqual(self.store.ingest(obj)['event_count'], 0)
        self.assertEqual(self.store.faults.query({})['items'][0]['count'], 2)

    def test_faults_receipts_and_sequence_commit_atomically(self):
        self.store.db.execute("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON sources BEGIN SELECT RAISE(ABORT,'test'); END")
        obj = batch([fault()])
        with self.assertRaises(sqlite3.Error):
            self.store.ingest(obj)
        self.assertEqual(self.store.faults.summary()['total'], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_receipts').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_spans').fetchone()[0], 0)
        self.assertEqual(self.store.sources, {})
        self.store.db.execute('DROP TRIGGER reject_checkpoint')
        self.assertEqual(self.store.ingest(obj)['event_count'], 1)

    def test_outage_backlog_uses_receipt_retention_and_hard_caps(self):
        self.store.faults.max_records = 3
        self.store.faults.max_receipts = 4
        with self.store.db:
            for i in range(6):
                self.store.faults.add_batch('test-robot', 'rk3588', [fault(('alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot')[i], timestamp=1)])
        self.assertEqual(self.store.faults.summary()['total'], 3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_receipts').fetchone()[0], 4)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_spans').fetchone()[0], 3)
        self.now[0] += 31 * 86400
        with self.store.db:
            self.store.faults.prune(force=True)
        self.assertEqual(self.store.faults.summary()['total'], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_receipts').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_spans').fetchone()[0], 0)

    def test_source_ids_scope_dedup_and_search_escapes_wildcards(self):
        event = fault('含百分号 100%_安全')
        with self.store.db:
            self.store.faults.add_batch('test-robot', 'rk3588', [event])
            self.store.faults.add_batch('test-robot', 'jetson', [event, fault('其他')])
        self.assertEqual(self.store.faults.summary()['total'], 3)
        self.assertEqual(self.store.faults.query({'q': '%_'})['total'], 2)
        self.assertEqual(self.store.faults.query({'q': "' OR 1=1 --"})['total'], 0)

    def test_recording_policy_keeps_key_warnings_and_all_serious_errors(self):
        for code in KEY_WARNING_CODES:
            self.assertTrue(should_record_fault('WARN', code), code)
            self.assertFalse(should_record_fault('INFO', code), code)
        for code in ('ROS_WARNING', 'ROS_DIAGNOSTIC_WARN', 'NODE_TIMEOUT', 'UNKNOWN_WARNING'):
            self.assertFalse(should_record_fault('WARN', code), code)
        for level in ('ERROR', 'FATAL'):
            self.assertTrue(should_record_fault(level, 'UNKNOWN_ERROR'))
        self.assertTrue(should_record_fault('WARN', 'REPEATED_FAULT_SUMMARY', {'original_code': 'NAVIGATION_TIMEOUT'}))
        self.assertFalse(should_record_fault('WARN', 'REPEATED_FAULT_SUMMARY', {'original_code': 'ROS_WARNING'}))
        self.assertFalse(should_record_fault('INFO', 'FAULT_MONITOR_STARTED'))
        self.assertFalse(should_record_fault('INFO', 'DEVICE_DATA_RECOVERED'))

    def test_filtered_events_receive_durable_receipts_without_fault_rows(self):
        events = [fault('恢复', level='INFO', code='DEVICE_DATA_RECOVERED'),
                  fault('普通告警', level='WARN', code='ROS_WARNING'),
                  fault('采样断档', level='WARN', code='LIDAR_TIMESTAMP_GAP'),
                  fault('未知严重故障', code='UNKNOWN_ERROR'),
                  fault('进程终止', level='FATAL', code='PROCESS_EXITED')]
        obj = batch(events)
        self.assertEqual(self.store.ingest(obj)['event_count'], 5)
        self.assertEqual(self.store.faults.summary()['total'], 3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_receipts').fetchone()[0], 5)
        obj['sequence'] = 2
        self.assertEqual(self.store.ingest(obj)['event_count'], 0)
        self.assertEqual(self.store.faults.summary()['total'], 3)

    def test_filtered_only_receipts_and_checkpoint_are_atomic(self):
        self.store.db.execute("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON sources BEGIN SELECT RAISE(ABORT,'test'); END")
        obj = batch([fault('普通告警', level='WARN', code='ROS_WARNING')])
        with self.assertRaises(sqlite3.Error):
            self.store.ingest(obj)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_receipts').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_spans').fetchone()[0], 0)
        self.assertEqual(self.store.sources, {})
        self.store.db.execute('DROP TRIGGER reject_checkpoint')
        self.assertEqual(self.store.ingest(obj)['event_count'], 1)
        self.assertEqual(self.store.faults.summary()['total'], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_receipts').fetchone()[0], 1)

    def test_legacy_noise_is_hidden_consistently_without_deleting_history(self):
        events = [fault('启动', level='INFO', code='FAULT_MONITOR_STARTED'),
                  fault('普通告警', level='WARN', code='ROS_WARNING'),
                  fault('采样断档', level='WARN', code='IMU_TIMESTAMP_GAP'),
                  fault('未知严重故障', code='UNKNOWN_ERROR'),
                  fault('关键重复', level='WARN', code='REPEATED_FAULT_SUMMARY', details={'original_code': 'NAVIGATION_TIMEOUT'}),
                  fault('普通重复', level='WARN', code='REPEATED_FAULT_SUMMARY', details={'original_code': 'ROS_WARNING'})]
        with self.store.db:
            for event in events:
                self.store.db.execute('INSERT INTO fault_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (event['id'], 'test-robot', 'rk3588', event['timestamp'], event['timestamp'], self.now[0],
                     event['level'], event['code'], event['component'], event['message'], json.dumps(event['details']), 1, event['id']))
        expected = {'IMU_TIMESTAMP_GAP', 'UNKNOWN_ERROR', 'NAVIGATION_TIMEOUT'}
        result = self.store.faults.query({})
        self.assertEqual(result['total'], 3)
        self.assertEqual({item['code'] for item in result['items']}, expected)
        self.assertEqual(self.store.faults.query({'level': 'INFO'})['total'], 0)
        self.assertEqual(self.store.faults.query({'code': 'ROS_WARNING'})['total'], 0)
        exported = [self.store.faults._item(row) for row in self.store.faults.select({}, export=True)]
        self.assertEqual({item['code'] for item in exported}, expected)
        summary = self.store.faults.summary()
        self.assertEqual((summary['total'], summary['error'], summary['warning']), (3, 1, 2))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_logs').fetchone()[0], 6)

    def test_semantic_incidents_merge_for_ten_minutes_and_count_occurrences(self):
        first = fault('Goal alpha timed out after 2.3s', level='WARN', code='NAVIGATION_TIMEOUT')
        with self.store.db:
            self.store.faults.add_batch('test-robot', 'rk3588', [first])
        self.now[0] += 500
        second = fault('Goal beta timed out after 4.1s', timestamp=first['timestamp'] + 500,
                       level='WARN', code='NAVIGATION_TIMEOUT', details={'occurrences': 7, 'latest': 'beta'})
        with self.store.db:
            self.store.faults.add_batch('test-robot', 'rk3588', [second])
        self.assertEqual(self.store.faults.summary()['total'], 1)
        row = self.store.faults.query({})['items'][0]
        self.assertEqual(row['count'], 8)
        self.assertEqual(row['timestamp'], first['timestamp'])
        self.assertEqual(row['last_timestamp'], second['timestamp'])
        self.assertEqual(row['message'], second['message'])
        self.assertEqual(row['details']['latest'], 'beta')
        # A sliding receive window, rather than a fixed bucket after first fault.
        self.now[0] += 500
        with self.store.db:
            self.store.faults.add_batch('test-robot', 'rk3588', [dict(second, id=str(uuid.uuid4()))])
        self.assertEqual(self.store.faults.summary()['total'], 1)
        self.assertEqual(self.store.faults.query({})['items'][0]['count'], 15)
        self.now[0] += MERGE_WINDOW_SECONDS + 1
        with self.store.db:
            self.store.faults.add_batch('test-robot', 'rk3588', [dict(second, id=str(uuid.uuid4()))])
        self.assertEqual(self.store.faults.summary()['total'], 2)

    def test_generic_error_groups_preserve_semantics_components_and_levels(self):
        a = 'Lookup failed at 12.3 for goal 8f1d65e6-2f12-4f3a-983e-00315bf863c7 address 0xAB12'
        b = 'Lookup failed at 99.7 for goal c95edf10-4f2c-4a01-aec6-88aac1701e7d address 0xCD34'
        self.assertEqual(fault_group_key('ROS_ERROR', 'node', a), fault_group_key('ROS_ERROR', 'node', b))
        self.assertNotEqual(fault_group_key('ROS_ERROR', 'node', a), fault_group_key('ROS_ERROR', 'node', 'Socket failed at 12.3'))
        self.assertNotEqual(fault_group_key('ROS_ERROR', 'node', 'camera1 failed'), fault_group_key('ROS_ERROR', 'node', 'camera2 failed'))
        for prefix in ('errno ', 'error code=', 'status: ', 'signal ', '错误码：'):
            self.assertNotEqual(fault_group_key('ROS_ERROR', 'node', prefix + '2'),
                                fault_group_key('ROS_ERROR', 'node', prefix + '5'), prefix)
        events = [fault(a, code='ROS_ERROR', details={'occurrences': 2}), fault(b, code='ROS_ERROR'),
                  fault('Socket failed at 12.3', code='ROS_ERROR'),
                  fault(a, code='ROS_ERROR', component='another'), fault(a, code='ROS_ERROR', level='FATAL')]
        with self.store.db:
            self.store.faults.add_batch('test-robot', 'rk3588', events)
        rows = self.store.faults.query({})['items']
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(row['count'] for row in rows), 6)

    def test_occurrences_rejects_booleans_floats_strings_and_out_of_range(self):
        values = [True, False, 0, -1, 2.5, '99', 1_000_001, None, 1_000_000]
        with self.store.db:
            self.store.faults.add_batch('test-robot', 'rk3588',
                [fault('明确故障', code='NAVIGATION_FAILURE', details={'occurrences': value}) for value in values])
        self.assertEqual(self.store.faults.query({})['items'][0]['count'], 1_000_008)

    def test_history_groups_old_fingerprints_with_stable_id_full_times_and_latest_evidence(self):
        base = self.now[0]
        first = fault('first-only 超时 2s', timestamp=base - 3000, level='WARN', code='NAVIGATION_TIMEOUT')
        second = fault('middle-only 超时 4s', timestamp=base - 2500, level='WARN', code='NAVIGATION_TIMEOUT')
        latest = fault('latest-only 超时 9s', timestamp=base - 2000, level='WARN', code='NAVIGATION_TIMEOUT',
                       details={'latest': 'evidence', 'occurrences': 7})
        with self.store.db:
            historical_row(self.store.db, first, received_at=base)
            historical_row(self.store.db, second, received_at=base + 500)
            historical_row(self.store.db, latest, received_at=base + 1000, last_timestamp=base - 1800, count=7)
        before = self.store.db.execute('SELECT rowid,* FROM fault_logs ORDER BY rowid').fetchall()
        changes = self.store.db.total_changes
        result = self.store.faults.query({})
        self.assertEqual(result['total'], 1)
        row = result['items'][0]
        self.assertEqual(row['id'], first['id'])
        self.assertEqual((row['timestamp'], row['last_timestamp'], row['received_at']), (base - 3000, base - 1800, base + 1000))
        self.assertEqual(row['message'], latest['message'])
        self.assertEqual(row['details'], latest['details'])
        self.assertEqual(row['count'], 9)  # stored count already includes occurrences
        # Filters operate after complete incidents have been formed.
        filtered = self.store.faults.query({'start': base - 1900, 'end': base - 2900, 'q': 'latest-only'})
        self.assertEqual(filtered['items'][0]['id'], first['id'])
        self.assertEqual(filtered['items'][0]['timestamp'], base - 3000)
        self.assertEqual(filtered['items'][0]['count'], 9)
        self.assertEqual(self.store.faults.query({'q': 'first-only'})['total'], 0)
        self.assertEqual(self.store.faults.summary()['total'], 1)
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(self.store.db.execute('SELECT rowid,* FROM fault_logs ORDER BY rowid').fetchall(), before)
        self.assertEqual(self.store.faults.query({})['items'][0]['id'], first['id'])

    def test_history_continuous_window_includes_600_seconds_and_splits_larger_gap(self):
        base = self.now[0]
        events = [fault('Planner failed at ' + str(value), timestamp=base + value, code='ROS_ERROR')
                  for value in (0, 600, 1200, 1801, 2401)]
        with self.store.db:
            for value, event in zip((0, 600, 1200, 1801, 2401), events):
                historical_row(self.store.db, event, received_at=base + value)
        result = self.store.faults.query({'limit': 1, 'offset': 0})
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['items'][0]['id'], events[3]['id'])
        self.assertEqual(result['items'][0]['count'], 2)
        previous = self.store.faults.query({'limit': 1, 'offset': 1})['items'][0]
        self.assertEqual(previous['id'], events[0]['id'])
        self.assertEqual(previous['count'], 3)
        self.assertEqual(self.store.faults.summary()['error'], 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_logs').fetchone()[0], 5)

    def test_history_groups_respect_source_robot_component_level_and_error_code(self):
        base = self.now[0]
        variants = [
            (fault('Planner failed at 12.3', code='ROS_ERROR'), {}),
            (fault('Planner failed at 14.9', code='ROS_ERROR'), {}),
            (fault('Planner failed errno 2', code='ROS_ERROR'), {}),
            (fault('Planner failed errno 5', code='ROS_ERROR'), {}),
            (fault('Planner failed at 14.9', code='ROS_ERROR'), {'source': 'jetson'}),
            (fault('Planner failed at 14.9', code='ROS_ERROR'), {'robot': 'another-robot'}),
            (fault('Planner failed at 14.9', code='ROS_ERROR', level='FATAL'), {}),
            (fault('Planner failed at 14.9', code='ROS_ERROR', component='different'), {}),
            (fault('Planner failed at 14.9', code='NODE_ERROR'), {}),
        ]
        with self.store.db:
            for index, (event, identity) in enumerate(variants):
                historical_row(self.store.db, event, received_at=base + index, **identity)
        result = self.store.faults.query({})
        self.assertEqual(result['total'], 8)
        self.assertEqual(sum(row['count'] for row in result['items']), 9)
        self.assertEqual(sum(row['count'] == 2 for row in result['items']), 1)
        self.assertEqual(self.store.faults.summary()['error'], 8)

    def test_history_legacy_summaries_join_original_fault_and_propagate_count_lower_bound(self):
        base = self.now[0]
        first = fault('第一次超时', level='WARN', code='NAVIGATION_TIMEOUT')
        summary = fault('旧汇总', level='WARN', code='REPEATED_FAULT_SUMMARY',
                        details={'original_code': 'NAVIGATION_TIMEOUT', 'original_message': '旧超时', 'suppressed_count': 4})
        ambiguous = fault('多批旧汇总', level='WARN', code='REPEATED_FAULT_SUMMARY',
                          details={'original_code': 'NAVIGATION_TIMEOUT', 'original_message': '多批旧超时', 'suppressed_count': 8})
        modern = fault('最新超时', level='WARN', code='NAVIGATION_TIMEOUT',
                       details={'occurrences': 7, 'similar_events_suppressed_since_previous': 6, 'latest': True})
        with self.store.db:
            historical_row(self.store.db, first, received_at=base)
            historical_row(self.store.db, summary, received_at=base + 100)
            historical_row(self.store.db, ambiguous, received_at=base + 200, count=3)
            historical_row(self.store.db, modern, received_at=base + 300, count=7)
        result = self.store.faults.query({})
        self.assertEqual(result['total'], 1)
        row = result['items'][0]
        self.assertEqual(row['code'], 'NAVIGATION_TIMEOUT')
        self.assertEqual(row['count'], 20)  # 1 + 4 + known lower bound 8 + already-actual 7
        self.assertEqual(row['message'], modern['message'])
        self.assertTrue(row['details']['latest'])
        self.assertTrue(row['details']['legacy_count_is_lower_bound'])
        self.assertEqual(row['id'], first['id'])
        self.assertEqual(self.store.faults.query({'code': 'NAVIGATION_TIMEOUT'})['total'], 1)
        self.assertEqual(self.store.faults.query({'code': 'REPEATED_FAULT_SUMMARY'})['total'], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_logs').fetchone()[0], 4)

    def test_legacy_suppressed_counts_cannot_overflow_sql_or_fabricate_occurrences(self):
        base = self.now[0]
        with self.store.db:
            for index, value in enumerate((2 ** 63 - 1, 2 ** 63 - 1, 1_000_001, True, -10, 3.5)):
                event = fault('旧汇总', level='WARN', code='REPEATED_FAULT_SUMMARY',
                              details={'original_code': 'NAVIGATION_TIMEOUT', 'original_message': '导航超时', 'suppressed_count': value})
                historical_row(self.store.db, event, received_at=base + index)
        result = self.store.faults.query({})
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['count'], 6)
        self.assertEqual(self.store.faults.summary()['total'], 1)

    def test_receive_span_keeps_legacy_group_and_id_stable_across_hours_of_updates(self):
        base = self.now[0]
        legacy = fault('Planner failed at 1.2', timestamp=base, code='ROS_ERROR')
        with self.store.db:
            historical_row(self.store.db, legacy, received_at=base)
        legacy_before = self.store.db.execute('SELECT * FROM fault_logs WHERE id=?', (legacy['id'],)).fetchone()
        # A newer physical row has a new fingerprint but the same semantic key.
        self.now[0] = base + 500
        with self.store.db:
            self.store.faults.add_batch('test-robot', 'rk3588', [fault('Planner failed at 2.4', timestamp=base - 10000, code='ROS_ERROR')])
        # Simulate a pre-span row being upgraded only on its next normal write.
        with self.store.db:
            self.store.db.execute('DELETE FROM fault_log_spans')
        for offset in (1000, 1500, 2000, 2500, 3000, 3500, 4000):
            self.now[0] = base + offset
            with self.store.db:
                self.store.faults.add_batch('test-robot', 'rk3588', [fault('Planner failed at ' + str(offset),
                    timestamp=base + 100000 - offset, code='ROS_ERROR')])
            result = self.store.faults.query({})
            self.assertEqual(result['total'], 1, offset)
            self.assertEqual(result['items'][0]['id'], legacy['id'], offset)
        self.assertEqual(result['items'][0]['count'], 9)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_logs').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute('SELECT first_received_at FROM fault_log_spans').fetchone()[0], base + 500)
        self.assertEqual(self.store.db.execute('SELECT * FROM fault_logs WHERE id=?', (legacy['id'],)).fetchone(), legacy_before)
        # An actual 601-second receive gap still starts a separate incident.
        self.now[0] += 601
        with self.store.db:
            self.store.faults.add_batch('test-robot', 'rk3588', [fault('Planner failed at 9000', code='ROS_ERROR')])
        self.assertEqual(self.store.faults.query({})['total'], 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_spans').fetchone()[0], 2)

    def test_receive_intervals_use_running_max_end_and_ignore_source_clock_spans(self):
        base = self.now[0]
        events = [fault('导航超时', timestamp=base + delta, level='WARN', code='NAVIGATION_TIMEOUT') for delta in (0, 500, 2500)]
        with self.store.db:
            historical_row(self.store.db, events[0], received_at=base + 2000)
            self.store.db.execute('INSERT INTO fault_log_spans VALUES (?,?)', (events[0]['id'], base))
            historical_row(self.store.db, events[1], received_at=base + 500)
            historical_row(self.store.db, events[2], received_at=base + 2500)
            # A separate source has an enormous source time span, but only two
            # actual receipts 1000 seconds apart. Do not infer a receive bridge.
            historical_row(self.store.db, fault('导航超时', timestamp=base - 100000, level='WARN', code='NAVIGATION_TIMEOUT'),
                           source='another-source', received_at=base, last_timestamp=base + 100000, count=20)
            historical_row(self.store.db, fault('导航超时', timestamp=base, level='WARN', code='NAVIGATION_TIMEOUT'),
                           source='another-source', received_at=base + 1000)
        result = self.store.faults.query({})
        self.assertEqual(result['total'], 3)
        primary = [row for row in result['items'] if row['source_id'] == 'rk3588']
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0]['id'], events[0]['id'])
        self.assertEqual(primary[0]['count'], 3)

    def test_existing_database_adds_compatible_span_table_without_rewriting_history(self):
        first = fault('旧导航故障', code='NAVIGATION_FAILURE')
        with self.store.db:
            historical_row(self.store.db, first, received_at=self.now[0])
            self.store.db.execute('DROP TABLE fault_log_spans')
        before = self.store.db.execute('SELECT rowid,* FROM fault_logs').fetchall()
        self.store.close()
        settings = config(self.temp.name)
        settings.validate()
        self.store = Store(settings, clock=lambda: self.now[0])
        self.assertEqual(self.store.db.execute('SELECT rowid,* FROM fault_logs').fetchall(), before)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_log_spans').fetchone()[0], 0)
        self.assertEqual(self.store.faults.query({})['items'][0]['id'], first['id'])

    def test_read_only_export_registers_functions_and_keeps_snapshot_during_ingest(self):
        base = self.now[0]
        first = fault('Planner failed at 1.2', timestamp=base, code='ROS_ERROR')
        with self.store.db:
            historical_row(self.store.db, first, received_at=base)
            historical_row(self.store.db, fault('Planner failed at 4.5', timestamp=base + 1, code='ROS_ERROR'), received_at=base + 1)
        db = sqlite3.connect('file:' + str(self.store.path) + '?mode=ro', uri=True)
        cursor = None
        try:
            db.execute('BEGIN')
            cursor = self.store.faults.select({}, export=True, connection=db)
            with self.store.db:
                self.store.faults.add_batch('test-robot', 'rk3588', [fault('Independent hardware failure', code='HARDWARE_FAILURE')])
            rows = []
            while True:
                chunk = cursor.fetchmany(1)
                if not chunk:
                    break
                rows.extend(chunk)
            self.assertEqual(len(rows), 1)
            self.assertEqual(self.store.faults._item(rows[0])['count'], 2)
            self.assertEqual(self.store.faults._item(rows[0])['id'], first['id'])
            self.assertEqual(db.total_changes, 0)
        finally:
            if cursor is not None:
                cursor.close()
            db.close()
        self.assertEqual(self.store.faults.query({})['total'], 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fault_logs').fetchone()[0], 3)

    def test_cloud_outage_and_recovery_record_without_a_browser(self):
        self.store.poll_faults()
        self.assertEqual(self.store.faults.summary()['total'], 0)
        obj = envelope('heartbeat', timestamp=self.now[0])
        self.store.ingest(obj)
        self.store.poll_faults()
        self.assertEqual(self.store.faults.summary()['total'], 0)
        self.now[0] += 20
        self.store.poll_faults()
        self.store.poll_faults()
        self.assertEqual(self.store.faults.summary()['total'], 1)
        self.assertEqual(self.store.faults.query({})['items'][0]['code'], 'COLLECTOR_CONNECTION_LOST')
        obj.update(sequence=2, collector_timestamp=self.now[0])
        self.store.ingest(obj)
        self.store.poll_faults()
        # Restoration remains part of live status, not the focused fault history.
        self.assertEqual(self.store.faults.summary()['total'], 1)


class FaultAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='observer-fault-api-')
        self.app = create_app(config(self.temp.name))
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def login(self):
        response = await self.client.post('/api/login', json={'username': 'viewer', 'password': PASSWORD}, headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200)

    async def ingest(self, obj, token=TOKEN):
        return await self.client.post('/api/v1/ingest', json=obj, headers={'Authorization': 'Bearer ' + token})

    async def test_auth_pending_and_approved_viewer_boundaries(self):
        paths = ('/api/fault-logs', '/api/fault-logs/summary', '/api/fault-logs/export?format=json')
        for path in paths:
            response = await self.client.get(path, headers={'Authorization': 'Bearer ' + TOKEN})
            self.assertEqual(response.status, 401)
        response = await self.client.post('/api/register', json={'username': 'fault.viewer', 'password': 'isolated-fault-password'}, headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 201)
        for path in paths:
            self.assertEqual((await self.client.get(path)).status, 403)
        account = self.app[ACCOUNTS].find('fault.viewer')
        self.app[ACCOUNTS].approve(account['id'], 'viewer')
        for path in paths:
            self.assertEqual((await self.client.get(path)).status, 200)
        self.assertEqual((await self.client.post('/api/v1/ingest', json=batch([fault()]))).status, 401)

    async def test_ingest_filters_exports_all_rows_and_csv_formula_safety(self):
        stamp = time.time() - 10
        events = [fault('正常测试', stamp), fault('=HYPERLINK("test")', stamp + 1),
                  fault('设备数据中断', stamp + 2, level='WARN', code='SENSOR_STALE')]
        response = await self.ingest(batch(events))
        self.assertEqual(response.status, 200, await response.text())
        await self.login()
        response = await self.client.get('/api/fault-logs?limit=1&offset=1')
        result = await response.json()
        self.assertEqual(result['total'], 3)
        self.assertEqual(len(result['items']), 1)
        response = await self.client.get('/api/fault-logs?level=WARN&q=中断')
        self.assertEqual((await response.json())['total'], 1)
        summary = await (await self.client.get('/api/fault-logs/summary')).json()
        self.assertEqual((summary['error'], summary['warning']), (2, 1))
        self.assertTrue(summary['monitoring']['online'])
        response = await self.client.get('/api/fault-logs/export?format=json')
        self.assertEqual(response.status, 200)
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        result = await response.json()
        self.assertEqual(len(result['items']), 3)
        self.assertIn('=HYPERLINK', json.dumps(result))
        response = await self.client.get('/api/fault-logs/export?format=csv&level=ERROR')
        raw = await response.read()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][8], "'=HYPERLINK(\"test\")")
        self.assertIn('+08:00', rows[1][0])

    async def test_page_summary_and_exports_share_focused_policy(self):
        events = [fault('恢复', level='INFO', code='DEVICE_DATA_RECOVERED'),
                  fault('普通警告', level='WARN', code='ROS_WARNING'),
                  fault('导航超时', level='WARN', code='NAVIGATION_TIMEOUT'),
                  fault('严重故障', code='OTHER_SERIOUS_FAILURE')]
        response = await self.ingest(batch(events))
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['event_count'], 4)
        await self.login()
        data = await (await self.client.get('/api/fault-logs')).json()
        summary = await (await self.client.get('/api/fault-logs/summary')).json()
        exported = await (await self.client.get('/api/fault-logs/export?format=json')).json()
        self.assertEqual(data['total'], 2)
        self.assertEqual(summary['total'], data['total'])
        self.assertEqual(len(exported['items']), data['total'])
        self.assertEqual({item['code'] for item in exported['items']}, {'NAVIGATION_TIMEOUT', 'OTHER_SERIOUS_FAILURE'})
        raw = await (await self.client.get('/api/fault-logs/export?format=csv')).read()
        self.assertEqual(len(list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))), 3)
        info = await (await self.client.get('/api/fault-logs?level=INFO')).json()
        self.assertEqual(info['total'], 0)

    async def test_legacy_grouping_matches_paginated_api_summary_and_full_downloads(self):
        store = self.app[STORE]
        base = time.time()
        events = [fault('Planner failed at 1.2', timestamp=base - 30, code='ROS_ERROR'),
                  fault('Planner failed at 8.9', timestamp=base - 20, code='ROS_ERROR'),
                  fault('导航超时', timestamp=base - 10, level='WARN', code='NAVIGATION_TIMEOUT')]
        with store.db:
            for index, event in enumerate(events):
                historical_row(store.db, event, received_at=base + index)
            historical_row(store.db, fault('无关信息', level='INFO', code='DEVICE_DATA_RECOVERED'), received_at=base)
        await self.login()
        page = await (await self.client.get('/api/fault-logs?limit=1&offset=1')).json()
        summary = await (await self.client.get('/api/fault-logs/summary')).json()
        exported = await (await self.client.get('/api/fault-logs/export?format=json')).json()
        raw = await (await self.client.get('/api/fault-logs/export?format=csv')).read()
        self.assertEqual(page['total'], 2)
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['items'][0]['count'], 2)
        self.assertEqual((summary['total'], summary['error'], summary['warning']), (2, 1, 1))
        self.assertEqual(len(exported['items']), 2)
        self.assertEqual(sum(item['count'] for item in exported['items']), 3)
        self.assertEqual(len(list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))), 3)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM fault_logs').fetchone()[0], 4)

    async def test_bad_filters_and_source_spoof_do_not_write(self):
        obj = batch([fault()])
        obj['source_id'] = 'jetson'
        self.assertEqual((await self.ingest(obj)).status, 403)
        obj['payload']['events'][0]['message'] = 'source authenticated'
        self.assertEqual((await self.ingest(obj, CAMERA_TOKEN)).status, 200)
        await self.login()
        for query in ('level=OTHER', 'limit=99999', 'offset=-1', 'start=nan', 'end=inf',
                      'start=20&end=10', 'limit=1&limit=2', 'q=' + 'x' * 257,
                      'unknown=1', 'code=bad-code'):
            response = await self.client.get('/api/fault-logs?' + query)
            self.assertEqual(response.status, 400, query)
        self.assertEqual((await self.client.get('/api/fault-logs/export?format=html')).status, 400)
        self.assertEqual((await self.client.get('/api/fault-logs/export?limit=1')).status, 400)
        for text in ('=x', '+x', '-x', '@x', '\ttext', '  =x', '\rtext'):
            self.assertTrue(csv_cell(text).startswith("'"))
