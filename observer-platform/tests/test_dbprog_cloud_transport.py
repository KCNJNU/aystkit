"""Cloud warehouse login and downloads keep working without robot API sockets."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from cloud.backend.app import create_app
from cloud.backend.dbprog import DbprogGateway
from tests.test_backend import ORIGIN, config
from tests import test_dbprog_full as fixtures
from tests.test_dbprog_full import DB_CSRF, DB_TOKEN, FILE_ID, INDEX


class CloudWarehouseTransportTests(unittest.IsolatedAsyncioTestCase):
    api = fixtures.FullWarehouseProxyTests.api
    object = fixtures.FullWarehouseProxyTests.object
    cloud_login = fixtures.FullWarehouseProxyTests.cloud_login
    login = fixtures.FullWarehouseProxyTests.login

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='warehouse-cloud-')
        root = Path(self.temp.name)
        (root / 'frontend/warehouse').mkdir(parents=True)
        (root / 'frontend/warehouse/index.html').write_text(INDEX)
        self.api_calls, self.object_calls = [], []
        self.role, self.object_type = 'OPERATOR', 'image/png'
        self.object_bytes = b'ISOLATED_CLOUD_OBJECT'
        self.signature_path = '/bucket/a%20b/%E6%B5%8B.png?X-Amz-Credential=example%2Ftest%2Fs3&X-Amz-Signature=001122'
        self.sse_mode, self.sse_cancelled = 'finite', asyncio.Event()
        self.upstreams = []
        for handler in (self.api, self.object):
            app = web.Application()
            app.router.add_route('*', '/{tail:.*}', handler)
            server = TestServer(app)
            await server.start_server()
            self.upstreams.append(server)
        self.api_origin, self.object_origin = [str(server.make_url('/')).rstrip('/') for server in self.upstreams]
        self.signed_url = self.object_origin + self.signature_path
        self.gateway = DbprogGateway(str(root / 'absent-robot-api.sock'), str(root / 'absent-robot-objects.sock'),
            api_url=self.api_origin, object_urls=self.object_origin)
        self.client = TestClient(TestServer(create_app(
            config(str(root / 'data'), frontend_dir=str(root / 'frontend')), dbprog=self.gateway)),
            cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for server in self.upstreams:
            await server.close()
        self.temp.cleanup()

    async def test_login_session_and_directory_need_no_robot_socket(self):
        response = await self.login()
        self.assertNotIn('sessionToken', await response.json())
        response = await self.client.get('/dbprog/api/v1/auth/session')
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['csrfToken'], DB_CSRF)
        response = await self.client.get('/dbprog/api/v1/datasets')
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['items'], [])
        self.assertEqual(self.api_calls[-1]['headers']['Cookie'], 'cr_session=' + DB_TOKEN)
        self.assertEqual(self.api_calls[-1]['headers']['Host'], URL(self.api_origin).authority)

    async def test_cloud_transport_keeps_platform_auth_and_csrf(self):
        response = await self.client.get('/dbprog/api/v1/datasets')
        self.assertEqual(response.status, 401)
        self.assertEqual(self.api_calls, [])
        await self.login()
        response = await self.client.post('/dbprog/api/v1/datasets', json={}, headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 403)
        response = await self.client.post('/dbprog/api/v1/datasets', json={},
            headers={'Origin': ORIGIN, 'X-CSRF-Token': DB_CSRF})
        self.assertEqual(response.status, 200)

    async def test_signed_object_host_path_range_and_credentials_are_preserved(self):
        await self.login()
        response = await self.client.get('/dbprog/objects/' + FILE_ID, headers={'Range': 'bytes=0-3'})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), self.object_bytes[:4])
        request = self.object_calls[-1]
        self.assertEqual(request['raw_path'], self.signature_path)
        self.assertEqual(request['host'], URL(self.object_origin).authority)
        self.assertNotIn('Cookie', request['headers'])
        self.assertNotIn('Authorization', request['headers'])

    async def test_signed_url_outside_storage_allowlist_is_not_requested(self):
        await self.login()
        self.signed_url = self.api_origin + '/sensitive?signature=fixture'
        response = await self.client.get('/dbprog/objects/' + FILE_ID)
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['error'], 'DBPROG_RESPONSE')
        self.assertEqual(self.object_calls, [])
        self.assertFalse(any(call['path'].startswith('/sensitive') for call in self.api_calls))

    async def test_legacy_and_cloud_storage_are_both_explicitly_allowed(self):
        self.gateway.object_origins += (URL(self.api_origin),)
        self.gateway.objects.origins = self.gateway.object_origins
        response = await self.gateway.objects.get(self.api_origin + '/health', allow_redirects=False)
        async with response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.api_calls[-1]['path'], '/health')

    async def test_cloud_download_inactivity_window_does_not_extend_api_timeout(self):
        download = self.gateway.objects.session.timeout
        self.assertIsNone(download.total)
        self.assertEqual(download.connect, 8)
        self.assertEqual(download.sock_read, 300)
        self.assertEqual(self.gateway.api.session.timeout.total, 8)

    async def test_legacy_download_has_same_bounded_inactivity_window(self):
        legacy = DbprogGateway('/missing-api.sock', '/missing-object.sock', api_url='', object_urls='')
        await legacy.start(None)
        try:
            self.assertIsNone(legacy.objects.timeout.total)
            self.assertEqual(legacy.objects.timeout.connect, 8)
            self.assertEqual(legacy.objects.timeout.sock_read, 300)
            self.assertEqual(legacy.api.timeout.total, 8)
        finally:
            await legacy.close(None)


class CloudTransportConfigurationTests(unittest.TestCase):
    def test_transport_configuration_requires_both_origins(self):
        with self.assertRaises(ValueError):
            DbprogGateway(api_url='http://warehouse-api:3000', object_urls='')

    def test_upstream_origin_cannot_include_credentials_paths_or_queries(self):
        for value in ('file:///tmp/warehouse', 'http://user:secret@warehouse-api',
                      'http://warehouse-api/api', 'http://warehouse-api?key=secret',
                      'http://warehouse-api#fragment', 'http://warehouse-api\n', 'http://warehouse-api:bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DbprogGateway(api_url=value, object_urls='http://warehouse-minio:9000')
