"""Original warehouse app proxy: isolated HTTP clients and private Unix upstreams."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from cloud.backend.app import CONFIG, create_app
from cloud.backend.dbprog import DbprogGateway
from cloud.backend.dbprog_full import APP_COOKIE
from tests.test_backend import ORIGIN, PASSWORD, config


DB_TOKEN = 'isolated-full-dbprog-token-1234567890123456789'
DB_CSRF = 'isolated-full-dbprog-csrf-12345678901234567890'
FILE_ID = '12345678-1234-4234-8234-123456789abc'
INDEX = '<!doctype html><html><body style="background:white">原数仓测试应用</body></html>'


class FullWarehouseProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-full-dbprog-')
        base = Path(self.temp.name)
        assets = base / 'frontend/warehouse/assets'
        assets.mkdir(parents=True)
        (assets.parent / 'index.html').write_text(INDEX)
        (assets / 'app.js').write_text('/* isolated original-app fixture */')
        self.api_calls, self.object_calls = [], []
        self.role = 'OPERATOR'
        self.object_type = 'image/png'
        self.object_bytes = b'ISOLATED_OBJECT_BYTES'
        self.object_range_header = True
        self.metadata_size = None
        self.signature_path = '/bucket/a%20b/%E6%B5%8B%E8%AF%95.png?X-Amz-Credential=example%2Ftest%2Fs3&X-Amz-Signature=001122'
        self.signed_url = 'http://never-resolves.invalid:9000' + self.signature_path
        self.sse_mode = 'finite'
        self.sse_cancelled = asyncio.Event()
        self.runners = []
        for name, handler in (('api', self.api), ('objects', self.object)):
            fixture = web.Application(client_max_size=20 * 1024 * 1024)
            fixture.router.add_route('*', '/{tail:.*}', handler)
            runner = web.AppRunner(fixture, access_log=None)
            await runner.setup()
            await web.UnixSite(runner, str(base / (name + '.sock'))).start()
            self.runners.append(runner)
        self.gateway = DbprogGateway(str(base / 'api.sock'), str(base / 'objects.sock'))
        self.app = create_app(config(str(base / 'data'), frontend_dir=str(base / 'frontend')), dbprog=self.gateway)
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for runner in self.runners:
            await runner.cleanup()
        self.temp.cleanup()

    async def cloud_login(self):
        response = await self.client.post('/api/login', json={'username': 'viewer', 'password': PASSWORD},
            headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200, await response.text())

    async def login(self):
        await self.cloud_login()
        response = await self.client.post('/dbprog/api/v1/auth/login',
            json={'email': 'operator@example.invalid', 'password': 'isolated-not-real-password'},
            headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200, await response.text())
        return response

    async def api(self, request):
        body = await request.read()
        self.api_calls.append({'method': request.method, 'path': request.path_qs, 'body': body,
            'headers': dict(request.headers)})
        if request.path in ('/health', '/ready'):
            return web.json_response({'status': 'healthy'})
        if request.path == '/version':
            return web.json_response({'service': 'api', 'version': '0.1.0', 'gitCommit': 'test', 'buildTime': 'unknown'})
        if request.path == '/api/v1/auth/login':
            response = web.json_response({'user': {'role': self.role, 'email': 'operator@example.invalid'},
                'csrfToken': DB_CSRF, 'sessionToken': 'MUST_NOT_LEAK', 'expiresAt': '2099-01-01T00:00:00Z'})
            response.set_cookie('cr_session', DB_TOKEN, httponly=True)
            return response
        if request.headers.get('Cookie') != 'cr_session=' + DB_TOKEN:
            return web.json_response({'message': 'Authentication required', 'error': 'Unauthorized', 'statusCode': 401}, status=401)
        if request.path == '/api/v1/auth/session':
            return web.json_response({'user': {'role': self.role}, 'csrfToken': DB_CSRF})
        if request.method not in ('GET', 'HEAD'):
            if request.headers.get('X-CSRF-Token') != DB_CSRF:
                return web.json_response({'message': 'Invalid CSRF token', 'error': 'Forbidden', 'statusCode': 403}, status=403)
            if self.role == 'VIEWER' and not request.path.startswith('/api/v1/auth/'):
                return web.json_response({'message': 'Insufficient role', 'error': 'Forbidden', 'statusCode': 403}, status=403)
        if request.path in ('/api/v1/auth/logout', '/api/v1/auth/change-password'):
            response = web.json_response({'success': True})
            response.del_cookie('cr_session')
            return response
        if request.path == '/api/ai/chat/stream':
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})
            await response.prepare(request)
            try:
                await response.write(b'data: {"type":"delta","data":{"text":"first"}}\n\n')
                if self.sse_mode == 'cancel':
                    while True:
                        await asyncio.sleep(.02)
                        await response.write(b': heartbeat\n\n')
                else:
                    await asyncio.sleep(.03)
                    await response.write(b'data: {"type":"done","data":{"answer":"first"}}\n\n')
                    await response.write_eof()
            except (ConnectionError, asyncio.CancelledError):
                self.sse_cancelled.set()
            return response
        if request.path == f'/api/v1/files/{FILE_ID}/download':
            return web.json_response({'url': self.signed_url})
        if request.path == f'/api/v1/files/{FILE_ID}':
            return web.json_response({'id': FILE_ID, 'originalName': '测试文件.png',
                'mimeType': self.object_type, 'fileSize': getattr(self, 'metadata_size', None)})
        if request.path == '/api/v1/audit-logs/export':
            return web.Response(text='actor,action\nfixture,read\n', content_type='text/csv')
        if request.path == '/api/v1/redirect':
            return web.Response(status=302, headers={'Location': 'http://never-resolves.invalid/escape'})
        if request.path == '/api/v1/datasets/remove' and request.method == 'DELETE':
            return web.Response(status=204)
        if '/parts/' in request.path:
            return web.json_response({'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
        return web.json_response({'items': [], 'received': json.loads(body) if body and request.content_type == 'application/json' else None})

    async def object(self, request):
        self.object_calls.append({'method': request.method, 'host': request.host,
            'raw_path': request.raw_path, 'headers': dict(request.headers)})
        # Model an actual S3 GET-signed URL: changing its method invalidates
        # the signature, even when the caller only wants response headers.
        if request.method != 'GET':
            return web.Response(status=403, text='SignatureDoesNotMatch')
        release = getattr(self, 'object_body_release', None)
        if release is not None:
            response = web.StreamResponse(headers={'Content-Type': self.object_type,
                'Content-Length': str(len(self.object_bytes))})
            finished = self.object_body_finished
            try:
                await response.prepare(request)
                await release.wait()
                await response.write(self.object_bytes)
                await response.write_eof()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                finished.set()
            return response
        status, body, headers = 200, self.object_bytes, {'Set-Cookie': 'untrusted-object-cookie=not-forwarded'}
        if request.headers.get('Range') == 'bytes=0-3':
            status, body = 206, body[:4]
            headers['Content-Range'] = f'bytes 0-3/{len(self.object_bytes)}'
        elif request.headers.get('Range') == f'bytes={len(self.object_bytes)}-':
            status, body = 416, b''
            if getattr(self, 'object_range_header', True):
                headers['Content-Range'] = f'bytes */{len(self.object_bytes)}'
        return web.Response(status=status, body=body, content_type=self.object_type, headers=headers)

    async def test_original_frontend_deep_links_assets_and_platform_auth(self):
        response = await self.client.get('/dbprog/', allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], '/login')
        response = await self.client.get('/dbprog/api/v1/files')
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())['error'], 'observer_auth_required')
        await self.cloud_login()
        response = await self.client.get('/dbprog', allow_redirects=False)
        self.assertEqual(response.headers['Location'], '/dbprog/')
        for path in ('/dbprog/', f'/dbprog/datasets/{FILE_ID}/history', '/dbprog/system-settings'):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), INDEX)
        response = await self.client.get('/dbprog/assets/app.js')
        self.assertEqual(response.status, 200)
        self.assertIn('javascript', response.content_type)
        for path in ('/dbprog/.env', '/dbprog/assets/app.js.map', '/dbprog/assets/missing.js', '/dbprog/api/internal/config'):
            self.assertEqual((await self.client.get(path)).status, 404)
        response = await self.client.get('/dbprog/api/v1/files')
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())['message'], 'Authentication required')

    async def test_pending_observer_cannot_access_full_app_or_proxy(self):
        response = await self.client.post('/api/register', json={'username': 'pending.full', 'password': 'isolated-pending-password'},
            headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 201)
        response = await self.client.get('/dbprog/', allow_redirects=False)
        self.assertEqual(response.headers['Location'], '/pending')
        response = await self.client.get('/dbprog/api/v1/files')
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())['error'], 'observer_access_required')
        self.assertEqual(self.api_calls, [])

    async def test_login_preserves_original_csrf_contract_and_nonce_bound_secure_cookie(self):
        response = await self.login()
        data = await response.json()
        self.assertEqual(data['csrfToken'], DB_CSRF)
        self.assertEqual(data['user']['role'], 'OPERATOR')
        self.assertNotIn('sessionToken', data)
        self.assertNotIn('cr_session', response.cookies)
        cookie = response.cookies[APP_COOKIE]
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['path'], '/dbprog')
        self.assertEqual(cookie['samesite'], 'Strict')
        response = await self.client.get('/dbprog/api/v1/auth/session')
        self.assertEqual((await response.json())['csrfToken'], DB_CSRF)
        self.assertEqual(self.api_calls[-1]['headers']['Cookie'], 'cr_session=' + DB_TOKEN)
        self.assertNotIn('Authorization', self.api_calls[-1]['headers'])
        await self.cloud_login()
        response = await self.client.get('/dbprog/api/v1/auth/session')
        self.assertEqual(response.status, 401)
        self.assertNotIn('Cookie', self.api_calls[-1]['headers'])

    async def test_original_writes_csrf_role_errors_and_delete_204_are_preserved(self):
        await self.login()
        endpoint = '/dbprog/api/v1/system/settings'
        before = len(self.api_calls)
        response = await self.client.patch(endpoint, json={'values': {'key': 2}})
        self.assertEqual(response.status, 403)
        self.assertEqual(len(self.api_calls), before)
        response = await self.client.patch(endpoint, json={'values': {'key': 2}}, headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())['message'], 'Invalid CSRF token')
        payload = {'values': {'key': 2}, 'metadata': {'sessionToken': 'ordinary-user-metadata'}}
        response = await self.client.patch(endpoint, json=payload,
            headers={'Origin': ORIGIN, 'X-CSRF-Token': DB_CSRF, 'Idempotency-Key': 'fixture-idempotency'})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['received'], payload)
        self.assertEqual(self.api_calls[-1]['headers']['Idempotency-Key'], 'fixture-idempotency')
        self.role = 'VIEWER'
        response = await self.client.post('/dbprog/api/v1/datasets', json={'name': 'fixture'},
            headers={'Origin': ORIGIN, 'X-CSRF-Token': DB_CSRF})
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())['message'], 'Insufficient role')
        self.role = 'OPERATOR'
        response = await self.client.delete('/dbprog/api/v1/datasets/remove', json={'confirmationName': 'fixture'},
            headers={'Origin': ORIGIN, 'X-CSRF-Token': DB_CSRF})
        self.assertEqual(response.status, 204)
        self.assertEqual(await response.read(), b'')
        response = await self.client.head('/dbprog/api/v1/files')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'')

    async def test_binary_upload_larger_than_original_limit_is_exact_and_bounded(self):
        await self.login()
        body = b'\x00' * (8 * 1024 * 1024)
        checksum = hashlib.sha256(body).hexdigest()
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': DB_CSRF, 'Content-Type': 'application/octet-stream',
            'Content-Range': f'bytes 0-{len(body)-1}/{len(body)}', 'X-Part-SHA256': checksum}
        response = await self.client.put('/dbprog/api/v1/uploads/fixture/parts/1', data=body, headers=headers)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.json(), {'bytes': len(body), 'sha256': checksum})
        self.assertEqual(self.api_calls[-1]['body'], body)
        self.assertEqual(self.api_calls[-1]['headers']['Content-Range'], headers['Content-Range'])
        self.assertEqual(self.api_calls[-1]['headers']['X-Part-SHA256'], checksum)

    async def test_chunked_request_limit_is_enforced_without_content_length(self):
        await self.login()
        before = len(self.api_calls)

        async def chunks():
            for _ in range(17):
                yield b'\x00' * (1024 * 1024)

        response = await self.client.put('/dbprog/api/v1/uploads/fixture/parts/1', data=chunks(),
            headers={'Origin': ORIGIN, 'X-CSRF-Token': DB_CSRF, 'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status, 413, await response.text())
        self.assertEqual(len(self.api_calls), before)

    async def test_sse_is_progressive_and_cancellation_closes_private_upstream(self):
        await self.login()
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': DB_CSRF}
        response = await self.client.post('/dbprog/api/ai/chat/stream', json={'message': 'fixture'}, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['X-Accel-Buffering'], 'no')
        self.assertEqual(response.content_type, 'text/event-stream')
        first = await response.content.readline()
        self.assertIn(b'"type":"delta"', first)
        self.assertIn(b'"type":"done"', await response.read())
        self.sse_mode = 'cancel'
        response = await self.client.post('/dbprog/api/ai/chat/stream', json={'message': 'cancel'}, headers=headers)
        await response.content.readline()
        response.close()
        await asyncio.wait_for(self.sse_cancelled.wait(), timeout=2)

    async def test_download_json_rewrites_url_and_previews_keep_mime_and_signed_wire_url(self):
        await self.login()
        response = await self.client.get(f'/dbprog/api/v1/files/{FILE_ID}/download')
        self.assertEqual(await response.json(), {'url': '/dbprog/objects/' + FILE_ID})
        response = await self.client.get('/dbprog/objects/' + FILE_ID)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, 'image/png')
        self.assertEqual(await response.read(), self.object_bytes)
        self.assertNotIn('Content-Disposition', response.headers)
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(self.object_calls[-1]['host'], 'never-resolves.invalid:9000')
        self.assertEqual(self.object_calls[-1]['raw_path'], self.signature_path)
        self.assertNotIn('Cookie', self.object_calls[-1]['headers'])
        self.assertNotIn('Authorization', self.object_calls[-1]['headers'])
        self.object_type, self.object_bytes = 'application/json', b'{"fixture":true}'
        response = await self.client.get('/dbprog/objects/' + FILE_ID)
        self.assertEqual(await response.json(), {'fixture': True})
        response = await self.client.get('/dbprog/objects/' + FILE_ID, headers={'Range': 'bytes=0-3'})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), self.object_bytes[:4])
        self.assertIn('Content-Range', response.headers)
        response = await self.client.head('/dbprog/objects/' + FILE_ID)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'')
        self.assertEqual(response.headers['Content-Length'], str(len(self.object_bytes)))
        self.assertEqual(self.object_calls[-1]['method'], 'GET')
        response = await self.client.head('/dbprog/objects/' + FILE_ID, headers={'Range': 'bytes=0-3'})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b'')
        self.assertEqual(response.headers['Content-Length'], '4')
        self.assertEqual(response.headers['Content-Range'], f'bytes 0-3/{len(self.object_bytes)}')
        self.assertEqual(self.object_calls[-1]['method'], 'GET')

    async def test_active_uploaded_content_forces_attachment_and_logout_clears_correct_scope(self):
        await self.login()
        for mime in ('text/html', 'application/javascript', 'image/svg+xml', 'application/xhtml+xml'):
            self.object_type, self.object_bytes = mime, b'<script>UNTRUSTED_FIXTURE</script>'
            response = await self.client.get('/dbprog/objects/' + FILE_ID)
            self.assertEqual(response.content_type, 'application/octet-stream')
            self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            await response.read()
        response = await self.client.post('/dbprog/api/v1/auth/logout', headers={'Origin': ORIGIN, 'X-CSRF-Token': DB_CSRF})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.cookies[APP_COOKIE]['path'], '/dbprog')
        self.assertEqual(response.cookies[APP_COOKIE]['max-age'], '0')
        response = await self.client.get('/dbprog/api/v1/files')
        self.assertEqual(response.status, 401)

    async def test_unsatisfiable_download_range_preserves_storage_status_and_size(self):
        await self.login()
        size = len(self.object_bytes)
        for method in ('get', 'head'):
            response = await getattr(self.client, method)('/dbprog/objects/' + FILE_ID,
                headers={'Range': f'bytes={size}-'})
            self.assertEqual(response.status, 416, await response.text())
            self.assertEqual(response.headers.get('Content-Range'), f'bytes */{size}')
            self.assertEqual(await response.read(), b'')
            self.assertEqual(self.object_calls[-1]['method'], 'GET')
            self.assertEqual(self.object_calls[-1]['headers']['Range'], f'bytes={size}-')

    async def test_minio_unsatisfiable_range_without_header_uses_authorized_file_size(self):
        await self.login()
        self.object_range_header = False
        for size in (0, len(self.object_bytes)):
            self.metadata_size = str(size)
            for method in ('get', 'head'):
                response = await getattr(self.client, method)('/dbprog/objects/' + FILE_ID,
                    headers={'Range': f'bytes={len(self.object_bytes)}-'})
                self.assertEqual(response.status, 416, await response.text())
                self.assertEqual(response.headers.get('Content-Range'), f'bytes */{size}')
                self.assertEqual(await response.read(), b'')

    async def test_minio_unsatisfiable_range_never_fabricates_an_invalid_metadata_size(self):
        await self.login()
        self.object_range_header = False
        for size in (None, True, False, -1, 20, '', '-1', '+20', '2.0', ' 20', '20 ', '02', '２',
                     '1' * 21, '1' * 1000, '20\r\nX-Injected: yes', {}, []):
            self.metadata_size = size
            response = await self.client.get('/dbprog/objects/' + FILE_ID,
                headers={'Range': f'bytes={len(self.object_bytes)}-'})
            self.assertEqual(response.status, 416, await response.text())
            self.assertNotIn('Content-Range', response.headers)
            self.assertNotIn('X-Injected', response.headers)
            await response.read()

    async def test_storage_content_range_takes_precedence_over_metadata_size(self):
        await self.login()
        self.metadata_size = '12345'
        response = await self.client.get('/dbprog/objects/' + FILE_ID,
            headers={'Range': f'bytes={len(self.object_bytes)}-'})
        self.assertEqual(response.status, 416, await response.text())
        self.assertEqual(response.headers.get('Content-Range'), f'bytes */{len(self.object_bytes)}')
        await response.read()

    async def test_health_version_csv_and_redirect_boundaries(self):
        await self.login()
        for path in ('/dbprog/nest/health', '/dbprog/nest/ready', '/dbprog/nest/version', '/dbprog/version.json'):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertIsInstance(await response.json(), dict)
        response = await self.client.get('/dbprog/api/v1/audit-logs/export')
        self.assertEqual(response.content_type, 'text/csv')
        self.assertEqual(await response.text(), 'actor,action\nfixture,read\n')
        before = len(self.api_calls)
        response = await self.client.get('/dbprog/api/v1/redirect')
        self.assertEqual(response.status, 502)
        self.assertEqual(len(self.api_calls), before + 1)
        self.assertNotIn('never-resolves.invalid', await response.text())
