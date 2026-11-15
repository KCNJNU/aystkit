"""Password changes preserve approvals and invalidate every prior login."""
import asyncio
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from cloud.backend.app import ACCOUNTS, COOKIE, SESSIONS
from cloud.backend.security import hash_password, verify_password
from tests.test_backend import ORIGIN, PASSWORD
from tests.test_registration import RegistrationTests, USER_PASSWORD


class PasswordTests(unittest.IsolatedAsyncioTestCase):
    # Reuse isolated real-HTTP fixtures without inheriting unrelated test cases.
    asyncSetUp = RegistrationTests.asyncSetUp
    asyncTearDown = RegistrationTests.asyncTearDown
    start_app = RegistrationTests.start_app
    separate_client = RegistrationTests.separate_client
    login = RegistrationTests.login
    register = RegistrationTests.register
    admin_client = RegistrationTests.admin_client
    listed_user = RegistrationTests.listed_user
    approve = RegistrationTests.approve

    async def change(self, client=None, current=USER_PASSWORD, new='7', **kwargs):
        return await (client or self.client).post('/api/account/password',
            json={'currentPassword': current, 'newPassword': new},
            headers=kwargs.pop('headers', {'Origin': ORIGIN}), **kwargs)

    async def reset(self, admin, user_id, new='8', **kwargs):
        return await admin.post(f'/api/admin/users/{user_id}/password',
            json={'newPassword': new}, headers=kwargs.pop('headers', {'Origin': ORIGIN}), **kwargs)

    async def test_anonymous_pages_and_mutations_require_login(self):
        for path in ('/password', '/password.html'):
            result = await self.client.get(path, allow_redirects=False)
            self.assertEqual(result.status, 302)
            self.assertEqual(result.headers['Location'], '/login')
        self.assertEqual((await self.change()).status, 401)
        self.assertEqual((await self.reset(self.client, 1)).status, 401)

    async def test_pending_member_can_change_own_password_without_approval(self):
        await self.register()
        before = self.app[ACCOUNTS].list_users()
        self.assertEqual((await self.client.get('/password')).status, 200)
        result = await self.change()
        self.assertEqual(result.status, 200, await result.text())
        self.assertEqual(await result.json(), {'changed': True, 'reauthenticate': True})
        self.assertEqual(result.cookies[COOKIE]['max-age'], '0')
        self.assertTrue(result.cookies[COOKIE]['httponly'])
        self.assertEqual(result.cookies[COOKIE]['samesite'], 'Strict')
        self.assertEqual(self.app[ACCOUNTS].list_users(), before)
        self.assertEqual((await self.client.get('/api/session')).status, 401)
        invalid = await self.client.post('/api/login', json={'username': 'new.viewer', 'password': USER_PASSWORD}, headers={'Origin': ORIGIN})
        self.assertEqual(invalid.status, 401)
        session = await self.login(username='new.viewer', password='7')
        self.assertFalse(session['approved'])
        self.assertEqual((await self.client.get('/snapshot.json')).status, 403)
        for file in Path(self.temp.name).glob('accounts.sqlite3*'):
            self.assertNotIn(USER_PASSWORD.encode(), file.read_bytes())

    async def test_approved_member_change_revokes_other_sessions_and_socket(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        await self.approve(admin, user)
        before = self.app[ACCOUNTS].list_users()
        other = await self.separate_client()
        await self.login(other, 'new.viewer', USER_PASSWORD)
        socket = await other.ws_connect('/ws', headers={'Origin': ORIGIN})
        old_cookie = other.cookie_jar.filter_cookies(self.client.make_url('/'))[COOKIE].value
        result = await self.change(new='长密碼' * 300)
        self.assertEqual(result.status, 200, await result.text())
        self.assertEqual(self.app[ACCOUNTS].list_users(), before)
        self.assertIsNone(self.app[SESSIONS].read(old_cookie))
        self.assertEqual((await other.get('/api/session')).status, 401)
        await asyncio.wait_for(socket.receive(), 3)
        self.assertEqual((await admin.get('/api/session')).status, 200)
        session = await self.login(username='new.viewer', password='长密碼' * 300)
        self.assertTrue(session['approved'])
        await socket.close()

    async def test_wrong_current_password_preserves_credentials_and_session(self):
        await self.register()
        original = self.app[ACCOUNTS].find('new.viewer')['password_hash']
        self.assertEqual((await self.change(current='wrong')).status, 400)
        self.assertEqual(self.app[ACCOUNTS].find('new.viewer')['password_hash'], original)
        self.assertEqual((await self.client.get('/api/session')).status, 200)

    async def test_administrator_password_survives_restart_and_old_password_fails(self):
        await self.login()
        other = await self.admin_client()
        result = await self.change(current=PASSWORD, new='9')
        self.assertEqual(result.status, 200, await result.text())
        self.assertEqual((await other.get('/api/session')).status, 401)
        await self.client.close()
        await self.start_app()
        old = await self.client.post('/api/login', json={'username': 'observer', 'password': PASSWORD}, headers={'Origin': ORIGIN})
        self.assertEqual(old.status, 401)
        session = await self.login(password='9')
        self.assertEqual(session['role'], 'admin')
        self.assertEqual((await self.change(current='9', new='x')).status, 200)
        await self.login(password='x')
        self.assertEqual(self.app[ACCOUNTS].list_users(), [])

    async def test_reset_preserves_status_revokes_target_and_keeps_administrator(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        for approved in (False, True):
            if approved:
                await self.approve(admin, user)
            before = self.app[ACCOUNTS].list_users()
            result = await self.reset(admin, user['id'], 'a' if not approved else 'b')
            self.assertEqual(result.status, 200, await result.text())
            self.assertEqual(set(await result.json()), {'changed', 'user'})
            self.assertEqual(self.app[ACCOUNTS].list_users(), before)
            self.assertEqual((await self.client.get('/api/session')).status, 401)
            self.assertEqual((await admin.get('/api/session')).status, 200)
            session = await self.login(username='new.viewer', password='a' if not approved else 'b')
            self.assertEqual(session['approved'], approved)

    async def test_viewers_cannot_reset_other_users_even_when_approved(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        self.assertEqual((await self.reset(self.client, user['id'])).status, 403)
        await self.approve(admin, user)
        self.assertEqual((await self.reset(self.client, user['id'])).status, 403)

    async def test_origin_exact_body_utf8_and_nonempty_are_enforced(self):
        await self.register()
        for headers in ({}, {'Origin': 'https://foreign.example'}):
            self.assertEqual((await self.change(headers=headers)).status, 403)
        bodies = ({}, {'newPassword': 'x'}, {'currentPassword': USER_PASSWORD, 'newPassword': ''},
                  {'currentPassword': USER_PASSWORD, 'newPassword': 1},
                  {'currentPassword': '', 'newPassword': 'x'},
                  {'currentPassword': USER_PASSWORD, 'newPassword': '\ud800'},
                  {'currentPassword': USER_PASSWORD, 'newPassword': 'x', 'username': 'observer'})
        for body in bodies:
            result = await self.client.post('/api/account/password', json=body, headers={'Origin': ORIGIN})
            self.assertEqual(result.status, 400, repr(body))
        result = await self.change(new='x' * 9000)
        self.assertEqual(result.status, 400)
        self.assertEqual((await self.client.get('/api/session')).status, 200)

    async def test_reset_validates_origin_body_id_and_protected_admin_row(self):
        admin = await self.admin_client()
        await self.register()
        user = await self.listed_user(admin)
        for bad_id in ('0', '-1', 'abc', '9999999999999999999', '999'):
            self.assertEqual((await self.reset(admin, bad_id)).status, 404)
        for headers in ({}, {'Origin': 'https://foreign.example'}):
            self.assertEqual((await self.reset(admin, user['id'], headers=headers)).status, 403)
        self.assertEqual((await self.reset(admin, user['id'], '')).status, 400)
        forged = self.app[ACCOUNTS].register('observer', hash_password('unchanged'))
        self.assertEqual((await self.reset(admin, forged['id'])).status, 409)

    async def test_repeated_wrong_password_is_rate_limited(self):
        await self.register()
        statuses = [(await self.change(current='wrong')).status for _ in range(6)]
        self.assertEqual(statuses, [400] * 5 + [429])
        self.assertEqual((await self.client.get('/api/session')).status, 200)

    async def test_password_compare_and_swap_rejects_stale_hash(self):
        await self.register()
        accounts = self.app[ACCOUNTS]
        original = accounts.find('new.viewer')
        updated = hash_password('new')
        self.assertTrue(accounts.change_password(original['id'], original['password_hash'], updated))
        self.assertFalse(accounts.change_password(original['id'], original['password_hash'], hash_password('stale')))
        self.assertTrue(verify_password('new', accounts.find('new.viewer')['password_hash']))
        fallback = hash_password('fallback')
        self.assertTrue(accounts.change_administrator_password('observer', fallback, updated, fallback))
        self.assertFalse(accounts.change_administrator_password('observer', fallback, hash_password('stale'), fallback))
        self.assertEqual(accounts.administrator_password('observer', fallback), updated)

    async def test_login_started_before_reset_cannot_issue_a_fresh_session(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        client = await self.separate_client()
        entered, release = threading.Event(), threading.Event()
        original_verify = verify_password
        def delayed(password, encoded):
            if password == USER_PASSWORD:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test release missing')
            return original_verify(password, encoded)
        with patch('cloud.backend.app.verify_password', delayed):
            login = asyncio.create_task(client.post('/api/login', json={'username': 'new.viewer', 'password': USER_PASSWORD}, headers={'Origin': ORIGIN}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.assertEqual((await self.reset(admin, user['id'])).status, 200)
            finally:
                release.set()
            self.assertEqual((await login).status, 401)
        self.assertEqual((await client.get('/api/session')).status, 401)

    async def test_deleted_or_reset_account_during_self_hash_cannot_change_password(self):
        await self.register()
        admin = await self.admin_client()
        user = await self.listed_user(admin)
        entered, release = threading.Event(), threading.Event()
        original_hash = hash_password
        def delayed(password):
            if password == 'blocked-new':
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test release missing')
            return original_hash(password)
        with patch('cloud.backend.app.hash_password', delayed):
            change = asyncio.create_task(self.change(new='blocked-new'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.assertEqual((await self.reset(admin, user['id'], 'wins')).status, 200)
            finally:
                release.set()
            self.assertEqual((await change).status, 401)
        self.assertTrue(verify_password('wins', self.app[ACCOUNTS].find('new.viewer')['password_hash']))
