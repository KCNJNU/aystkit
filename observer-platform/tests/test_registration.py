"""Account approval boundaries, using the real backend and persistent database."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientSession, CookieJar, WSServerHandshakeError, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from cloud.backend.app import ACCOUNTS, CONFIG, COOKIE, LIMITER, SESSIONS, SOCKETS, _approved_cookie, create_app
from cloud.backend.accounts import Accounts
from cloud.backend.security import verify_password
from tests.test_backend import ORIGIN, PASSWORD, config

USER_PASSWORD = 'isolated-registered-user-password'


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='observer-registration-test-')
        self.extra_clients = []
        await self.start_app()

    async def start_app(self):
        self.app = create_app(config(self.temp.name, viewer_username='observer'))
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        for client in self.extra_clients:
            await client.close()
        await self.client.close()
        self.temp.cleanup()

    async def separate_client(self):
        client = ClientSession(base_url=str(self.client.make_url('/')),
                               cookie_jar=CookieJar(unsafe=True))
        self.extra_clients.append(client)
        return client

    async def login(self, client=None, username='observer', password=PASSWORD):
        client = client or self.client
        result = await client.post('/api/login', json={'username': username, 'password': password},
                                   headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 200, await result.text())
        return await result.json()

    async def admin_client(self):
        client = await self.separate_client()
        await self.login(client)
        return client

    async def register(self, username='new.viewer', password=USER_PASSWORD, client=None):
        client = client or self.client
        result = await client.post('/api/register', json={'username': username, 'password': password},
                                   headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 201, await result.text())
        return result

    async def listed_user(self, admin, username='new.viewer'):
        result = await admin.get('/api/admin/users')
        self.assertEqual(result.status, 200, await result.text())
        payload = await result.json()
        self.assertEqual(set(payload), {'users'})
        return next(item for item in payload['users'] if item['username'] == username)

    async def approve(self, admin, user):
        result = await admin.post(f"/api/admin/users/{user['id']}/approve", json={},
                                  headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 200, await result.text())
        return await result.json()

    async def test_existing_observer_remains_administrator(self):
        payload = await self.login()
        self.assertTrue(payload['authenticated'])
        self.assertEqual(payload['username'], 'observer')
        self.assertEqual(payload['role'], 'admin')
        self.assertTrue(payload['approved'])
        self.assertEqual(payload['status'], 'approved')
        self.assertEqual((await self.client.get('/snapshot.json')).status, 200)
        self.assertEqual(await (await self.client.get('/api/admin/users')).json(), {'users': []})
        result = await self.client.get('/users')
        self.assertEqual(result.status, 200)

    async def test_registration_is_pending_and_does_not_expose_credentials(self):
        result = await self.register('  NEW.Viewer  ')
        self.assertTrue(result.cookies[COOKIE]['httponly'])
        self.assertEqual(result.cookies[COOKIE]['samesite'], 'Strict')
        payload = await result.json()
        self.assertEqual(payload, {'authenticated': True, 'username': 'new.viewer',
                                  'role': 'viewer', 'approved': False, 'status': 'pending'})
        self.assertEqual(await (await self.client.get('/api/session')).json(), payload)
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        self.assertEqual(set(user), {'id', 'username', 'status', 'created_at', 'approved_at'})
        self.assertIsInstance(user['id'], int)
        self.assertEqual(user['status'], 'pending')
        self.assertIsNone(user['approved_at'])
        self.assertTrue(user['created_at'])
        serialized = json.dumps(user)
        self.assertNotIn(USER_PASSWORD, serialized)
        self.assertNotIn('password', serialized)
        self.assertNotIn('scrypt$', serialized)
        self.assertTrue((Path(self.temp.name) / 'accounts.sqlite3').exists())
        for file in Path(self.temp.name).glob('accounts.sqlite3*'):
            self.assertNotIn(USER_PASSWORD.encode(), file.read_bytes())

    async def test_pending_session_cannot_read_any_monitoring_route(self):
        await self.register()
        protected = ('/api/robots', '/snapshot.json', '/visuals.json', '/camera/usbFront.jpg',
                     '/camera/color.jpg', '/visual/map.png', '/native-camera/status', '/native-camera/detections',
                     '/native-camera/usbFront/video.mp4')
        for path in protected:
            with self.subTest(path=path):
                result = await self.client.get(path)
                self.assertEqual(result.status, 403, await result.text())
                self.assertEqual(await result.json(), {'error': '当前未获取权限'})
        with self.assertRaises(WSServerHandshakeError) as error:
            await self.client.ws_connect('/ws', headers={'Origin': ORIGIN})
        self.assertEqual(error.exception.status, 403)
        for path in ('/', '/index.html'):
            result = await self.client.get(path, allow_redirects=False)
            self.assertEqual(result.status, 302)
            self.assertEqual(result.headers['Location'], '/pending')
        pending = await self.client.get('/pending')
        self.assertEqual(pending.status, 200)
        alias = await self.client.get('/pending.html')
        self.assertEqual(alias.status, 200)
        html = await pending.text()
        self.assertEqual(await alias.text(), html)
        self.assertIn('当前未获取权限', html)
        for forbidden in ('src="/app.js"', 'src="/media.js"', 'src="/native-player.js"', 'data-page-name='):
            self.assertNotIn(forbidden, html)

    async def test_admin_routes_require_administrator_not_just_a_cookie(self):
        for path in ('/api/admin/users',):
            self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual((await self.client.post('/api/admin/users/1/approve', json={},
                                               headers={'Origin': ORIGIN})).status, 401)
        await self.register()
        self.assertEqual((await self.client.get('/api/admin/users')).status, 403)
        self.assertEqual((await self.client.post('/api/admin/users/1/approve', json={},
                                               headers={'Origin': ORIGIN})).status, 403)
        for path in ('/users', '/users.html'):
            page = await self.client.get(path, allow_redirects=False)
            self.assertNotEqual(page.status, 200)
            self.assertNotIn('id="users-list"', await page.text())

    async def test_approval_activates_existing_cookie_without_admin_privileges(self):
        await self.register()
        original = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        approval = await self.approve(admin, user)
        self.assertEqual(approval['user']['status'], 'approved')
        self.assertTrue(approval['user']['approved_at'])
        # Idempotent approval preserves the original approval record.
        self.assertEqual(await self.approve(admin, user), approval)
        session = await (await self.client.get('/api/session')).json()
        self.assertTrue(session['approved'])
        self.assertEqual(session['status'], 'approved')
        self.assertEqual(session['role'], 'viewer')
        current = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
        self.assertEqual(current, original)
        for path in ('/', '/api/robots', '/snapshot.json', '/visuals.json'):
            self.assertEqual((await self.client.get(path)).status, 200, path)
        ws = await self.client.ws_connect('/ws', headers={'Origin': ORIGIN})
        message = await ws.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.TEXT)
        self.assertIn('cloud', message.json())
        await ws.close()

        self.assertEqual((await self.client.get('/api/admin/users')).status, 403)
        self.assertEqual((await self.client.post(f"/api/admin/users/{user['id']}/approve", json={},
                                               headers={'Origin': ORIGIN})).status, 403)

    async def test_registration_rejects_invalid_credentials_and_privilege_fields(self):
        invalid = [
            {'username': 'ab', 'password': USER_PASSWORD},
            {'username': 'a' * 33, 'password': USER_PASSWORD},
            {'username': '用户名', 'password': USER_PASSWORD},
            {'username': 'admin/other', 'password': USER_PASSWORD},
            {'username': 'person@example.com', 'password': USER_PASSWORD},
            {'username': 'valid', 'password': ''},
            {'username': 'valid', 'password': USER_PASSWORD, 'role': 'admin'},
            {'username': 'valid', 'password': USER_PASSWORD, 'approved': True},
            {'username': 'valid', 'password': USER_PASSWORD, 'status': 'approved'},
            {'username': 'valid', 'password': 123456789012},
            {'username': 'valid'},
            ['valid', USER_PASSWORD],
        ]
        for payload in invalid:
            with self.subTest(payload=repr(payload)[:70]):
                self.app[LIMITER].buckets.clear()  # Exercise validation independently of rate limiting.
                result = await self.client.post('/api/register', json=payload, headers={'Origin': ORIGIN})
                self.assertEqual(result.status, 400, await result.text())
                self.assertNotIn(COOKIE, result.cookies)
        self.assertEqual((await self.client.get('/api/session')).status, 401)
        admin = await self.admin_client()
        self.assertEqual(await (await admin.get('/api/admin/users')).json(), {'users': []})

    async def test_registration_and_login_accept_short_and_long_nonempty_passwords(self):
        for number, password in enumerate(('x', '长' * 1024)):
            with self.subTest(characters=len(password)):
                client = await self.separate_client()
                username = f'length.user{number}'
                await self.register(username, password, client)
                self.assertEqual((await self.login(client, username, password))['username'], username)

    async def test_reserved_names_and_case_duplicates_cannot_overwrite_accounts(self):
        for name in ('observer', 'OBSERVER', ' Observer '):
            self.app[LIMITER].buckets.clear()
            result = await self.client.post('/api/register', json={'username': name, 'password': USER_PASSWORD},
                                           headers={'Origin': ORIGIN})
            self.assertEqual(result.status, 409)
        self.app[LIMITER].buckets.clear()
        await self.register('Unique.User')
        result = await self.client.post('/api/register', json={'username': ' UNIQUE.USER ',
                                                               'password': 'different-password-for-duplicate'},
                                       headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 409)
        result = await self.client.post('/api/login', json={'username': 'unique.user',
                                                           'password': 'different-password-for-duplicate'},
                                       headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 401)
        session = await self.login(username='unique.user', password=USER_PASSWORD)
        self.assertEqual(session['status'], 'pending')
        admin = await self.admin_client()
        users = (await (await admin.get('/api/admin/users')).json())['users']
        self.assertEqual([user['username'] for user in users], ['unique.user'])

    async def test_registration_and_approval_reject_cross_origin_requests(self):
        for origin in (None, 'https://foreign.example'):
            result = await self.client.post('/api/register', json={'username': 'new.viewer', 'password': USER_PASSWORD},
                                           headers={} if origin is None else {'Origin': origin})
            self.assertEqual(result.status, 403)
        self.app[LIMITER].buckets.clear()
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        for origin in (None, 'https://foreign.example'):
            result = await admin.post(f"/api/admin/users/{user['id']}/approve", json={},
                                      headers={} if origin is None else {'Origin': origin})
            self.assertEqual(result.status, 403)
        self.assertEqual((await self.listed_user(admin))['status'], 'pending')
        result = await admin.post(f"/api/admin/users/{user['id']}/approve", json={'role': 'admin'},
                                  headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 400)
        self.assertFalse((await (await self.client.get('/api/session')).json())['approved'])

    async def test_pending_users_can_logout_and_log_back_in(self):
        await self.register()
        old_cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
        self.assertEqual((await self.client.post('/api/logout')).status, 403)
        result = await self.client.post('/api/logout', headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 200)
        self.assertFalse((await result.json())['authenticated'])
        self.assertEqual((await self.client.get('/api/session')).status, 401)
        replay = await self.separate_client()
        result = await replay.get('/api/session', headers={'Cookie': f'{COOKIE}={old_cookie}'})
        self.assertEqual(result.status, 401)
        session = await self.login(username='new.viewer', password=USER_PASSWORD)
        self.assertFalse(session['approved'])
        self.assertEqual((await self.client.get('/snapshot.json')).status, 403)

    async def test_accounts_and_approval_survive_backend_restart(self):
        await self.register('approved.user')
        other = await self.separate_client()
        await self.register('pending.user', client=other)
        admin = await self.admin_client()
        before = await self.approve(admin, await self.listed_user(admin, 'approved.user'))
        for client in self.extra_clients:
            await client.close()
        self.extra_clients.clear()
        await self.client.close()
        await self.start_app()
        approved = await self.login(username='approved.user', password=USER_PASSWORD)
        self.assertTrue(approved['approved'])
        self.assertEqual((await self.client.get('/snapshot.json')).status, 200)
        pending = await self.separate_client()
        await self.login(pending, 'pending.user', USER_PASSWORD)
        self.assertEqual((await pending.get('/snapshot.json')).status, 403)
        admin = await self.admin_client()
        self.assertEqual(await self.listed_user(admin, 'approved.user'), before['user'])
        self.assertEqual((await self.listed_user(admin, 'pending.user'))['status'], 'pending')

    async def test_registered_login_failure_is_generic(self):
        await self.register()
        errors = []
        for username, password in (('new.viewer', 'wrong-password'), ('does.not.exist', USER_PASSWORD),
                                   ('does.not.exist', PASSWORD), ('observer', USER_PASSWORD)):
            result = await self.client.post('/api/login', json={'username': username, 'password': password},
                                           headers={'Origin': ORIGIN})
            self.assertEqual(result.status, 401)
            errors.append(await result.json())
        self.assertTrue(all(item == errors[0] for item in errors))
        self.assertNotIn('password', json.dumps(errors))
        self.assertNotIn('scrypt$', json.dumps(errors))

    async def test_registration_rate_limits_repeated_attempts(self):
        statuses = []
        for _ in range(4):
            result = await self.client.post('/api/register', json={'username': 'valid', 'password': ''},
                                           headers={'Origin': ORIGIN})
            statuses.append(result.status)
        self.assertEqual(statuses, [400, 400, 400, 429])

    async def test_admin_can_delete_pending_and_approved_users_without_deleting_other_accounts(self):
        await self.register('pending.user')
        other = await self.separate_client()
        await self.register('approved.user', client=other)
        admin = await self.admin_client()
        approved = await self.listed_user(admin, 'approved.user')
        await self.approve(admin, approved)
        for username, client in (('pending.user', self.client), ('approved.user', other)):
            user = await self.listed_user(admin, username)
            response = await admin.delete(f"/api/admin/users/{user['id']}", json={}, headers={'Origin': ORIGIN})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual(await response.json(), {'deleted': True, 'user': user})
            self.assertIsNone(self.app[ACCOUNTS].find(username))
            self.assertEqual((await client.get('/api/session')).status, 401)
            self.assertEqual((await admin.get('/api/session')).status, 200)
            response = await admin.delete(f"/api/admin/users/{user['id']}", json={}, headers={'Origin': ORIGIN})
            self.assertEqual(response.status, 404)
        self.assertEqual(await (await admin.get('/api/admin/users')).json(), {'users': []})

    async def test_delete_requires_administrator_even_for_approved_viewer(self):
        response = await self.client.delete('/api/admin/users/1', json={}, headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 401)
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        path = f"/api/admin/users/{user['id']}"
        self.assertEqual((await self.client.delete(path, json={}, headers={'Origin': ORIGIN})).status, 403)
        await self.approve(admin, user)
        self.assertEqual((await self.client.delete(path, json={}, headers={'Origin': ORIGIN})).status, 403)
        self.assertIsNotNone(self.app[ACCOUNTS].find(user['username']))

    async def test_delete_requires_same_origin_empty_json_and_valid_id(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        path = f"/api/admin/users/{user['id']}"
        for origin in (None, 'https://foreign.example'):
            response = await admin.delete(path, json={}, headers={} if origin is None else {'Origin': origin})
            self.assertEqual(response.status, 403)
        for body in ({'username': user['username']}, {'role': 'admin'}, [], None):
            response = await admin.delete(path, json=body, headers={'Origin': ORIGIN})
            self.assertEqual(response.status, 400)
        for identifier in ('0', '-1', '1.0', '９', '9999999999999999999'):
            response = await admin.delete(f'/api/admin/users/{identifier}', json={}, headers={'Origin': ORIGIN})
            self.assertEqual(response.status, 404)
        self.assertEqual((await self.listed_user(admin))['id'], user['id'])

    async def test_delete_cannot_remove_configured_administrator_even_with_imported_registry_row(self):
        admin = await self.admin_client()
        row = self.app[ACCOUNTS].register('OBSERVER', 'unused-isolated-hash')
        response = await admin.delete(f"/api/admin/users/{row['id']}", json={}, headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 400)
        self.assertEqual((await self.client.get('/api/session')).status, 401)
        self.assertEqual((await (await admin.get('/api/session')).json())['role'], 'admin')
        self.assertIsNotNone(self.app[ACCOUNTS].find('OBSERVER'))

    async def test_delete_revokes_every_session_and_stream_and_recreation_cannot_restore_old_cookie(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        await self.approve(admin, user)
        second = await self.separate_client()
        await self.login(second, 'new.viewer', USER_PASSWORD)
        cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
        ws = await self.client.ws_connect('/ws', headers={'Origin': ORIGIN})
        self.assertEqual((await ws.receive(timeout=3)).type, WSMsgType.TEXT)
        response = await admin.delete(f"/api/admin/users/{user['id']}", json={}, headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200, await response.text())
        for client in (self.client, second):
            for path in ('/api/session', '/snapshot.json', '/native-camera/status',
                         '/native-camera/usbFront/video.mp4', '/api/tests'):
                self.assertEqual((await client.get(path)).status, 401, path)
        message = await ws.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(message.data, 1008)
        await ws.close()
        recreated = await self.separate_client()
        await self.register(client=recreated)
        replacement = await self.listed_user(admin)
        self.assertGreater(replacement['id'], user['id'])
        await self.approve(admin, replacement)
        self.assertEqual((await recreated.get('/snapshot.json')).status, 200)
        replay = await self.separate_client()
        response = await replay.get('/api/session', headers={'Cookie': f'{COOKIE}={cookie}'})
        self.assertEqual(response.status, 401)
        self.assertEqual((await self.client.get('/api/session')).status, 401)

    async def test_cookie_cannot_rebind_to_recreated_username_even_without_session_revoke(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        await self.approve(admin, user)
        cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
        self.app[ACCOUNTS].delete(user['id'], 'observer')
        recreated = self.app[ACCOUNTS].register(user['username'], 'isolated-replacement-hash')
        self.app[ACCOUNTS].approve(recreated['id'], 'observer')
        self.assertIsNotNone(self.app[SESSIONS].read(cookie), 'Exercise account identity, without relying on nonce removal')
        self.assertEqual((await self.client.get('/api/session')).status, 401)
        self.assertEqual((await self.client.get('/snapshot.json')).status, 401)

    async def test_delete_still_succeeds_when_one_existing_socket_close_fails(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        await self.approve(admin, user)
        cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
        nonce = self.app[SESSIONS].read(cookie)['n']
        broken, healthy = Mock(), Mock()
        broken.close = AsyncMock(side_effect=ConnectionError('isolated closed socket'))
        healthy.close = AsyncMock(return_value=True)
        self.app[SOCKETS][nonce] = {broken, healthy}
        try:
            response = await admin.delete(f"/api/admin/users/{user['id']}", json={}, headers={'Origin': ORIGIN})
        finally:
            self.app[SOCKETS].pop(nonce, None)
        self.assertEqual(response.status, 200, await response.text())
        for socket in (broken, healthy):
            socket.close.assert_awaited_once_with(code=1008, message=b'account deleted')
        self.assertEqual((await self.client.get('/api/session')).status, 401)
        self.assertIsNone(self.app[SESSIONS].read(cookie))

    async def test_delete_bounds_socket_cleanup_without_waiting_on_slow_peers(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        await self.approve(admin, user)
        cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
        nonce = self.app[SESSIONS].read(cookie)['n']
        closing = asyncio.Event()

        async def slow_close(**kwargs):
            closing.set()
            await asyncio.Event().wait()

        slow = Mock()
        slow.close = AsyncMock(side_effect=slow_close)
        self.app[SOCKETS][nonce] = {slow}
        began = time.monotonic()
        try:
            request = asyncio.create_task(admin.delete(f"/api/admin/users/{user['id']}",
                json={}, headers={'Origin': ORIGIN}))
            await asyncio.wait_for(closing.wait(), timeout=3)
            self.assertIsNone(self.app[SESSIONS].read(cookie), 'Revoke authorization before waiting on peers')
            self.assertEqual((await self.client.get('/api/session')).status, 401)
            response = await asyncio.wait_for(request, timeout=5)
        finally:
            self.app[SOCKETS].pop(nonce, None)
        self.assertEqual(response.status, 200, await response.text())
        self.assertLess(time.monotonic() - began, 6)
        slow.close.assert_awaited_once_with(code=1008, message=b'account deleted')
        self.assertEqual((await self.client.get('/api/session')).status, 401)

    async def test_deletion_while_login_hash_is_running_cannot_sign_in_to_recreated_account(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        original_hash = self.app[ACCOUNTS].find(user['username'])['password_hash']
        hashing = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def paused_verification(password, encoded):
            if password == USER_PASSWORD and encoded == original_hash:
                loop.call_soon_threadsafe(hashing.set)
                if not release.wait(timeout=10):
                    raise AssertionError('test did not release password verification')
            return verify_password(password, encoded)

        waiting = await self.separate_client()
        with patch('cloud.backend.app.verify_password', side_effect=paused_verification):
            task = asyncio.create_task(waiting.post('/api/login',
                json={'username': user['username'], 'password': USER_PASSWORD}, headers={'Origin': ORIGIN}))
            try:
                await asyncio.wait_for(hashing.wait(), timeout=3)
                response = await admin.delete(f"/api/admin/users/{user['id']}", json={}, headers={'Origin': ORIGIN})
                self.assertEqual(response.status, 200, await response.text())
                recreated = await self.separate_client()
                await self.register(password='replacement-password', client=recreated)
                await self.approve(admin, await self.listed_user(admin))
            finally:
                release.set()
            response = await asyncio.wait_for(task, timeout=5)
            self.assertEqual(response.status, 401, await response.text())
            self.assertNotIn(COOKIE, response.cookies)
        self.assertEqual((await waiting.get('/api/session')).status, 401)
        self.assertEqual((await self.login(recreated, user['username'], 'replacement-password'))['role'], 'viewer')

    async def test_deletion_while_reading_mutation_body_cannot_write_tests_or_issues(self):
        admin = await self.admin_client()
        for number, (path, body) in enumerate((
            ('/api/tests/control', {'action': 'start', 'revision': 0,
                'command_id': str(uuid.uuid4()), 'name': 'must not start', 'tester_name': '撤权测试员'}),
            ('/api/tests/issues', {'description': 'must not be written'}),
        )):
            with self.subTest(path=path):
                username = f'delete.race{number}'
                await self.register(username)
                user = await self.listed_user(admin, username)
                await self.approve(admin, user)
                before = await (await admin.get('/api/tests')).json()
                cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
                reading, release = asyncio.Event(), asyncio.Event()
                original_read = web.Request.read

                async def paused_read(request):
                    if request.path == path and request.cookies.get(COOKIE) == cookie:
                        reading.set()
                        await release.wait()
                    return await original_read(request)

                with patch.object(web.Request, 'read', paused_read):
                    task = asyncio.create_task(self.client.post(path, json=body, headers={'Origin': ORIGIN}))
                    try:
                        await asyncio.wait_for(reading.wait(), timeout=3)
                        response = await admin.delete(f"/api/admin/users/{user['id']}", json={}, headers={'Origin': ORIGIN})
                        self.assertEqual(response.status, 200, await response.text())
                    finally:
                        release.set()
                    response = await asyncio.wait_for(task, timeout=3)
                    self.assertEqual(response.status, 401, await response.text())
                after = await (await admin.get('/api/tests')).json()
                self.assertEqual(after['runs'], before['runs'])
                self.assertEqual(after['test_issues'], before['test_issues'])

    async def test_removed_account_loses_existing_stream_and_native_authorization(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        await self.approve(admin, user)
        ws = await self.client.ws_connect('/ws', headers={'Origin': ORIGIN})
        self.assertEqual((await ws.receive(timeout=3)).type, WSMsgType.TEXT)
        # Simulate an operator restoring the account database without this user.
        with sqlite3.connect(Path(self.temp.name) / 'accounts.sqlite3') as database:
            database.execute('DELETE FROM users WHERE id = ?', (user['id'],))
        message = await ws.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(message.data, 1008)
        await ws.close()
        for path in ('/api/session', '/snapshot.json', '/native-camera/status', '/native-camera/usbFront/video.mp4'):
            self.assertEqual((await self.client.get(path)).status, 401, path)

    async def test_https_registration_cookie_is_secure(self):
        cfg = self.app[CONFIG]
        cfg.allow_insecure_local = False
        cfg.public_origin = 'https://observer.example.com'
        result = await self.client.post('/api/register', json={'username': 'new.viewer', 'password': USER_PASSWORD},
                                       headers={'Origin': cfg.public_origin, 'X-Forwarded-Proto': 'https'})
        self.assertEqual(result.status, 201, await result.text())
        self.assertTrue(result.cookies[COOKIE]['secure'])
        self.assertTrue(result.cookies[COOKIE]['httponly'])
        self.assertEqual(result.cookies[COOKIE]['samesite'], 'Strict')
        self.assertEqual(result.headers['Cache-Control'], 'no-store')

    async def test_capacity_rejects_new_user_without_elevating_existing_session(self):
        with patch.object(Accounts, 'MAXIMUM', 1):
            await self.register('first.user')
            result = await self.client.post('/api/register',
                                           json={'username': 'second.user', 'password': USER_PASSWORD},
                                           headers={'Origin': ORIGIN})
            self.assertEqual(result.status, 503)
            self.assertNotIn(COOKIE, result.cookies)
            session = await (await self.client.get('/api/session')).json()
            self.assertEqual(session['username'], 'first.user')
            self.assertFalse(session['approved'])
            self.assertIsNone(self.app[ACCOUNTS].find('second.user'))
        self.assertEqual(Accounts.MAXIMUM, 1000)

    async def test_simultaneous_duplicate_registration_creates_only_one_account(self):
        second = await self.separate_client()
        responses = await asyncio.gather(*[
            client.post('/api/register', json={'username': 'concurrent.user', 'password': USER_PASSWORD},
                        headers={'Origin': ORIGIN}) for client in (self.client, second)])
        self.assertEqual(sorted(result.status for result in responses), [201, 409])
        for result in responses:
            await result.read()
        admin = await self.admin_client()
        users = (await (await admin.get('/api/admin/users')).json())['users']
        self.assertEqual([item['username'] for item in users], ['concurrent.user'])
        self.assertEqual(users[0]['status'], 'pending')

    async def test_account_storage_failure_closes_active_stream_instead_of_bypassing_guard(self):
        await self.register()
        admin = await self.admin_client()
        await self.approve(admin, await self.listed_user(admin))
        cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
        ws = await self.client.ws_connect('/ws', headers={'Origin': ORIGIN})
        self.assertEqual((await ws.receive(timeout=3)).type, WSMsgType.TEXT)
        with patch.object(self.app[ACCOUNTS], 'find', side_effect=sqlite3.OperationalError('isolated failure')):
            self.assertIsNone(_approved_cookie(self.app, cookie))
            result = await self.client.get('/snapshot.json')
            self.assertEqual(result.status, 503)
            message = await ws.receive(timeout=3)
            self.assertEqual(message.type, WSMsgType.CLOSE)
            self.assertEqual(message.data, 1008)
        await ws.close()


class AccountDeletionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='observer-account-delete-test-')
        self.accounts = Accounts(self.temp.name)

    def tearDown(self):
        self.accounts.close()
        self.temp.cleanup()

    def test_deletion_persists_and_recreation_never_reuses_id(self):
        first = self.accounts.register('first.user', 'isolated-hash')
        other = self.accounts.register('other.user', 'other-isolated-hash')
        self.accounts.approve(first['id'], 'observer')
        original = next(row for row in self.accounts.list_users() if row['id'] == first['id'])
        deleted = self.accounts.delete(first['id'], 'observer')
        self.assertEqual(deleted, original)
        self.assertNotIn('password_hash', deleted)
        self.assertIsNone(self.accounts.find('first.user'))
        self.assertIsNone(self.accounts.delete(first['id'], 'observer'))
        self.accounts.close()
        self.accounts = Accounts(self.temp.name)
        self.assertIsNone(self.accounts.find('first.user'))
        self.assertEqual(self.accounts.list_users(), [other])
        recreated = self.accounts.register('first.user', 'replacement-isolated-hash')
        self.assertGreater(recreated['id'], max(first['id'], other['id']))

    def test_configured_administrator_row_is_protected_case_insensitively(self):
        administrator = self.accounts.register('Custom.Admin', 'isolated-hash')
        with self.assertRaisesRegex(ValueError, '不能删除管理员账号'):
            self.accounts.delete(administrator['id'], 'CUSTOM.ADMIN')
        self.assertEqual(self.accounts.find('Custom.Admin')['id'], administrator['id'])
        ordinary = self.accounts.register('observer', 'isolated-hash')
        self.assertEqual(self.accounts.delete(ordinary['id'], 'CUSTOM.ADMIN'), ordinary)

    def test_deleted_registration_frees_capacity_without_touching_another_user(self):
        with patch.object(Accounts, 'MAXIMUM', 1):
            first = self.accounts.register('first.user', 'isolated-hash')
            self.accounts.delete(first['id'], 'observer')
            replacement = self.accounts.register('other.user', 'isolated-hash')
        self.assertEqual(self.accounts.list_users(), [replacement])
