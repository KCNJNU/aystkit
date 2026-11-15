"""Cloud authorization and real private-socket DBProg transport, using isolated fixtures."""
import asyncio
import hashlib
import hmac
import json
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from cloud.backend.app import CONFIG, COOKIE as OBSERVER_COOKIE, SESSIONS, create_app
from cloud.backend.dbprog import COOKIE, MAX_JSON, DbprogError, DbprogGateway
from tests.test_backend import ORIGIN, PASSWORD, config


DB_TOKEN = 'isolated-dbprog-token-123456789012345678901234'
DB_CSRF = 'isolated-dbprog-csrf-1234567890123456789012345'
DATASET_ID = '12345678-1234-4234-8234-123456789abc'
FILE_ID = '87654321-1234-4234-8234-123456789abc'
DB_PASSWORD = 'isolated-dbprog-password'
LOGIN_USER = {'id': 'dbprog-viewer', 'displayName': '数仓用户', 'role': 'VIEWER'}


class FakeContent:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, _size):
        yield self.data


class FakeObjectResponse:
    def __init__(self, data=b'ISOLATED_DBPROG_OBJECT', status=200):
        self.status = status
        self.headers = {'Content-Length': str(len(data)), 'Set-Cookie': 'private-upstream=must-not-forward'}
        self.content = FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


class FakeObjects:
    def __init__(self):
        self.calls = []
        self.response = FakeObjectResponse()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.objects = FakeObjects()
        self.download_slots = asyncio.Semaphore(3)
        self.started = self.closed = False

    async def start(self, _app):
        self.started = True

    async def close(self, _app):
        self.closed = True

    async def request(self, path, token='', method='GET', payload=None, csrf=''):
        self.calls.append({'path': path, 'token': token, 'method': method, 'payload': payload, 'csrf': csrf})
        if path in self.overrides:
            value = self.overrides[path]
            if isinstance(value, Exception):
                raise value
            return value
        if path == '/ready':
            return {'status': 'healthy'}, []
        if path == '/api/v1/auth/login':
            return {'user': LOGIN_USER, 'csrfToken': DB_CSRF, 'sessionToken': DB_TOKEN}, [
                f'cr_session={DB_TOKEN}; Path=/; HttpOnly; SameSite=Lax',
            ]
        if path == '/api/v1/auth/session':
            return {'csrfToken': DB_CSRF}, []
        if path == '/api/v1/auth/logout':
            return {'success': True}, []
        if path == '/api/system/status':
            return {'status': 'healthy', 'services': {'postgresql': {'status': 'healthy', 'latency_ms': 2}}}, []
        if path == '/api/warehouse/overview':
            return {'summary': {'dataset_count': 2, 'file_count': 3, 'managed_bytes': '100'}, 'datasets': [], 'uploads': []}, []
        if path.endswith('/folders'):
            return [{'id': FILE_ID, 'datasetId': DATASET_ID, 'name': '扫描', 'path': '扫描', 'parentPath': ''}], []
        if path == f'/api/v1/files/{FILE_ID}/download':
            return {'url': 'http://private-storage.invalid:9000/bucket/object?signature=private-signature'}, []
        if path == f'/api/v1/files/{FILE_ID}':
            return {'id': FILE_ID, 'originalName': '扫描 "记录".json'}, []
        return {'items': [], 'total': 0, 'page': 1, 'pageSize': 20}, []


class DbprogBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-cloud-dbprog-')
        self.gateway = FakeGateway()
        self.app = create_app(config(self.temp.name), dbprog=self.gateway)
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.extra_clients = []

    async def asyncTearDown(self):
        for client in self.extra_clients:
            await client.close()
        await self.client.close()
        self.assertTrue(self.gateway.closed)
        self.temp.cleanup()

    async def cloud_login(self, client=None):
        client = client or self.client
        response = await client.post('/api/login', json={'username': 'viewer', 'password': PASSWORD},
            headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200, await response.text())
        return response.cookies[OBSERVER_COOKIE].value

    async def db_login(self, client=None):
        response = await (client or self.client).post('/api/dbprog/login',
            json={'email': '  viewer@example.invalid  ', 'password': DB_PASSWORD}, headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200, await response.text())
        return response

    async def authenticated(self):
        await self.cloud_login()
        return await self.db_login()

    async def test_anonymous_and_device_tokens_cannot_access_dbprog(self):
        for path in ('status', 'overview', 'datasets', 'files', f'datasets/{DATASET_ID}/folders', f'files/{FILE_ID}/download'):
            with self.subTest(path=path):
                response = await self.client.get('/api/dbprog/' + path,
                    headers={'Authorization': 'Bearer isolated-device-token-not-a-viewer'})
                self.assertEqual(response.status, 401)
                self.assertEqual((await response.json())['error'], 'observer_auth_required')
        self.assertEqual(self.gateway.calls, [])
        self.assertTrue(self.gateway.started)

    async def test_page_redirects_anonymous_and_pending_cloud_accounts(self):
        response = await self.client.get('/dbprog', allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], '/login')
        registered = await self.client.post('/api/register', json={
            'username': 'pending.dbprog', 'password': 'isolated-pending-password'}, headers={'Origin': ORIGIN})
        self.assertEqual(registered.status, 201)
        for path in ('/dbprog', '/dbprog/'):
            response = await self.client.get(path, allow_redirects=False)
            self.assertEqual(response.status, 302)
            self.assertEqual(response.headers['Location'], '/pending')
        for path in ('status', 'overview', 'datasets', 'files'):
            response = await self.client.get('/api/dbprog/' + path)
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())['error'], 'observer_access_required')
        response = await self.client.post('/api/dbprog/login', json={'email': 'admin', 'password': DB_PASSWORD},
            headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.gateway.calls, [])

    async def test_approved_page_has_no_dbprog_credentials_or_lan_dependency(self):
        await self.cloud_login()
        response = await self.client.get('/dbprog')
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertIn('/dbprog/assets/', text)
        self.assertIn('<div id="root">', text)
        self.assertNotIn(DB_TOKEN, text)
        self.assertNotIn(DB_PASSWORD, text)
        self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])

    async def test_signed_cookie_is_scoped_httponly_and_bound_to_cloud_session_nonce(self):
        cloud_cookie = await self.cloud_login()
        response = await self.db_login()
        self.assertEqual(await response.json(), {'authenticated': True, 'user': LOGIN_USER})
        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['path'], '/api/dbprog')
        self.assertEqual(cookie['samesite'], 'Strict')
        owner = self.app[SESSIONS].read(cloud_cookie)
        expected = hmac.new(self.app[CONFIG].session_secret.encode(),
            (owner['n'] + ':' + DB_TOKEN).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(cookie.value, DB_TOKEN + '.' + expected)
        self.assertNotIn(COOKIE, self.client.session.cookie_jar.filter_cookies(self.client.make_url('/')))
        self.assertIn(COOKIE, self.client.session.cookie_jar.filter_cookies(self.client.make_url('/api/dbprog/status')))
        login = self.gateway.calls[-1]
        self.assertEqual(login['payload'], {'email': 'viewer@example.invalid', 'password': DB_PASSWORD})
        self.assertEqual(login['token'], '')
        body = await response.text()
        self.assertNotIn(DB_CSRF, body)
        self.assertNotIn(DB_TOKEN, body)
        self.assertNotIn(DB_PASSWORD, body)

    async def test_production_cookie_is_secure_behind_the_trusted_https_proxy(self):
        production_origin = 'https://observer.example.invalid'
        self.app[CONFIG].allow_insecure_local = False
        self.app[CONFIG].public_origin = production_origin
        headers = {'Origin': production_origin, 'X-Forwarded-Proto': 'https'}
        response = await self.client.post('/api/login', json={'username': 'viewer', 'password': PASSWORD},
            headers=headers)
        self.assertEqual(response.status, 200, await response.text())
        headers['Cookie'] = OBSERVER_COOKIE + '=' + response.cookies[OBSERVER_COOKIE].value
        response = await self.client.post('/api/dbprog/login',
            json={'email': 'viewer@example.invalid', 'password': DB_PASSWORD}, headers=headers)
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue(response.cookies[COOKIE]['secure'])
        self.assertTrue(response.cookies[COOKIE]['httponly'])
        self.assertEqual(response.cookies[COOKIE]['samesite'], 'Strict')
        self.assertEqual(response.cookies[COOKIE]['path'], '/api/dbprog')

    async def test_cloud_relogin_invalidates_previous_dbprog_cookie_without_global_token(self):
        await self.authenticated()
        self.assertEqual((await self.client.get('/api/dbprog/overview')).status, 200)
        await self.cloud_login()
        before = len(self.gateway.calls)
        response = await self.client.get('/api/dbprog/overview')
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())['error'], 'DBPROG_AUTH_REQUIRED')
        self.assertEqual(len(self.gateway.calls), before)

    async def test_other_cloud_session_cannot_reuse_a_transplanted_cookie(self):
        response = await self.authenticated()
        copied = response.cookies[COOKIE].value
        other = TestClient(TestServer(create_app(config(self.temp.name + '/other'), dbprog=FakeGateway())),
            cookie_jar=CookieJar(unsafe=True))
        self.extra_clients.append(other)
        await other.start_server()
        await self.cloud_login(other)
        response = await other.get('/api/dbprog/overview', headers={'Cookie': COOKIE + '=' + copied})
        self.assertEqual(response.status, 401)

    async def test_tampered_cookie_and_original_cr_session_are_not_accepted(self):
        await self.cloud_login()
        for cookie in (DB_TOKEN, DB_TOKEN + '.' + '0' * 64, 'invalid-token', 'short.' + '0' * 64):
            response = await self.client.get('/api/dbprog/overview', headers={'Cookie': COOKIE + '=' + cookie})
            self.assertEqual(response.status, 401)
        response = await self.client.get('/api/dbprog/overview', headers={'Cookie': 'cr_session=' + DB_TOKEN})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.gateway.calls, [])

    async def test_login_and_logout_require_cloud_identity_and_exact_origin(self):
        for path in ('login', 'logout'):
            response = await self.client.post('/api/dbprog/' + path, json={'email': 'admin', 'password': DB_PASSWORD},
                headers={'Origin': ORIGIN})
            self.assertEqual(response.status, 401)
        await self.cloud_login()
        for path in ('login', 'logout'):
            for headers in ({}, {'Origin': 'https://attacker.invalid'}, {'Origin': ORIGIN + '.attacker.invalid'}):
                with self.subTest(path=path, origin=headers):
                    response = await self.client.post('/api/dbprog/' + path,
                        json={'email': 'admin', 'password': DB_PASSWORD}, headers=headers)
                    self.assertEqual(response.status, 403)
                    self.assertEqual((await response.json())['error'], 'DBPROG_ORIGIN')
        self.assertEqual(self.gateway.calls, [])

    async def test_status_never_queries_private_data_without_dbprog_login(self):
        await self.cloud_login()
        response = await self.client.get('/api/dbprog/status')
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(data['available'])
        self.assertFalse(data['authenticated'])
        self.assertIsNone(data['health'])
        self.assertEqual(self.gateway.calls, [{'path': '/ready', 'token': '', 'method': 'GET', 'payload': None, 'csrf': ''}])

    async def test_periodic_status_uses_current_token_but_never_rotates_csrf(self):
        await self.authenticated()
        self.gateway.calls.clear()
        for _ in range(2):
            response = await self.client.get('/api/dbprog/status')
            data = await response.json()
            self.assertTrue(data['authenticated'])
            self.assertEqual(data['health']['services']['postgresql']['status'], 'healthy')
        self.assertEqual([call['path'] for call in self.gateway.calls], ['/ready', '/api/system/status'] * 2)
        self.assertEqual([call['token'] for call in self.gateway.calls], ['', DB_TOKEN] * 2)
        self.assertTrue(all(call['method'] == 'GET' and not call['csrf'] for call in self.gateway.calls))

    async def test_upstream_auth_and_role_errors_are_preserved(self):
        await self.authenticated()
        for status, code in ((401, 'DBPROG_AUTH_REQUIRED'), (403, 'DBPROG_FORBIDDEN'), (403, 'PASSWORD_CHANGE_REQUIRED')):
            with self.subTest(code=code):
                self.gateway.overrides['/api/warehouse/overview'] = DbprogError(status, code, 'safe upstream message')
                response = await self.client.get('/api/dbprog/overview')
                self.assertEqual(response.status, status)
                self.assertEqual((await response.json())['error'], code)

    async def test_files_translate_query_and_preserve_explicit_root_directory(self):
        await self.authenticated()
        response = await self.client.get('/api/dbprog/files', params={
            'dataset_id': DATASET_ID, 'folder_path': '', 'page': '2', 'page_size': '50', 'keyword': ' 扫描 '})
        self.assertEqual(response.status, 200)
        call = self.gateway.calls[-1]
        self.assertEqual(parse_qs(urlsplit(call['path']).query, keep_blank_values=True), {
            'datasetId': [DATASET_ID], 'folderPath': [''], 'page': ['2'], 'pageSize': ['50'], 'search': ['扫描']})
        self.assertEqual(call['token'], DB_TOKEN)
        response = await self.client.get('/api/dbprog/files')
        self.assertEqual(response.status, 200)
        self.assertNotIn('folderPath', parse_qs(urlsplit(self.gateway.calls[-1]['path']).query))

    async def test_datasets_page_and_actual_folder_array_contract(self):
        await self.authenticated()
        response = await self.client.get('/api/dbprog/datasets?page=3&page_size=10&keyword=robot')
        self.assertEqual(response.status, 200)
        self.assertEqual(parse_qs(urlsplit(self.gateway.calls[-1]['path']).query), {
            'page': ['3'], 'pageSize': ['10'], 'search': ['robot']})
        response = await self.client.get(f'/api/dbprog/datasets/{DATASET_ID}/folders')
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertIsInstance(data['items'], list)
        self.assertEqual(data['items'][0]['path'], '扫描')

    async def test_unbounded_queries_and_bad_ids_stop_before_upstream(self):
        await self.authenticated()
        before = len(self.gateway.calls)
        for path in ('datasets?page=0', 'datasets?page=nan', 'datasets?page_size=101',
                     'datasets?keyword=' + 'x' * 501, 'files?dataset_id=invalid',
                     'files?folder_path=' + 'x' * 751, 'datasets/invalid/folders', 'files/invalid/download'):
            with self.subTest(path=path[:80]):
                response = await self.client.get('/api/dbprog/' + path)
                self.assertEqual(response.status, 422)
        self.assertEqual(len(self.gateway.calls), before)

    async def test_explicit_logout_rotates_csrf_then_clears_only_scoped_db_cookie(self):
        await self.authenticated()
        self.gateway.calls.clear()
        response = await self.client.post('/api/dbprog/logout', headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'authenticated': False})
        self.assertEqual([call['path'] for call in self.gateway.calls], ['/api/v1/auth/session', '/api/v1/auth/logout'])
        self.assertEqual(self.gateway.calls[-1]['csrf'], DB_CSRF)
        self.assertEqual(self.gateway.calls[-1]['token'], DB_TOKEN)
        self.assertEqual(response.cookies[COOKIE]['path'], '/api/dbprog')
        self.assertEqual(response.cookies[COOKIE]['max-age'], '0')
        self.assertEqual((await self.client.get('/api/session')).status, 200)
        self.assertEqual((await self.client.get('/api/dbprog/overview')).status, 401)

    async def test_download_streams_same_origin_bytes_and_does_not_expose_lan_url(self):
        await self.authenticated()
        response = await self.client.get(f'/api/dbprog/files/{FILE_ID}/download')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'ISOLATED_DBPROG_OBJECT')
        self.assertEqual(response.content_type, 'application/octet-stream')
        self.assertIn("filename*=UTF-8''", response.headers['Content-Disposition'])
        self.assertNotIn('private-storage', str(response.headers))
        self.assertNotIn('signature', str(response.headers))
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        url, options = self.gateway.objects.calls[0]
        self.assertIn('private-storage.invalid', str(url))
        self.assertEqual(options, {'allow_redirects': False})
        self.assertEqual((await self.client.head(f'/api/dbprog/files/{FILE_ID}/download')).status, 405)

    async def test_download_rejects_unsafe_urls_and_upstream_redirects(self):
        await self.authenticated()
        path = f'/api/v1/files/{FILE_ID}/download'
        for value in (None, {}, 'file:///etc/passwd', 'javascript:alert(1)', 'http://user:private@storage/object',
                      'http://[invalid', 'http://storage:bad/object', 'http://storage/object#fragment'):
            with self.subTest(url=value):
                self.gateway.overrides[path] = ({'url': value}, [])
                response = await self.client.get(f'/api/dbprog/files/{FILE_ID}/download')
                self.assertEqual(response.status, 502)
                self.assertEqual((await response.json())['error'], 'DBPROG_RESPONSE')
        self.assertEqual(self.gateway.objects.calls, [])
        self.gateway.overrides.pop(path)
        self.gateway.objects.response = FakeObjectResponse(status=302)
        response = await self.client.get(f'/api/dbprog/files/{FILE_ID}/download')
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['error'], 'DBPROG_DOWNLOAD')


class DbprogUnixIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dbprog-unix-')
        self.api_path, self.object_path = (self.temp.name + '/api.sock', self.temp.name + '/objects.sock')
        self.api_calls, self.object_calls = [], []
        self.api_mode = 'normal'
        self.object_status = 200
        self.object_data = b'ISOLATED_MINIO_OBJECT_NOT_ROBOT_DATA'
        self.signed_path = '/bucket/raw%20data/%E6%B5%8B%E8%AF%95.json?X-Amz-Credential=example%2F20261027%2Ftest%2Fs3%2Faws4_request&X-Amz-Signature=001122&response-content-disposition=attachment%3B%20filename%3D%22test.json%22'
        self.signed_url = 'http://never-resolves.invalid:9000' + self.signed_path
        self.api_app = web.Application()
        self.api_app.router.add_route('*', '/{tail:.*}', self.api_handler)
        self.api_runner = web.AppRunner(self.api_app, access_log=None)
        await self.api_runner.setup()
        await web.UnixSite(self.api_runner, self.api_path).start()
        self.object_app = web.Application()
        self.object_app.router.add_route('*', '/{tail:.*}', self.object_handler)
        self.object_runner = web.AppRunner(self.object_app, access_log=None)
        await self.object_runner.setup()
        await web.UnixSite(self.object_runner, self.object_path).start()
        self.gateway = DbprogGateway(self.api_path, self.object_path)
        self.app = create_app(config(self.temp.name + '/cloud'), dbprog=self.gateway)
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.api_runner.cleanup()
        await self.object_runner.cleanup()
        self.temp.cleanup()

    async def api_handler(self, request):
        self.api_calls.append({'path': request.path_qs, 'host': request.host,
            'cookie': request.headers.get('Cookie'), 'authorization': request.headers.get('Authorization'),
            'csrf': request.headers.get('X-CSRF-Token'), 'method': request.method,
            'body': await request.json() if request.can_read_body else None})
        if request.path == '/ready':
            response = web.json_response({'status': 'healthy'})
            response.set_cookie('cr_session', 'UNREQUESTED_PRIVATE_COOKIE_DO_NOT_STORE')
            return response
        if request.path == '/api/v1/auth/login':
            response = web.json_response({'user': LOGIN_USER, 'csrfToken': DB_CSRF})
            response.set_cookie('cr_session', DB_TOKEN, httponly=True)
            return response
        if request.path == f'/api/v1/files/{FILE_ID}/download':
            return web.json_response({'url': self.signed_url})
        if request.path == f'/api/v1/files/{FILE_ID}':
            return web.json_response({'id': FILE_ID, 'originalName': '测试文件.json'})
        if request.path == '/api/system/status':
            return web.json_response({'status': 'healthy', 'services': {'postgresql': {'status': 'healthy'}}})
        if self.api_mode == 'redirect':
            return web.Response(status=302, headers={'Location': 'http://never-resolves.invalid/escape'},
                text='PRIVATE_UPSTREAM_REDIRECT')
        if self.api_mode == 'invalid':
            return web.Response(body=b'PRIVATE_UPSTREAM_NOT_JSON')
        if self.api_mode == 'large':
            return web.Response(body=b'x' * (MAX_JSON + 1), content_type='application/json')
        if self.api_mode == 'array':
            return web.json_response([])
        if self.api_mode in ('401', '403', 'password-change', '500'):
            status = 403 if self.api_mode == 'password-change' else int(self.api_mode)
            return web.json_response({'message': 'PASSWORD_CHANGE_REQUIRED' if self.api_mode == 'password-change'
                                     else 'PRIVATE_DATABASE_CREDENTIALS'}, status=status)
        if request.path.endswith('/folders'):
            return web.json_response([{'path': '扫描', 'name': '扫描'}])
        return web.json_response({'items': []})

    async def object_handler(self, request):
        self.object_calls.append({'host': request.host, 'raw_path': request.raw_path,
            'cookie': request.headers.get('Cookie'), 'authorization': request.headers.get('Authorization')})
        if self.object_status == 302:
            return web.Response(status=302, headers={'Location': 'http://never-resolves.invalid/escape'})
        return web.Response(body=self.object_data, status=self.object_status, content_type='application/json',
            headers={'Set-Cookie': 'private-storage-cookie=not-forwarded'})

    async def login(self):
        response = await self.client.post('/api/login', json={'username': 'viewer', 'password': PASSWORD},
            headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200, await response.text())
        response = await self.client.post('/api/dbprog/login', json={'email': 'viewer@example.invalid', 'password': DB_PASSWORD},
            headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200, await response.text())

    async def test_real_api_unix_socket_uses_no_global_cookies_or_authorization(self):
        data, _ = await self.gateway.request('/ready')
        self.assertEqual(data['status'], 'healthy')
        await self.gateway.request('/probe', DB_TOKEN)
        await self.gateway.request('/probe')
        self.assertEqual([call['cookie'] for call in self.api_calls], [None, 'cr_session=' + DB_TOKEN, None])
        self.assertTrue(all(call['authorization'] is None for call in self.api_calls))
        self.assertTrue(all(call['host'] == 'dbprog' for call in self.api_calls))

    async def test_real_login_and_health_keep_dbprog_cookie_private_and_do_not_rotate_csrf(self):
        await self.login()
        response = await self.client.get('/api/dbprog/status')
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['authenticated'])
        self.assertEqual([call['path'] for call in self.api_calls], ['/api/v1/auth/login', '/ready', '/api/system/status'])
        self.assertEqual(self.api_calls[-1]['cookie'], 'cr_session=' + DB_TOKEN)
        self.assertIsNone(self.api_calls[-2]['cookie'])
        self.assertIsNone(self.api_calls[-1]['csrf'])

    async def test_real_download_uses_fixed_object_socket_and_preserves_signed_host_and_path(self):
        await self.login()
        response = await self.client.get(f'/api/dbprog/files/{FILE_ID}/download')
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.read(), self.object_data)
        self.assertEqual(len(self.object_calls), 1)
        call = self.object_calls[0]
        self.assertEqual(call['host'], 'never-resolves.invalid:9000')
        self.assertEqual(call['raw_path'], self.signed_path)
        self.assertIsNone(call['cookie'])
        self.assertIsNone(call['authorization'])
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertNotIn('never-resolves.invalid', str(response.headers))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_real_gateway_response_errors_are_bounded_redacted_and_do_not_follow_redirect(self):
        for mode, status, code in [('invalid', 502, 'DBPROG_RESPONSE'), ('large', 502, 'DBPROG_RESPONSE'),
                                   ('array', 502, 'DBPROG_RESPONSE'), ('redirect', 502, 'DBPROG_RESPONSE'),
                                   ('401', 401, 'DBPROG_AUTH_REQUIRED'), ('403', 403, 'DBPROG_FORBIDDEN'),
                                   ('password-change', 403, 'PASSWORD_CHANGE_REQUIRED'), ('500', 502, 'DBPROG_UPSTREAM')]:
            self.api_mode = mode
            before = len(self.api_calls)
            with self.subTest(mode=mode):
                with self.assertRaises(DbprogError) as failure:
                    await self.gateway.request('/error-probe', DB_TOKEN)
                self.assertEqual(failure.exception.status, status)
                self.assertEqual(failure.exception.code, code)
                self.assertNotIn('PRIVATE_', str(failure.exception))
                self.assertEqual(len(self.api_calls), before + 1)

    async def test_actual_unix_download_refuses_object_redirect(self):
        await self.login()
        self.object_status = 302
        response = await self.client.get(f'/api/dbprog/files/{FILE_ID}/download')
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['error'], 'DBPROG_DOWNLOAD')
        self.assertEqual(len(self.object_calls), 1)

    async def test_missing_api_socket_returns_safe_unavailable_error(self):
        gateway = DbprogGateway(self.temp.name + '/missing-api.sock', self.object_path)
        await gateway.start(None)
        try:
            with self.assertRaises(DbprogError) as failure:
                await gateway.request('/ready')
            self.assertEqual(failure.exception.status, 503)
            self.assertEqual(failure.exception.code, 'DBPROG_UNAVAILABLE')
            self.assertNotIn(self.temp.name, str(failure.exception))
        finally:
            await gateway.close(None)
