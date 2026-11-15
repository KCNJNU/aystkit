"""Folder archives keep their exact scope behind both logins; isolated HTTP only."""
import asyncio
import json
import unittest
from urllib.parse import quote, unquote
from unittest.mock import patch

from aiohttp import web

from cloud.backend.dbprog_full import APP_COOKIE
from tests import test_dbprog_full as fixtures
from tests import test_dbprog_cloud_transport as cloud_fixtures
from tests import test_dbprog_packages as package_fixtures
from tests.test_backend import ORIGIN
from tests.test_dbprog_full import DB_TOKEN, FILE_ID


API_PATH = '/api/v1/datasets/' + FILE_ID + '/folder-package'
PUBLIC_API = '/dbprog' + API_PATH
PUBLIC_PACKAGE = '/dbprog/folder-packages/' + FILE_ID
FOLDER = '原相机前/16:26-16:55'


def query(path=FOLDER):
    return '?path=' + quote(path, safe='')


class FolderPackageProxyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = cloud_fixtures.CloudWarehouseTransportTests.asyncSetUp
    asyncTearDown = cloud_fixtures.CloudWarehouseTransportTests.asyncTearDown
    cloud_login = fixtures.FullWarehouseProxyTests.cloud_login
    login = fixtures.FullWarehouseProxyTests.login
    revoke_observer = package_fixtures.WarehousePackageProxyTests.revoke_observer

    async def api(self, request):
        if '/folder-package' not in request.path:
            return await fixtures.FullWarehouseProxyTests.api(self, request)
        self.api_calls.append(dict(method=request.method, path=request.path_qs,
            raw_path=request.raw_path, folder=request.query.get('path'), headers=dict(request.headers)))
        if request.headers.get('Cookie') != 'cr_session=' + DB_TOKEN:
            return web.json_response({'error': 'unauthorized'}, status=401)
        if hasattr(self, 'prepare_entered'):
            self.prepare_entered.set()
            await self.prepare_release.wait()
        status = getattr(self, 'package_status', 200)
        permitted = getattr(self, 'allowed_folder', None)
        if permitted is not None and request.query.get('path') != permitted:
            status = 403
        if request.path.split('/')[4] != FILE_ID:
            status = 404
        payload = getattr(self, 'package_payload', None)
        if payload is None:
            payload = dict(url=self.signed_url,
                fileName=getattr(self, 'package_filename', '原相机前_20261102_文件夹.tar.zst'))
        return web.json_response(payload, status=status)

    async def object(self, request):
        if getattr(self, 'range_unsatisfied', False):
            self.object_calls.append(dict(method=request.method, headers=dict(request.headers)))
            return web.Response(status=416, headers={'Content-Range': 'bytes */123'})
        return await fixtures.FullWarehouseProxyTests.object(self, request)

    def folder_calls(self):
        return [call for call in self.api_calls if '/folder-package' in call['path']]

    async def test_exact_get_rewrites_to_same_origin_with_required_folder_scope(self):
        await self.login()
        response = await self.client.get(PUBLIC_API + query())
        self.assertEqual(response.status, 200)
        value = await response.json()
        self.assertEqual(value['url'], PUBLIC_PACKAGE + query())
        self.assertEqual(value['fileName'], '原相机前_20261102_文件夹.tar.zst')
        self.assertNotIn(self.object_origin, json.dumps(value))
        self.assertEqual(self.object_calls, [])

    async def test_root_folder_is_explicit_empty_path_not_a_missing_query(self):
        await self.login()
        response = await self.client.get(PUBLIC_API + '?path=')
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['url'], PUBLIC_PACKAGE + '?path=')
        response = await self.client.get(PUBLIC_PACKAGE + '?path=')
        self.assertEqual(response.status, 200)
        await response.read()
        self.assertEqual(self.folder_calls()[-1]['folder'], '')

    async def test_literal_unicode_percent_plus_ampersand_and_descendant_path_round_trip_once(self):
        await self.login()
        folder = '相机 + 50%/literal%2F&子目录'
        self.allowed_folder = folder
        response = await self.client.get(PUBLIC_API + query(folder))
        value = await response.json()
        self.assertEqual(value['url'], PUBLIC_PACKAGE + query(folder))
        response = await self.client.get(value['url'])
        self.assertEqual(response.status, 200)
        await response.read()
        call = self.folder_calls()[-1]
        self.assertEqual(call['folder'], folder)
        self.assertTrue(call['raw_path'].endswith(query(folder)))
        self.assertIn('%252F', call['raw_path'])

    async def test_query_is_exactly_one_path_and_rejects_caller_urls_before_api(self):
        await self.login()
        before = len(self.api_calls)
        for endpoint in (PUBLIC_API, PUBLIC_PACKAGE):
            for suffix in ('', '?url=' + quote(self.api_origin + '/sensitive', safe=''),
                           '?path=a&path=b', '?path=a&url=http://untrusted.invalid', '?path=a&download=1'):
                with self.subTest(endpoint=endpoint, query=suffix):
                    response = await self.client.get(endpoint + suffix)
                    self.assertEqual(response.status, 422)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_invalid_api_folder_paths_do_not_reach_api_or_storage(self):
        await self.login()
        before = len(self.api_calls)
        for folder in ('../other', 'a/../b', '/absolute', 'a//b', 'a/', 'a\\b',
                       'a\x00b', 'a\x7fb', ' a', 'a ', '.folder', 'a/.dataset',
                       '中'*67, '/'.join(['a'*200]*4)):
            for endpoint in (PUBLIC_API, PUBLIC_PACKAGE):
                with self.subTest(folder=folder, endpoint=endpoint):
                    response = await self.client.get(endpoint + query(folder))
                    self.assertEqual(response.status, 422)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_no_observer_never_contacts_api_or_storage(self):
        response = await self.client.get(PUBLIC_PACKAGE + query())
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())['error'], 'observer_auth_required')
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.object_calls, [])

    async def test_pending_observer_never_contacts_api_or_storage(self):
        response = await self.client.post('/api/register',
            json={'username': 'pending.folder', 'password': 'isolated-pending-password'},
            headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 201)
        response = await self.client.get(PUBLIC_PACKAGE + query())
        self.assertEqual(response.status, 403)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.object_calls, [])

    async def test_missing_forged_or_nonce_stale_db_login_cannot_reach_api(self):
        await self.cloud_login()
        for value in ('', DB_TOKEN + '.' + '0'*64):
            with self.subTest(value=value):
                headers = {'Cookie': APP_COOKIE + '=' + value} if value else {}
                response = await self.client.get(PUBLIC_PACKAGE + query(), headers=headers)
                self.assertEqual(response.status, 401)
        self.assertEqual(self.api_calls, [])
        await self.login()
        await self.cloud_login()
        before = len(self.api_calls)
        response = await self.client.get(PUBLIC_PACKAGE + query())
        self.assertEqual(response.status, 401)
        self.assertEqual(len(self.api_calls), before)
        self.assertEqual(self.object_calls, [])

    async def test_dataset_and_folder_permissions_are_rechecked_before_every_get_or_head(self):
        await self.login()
        self.allowed_folder = FOLDER
        response = await self.client.get(PUBLIC_PACKAGE + query())
        self.assertEqual(response.status, 200)
        await response.read()
        response = await self.client.head(PUBLIC_PACKAGE + query(FOLDER + '/outside'))
        self.assertEqual(response.status, 403)
        self.package_status = 403
        response = await self.client.head(PUBLIC_PACKAGE + query())
        self.assertEqual(response.status, 403)
        self.package_status = 200
        other = PUBLIC_PACKAGE.replace(FILE_ID, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
        response = await self.client.get(other + query())
        self.assertEqual(response.status, 404)
        self.assertEqual(len(self.object_calls), 1)
        self.assertEqual([row['folder'] for row in self.folder_calls()],
                         [FOLDER, FOLDER + '/outside', FOLDER, FOLDER])
        self.assertTrue(all(row['headers']['Cookie'] == 'cr_session=' + DB_TOKEN for row in self.folder_calls()))

    async def test_upstream_errors_are_preserved_without_contacting_objects(self):
        await self.login()
        for status in (401, 403, 404, 409, 429, 503):
            self.package_status = status
            with self.subTest(status=status):
                response = await self.client.get(PUBLIC_PACKAGE + query())
                self.assertEqual(response.status, status)
                self.assertNotIn(self.signed_url, await response.text())
        self.assertEqual(self.object_calls, [])

    async def test_get_and_head_ranges_keep_signature_bytes_without_forwarding_credentials(self):
        await self.login()
        for method in ('GET', 'HEAD'):
            with self.subTest(method=method):
                response = await self.client.request(method, PUBLIC_PACKAGE + query(),
                    headers={'Range': 'bytes=0-3', 'If-Range': 'fixture-etag',
                             'Authorization': 'Bearer caller-secret', 'Origin': ORIGIN})
                self.assertEqual(response.status, 206)
                self.assertEqual(await response.read(), self.object_bytes[:4] if method == 'GET' else b'')
                self.assertEqual(response.headers['Content-Length'], '4')
                self.assertEqual(response.headers['Content-Range'], f'bytes 0-3/{len(self.object_bytes)}')
                self.assertNotIn('Set-Cookie', response.headers)
                obj = self.object_calls[-1]
                self.assertEqual(obj['method'], 'GET')
                self.assertEqual(obj['raw_path'], self.signature_path)
                self.assertEqual(obj['headers']['If-Range'], 'fixture-etag')
                self.assertNotIn('Cookie', obj['headers'])
                self.assertNotIn('Authorization', obj['headers'])
                self.assertNotIn('Authorization', self.folder_calls()[-1]['headers'])

    async def test_unsatisfied_range_status_and_content_range_survive_proxy(self):
        await self.login()
        self.range_unsatisfied = True
        for method in ('GET', 'HEAD'):
            response = await self.client.request(method, PUBLIC_PACKAGE + query(), headers={'Range': 'bytes=999-1000'})
            self.assertEqual(response.status, 416)
            self.assertEqual(response.headers['Content-Range'], 'bytes */123')
            await response.read()

    async def test_head_does_not_wait_for_or_read_storage_body(self):
        await self.login()
        self.object_body_release = asyncio.Event()
        self.object_body_finished = asyncio.Event()
        try:
            response = await asyncio.wait_for(self.client.head(PUBLIC_PACKAGE + query()), timeout=2)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b'')
            self.assertEqual(response.headers['Content-Length'], str(len(self.object_bytes)))
            self.assertFalse(self.object_body_release.is_set())
        finally:
            self.object_body_release.set()
            await asyncio.wait_for(self.object_body_finished.wait(), timeout=2)

    async def test_filename_is_sanitized_and_unicode_download_name_preserved(self):
        await self.login()
        self.package_filename = '../unsafe\\原相机\r\nInjected.tar.zst'
        response = await self.client.get(PUBLIC_PACKAGE + query())
        self.assertEqual(response.status, 200)
        await response.read()
        value = unquote(response.headers['Content-Disposition'].split("filename*=UTF-8''", 1)[1])
        self.assertIn('原相机', value)
        self.assertFalse(any(c in value for c in ('/', '\\', '\r', '\n')))
        self.assertNotIn('Injected', response.headers)

    async def test_invalid_or_unconfigured_signed_url_never_becomes_a_proxy_target(self):
        await self.login()
        for url in (self.api_origin + '/sensitive?secret=fixture', 'file:///tmp/private',
                    'http://user:password@warehouse-minio:9000/private', 'http://untrusted.invalid/object'):
            self.signed_url = url
            for endpoint in (PUBLIC_API, PUBLIC_PACKAGE):
                with self.subTest(url=url, endpoint=endpoint):
                    response = await self.client.get(endpoint + query())
                    self.assertEqual(response.status, 502)
        self.assertEqual(self.object_calls, [])
        self.assertFalse(any(call['path'].startswith('/sensitive') for call in self.api_calls))

    async def test_incomplete_package_response_is_rejected_before_objects(self):
        await self.login()
        for payload in ({'url': self.signed_url}, {'fileName': 'folder.tar.zst'}, [],
                        {'url': self.signed_url, 'fileName': None}):
            self.package_payload = payload
            for endpoint in (PUBLIC_API, PUBLIC_PACKAGE):
                with self.subTest(payload=payload, endpoint=endpoint):
                    response = await self.client.get(endpoint + query())
                    self.assertEqual(response.status, 502)
        self.assertEqual(self.object_calls, [])

    async def test_foreign_origin_is_rejected_before_prepare_and_timeout_stays_180_seconds(self):
        await self.login()
        before = len(self.api_calls)
        response = await self.client.get(PUBLIC_PACKAGE + query(), headers={'Origin': 'https://untrusted.invalid'})
        self.assertEqual(response.status, 403)
        self.assertEqual(len(self.api_calls), before)
        with patch.object(self.gateway.api, 'request', wraps=self.gateway.api.request) as calls:
            response = await self.client.get(PUBLIC_PACKAGE + query())
            self.assertEqual(response.status, 200)
            await response.read()
        self.assertEqual(len(calls.call_args_list), 1)
        self.assertEqual(calls.call_args.kwargs['timeout'].total, 180)
        self.assertEqual(calls.call_args.args[0], 'GET')

    async def test_revoked_observer_during_prepare_never_reaches_objects(self):
        await self.login()
        self.prepare_entered, self.prepare_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.client.get(PUBLIC_PACKAGE + query()))
        try:
            await asyncio.wait_for(self.prepare_entered.wait(), 2)
            self.revoke_observer()
        finally:
            self.prepare_release.set()
        response = await task
        self.assertEqual(response.status, 401)
        self.assertEqual(self.object_calls, [])

    async def test_revoked_observer_waiting_for_slot_never_reaches_objects(self):
        await self.login()
        entered, release = asyncio.Event(), asyncio.Event()
        class Slot:
            async def __aenter__(self):
                entered.set()
                await release.wait()
            async def __aexit__(self, *_args):
                pass
        self.gateway.download_slots = Slot()
        task = asyncio.create_task(self.client.get(PUBLIC_PACKAGE + query()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.revoke_observer()
        finally:
            release.set()
        response = await task
        self.assertEqual(response.status, 401)
        self.assertEqual(self.object_calls, [])


if __name__ == '__main__':
    unittest.main()
