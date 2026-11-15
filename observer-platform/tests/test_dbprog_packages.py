"""Isolated authenticated package proxy tests; no real account or object access."""
import asyncio
import json
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from aiohttp import web

from cloud.backend.dbprog_full import APP_COOKIE
from cloud.backend.app import COOKIE as OBSERVER_COOKIE, SESSIONS
from tests import test_dbprog_full as fixtures
from tests.test_backend import ORIGIN
from tests import test_dbprog_cloud_transport as cloud_fixtures
from tests.test_dbprog_full import DB_CSRF, DB_TOKEN, FILE_ID


RECORDING = 'abcd1234' * 4
API_PATH = f'/api/v1/datasets/{FILE_ID}/recordings/{RECORDING}/package'
PUBLIC_API = '/dbprog' + API_PATH
PACKAGE_PATH = f'/dbprog/packages/{FILE_ID}/{RECORDING}'


class WarehousePackageProxyTests(unittest.IsolatedAsyncioTestCase):
    # Reuse fixture lifecycle/helpers without inheriting their test methods.
    asyncSetUp = cloud_fixtures.CloudWarehouseTransportTests.asyncSetUp
    asyncTearDown = cloud_fixtures.CloudWarehouseTransportTests.asyncTearDown
    object = fixtures.FullWarehouseProxyTests.object
    cloud_login = fixtures.FullWarehouseProxyTests.cloud_login
    login = fixtures.FullWarehouseProxyTests.login

    async def api(self, request):
        if '/recordings/' not in request.path:
            return await fixtures.FullWarehouseProxyTests.api(self, request)
        body = await request.read()
        self.api_calls.append({'method': request.method, 'path': request.path_qs,
            'body': body, 'headers': dict(request.headers)})
        if request.headers.get('Cookie') != 'cr_session=' + DB_TOKEN:
            return web.json_response({'message': 'Authentication required'}, status=401)
        if hasattr(self, 'prepare_entered'):
            self.prepare_entered.set()
            await self.prepare_release.wait()
        status = getattr(self, 'package_status', 200)
        payload = getattr(self, 'package_payload', None)
        if payload is None:
            payload = {'url': self.signed_url,
                'fileName': getattr(self, 'package_filename', '前相机_20261102_123000.tar.zst')}
        return web.json_response(payload, status=status)

    def package_calls(self):
        return [row for row in self.api_calls if '/recordings/' in row['path']]

    async def test_exact_successful_get_rewrites_url_but_keeps_filename(self):
        await self.login()
        response = await self.client.get(PUBLIC_API)
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload['url'], PACKAGE_PATH)
        self.assertEqual(payload['fileName'], '前相机_20261102_123000.tar.zst')
        self.assertNotIn(self.object_origin, json.dumps(payload))
        self.assertEqual(self.object_calls, [])

    async def test_rewrite_requires_exact_route_method_and_status(self):
        await self.login()
        for path in (PUBLIC_API + '/extra', PUBLIC_API.replace(RECORDING, 'abc'),
                PUBLIC_API.replace(RECORDING, RECORDING.upper()),
                PUBLIC_API.replace(FILE_ID, 'not-a-uuid')):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['url'], self.signed_url)
        response = await self.client.post(PUBLIC_API, json={},
            headers={'Origin': ORIGIN, 'X-CSRF-Token': DB_CSRF})
        self.assertEqual((await response.json())['url'], self.signed_url)
        self.package_status = 202
        response = await self.client.get(PUBLIC_API)
        self.assertEqual(response.status, 202)
        self.assertEqual((await response.json())['url'], self.signed_url)
        response = await self.client.head(PUBLIC_API)
        self.assertEqual(await response.read(), b'')
        self.assertEqual(self.object_calls, [])

    async def test_success_requires_complete_package_json(self):
        await self.login()
        for payload in ({'url': self.signed_url}, {'url': self.signed_url, 'fileName': None},
                {'url': None, 'fileName': 'archive.tar.zst'}):
            self.package_payload = payload
            with self.subTest(payload=payload):
                response = await self.client.get(PUBLIC_API)
                self.assertEqual(response.status, 502)
        self.assertEqual(self.object_calls, [])

    async def test_no_observer_never_requests_warehouse_or_storage(self):
        response = await self.client.get(PACKAGE_PATH)
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())['error'], 'observer_auth_required')
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.object_calls, [])

    async def test_pending_observer_never_requests_warehouse_or_storage(self):
        response = await self.client.post('/api/register',
            json={'username': 'pending.package', 'password': 'isolated-pending-password'},
            headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 201)
        response = await self.client.get(PACKAGE_PATH)
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())['error'], 'observer_access_required')
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.object_calls, [])

    async def test_missing_or_forged_warehouse_cookie_never_reaches_upstream(self):
        await self.cloud_login()
        for value in ('', DB_TOKEN + '.' + '0' * 64):
            with self.subTest(cookie=value):
                headers = {'Cookie': APP_COOKIE + '=' + value} if value else {}
                response = await self.client.get(PACKAGE_PATH, headers=headers)
                self.assertEqual(response.status, 401)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.object_calls, [])

    async def test_nonce_bound_cookie_is_invalid_after_new_observer_login(self):
        await self.login()
        await self.cloud_login()
        before = len(self.api_calls)
        response = await self.client.get(PACKAGE_PATH)
        self.assertEqual(response.status, 401)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_upstream_auth_permission_and_missing_errors_never_touch_objects(self):
        await self.login()
        for status in (401, 403, 404):
            self.package_status = status
            with self.subTest(status=status):
                response = await self.client.get(PACKAGE_PATH)
                self.assertEqual(response.status, status)
                self.assertNotIn(self.signed_url, await response.text())
        self.assertEqual(self.object_calls, [])

    async def test_range_head_content_and_storage_credentials_are_preserved_safely(self):
        await self.login()
        self.object_type = 'application/zstd'
        for method in ('GET', 'HEAD'):
            with self.subTest(method=method):
                response = await self.client.request(method, PACKAGE_PATH,
                    headers={'Range': 'bytes=0-3', 'Authorization': 'Bearer caller-secret'})
                self.assertEqual(response.status, 206)
                self.assertEqual(await response.read(), self.object_bytes[:4] if method == 'GET' else b'')
                self.assertEqual(response.headers['Content-Length'], '4')
                self.assertEqual(response.headers['Content-Range'], f'bytes 0-3/{len(self.object_bytes)}')
                self.assertIn("filename*=UTF-8''", response.headers['Content-Disposition'])
                self.assertIn('前相机', unquote(response.headers['Content-Disposition']))
                self.assertNotIn('Set-Cookie', response.headers)
                upstream = self.object_calls[-1]
                self.assertEqual(upstream['method'], 'GET')
                self.assertEqual(upstream['raw_path'], self.signature_path)
                self.assertEqual(upstream['headers']['Range'], 'bytes=0-3')
                self.assertNotIn('Cookie', upstream['headers'])
                self.assertNotIn('Authorization', upstream['headers'])
                self.assertEqual(self.package_calls()[-1]['method'], 'GET')
                self.assertEqual(self.package_calls()[-1]['headers']['Cookie'], 'cr_session=' + DB_TOKEN)
                self.assertNotIn('Authorization', self.package_calls()[-1]['headers'])

    async def test_get_signed_storage_rejects_head_but_both_proxy_heads_succeed(self):
        await self.login()
        async with self.gateway.objects.request('HEAD', self.signed_url,
                allow_redirects=False) as storage:
            self.assertEqual(storage.status, 403)
        for endpoint in ('/dbprog/objects/' + FILE_ID, PACKAGE_PATH):
            with self.subTest(endpoint=endpoint):
                response = await self.client.head(endpoint, headers={'Range': 'bytes=0-3'})
                self.assertEqual(response.status, 206)
                self.assertEqual(await response.read(), b'')
                self.assertEqual(response.headers['Content-Length'], '4')
                self.assertEqual(response.headers['Content-Range'], f'bytes 0-3/{len(self.object_bytes)}')
                self.assertEqual(self.object_calls[-1]['method'], 'GET')
                self.assertNotIn('Cookie', self.object_calls[-1]['headers'])
                self.assertNotIn('Authorization', self.object_calls[-1]['headers'])

    async def test_head_finishes_without_waiting_for_or_reading_storage_body(self):
        await self.login()
        for endpoint in ('/dbprog/objects/' + FILE_ID, PACKAGE_PATH):
            with self.subTest(endpoint=endpoint):
                self.object_body_release = asyncio.Event()
                self.object_body_finished = asyncio.Event()
                try:
                    response = await asyncio.wait_for(self.client.head(endpoint), timeout=2)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.read(), b'')
                    self.assertEqual(response.headers['Content-Length'], str(len(self.object_bytes)))
                    self.assertFalse(self.object_body_release.is_set())
                    self.assertEqual(self.object_calls[-1]['method'], 'GET')
                finally:
                    self.object_body_release.set()
                    await asyncio.wait_for(self.object_body_finished.wait(), timeout=2)

    async def test_filename_cannot_inject_headers_or_paths(self):
        await self.login()
        self.package_filename = '../private\\前相机\r\nX-Injected: yes.tar.zst'
        response = await self.client.get(PACKAGE_PATH)
        self.assertEqual(response.status, 200)
        await response.read()
        disposition = response.headers['Content-Disposition']
        filename = unquote(disposition.split("filename*=UTF-8''", 1)[1])
        for bad in ('/', '\\', '\r', '\n'):
            self.assertNotIn(bad, filename)
        self.assertNotIn('X-Injected', response.headers)

    async def test_unconfigured_object_origin_is_never_contacted(self):
        await self.login()
        self.signed_url = self.api_origin + '/sensitive?signature=fixture'
        response = await self.client.get(PACKAGE_PATH)
        self.assertEqual(response.status, 502)
        self.assertEqual(self.object_calls, [])
        self.assertFalse(any(row['path'].startswith('/sensitive') for row in self.api_calls))

    async def test_caller_query_and_foreign_origin_cannot_choose_upstream(self):
        await self.login()
        before = len(self.api_calls)
        for suffix in ('?url=' + self.api_origin + '/sensitive', '?download=1'):
            response = await self.client.get(PACKAGE_PATH + suffix)
            self.assertEqual(response.status, 422)
        response = await self.client.get(PACKAGE_PATH, headers={'Origin': 'https://untrusted.invalid'})
        self.assertEqual(response.status, 403)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_each_download_reauthorizes_with_package_build_timeout(self):
        await self.login()
        with patch.object(self.gateway.api, 'request', wraps=self.gateway.api.request) as requests:
            response = await self.client.get(PACKAGE_PATH)
            self.assertEqual(response.status, 200)
            await response.read()
            self.package_status = 403
            response = await self.client.head(PACKAGE_PATH)
            self.assertEqual(response.status, 403)
            await response.read()
        self.assertEqual(len(self.package_calls()), 2)
        self.assertEqual(len(self.object_calls), 1)
        self.assertEqual(len(requests.call_args_list), 2)
        for call in requests.call_args_list:
            self.assertEqual(call.args[0], 'GET')
            self.assertIn(API_PATH, str(call.args[1]))
            self.assertEqual(call.kwargs['timeout'].total, 180)

    def revoke_observer(self):
        cookie = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))[OBSERVER_COOKIE].value
        self.client.server.app[SESSIONS].revoke(cookie)

    async def test_observer_revoked_during_package_preparation_never_touches_objects(self):
        await self.login()
        self.prepare_entered, self.prepare_release = asyncio.Event(), asyncio.Event()
        download = asyncio.create_task(self.client.get(PACKAGE_PATH))
        try:
            await asyncio.wait_for(self.prepare_entered.wait(), 2)
            self.revoke_observer()
        finally:
            self.prepare_release.set()
        response = await download
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())['error'], 'observer_auth_required')
        self.assertEqual(self.object_calls, [])

    async def test_observer_revoked_while_waiting_for_a_download_slot_never_touches_objects(self):
        await self.login()
        entered, release = asyncio.Event(), asyncio.Event()

        class HeldSlot:
            async def __aenter__(self):
                entered.set()
                await release.wait()

            async def __aexit__(self, *_args):
                pass

        self.gateway.download_slots = HeldSlot()
        download = asyncio.create_task(self.client.get(PACKAGE_PATH))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.revoke_observer()
        finally:
            release.set()
        response = await download
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())['error'], 'observer_auth_required')
        self.assertEqual(self.object_calls, [])
