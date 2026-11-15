"""Per-account manual timers through real authenticated HTTP and isolated SQLite.

No public users, robots, device processes or production databases are touched.
"""
from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession, CookieJar
from aiohttp.test_utils import TestServer

from cloud.backend.app import ACCOUNTS, STORE, create_app
from tests.test_backend import ORIGIN, PASSWORD, PASSWORD_HASH, TOKEN, config


class AccountTestHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='account-test-http-')
        self.app = create_app(config(self.temp.name, viewer_username='observer'))
        self.now = [datetime(2026, 10, 29, 10, tzinfo=timezone(timedelta(hours=8))).timestamp()]
        self.store = self.app[STORE]
        self.store.clock = self.store.tests.clock = lambda: self.now[0]
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.clients = []
        self.accounts = self.app[ACCOUNTS]
        for username in ('alice', 'bob', 'pending'):
            self.accounts.register(username, PASSWORD_HASH)
            if username != 'pending':
                self.accounts.approve(self.accounts.find(username)['id'], 'observer')
        self.alice = await self.client('alice')
        self.bob = await self.client('bob')
        self.observer = await self.client('observer')

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await self.server.close()
        self.temp.cleanup()

    async def client(self, username=None):
        value = ClientSession(base_url=str(self.server.make_url('/')),
                              cookie_jar=CookieJar(unsafe=True))
        self.clients.append(value)
        if username is not None:
            response = await value.post('/api/login', json={'username': username, 'password': PASSWORD},
                                        headers={'Origin': ORIGIN})
            self.assertEqual(response.status, 200, await response.text())
            session = await response.json()
            self.assertEqual(session['username'], username)
            self.assertEqual(session['role'], 'admin' if username == 'observer' else 'viewer')
            self.assertEqual(session['approved'], username != 'pending')
        return value

    async def snapshot(self, client, query='?days=30'):
        response = await client.get('/api/tests' + query)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
        return await response.json()

    async def control(self, client, action, revision, *, expected=200, identifier=None,
                      name=None, tester=None, **extra):
        body = {'action': action, 'revision': revision,
                'command_id': identifier or str(uuid.uuid4())}
        if action == 'start':
            body.update(tester_name=tester or '测试人员', name=name or '隔离测试')
        body.update(extra)
        response = await client.post('/api/tests/control', json=body,
                                     headers={'Origin': ORIGIN})
        self.assertEqual(response.status, expected, await response.text())
        if response.content_type == 'application/json':
            return await response.json()
        return await response.text()

    def advance(self, seconds):
        self.now[0] += seconds

    def capture(self):
        return self.store.tests.collector_control_state()

    async def test_account_deletion_preserves_test_history_and_blocks_old_control_session(self):
        started = await self.control(self.alice, 'start', 0, name='保留的历史测试', tester='历史测试员')
        self.advance(10)
        await self.control(self.alice, 'finish', 1)
        before = await self.snapshot(self.observer)
        user = self.accounts.find('alice')
        response = await self.observer.delete(f"/api/admin/users/{user['id']}", json={},
                                              headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200, await response.text())
        self.assertIsNone(self.accounts.find('alice'))
        self.assertEqual((await self.alice.get('/api/tests')).status, 401)
        await self.control(self.alice, 'start', 2, expected=401)
        after = await self.snapshot(self.observer)
        self.assertEqual(after['runs'], before['runs'])
        self.assertEqual(after['totals'], before['totals'])
        self.assertEqual(after['runs'][0]['id'], started['active']['id'])
        self.assertEqual(after['runs'][0]['created_by'], 'alice')
        self.assertEqual(after['runs'][0]['tester_name'], '历史测试员')
        self.assertEqual((await self.snapshot(self.bob))['viewer']['username'], 'bob')

    async def test_overlapping_accounts_have_independent_controls_and_admin_sees_total(self):
        alice = await self.control(self.alice, 'start', 0, name='ALICE_RUN', tester='张三')
        alice_id = alice['active']['id']
        self.assertEqual(alice['active']['created_by'], 'alice')
        self.assertEqual(alice['active']['tester_name'], '张三')
        capture = self.capture()
        self.assertEqual(capture['state'], 'running')
        self.assertEqual(set(capture), {'revision', 'run_id', 'state', 'since'})

        self.advance(10)
        bob = await self.control(self.bob, 'start', 0, name='BOB_RUN', tester='李四')
        bob_id = bob['active']['id']
        self.assertNotEqual(bob_id, alice_id)
        self.assertEqual(bob['revision'], 1)
        self.assertEqual(self.capture(), capture, 'Joining must not reset the physical odometry baseline')
        admin = await self.snapshot(self.observer)
        self.assertIsNone(admin['active'], 'The administrator controls only its own timer')
        self.assertEqual(admin['revision'], 0)
        self.assertEqual({row['created_by'] for row in admin['runs']}, {'alice', 'bob'})
        await self.control(self.observer, 'pause', 0, expected=409)
        self.assertEqual((await self.snapshot(self.alice))['active']['state'], 'running')
        self.assertEqual((await self.snapshot(self.bob))['active']['state'], 'running')

        self.advance(10)
        alice = await self.control(self.alice, 'pause', 1)
        self.assertEqual(alice['active']['duration_s'], 20)
        self.assertEqual((await self.snapshot(self.bob))['active']['duration_s'], 10)
        self.assertEqual(self.capture(), capture)
        self.advance(10)
        admin = await self.control(self.observer, 'start', 0, name='ADMIN_RUN', tester='管理人员')
        self.assertEqual(admin['active']['created_by'], 'observer')
        self.assertEqual(admin['active']['duration_s'], 0)
        self.assertEqual(self.capture(), capture)
        self.advance(10)
        bob = await self.control(self.bob, 'finish', 1)
        self.assertIsNone(bob['active'])
        self.assertEqual(bob['runs'][0]['duration_s'], 30)
        self.assertEqual((await self.snapshot(self.alice))['active']['duration_s'], 20)
        self.assertEqual(self.capture(), capture)
        self.advance(10)
        await self.control(self.alice, 'resume', 2)
        self.assertEqual(self.capture(), capture)
        self.advance(10)
        admin = await self.control(self.observer, 'finish', 1)
        self.assertIsNone(admin['active'])
        self.assertEqual(admin['own_summary']['duration_s'], 30)
        self.assertEqual(admin['summary']['duration_s'], 90)
        self.assertEqual((await self.snapshot(self.alice))['active']['duration_s'], 30)
        self.assertEqual(self.capture(), capture)
        self.advance(10)
        await self.control(self.alice, 'finish', 3)
        self.assertEqual(self.capture()['state'], 'idle')

        alice, bob, admin = [await self.snapshot(c) for c in (self.alice, self.bob, self.observer)]
        for user, value, expected_duration in [('alice', alice, 40), ('bob', bob, 30)]:
            self.assertEqual(value['scope'], 'own')
            self.assertEqual(value['viewer'], {'username': user, 'can_view_all': False})
            self.assertEqual({row['created_by'] for row in value['runs']}, {user})
            self.assertEqual(value['summary']['duration_s'], expected_duration)
            self.assertEqual(value['totals']['duration_s'], expected_duration)
            self.assertFalse(value.get('users'))
            self.assertIsNone(value['summary']['distance_m'], 'Missing odometry is not a zero reading')
        self.assertEqual(admin['scope'], 'all')
        self.assertEqual(admin['viewer'], {'username': 'observer', 'can_view_all': True})
        self.assertEqual(admin['summary']['duration_s'], 100, 'Overlapping personal durations add to the account total')
        self.assertEqual(admin['totals']['duration_s'], 100)
        self.assertEqual(admin['totals']['sessions'], 3)
        self.assertEqual(len(admin['runs']), 3, 'Hidden acquisition runs never appear in human records')
        self.assertEqual({row['username']: row['total']['duration_s'] for row in admin['users']},
                         {'alice': 40, 'bob': 30, 'observer': 30})
        self.assertEqual(admin['revision'], 2)

    async def test_cross_account_command_uuid_replay_conflicts_without_changing_either_timer(self):
        identifier = str(uuid.uuid4())
        await self.control(self.alice, 'start', 0, identifier=identifier, name='同一请求', tester='甲')
        await self.control(self.bob, 'start', 0, identifier=identifier,
                           name='同一请求', tester='甲', expected=409)
        self.assertIsNone((await self.snapshot(self.bob))['active'])
        self.assertEqual((await self.snapshot(self.alice))['active']['created_by'], 'alice')
        await self.control(self.bob, 'start', 0, name='乙的测试', tester='乙')
        pause_id = str(uuid.uuid4())
        self.advance(5)
        await self.control(self.alice, 'pause', 1, identifier=pause_id)
        await self.control(self.bob, 'pause', 1, identifier=pause_id, expected=409)
        alice, bob = [await self.snapshot(c) for c in (self.alice, self.bob)]
        self.assertEqual(alice['active']['state'], 'paused')
        self.assertEqual(bob['active']['state'], 'running')
        self.advance(4)
        self.assertEqual((await self.snapshot(self.alice))['active']['duration_s'], 5)
        self.assertEqual((await self.snapshot(self.bob))['active']['duration_s'], 9)

    async def test_account_scope_cannot_be_spoofed_in_query_or_control_body(self):
        await self.control(self.alice, 'start', 0, tester='甲')
        bob = await self.control(self.bob, 'start', 0, tester='乙')
        for query in ('actor=bob', 'username=bob', 'user=bob', 'scope=all', 'read_all=1'):
            response = await self.alice.get('/api/tests?' + query)
            self.assertEqual(response.status, 400, query)
        response = await self.alice.get('/api/tests/export?format=json&actor=bob')
        self.assertEqual(response.status, 400)
        for extra in ({'actor': 'bob'}, {'username': 'observer'}, {'read_all': True},
                      {'run_id': bob['active']['id']}):
            await self.control(self.alice, 'pause', 1, expected=400, **extra)
        alice, bob = [await self.snapshot(c) for c in (self.alice, self.bob)]
        self.assertEqual(alice['active']['state'], 'running')
        self.assertEqual(bob['active']['state'], 'running')
        self.assertEqual(alice['revision'], bob['revision'])
        self.assertEqual(len(alice['runs']), 1)
        self.assertEqual(alice['runs'][0]['created_by'], 'alice')

    async def test_json_and_csv_exports_apply_signed_session_scope(self):
        await self.control(self.alice, 'start', 0, name='ALICE_PRIVATE_RUN', tester='甲的私有姓名')
        self.advance(1)
        await self.control(self.bob, 'start', 0, name='BOB_PRIVATE_RUN', tester='乙的私有姓名')
        self.advance(10)
        await self.control(self.alice, 'finish', 1)
        self.advance(10)
        await self.control(self.bob, 'finish', 1)
        response = await self.alice.get('/api/tests/export?days=1&format=json')
        self.assertEqual(response.status, 200)
        exported = await response.json()
        self.assertEqual({r['created_by'] for r in exported['runs']}, {'alice'})
        self.assertEqual(exported['totals']['duration_s'], 11)
        self.assertNotIn('乙的私有姓名', json.dumps(exported, ensure_ascii=False))
        self.assertNotIn('BOB_PRIVATE_RUN', json.dumps(exported))
        response = await self.observer.get('/api/tests/export?days=1&format=json')
        self.assertEqual(response.status, 200)
        exported = await response.json()
        self.assertEqual({r['created_by'] for r in exported['runs']}, {'alice', 'bob'})
        self.assertEqual(exported['totals']['duration_s'], 31)
        for client, expected_duration, allowed_testers in ((self.alice, 11, ('甲的私有姓名',)),
                                                         (self.observer, 31, ('甲的私有姓名', '乙的私有姓名'))):
            response = await client.get('/api/tests/export?days=1&format=csv')
            self.assertEqual(response.status, 200)
            content = (await response.read()).decode('utf-8-sig')
            rows = list(csv.DictReader(io.StringIO(content)))
            self.assertEqual(sum(float(row['测试时长（秒）']) for row in rows), expected_duration)
            for tester in allowed_testers:
                self.assertIn(tester, content)
            if client is self.alice:
                self.assertNotIn('乙的私有姓名', content)

    async def test_one_day_view_keeps_all_time_totals_and_admin_per_user_totals(self):
        await self.control(self.alice, 'start', 0, name='旧日测试', tester='历史测试员')
        self.advance(10)
        await self.control(self.alice, 'finish', 1)
        self.advance(3 * 86400)
        await self.control(self.alice, 'start', 2, name='今日甲', tester='甲')
        self.advance(5)
        await self.control(self.alice, 'finish', 3)
        await self.control(self.bob, 'start', 0, name='今日乙', tester='乙')
        self.advance(7)
        await self.control(self.bob, 'finish', 1)
        alice = await self.snapshot(self.alice, '?days=1')
        self.assertEqual(alice['summary']['duration_s'], 5)
        self.assertEqual(alice['totals']['duration_s'], 15)
        self.assertEqual([r['name'] for r in alice['runs']], ['今日甲'])
        admin = await self.snapshot(self.observer, '?days=1')
        self.assertEqual(admin['summary']['duration_s'], 12)
        self.assertEqual(admin['totals']['duration_s'], 22)
        users = {r['username']: r for r in admin['users']}
        self.assertEqual(users['alice']['summary']['duration_s'], 5)
        self.assertEqual(users['alice']['total']['duration_s'], 15)
        self.assertEqual(users['bob']['summary']['duration_s'], 7)
        self.assertEqual(users['bob']['total']['duration_s'], 7)
        self.assertNotIn('历史测试员', json.dumps(admin['runs'], ensure_ascii=False))

    async def test_pending_anonymous_and_device_credentials_cannot_read_or_control_tests(self):
        pending, anonymous = await self.client('pending'), await self.client()
        for client, expected in ((pending, 403), (anonymous, 401)):
            for path in ('/api/tests', '/api/tests/export?format=json', '/api/tests/export?format=csv'):
                response = await client.get(path, headers={'Authorization': 'Bearer ' + TOKEN})
                self.assertEqual(response.status, expected, path)
            await self.control(client, 'start', 0, expected=expected, tester='不能登记')
        alice, bob, admin = [await self.snapshot(c) for c in (self.alice, self.bob, self.observer)]
        self.assertIsNone(alice['active'])
        self.assertIsNone(bob['active'])
        self.assertEqual(admin['runs'], [])
        self.assertEqual(admin['totals']['sessions'], 0)
