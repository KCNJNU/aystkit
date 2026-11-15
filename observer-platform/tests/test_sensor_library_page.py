"""Authenticated sensor documents remain inside their dedicated static tree."""
from pathlib import Path
import tempfile
import unittest
from urllib.parse import quote

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from cloud.backend.app import ACCOUNTS, create_app
from tests.test_backend import ORIGIN, PASSWORD, TOKEN, config


class SensorLibraryPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sensor-library-page-')
        self.frontend = Path(self.temp.name) / 'frontend'
        self.library = self.frontend / 'sensor-library'
        self.library.mkdir(parents=True)
        self.pdf_name = 'documents/原厂规格/相机 规格（中文）.pdf'
        self.pdf = b'%PDF-1.7\nISOLATED SENSOR DOCUMENT\n'
        self.files = {
            'index.html': '<h1>传感器资料</h1>'.encode(),
            'app.js': b'const fixture = true;',
            'style.css': b'body { color: white; }',
            'data.json': b'{"isolated":true}',
            'assets/structure.png': b'\x89PNG\r\n\x1a\nfixture',
            'documents/原厂规格/README.MD': '原始规格说明'.encode(),
            'documents/驱动/help.txt': b'isolated driver help',
            'documents/全部规格.zip': b'PK\x03\x04ISOLATED ARCHIVE',
            'chapters/note.html': b'<p>nested reference</p>',
            self.pdf_name: self.pdf,
        }
        for name, content in self.files.items():
            path = self.library / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.library / '.secret.pdf').write_bytes(b'NEVER EXPOSE')
        (self.library / '.hidden').mkdir()
        (self.library / '.hidden' / 'index.html').write_bytes(b'NEVER EXPOSE')
        (self.library / 'private.py').write_bytes(b'NEVER EXPOSE')
        (self.frontend / 'private.pdf').write_bytes(b'NEVER EXPOSE')
        self.outside = Path(self.temp.name) / 'outside'
        self.outside.mkdir()
        (self.outside / 'secret.pdf').write_bytes(b'NEVER EXPOSE')
        (self.outside / 'index.html').write_bytes(b'NEVER EXPOSE')
        (self.library / 'file-link.pdf').symlink_to(self.outside / 'secret.pdf')
        (self.library / 'directory-link').symlink_to(self.outside, target_is_directory=True)
        (self.library / 'inside-link.pdf').symlink_to(self.library / self.pdf_name)
        coverage = self.frontend / 'coverage'
        (coverage / 'assets').mkdir(parents=True)
        (coverage / 'index.html').write_text('<h1>Existing coverage</h1>')
        (coverage / 'assets' / 'index-abc123.js').write_text('const coverage = true;')
        self.app = create_app(config(self.temp.name, frontend_dir=str(self.frontend)))
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post('/api/login',
            json={'username': 'viewer', 'password': PASSWORD}, headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 200)

    def assert_security_headers(self, result):
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        self.assertEqual(result.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(result.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(result.headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("script-src 'self';", result.headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-eval', result.headers['Content-Security-Policy'])

    async def test_anonymous_pages_redirect_and_all_asset_types_require_session(self):
        for path in ('/sensor-library', '/sensor-library/', '/sensor-library/index.html'):
            result = await self.client.get(path, allow_redirects=False)
            self.assertEqual(result.status, 302, path)
            self.assertEqual(result.headers['Location'], '/login')
            self.assert_security_headers(result)
        for name in self.files:
            if name == 'index.html':
                continue
            result = await self.client.get('/sensor-library/' + quote(name),
                headers={'Authorization': 'Bearer ' + TOKEN}, allow_redirects=False)
            self.assertEqual(result.status, 401, name)
            self.assert_security_headers(result)

    async def test_pending_assets_forbidden_then_approval_reuses_existing_session(self):
        result = await self.client.post('/api/register',
            json={'username': 'sensor.viewer', 'password': PASSWORD}, headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 201)
        for path in ('/sensor-library', '/sensor-library/', '/sensor-library/index.html'):
            result = await self.client.get(path, allow_redirects=False)
            self.assertEqual(result.status, 302)
            self.assertEqual(result.headers['Location'], '/pending')
        for name in self.files:
            if name != 'index.html':
                result = await self.client.get('/sensor-library/' + quote(name), allow_redirects=False)
                self.assertEqual(result.status, 403, name)
        user = self.app[ACCOUNTS].find('sensor.viewer')
        self.app[ACCOUNTS].approve(user['id'], 'viewer')
        self.assertEqual((await self.client.get('/sensor-library/')).status, 200)
        self.assertEqual((await self.client.get('/sensor-library/' + quote(self.pdf_name))).status, 200)
        self.app[ACCOUNTS].delete(user['id'], 'viewer')
        self.assertEqual((await self.client.get('/sensor-library/app.js')).status, 401)

    async def test_slash_redirect_retains_robot_selection_and_index_works(self):
        await self.login()
        result = await self.client.get('/sensor-library?robot=robot-two', allow_redirects=False)
        self.assertEqual(result.status, 302)
        self.assertEqual(result.headers['Location'], '/sensor-library/?robot=robot-two')
        for path in ('/sensor-library/', '/sensor-library/index.html'):
            result = await self.client.get(path)
            self.assertEqual(result.status, 200)
            self.assertEqual(result.content_type, 'text/html')
            self.assertEqual(await result.read(), self.files['index.html'])
            self.assert_security_headers(result)

    async def test_nested_assets_chinese_documents_and_uppercase_extension(self):
        await self.login()
        types = {'app.js': 'application/javascript', 'style.css': 'text/css',
            'data.json': 'application/json', 'assets/structure.png': 'image/png',
            'documents/原厂规格/README.MD': 'text/plain', 'documents/驱动/help.txt': 'text/plain',
            'documents/全部规格.zip': 'application/zip', self.pdf_name: 'application/pdf',
            'chapters/note.html': 'text/html'}
        for name, mime in types.items():
            result = await self.client.get('/sensor-library/' + quote(name))
            self.assertEqual(result.status, 200, name)
            self.assertEqual(result.content_type, mime, name)
            self.assertEqual(await result.read(), self.files[name], name)
            self.assert_security_headers(result)

    async def test_downloads_keep_original_chinese_names_in_rfc5987(self):
        await self.login()
        for name in (self.pdf_name, 'documents/全部规格.zip'):
            result = await self.client.get('/sensor-library/' + quote(name))
            disposition = result.headers['Content-Disposition']
            self.assertTrue(disposition.startswith('attachment; filename="document.'))
            self.assertIn("filename*=UTF-8''" + quote(Path(name).name, safe=''), disposition)
            self.assertNotIn('\r', disposition)
            self.assertNotIn('\n', disposition)

    async def test_missing_hidden_unsupported_and_encoded_traversal_are_not_served(self):
        await self.login()
        paths = ('missing.pdf', 'documents/', '.secret.pdf', '.hidden/index.html',
            'private.py', 'app.js.map', '../private.pdf', '%2e%2e%2fprivate.pdf',
            'documents/%2e%2e/%2e%2e/private.pdf', '%252e%252e%252fprivate.pdf',
            'documents/%5c..%5cprivate.pdf', '%2fprivate.pdf', 'documents//help.txt',
            'documents/%00secret.pdf', 'documents/%0asecret.pdf')
        for path in paths:
            result = await self.client.get(URL('/sensor-library/' + path, encoded=True),
                allow_redirects=False)
            self.assertEqual(result.status, 404, path)
            self.assertNotIn(b'NEVER EXPOSE', await result.read(), path)
            self.assert_security_headers(result)

    async def test_file_and_directory_symlinks_are_rejected(self):
        await self.login()
        for path in ('file-link.pdf', 'directory-link/secret.pdf', 'directory-link/index.html',
                     'inside-link.pdf'):
            result = await self.client.get('/sensor-library/' + path)
            self.assertEqual(result.status, 404, path)
            self.assertNotIn(b'NEVER EXPOSE', await result.read())

    async def test_library_root_symlink_is_rejected(self):
        await self.login()
        parked = self.frontend / 'parked-library'
        self.library.rename(parked)
        self.library.symlink_to(self.outside, target_is_directory=True)
        result = await self.client.get('/sensor-library/')
        self.assertEqual(result.status, 404)
        self.assertNotIn(b'NEVER EXPOSE', await result.read())

    async def test_head_and_range_preserve_authentication_and_security_headers(self):
        result = await self.client.head('/sensor-library/' + quote(self.pdf_name))
        self.assertEqual(result.status, 401)
        self.assertEqual(await result.read(), b'')
        await self.login()
        result = await self.client.head('/sensor-library/' + quote(self.pdf_name))
        self.assertEqual(result.status, 200)
        self.assertEqual(result.content_length, len(self.pdf))
        self.assertEqual(await result.read(), b'')
        self.assert_security_headers(result)
        result = await self.client.get('/sensor-library/' + quote(self.pdf_name),
            headers={'Range': 'bytes=0-7'})
        self.assertEqual(result.status, 206)
        self.assertEqual(await result.read(), self.pdf[:8])
        self.assertEqual(result.headers['Content-Range'], f'bytes 0-7/{len(self.pdf)}')
        self.assert_security_headers(result)
        result = await self.client.get('/sensor-library/' + quote(self.pdf_name),
            headers={'Range': 'bytes=99999-'})
        self.assertEqual(result.status, 416)
        self.assert_security_headers(result)

    async def test_existing_coverage_page_and_asset_are_unchanged(self):
        await self.login()
        library = await self.client.get('/sensor-library/')
        coverage = await self.client.get('/coverage')
        self.assertEqual(coverage.status, 200)
        self.assertIn('Existing coverage', await coverage.text())
        self.assertEqual(library.headers['Content-Security-Policy'],
                         coverage.headers['Content-Security-Policy'])
        asset = await self.client.get('/coverage-assets/assets/index-abc123.js')
        self.assertEqual(asset.status, 200)
        self.assertEqual(await asset.text(), 'const coverage = true;')


if __name__ == '__main__':
    unittest.main()
