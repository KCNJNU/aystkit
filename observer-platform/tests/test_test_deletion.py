"""Administrator deletion of abandoned logical timers; isolated HTTP and data."""
import sqlite3
import unittest
import uuid

from cloud.backend.test_stats import TestStats as Stats
from tests.test_account_tests_http import AccountTestHTTPTests
from tests.test_backend import ORIGIN


class TestDeletionHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = AccountTestHTTPTests.asyncSetUp
    asyncTearDown = AccountTestHTTPTests.asyncTearDown
    client = AccountTestHTTPTests.client
    snapshot = AccountTestHTTPTests.snapshot
    control = AccountTestHTTPTests.control
    advance = AccountTestHTTPTests.advance
    capture = AccountTestHTTPTests.capture

    async def delete(self, run_id, client=None, expected=200, headers=None, **kwargs):
        result = await (client or self.observer).delete('/api/admin/tests/' + run_id,
            json=kwargs.pop('json', {}), headers={'Origin': ORIGIN} if headers is None else headers, **kwargs)
        self.assertEqual(result.status, expected, await result.text())
        return result

    async def test_deleted_account_running_test_can_be_removed_without_affecting_other_timer(self):
        alice = await self.control(self.alice, 'start', 0)
        self.advance(10)
        bob = await self.control(self.bob, 'start', 0)
        capture = self.capture()
        user_id = self.accounts.find('alice')['id']
        result = await self.observer.delete(f'/api/admin/users/{user_id}', json={}, headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 200)
        self.assertIsNone(self.accounts.find('alice'))
        self.advance(20)
        result = await self.delete(alice['active']['id'])
        self.assertEqual(await result.json(), {'deleted': True, 'id': alice['active']['id']})
        admin = await self.snapshot(self.observer)
        self.assertEqual({x['id'] for x in admin['runs']}, {bob['active']['id']})
        self.assertNotIn('alice', {u['username'] for u in admin['users']})
        self.assertEqual(admin['summary']['duration_s'], 20)
        self.assertEqual(self.capture(), capture)
        self.assertEqual((await self.snapshot(self.bob))['active']['id'], bob['active']['id'])
        self.assertEqual(self.store.tests.control_state(actor='alice')['state'], 'idle')
        self.advance(30)
        self.assertEqual((await self.snapshot(self.observer))['summary']['duration_s'], 50)

    async def test_delete_last_active_timer_stops_shared_metrics_and_is_idempotent(self):
        run = (await self.control(self.alice, 'start', 0))['active']
        self.advance(10)
        await self.delete(run['id'])
        capture = self.capture()
        self.assertEqual(capture['state'], 'idle')
        self.assertIsNone((await self.snapshot(self.alice))['active'])
        self.assertEqual((await self.snapshot(self.alice))['totals']['sessions'], 0)
        await self.delete(run['id'])
        self.assertEqual(self.capture(), capture)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM account_test_deletions').fetchone()[0], 1)

    async def test_paused_timer_deletion_does_not_resume_or_end_other_paused_test(self):
        alice = await self.control(self.alice, 'start', 0)
        await self.control(self.bob, 'start', 0)
        self.advance(8)
        await self.control(self.alice, 'pause', 1)
        await self.control(self.bob, 'pause', 1)
        capture = self.capture()
        self.assertEqual(capture['state'], 'paused')
        await self.delete(alice['active']['id'])
        self.assertEqual(self.capture(), capture)
        self.assertEqual((await self.snapshot(self.bob))['active']['state'], 'paused')

    async def test_deleted_finished_run_disappears_from_exports_and_cannot_be_restored_by_restart(self):
        run = (await self.control(self.alice, 'start', 0))['active']
        self.advance(10)
        await self.control(self.alice, 'finish', 1)
        before_capture = self.capture()
        await self.delete(run['id'])
        self.assertEqual(self.capture(), before_capture)
        after = await self.snapshot(self.observer)
        self.assertEqual(after['runs'], [])
        self.assertEqual(after['totals']['sessions'], 0)
        self.assertEqual(after['summary']['duration_s'], 0)
        export = await self.observer.get('/api/tests/export?format=json&days=30')
        self.assertEqual(export.status, 200)
        self.assertNotIn(run['id'], await export.text())
        stats = Stats(self.store.db, self.store.config.robot_id, clock=self.store.tests.clock)
        self.assertEqual(stats.snapshot(actor='observer', read_all=True)['runs'], [])

    async def test_old_revision_and_replayed_start_cannot_resurrect_deleted_run_or_delete_new_one(self):
        command_id = str(uuid.uuid4())
        old = await self.control(self.alice, 'start', 0, identifier=command_id)
        await self.delete(old['active']['id'])
        replay = await self.control(self.alice, 'start', 0, identifier=command_id)
        self.assertIsNone(replay['active'])
        await self.control(self.alice, 'finish', 1, expected=409)
        newer = await self.control(self.alice, 'start', replay['revision'])
        await self.delete(old['active']['id'])
        self.assertEqual((await self.snapshot(self.alice))['active']['id'], newer['active']['id'])

    async def test_only_admin_with_same_origin_and_exact_empty_body_can_delete(self):
        run = (await self.control(self.alice, 'start', 0))['active']
        anonymous = await self.client()
        pending = await self.client('pending')
        for client, code in ((anonymous, 401), (pending, 403), (self.alice, 403), (self.bob, 403)):
            await self.delete(run['id'], client=client, expected=code)
        for headers in ({}, {'Origin': 'https://foreign.example'}):
            await self.delete(run['id'], expected=403, headers=headers)
        for body in ({'actor': 'alice'}, {'deleteAll': True}, [], None):
            await self.delete(run['id'], json=body, expected=400)
        await self.delete(run['id'] + '?all=true', expected=400)
        for invalid in ('invalid-id', '0', 'ABC'):
            await self.delete(invalid, expected=400)
        await self.delete(str(uuid.uuid4()), expected=404)
        self.assertEqual((await self.snapshot(self.alice))['active']['id'], run['id'])

    async def test_deletion_failure_rolls_back_timer_control_capture_and_tombstone(self):
        run = (await self.control(self.alice, 'start', 0))['active']
        self.advance(10)
        before = self.store.tests.control_state(actor='alice')
        capture = self.capture()
        self.store.db.execute("CREATE TRIGGER reject_test_delete BEFORE INSERT ON account_test_deletions BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.Error):
            self.store.tests.delete_run(run['id'], administrator='observer')
        self.assertEqual(self.store.tests.control_state(actor='alice'), before)
        self.assertEqual(self.capture(), capture)
        self.assertEqual((await self.snapshot(self.alice))['active']['state'], 'running')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM account_test_deletions').fetchone()[0], 0)

    async def test_deletion_preserves_raw_evidence_and_other_records(self):
        run = (await self.control(self.alice, 'start', 0))['active']
        capture = self.capture()
        self.advance(2)
        with self.store.db:
            self.store.db.execute('INSERT INTO test_samples VALUES (?,?,?,?,?,?,?,?,?,?)',
                (self.store.config.robot_id, 'primary', str(uuid.uuid4()), capture['run_id'], capture['revision'],
                 self.now[0]-2, self.now[0], 1.0, 2.0, self.now[0]))
        before = self.store.db.execute('SELECT * FROM test_samples').fetchall()
        await self.delete(run['id'])
        self.assertEqual(self.store.db.execute('SELECT * FROM test_samples').fetchall(), before)
        self.assertEqual((await self.snapshot(self.observer))['runs'], [])
        self.assertIsNotNone(self.accounts.find('alice'))
