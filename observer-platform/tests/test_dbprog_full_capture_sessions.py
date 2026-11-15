"""Exact capture snapshots use same-origin downloads and fresh account ACLs."""
import asyncio
import json
import unittest
from urllib.parse import unquote

from aiohttp import web

from cloud.backend.dbprog_full import APP_COOKIE
from tests import test_dbprog_cloud_transport as transport
from tests import test_dbprog_full as full
from tests import test_dbprog_packages as packages
from tests.test_backend import ORIGIN

ROBOT = '11111111-1111-4111-8111-111111111111'
SESSION = 'a' * 32
TASK = 'b' * 64
API = f'/api/v1/datasets/{full.FILE_ID}/capture-sessions/{SESSION}/package'
PUBLIC_API = '/dbprog' + API
PUBLIC_PACKAGE = f'/dbprog/capture-packages/{full.FILE_ID}/{SESSION}'
POLL = PUBLIC_API + '?robotId=' + ROBOT + '&async=true&taskId=' + TASK
DOWNLOAD = PUBLIC_PACKAGE + '?robotId=' + ROBOT + '&taskId=' + TASK


class CaptureSessionPackageProxyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = transport.CloudWarehouseTransportTests.asyncSetUp
    asyncTearDown = transport.CloudWarehouseTransportTests.asyncTearDown
    cloud_login = full.FullWarehouseProxyTests.cloud_login
    login = full.FullWarehouseProxyTests.login
    object = full.FullWarehouseProxyTests.object
    revoke_observer = packages.WarehousePackageProxyTests.revoke_observer

    async def api(self, request):
        if '/capture-sessions/' not in request.path:
            return await full.FullWarehouseProxyTests.api(self, request)
        self.api_calls.append({'method': request.method, 'path': request.path_qs,
            'headers': dict(request.headers), 'query': dict(request.query)})
        if request.headers.get('Cookie') != 'cr_session=' + full.DB_TOKEN:
            return web.json_response({'message': 'Authentication required'}, status=401)
        if hasattr(self, 'prepare_entered'):
            self.prepare_entered.set()
            await self.prepare_release.wait()
        responses = getattr(self, 'responses', None)
        if responses:
            status, payload = responses.pop(0)
        else:
            status = getattr(self, 'package_status', 200)
            payload = {'state': 'ready', 'taskId': TASK, 'url': self.signed_url,
                'fileName': getattr(self, 'package_filename', '采集_前相机与雷达.tar.zst')}
        return web.json_response(payload, status=status, headers={'Retry-After': '2'})

    def capture_calls(self):
        return [row for row in self.api_calls if '/capture-sessions/' in row['path']]

    async def test_processing_ready_and_download_preserve_exact_scope_and_drop_private_urls(self):
        await self.login()
        self.responses = [(202, {'state': 'processing', 'taskId': TASK, 'retryAfterMs': 2000,
            'privateUrl': self.signed_url}), (200, {'state': 'ready', 'taskId': TASK,
            'url': self.signed_url, 'fileName': '采集.tar.zst', 'privateUrl': self.signed_url})]
        response = await self.client.get(PUBLIC_API + '?robotId=' + ROBOT + '&async=true&retry=true')
        self.assertEqual(response.status, 202)
        self.assertEqual(await response.json(), {'state': 'processing', 'taskId': TASK, 'retryAfterMs': 2000})
        self.assertEqual(response.headers['Retry-After'], '2')
        response = await self.client.get(POLL)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'state': 'ready', 'taskId': TASK, 'url': DOWNLOAD, 'fileName': '采集.tar.zst'})
        self.assertEqual(self.object_calls, [])
        response = await self.client.get(DOWNLOAD)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.object_bytes)
        self.assertEqual([call['query'] for call in self.capture_calls()], [
            {'robotId': ROBOT, 'async': 'true', 'retry': 'true'},
            {'robotId': ROBOT, 'async': 'true', 'taskId': TASK},
            {'robotId': ROBOT, 'async': 'true', 'taskId': TASK}])
        self.assertTrue(all(call['headers']['Cookie'] == 'cr_session=' + full.DB_TOKEN for call in self.capture_calls()))

    async def test_every_head_range_and_conditional_download_reauthorizes_before_storage(self):
        await self.login()
        for method in ('GET', 'HEAD'):
            response = await self.client.request(method, DOWNLOAD,
                headers={'Range': 'bytes=0-3', 'If-Range': 'etag-fixture', 'Authorization': 'Bearer caller-secret'})
            self.assertEqual(response.status, 206)
            self.assertEqual(await response.read(), self.object_bytes[:4] if method == 'GET' else b'')
            self.assertEqual(response.headers['Content-Length'], '4')
            self.assertEqual(response.headers['Content-Range'], f'bytes 0-3/{len(self.object_bytes)}')
            self.assertIn('采集', unquote(response.headers['Content-Disposition']))
            self.assertNotIn('Set-Cookie', response.headers)
            call = self.object_calls[-1]
            self.assertEqual(call['method'], 'GET')
            self.assertEqual(call['raw_path'], self.signature_path)
            self.assertEqual(call['headers']['Range'], 'bytes=0-3')
            self.assertEqual(call['headers']['If-Range'], 'etag-fixture')
            self.assertNotIn('Cookie', call['headers'])
            self.assertNotIn('Authorization', call['headers'])
            self.assertEqual(self.capture_calls()[-1]['query'], {'robotId': ROBOT, 'async': 'true', 'taskId': TASK})
        self.package_status = 403
        response = await self.client.get(DOWNLOAD, headers={'If-None-Match': 'old-etag'})
        self.assertEqual(response.status, 403)
        self.assertEqual(len(self.object_calls), 2)
        self.assertEqual(len(self.capture_calls()), 3)

    async def test_head_does_not_wait_for_signed_get_body(self):
        await self.login()
        self.object_body_release, self.object_body_finished = asyncio.Event(), asyncio.Event()
        try:
            response = await asyncio.wait_for(self.client.head(DOWNLOAD), 2)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b'')
            self.assertFalse(self.object_body_release.is_set())
        finally:
            self.object_body_release.set()
            await asyncio.wait_for(self.object_body_finished.wait(), 2)

    async def test_missing_observer_and_warehouse_sessions_never_download(self):
        response = await self.client.get(DOWNLOAD)
        self.assertEqual(response.status, 401)
        self.assertEqual(self.api_calls, [])
        await self.cloud_login()
        for forged in ('', full.DB_TOKEN + '.' + '0' * 64):
            response = await self.client.get(DOWNLOAD, headers={'Cookie': APP_COOKIE + '=' + forged})
            self.assertEqual(response.status, 401)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.object_calls, [])

    async def test_actual_download_rejects_prepare_flags_duplicates_and_custom_addresses(self):
        await self.login()
        before = len(self.api_calls)
        for query in ('', '?robotId=' + ROBOT, '?taskId=' + TASK,
                '?robotId=not-a-uuid&taskId=' + TASK,
                '?robotId=' + ROBOT + '&taskId=short',
                '?robotId=' + ROBOT + '&taskId=' + 'A' * 64,
                '?robotId=' + ROBOT + '&robotId=' + ROBOT + '&taskId=' + TASK,
                '?robotId=' + ROBOT + '&taskId=' + TASK + '&taskId=' + TASK,
                '?robotId=' + ROBOT + '&taskId=' + TASK + '&async=true',
                '?robotId=' + ROBOT + '&taskId=' + TASK + '&retry=true',
                '?robotId=' + ROBOT + '&taskId=' + TASK + '&url=' + self.api_origin):
            for method in ('GET', 'HEAD'):
                with self.subTest(method=method, query=query):
                    response = await self.client.request(method, PUBLIC_PACKAGE + query)
                    self.assertEqual(response.status, 422)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_prepare_rejects_bad_scope_flags_duplicate_parameters_and_probe_side_effects(self):
        await self.login()
        before = len(self.api_calls)
        for query in ('', '?robotId=' + ROBOT, '?robotId=' + ROBOT + '&async=false',
                '?robotId=' + ROBOT + '&async=true&async=true',
                '?robotId=' + ROBOT + '&async=true&robotId=' + ROBOT,
                '?robotId=' + ROBOT + '&async=true&retry=1',
                '?robotId=' + ROBOT + '&async=true&taskId=' + TASK + '&retry=false',
                '?robotId=' + ROBOT + '&async=true&taskId=' + TASK + '&taskId=' + TASK,
                '?robotId=' + ROBOT + '&async=true&url=' + self.api_origin):
            response = await self.client.get(PUBLIC_API + query)
            self.assertEqual(response.status, 422, query)
        for path in (PUBLIC_API.replace(full.FILE_ID, 'not-a-uuid'), PUBLIC_API.replace(SESSION, 'BAD'),
                PUBLIC_PACKAGE.replace(full.FILE_ID, full.FILE_ID.replace('-', '')),
                PUBLIC_PACKAGE.replace(SESSION, 'A' * 32)):
            response = await self.client.get(path + '?robotId=' + ROBOT + '&async=true&taskId=' + TASK)
            self.assertEqual(response.status, 422)
        for method, headers in (('HEAD', {}), ('GET', {'Range': 'bytes=0-3'}), ('GET', {'If-Range': 'etag'})):
            response = await self.client.request(method, PUBLIC_API + '?robotId=' + ROBOT + '&async=true', headers=headers)
            self.assertEqual(response.status, 422)
        response = await self.client.post(POLL, json={}, headers={'Origin': ORIGIN, 'X-CSRF-Token': full.DB_CSRF})
        self.assertEqual(response.status, 405)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_bad_processing_or_ready_json_never_leaks_signed_urls(self):
        await self.login()
        cases = [(202, []), (202, {}), (202, {'state': 'processing', 'taskId': TASK, 'retryAfterMs': True}),
            (202, {'state': 'processing', 'taskId': TASK, 'retryAfterMs': 0}),
            (202, {'state': 'processing', 'taskId': TASK, 'retryAfterMs': 30001}),
            (202, {'state': 'processing', 'taskId': TASK, 'retryAfterMs': 2000, 'url': self.signed_url}),
            (202, {'state': 'processing', 'taskId': TASK, 'retryAfterMs': 2000, 'fileName': 'bad'}),
            (200, {'state': 'processing', 'taskId': TASK, 'url': self.signed_url, 'fileName': 'bad'}),
            (200, {'state': 'ready', 'taskId': TASK, 'url': self.signed_url}),
            (200, {'state': 'ready', 'taskId': TASK, 'url': self.api_origin + '/sensitive', 'fileName': 'bad'}),
            (200, {'state': 'ready', 'url': self.signed_url, 'fileName': 'bad'})]
        for status, payload in cases:
            with self.subTest(status=status, payload=payload):
                self.responses = [(status, payload)]
                response = await self.client.get(POLL)
                self.assertEqual(response.status, 502)
                self.assertNotIn(self.object_origin, await response.text())
        self.assertEqual(self.object_calls, [])

    async def test_cross_task_responses_are_rejected_for_polls_and_downloads(self):
        await self.login()
        for endpoint in (POLL, DOWNLOAD):
            for status, payload in ((202, {'state': 'processing', 'taskId': 'c' * 64, 'retryAfterMs': 2000}),
                    (200, {'state': 'ready', 'taskId': 'c' * 64, 'url': self.signed_url, 'fileName': 'bad.tar.zst'})):
                self.responses = [(status, payload)]
                response = await self.client.get(endpoint)
                self.assertEqual(response.status, 409 if endpoint == DOWNLOAD and status == 202 else 502)
        self.assertEqual(self.object_calls, [])

    async def test_upstream_rejections_prevent_storage_and_preserve_acl_status(self):
        await self.login()
        for status in (401, 403, 404, 409, 429, 503):
            for endpoint in (POLL, DOWNLOAD):
                self.responses = [(status, {'message': 'Denied'})]
                response = await self.client.get(endpoint)
                self.assertEqual(response.status, status)
                self.assertNotIn(self.object_origin, await response.text())
        self.assertEqual(self.object_calls, [])

    async def test_foreign_origin_and_unconfigured_object_origin_are_never_contacted(self):
        await self.login()
        before = len(self.api_calls)
        response = await self.client.get(DOWNLOAD, headers={'Origin': 'https://untrusted.invalid'})
        self.assertEqual(response.status, 403)
        self.assertEqual(len(self.api_calls), before)
        self.signed_url = self.api_origin + '/sensitive?signature=fixture'
        response = await self.client.get(DOWNLOAD)
        self.assertEqual(response.status, 502)
        self.assertFalse(any(row['path'].startswith('/sensitive') for row in self.api_calls))
        self.assertEqual(self.object_calls, [])

    async def test_observer_revoked_during_poll_never_receives_ready_url(self):
        await self.login()
        self.prepare_entered, self.prepare_release = asyncio.Event(), asyncio.Event()
        result = asyncio.create_task(self.client.get(POLL))
        try:
            await asyncio.wait_for(self.prepare_entered.wait(), 2)
            self.revoke_observer()
        finally:
            self.prepare_release.set()
        response = await result
        self.assertEqual(response.status, 401)
        self.assertEqual(self.object_calls, [])

    async def test_observer_revoked_during_download_authorization_never_touches_storage(self):
        await self.login()
        self.prepare_entered, self.prepare_release = asyncio.Event(), asyncio.Event()
        result = asyncio.create_task(self.client.get(DOWNLOAD))
        try:
            await asyncio.wait_for(self.prepare_entered.wait(), 2)
            self.revoke_observer()
        finally:
            self.prepare_release.set()
        response = await result
        self.assertEqual(response.status, 401)
        self.assertEqual(self.object_calls, [])

    async def test_filename_is_safe_and_ready_json_only_exposes_the_public_contract(self):
        await self.login()
        self.package_filename = '../private\\采集\r\nInjected: yes.tar.zst'
        response = await self.client.get(POLL)
        data = await response.json()
        self.assertEqual(set(data), {'state', 'taskId', 'url', 'fileName'})
        self.assertNotIn(self.object_origin, json.dumps(data))
        response = await self.client.get(data['url'])
        self.assertEqual(response.status, 200)
        filename = unquote(response.headers['Content-Disposition'].split("filename*=UTF-8''", 1)[1])
        for bad in ('/', '\\', '\r', '\n'):
            self.assertNotIn(bad, filename)
