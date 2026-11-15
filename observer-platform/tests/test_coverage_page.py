"""Coverage editor preserves approval and static asset boundaries."""
from pathlib import Path
import tempfile
import unittest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from cloud.backend.app import ACCOUNTS, create_app
from tests.test_backend import ORIGIN, PASSWORD, config

class CoveragePageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='coverage-page-')
        frontend = Path(self.temp.name) / 'frontend'
        assets = frontend / 'coverage' / 'assets'
        assets.mkdir(parents=True)
        (frontend / 'coverage' / 'index.html').write_text('<h1>传感器覆盖与盲区</h1>')
        (assets / 'index-abc123.js').write_text('export const fixture = true;')
        (assets / 'index-abc123.css').write_text('body{color:white}')
        (frontend / 'private.py').write_text('NEVER EXPOSE')
        self.app = create_app(config(self.temp.name, frontend_dir=str(frontend)))
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post('/api/login', json={'username':'viewer','password':PASSWORD}, headers={'Origin':ORIGIN})
        self.assertEqual(result.status,200)

    async def test_anonymous_page_and_asset_denied(self):
        for path in ('/coverage','/coverage/'):
            result = await self.client.get(path,allow_redirects=False)
            self.assertEqual(result.status,302)
            self.assertEqual(result.headers['Location'],'/login')
        self.assertEqual((await self.client.get('/coverage-assets/assets/index-abc123.js')).status,401)

    async def test_pending_denied_then_approved_existing_session_works(self):
        result = await self.client.post('/api/register',json={'username':'coverage.viewer','password':PASSWORD},headers={'Origin':ORIGIN})
        self.assertEqual(result.status,201)
        result = await self.client.get('/coverage',allow_redirects=False)
        self.assertEqual(result.headers['Location'],'/pending')
        self.assertEqual((await self.client.get('/coverage-assets/assets/index-abc123.js')).status,403)
        user=self.app[ACCOUNTS].find('coverage.viewer')
        self.app[ACCOUNTS].approve(user['id'],'viewer')
        self.assertEqual((await self.client.get('/coverage')).status,200)
        self.assertEqual((await self.client.get('/coverage-assets/assets/index-abc123.js')).status,200)

    async def test_approved_page_types_and_csp(self):
        await self.login()
        result=await self.client.get('/coverage')
        self.assertEqual(result.status,200)
        self.assertIn('传感器覆盖与盲区',await result.text())
        self.assertEqual(result.content_type,'text/html')
        self.assertIn("script-src 'self'",result.headers['Content-Security-Policy'])
        for ext,mime in (('js','application/javascript'),('css','text/css')):
            asset=await self.client.get('/coverage-assets/assets/index-abc123.'+ext)
            self.assertEqual(asset.status,200)
            self.assertEqual(asset.content_type,mime)
            self.assertEqual(asset.headers['X-Content-Type-Options'],'nosniff')

    async def test_assets_cannot_expose_other_paths(self):
        await self.login()
        for path in ('/coverage-assets/assets/private.py','/coverage-assets/index.html',
                     '/coverage-assets/assets/index-abc123.js.map','/coverage-assets/assets/missing.js',
                     '/coverage-assets/assets/%2e%2e%2fprivate.py'):
            result=await self.client.get(path)
            self.assertEqual(result.status,404,path)
            self.assertNotIn('NEVER EXPOSE',await result.text())

    async def test_gzip_negotiation(self):
        await self.login()
        for encoding,expected in (('gzip','gzip'),('gzip;q=0',None),('identity',None)):
            result=await self.client.get('/coverage-assets/assets/index-abc123.js',headers={'Accept-Encoding':encoding})
            self.assertEqual(result.status,200)
            self.assertEqual(result.headers.get('Content-Encoding'),expected)
            self.assertEqual(result.headers['Vary'],'Accept-Encoding')
            self.assertEqual(await result.text(),'export const fixture = true;')
