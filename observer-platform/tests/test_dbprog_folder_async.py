"""Short folder preparation polls preserve exact scope and both authorizations."""
import asyncio
import unittest

from aiohttp import web

from tests import test_dbprog_cloud_transport as transport
from tests import test_dbprog_folder_packages as folders
from tests import test_dbprog_full as full
from tests import test_dbprog_packages as packages
from cloud.backend.dbprog_full import APP_COOKIE

TASK_ID = 'a' * 64


class AsyncFolderPackageProxyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = transport.CloudWarehouseTransportTests.asyncSetUp
    asyncTearDown = transport.CloudWarehouseTransportTests.asyncTearDown
    cloud_login = full.FullWarehouseProxyTests.cloud_login
    login = full.FullWarehouseProxyTests.login
    object = folders.FolderPackageProxyTests.object
    revoke_observer = packages.WarehousePackageProxyTests.revoke_observer

    async def api(self, request):
        if '/folder-package' not in request.path:
            return await full.FullWarehouseProxyTests.api(self, request)
        self.api_calls.append({'method': request.method, 'path': request.path_qs,
            'raw_path': request.raw_path, 'headers': dict(request.headers),
            'query': dict(request.query)})
        if request.headers.get('Cookie') != 'cr_session=' + full.DB_TOKEN:
            return web.json_response({'error': 'unauthorized'}, status=401)
        if hasattr(self, 'prepare_entered'):
            self.prepare_entered.set()
            await self.prepare_release.wait()
        responses = getattr(self, 'responses', None)
        if responses:
            status, data = responses.pop(0)
        else:
            status, data = 200, {'state': 'ready', 'taskId': TASK_ID, 'url': self.signed_url,
                'fileName': '原相机前_2026-11-02.tar.zst'}
        return web.json_response(data, status=status, headers={'Retry-After': '2'})

    def poll(self, folder=folders.FOLDER, suffix='&async=true&taskId=' + TASK_ID):
        return folders.PUBLIC_API + folders.query(folder) + suffix

    async def test_pending_then_ready_then_download_reauthorizes_and_strips_prepare_flags(self):
        await self.login()
        self.responses = [(202, {'state': 'processing', 'retryAfterMs': 2000, 'taskId': TASK_ID}),
            (200, {'state': 'ready', 'taskId': TASK_ID, 'url': self.signed_url,
                'fileName': '原相机前_2026-11-02.tar.zst'})]
        response = await self.client.get(self.poll(suffix='&async=true&retry=true'))
        self.assertEqual(response.status, 202)
        self.assertEqual(await response.json(), {'state': 'processing', 'retryAfterMs': 2000, 'taskId': TASK_ID})
        self.assertEqual(response.headers['Retry-After'], '2')
        self.assertEqual(self.object_calls, [])
        response = await self.client.get(self.poll())
        self.assertEqual(response.status, 200)
        ready = await response.json()
        self.assertEqual(ready['state'], 'ready')
        self.assertEqual(ready['url'], folders.PUBLIC_PACKAGE + folders.query() + '&taskId=' + TASK_ID)
        self.assertEqual(self.object_calls, [])
        response = await self.client.head(ready['url'], headers={'Range': 'bytes=0-3'})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b'')
        calls = [call for call in self.api_calls if '/folder-package' in call['path']]
        self.assertEqual([call['query'] for call in calls], [
            {'path': folders.FOLDER, 'async': 'true', 'retry': 'true'},
            {'path': folders.FOLDER, 'async': 'true', 'taskId': TASK_ID},
            {'path': folders.FOLDER, 'async': 'true', 'taskId': TASK_ID}])
        self.assertTrue(all(call['headers']['Cookie'] == 'cr_session=' + full.DB_TOKEN for call in calls))
        self.assertEqual(self.object_calls[-1]['method'], 'GET')
        self.assertNotIn('Cookie', self.object_calls[-1]['headers'])

    async def test_literal_path_and_retry_query_are_forwarded_exactly_once(self):
        await self.login()
        folder = '相机 + 50%/literal%2F&子目录'
        response = await self.client.get(self.poll(folder, '&retry=true&async=true'))
        self.assertEqual(response.status, 200)
        value = await response.json()
        self.assertEqual(value['url'], folders.PUBLIC_PACKAGE + folders.query(folder) + '&taskId=' + TASK_ID)
        self.assertEqual(self.api_calls[-1]['query'], {'path': folder, 'retry': 'true', 'async': 'true'})
        self.assertIn('%252F', self.api_calls[-1]['raw_path'])
        self.assertIn('&retry=true&async=true', self.api_calls[-1]['raw_path'])

    async def test_only_literal_single_boolean_prepare_flags_are_accepted(self):
        await self.login()
        for suffix in ('&async=true', '&async=false', '&async=true&retry=true', '&async=true&retry=false'):
            with self.subTest(suffix=suffix):
                response = await self.client.get(self.poll(suffix=suffix))
                self.assertEqual(response.status, 200)
                await response.read()
        before = len(self.api_calls)
        for suffix in ('&async=1', '&async=True', '&async=', '&async=true&async=false',
                       '&retry=true', '&retry=false', '&async=false&retry=true', '&async=false&retry=false',
                       '&async=true&retry=', '&async=true&retry=1', '&async=true&retry=true&retry=false',
                       '&async=true&url=http://untrusted.invalid', '&async=true&download=1', '&async=true&path=other'):
            with self.subTest(suffix=suffix):
                response = await self.client.get(self.poll(suffix=suffix))
                self.assertEqual(response.status, 422)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_download_routes_reject_all_prepare_flags_for_get_and_head(self):
        await self.login()
        before = len(self.api_calls)
        for method in ('GET', 'HEAD'):
            for suffix in ('&async=true', '&async=false', '&retry=true', '&retry=false', '&async=true&retry=true'):
                with self.subTest(method=method, suffix=suffix):
                    response = await self.client.request(method, folders.PUBLIC_PACKAGE + folders.query() + suffix)
                    self.assertEqual(response.status, 422)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_observer_authentication_is_required_before_any_poll(self):
        response = await self.client.get(self.poll())
        self.assertEqual(response.status, 401)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.object_calls, [])

    async def test_missing_or_forged_warehouse_session_still_fails_upstream_auth(self):
        await self.cloud_login()
        for cookie in ('', full.DB_TOKEN + '.' + '0'*64):
            headers = {'Cookie': APP_COOKIE + '=' + cookie} if cookie else {}
            response = await self.client.get(self.poll(), headers=headers)
            self.assertEqual(response.status, 401)
            self.assertNotIn('Cookie', self.api_calls[-1]['headers'])
        self.assertEqual(self.object_calls, [])

    async def test_processing_requires_valid_bounded_delay_and_never_exposes_a_download_url(self):
        await self.login()
        malformed = [[], {}, {'state': 'ready', 'retryAfterMs': 2000},
            {'state': 'processing'}, {'state': 'processing', 'retryAfterMs': True},
            {'state': 'processing', 'retryAfterMs': '2000'}, {'state': 'processing', 'retryAfterMs': 2000.5},
            {'state': 'processing', 'retryAfterMs': 0}, {'state': 'processing', 'retryAfterMs': 30001},
            {'state': 'processing', 'retryAfterMs': 2000, 'url': self.signed_url},
            {'state': 'processing', 'retryAfterMs': 2000, 'url': None},
            {'state': 'processing', 'retryAfterMs': 2000, 'fileName': 'not-ready.tar.zst'}]
        for payload in malformed:
            if isinstance(payload, dict):
                payload = {'taskId': TASK_ID, **payload}
            with self.subTest(payload=payload):
                self.responses = [(202, payload)]
                response = await self.client.get(self.poll())
                self.assertEqual(response.status, 502)
                self.assertNotIn(self.object_origin, await response.text())
        self.assertEqual(self.object_calls, [])

    async def test_processing_is_not_accepted_for_legacy_sync_requests(self):
        await self.login()
        for suffix in ('', '&async=false'):
            self.responses = [(202, {'state': 'processing', 'retryAfterMs': 2000})]
            response = await self.client.get(folders.PUBLIC_API + folders.query() + suffix)
            self.assertEqual(response.status, 502)

    async def test_processing_only_returns_the_public_poll_contract(self):
        await self.login()
        self.responses = [(202, {'state': 'processing', 'retryAfterMs': 2000, 'taskId': TASK_ID,
            'internalStoragePath': '/private/backend/path', 'downloadUrl': self.signed_url})]
        response = await self.client.get(self.poll())
        self.assertEqual(response.status, 202)
        self.assertEqual(await response.json(), {'state': 'processing', 'retryAfterMs': 2000, 'taskId': TASK_ID})

    async def test_async_ready_rejects_invalid_state_and_storage_origin(self):
        await self.login()
        for payload in ({'state': 'processing', 'url': self.signed_url, 'fileName': 'bad.tar.zst'},
                        {'state': 'ready', 'url': self.api_origin + '/sensitive', 'fileName': 'bad.tar.zst'},
                        {'state': 'ready', 'url': self.signed_url},
                        {'state': 'ready', 'url': self.signed_url, 'fileName': None}):
            self.responses = [(200, {'taskId': TASK_ID, **payload})]
            response = await self.client.get(self.poll())
            self.assertEqual(response.status, 502)
        self.assertEqual(self.object_calls, [])

    async def test_task_id_scope_is_strict_and_cannot_be_combined_with_retry(self):
        await self.login()
        before = len(self.api_calls)
        for suffix in ('&taskId=' + TASK_ID, '&async=false&taskId=' + TASK_ID,
                       '&async=true&taskId=' + TASK_ID + '&retry=true',
                       '&async=true&taskId=' + TASK_ID + '&retry=false',
                       '&async=true&taskId=' + TASK_ID + '&taskId=' + TASK_ID,
                       '&async=true&taskId=', '&async=true&taskId=' + 'A'*64,
                       '&async=true&taskId=' + 'a'*63, '&async=true&taskId=' + 'g'*64):
            response = await self.client.get(self.poll(suffix=suffix))
            self.assertEqual(response.status, 422, suffix)
        for task in ('', 'A'*64, 'a'*63, 'g'*64, 'a'*65):
            for method in ('GET', 'HEAD'):
                response = await self.client.request(method, folders.PUBLIC_PACKAGE + folders.query() + '&taskId=' + task)
                self.assertEqual(response.status, 422)
        response = await self.client.get(folders.PUBLIC_PACKAGE + folders.query() + '&taskId=' + TASK_ID + '&taskId=' + TASK_ID)
        self.assertEqual(response.status, 422)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_pending_and_ready_must_remain_bound_to_the_requested_task(self):
        await self.login()
        for status, base in ((202, {'state': 'processing', 'retryAfterMs': 2000}),
                             (200, {'state': 'ready', 'url': self.signed_url, 'fileName': 'camera.tar.zst'})):
            for task in (None, 'b'*64, 'A'*64, 'short', 123):
                self.responses = [(status, {**base, 'taskId': task})]
                response = await self.client.get(self.poll())
                self.assertEqual(response.status, 502)
        self.assertEqual(self.object_calls, [])

    async def test_download_get_and_head_use_the_prepared_task_and_reject_cross_task_results(self):
        await self.login()
        for method in ('GET', 'HEAD'):
            response = await self.client.request(method, folders.PUBLIC_PACKAGE + folders.query() + '&taskId=' + TASK_ID,
                headers={'Range': 'bytes=0-3'})
            self.assertEqual(response.status, 206)
            self.assertEqual(await response.read(), self.object_bytes[:4] if method == 'GET' else b'')
            self.assertEqual(self.api_calls[-1]['query'], {'path': folders.FOLDER, 'async': 'true', 'taskId': TASK_ID})
        before = len(self.object_calls)
        for status, payload in ((200, {'state': 'ready', 'taskId': 'b'*64, 'url': self.signed_url, 'fileName': 'wrong.tar.zst'}),
                                (200, {'url': self.signed_url, 'fileName': 'unbound.tar.zst'}),
                                (202, {'state': 'processing', 'taskId': TASK_ID, 'retryAfterMs': 2000})):
            self.responses = [(status, payload)]
            response = await self.client.get(folders.PUBLIC_PACKAGE + folders.query() + '&taskId=' + TASK_ID)
            self.assertEqual(response.status, 409 if status == 202 else 502)
        self.assertEqual(len(self.object_calls), before)

    async def test_poll_failure_preserves_status_without_accessing_storage(self):
        await self.login()
        for status in (401, 403, 404, 409, 429, 503):
            self.responses = [(status, {'code': 'FOLDER_PACKAGE_FAILED', 'message': '准备失败，请重试'})]
            response = await self.client.get(self.poll())
            self.assertEqual(response.status, status)
            self.assertEqual((await response.json())['message'], '准备失败，请重试')
        self.assertEqual(self.object_calls, [])

    async def test_observer_revoked_during_poll_never_receives_a_ready_url(self):
        await self.login()
        self.prepare_entered, self.prepare_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.client.get(self.poll()))
        try:
            await asyncio.wait_for(self.prepare_entered.wait(), 2)
            self.revoke_observer()
        finally:
            self.prepare_release.set()
        response = await task
        self.assertEqual(response.status, 401)
        self.assertEqual(self.object_calls, [])
