"""Warehouse upload limits and WASM permission stay scoped to its mount."""
from pathlib import Path
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cloud.backend.app import CONFIG, LIMITER, security_headers, transport_and_limits
from cloud.backend.security import RateLimiter
from tests.test_backend import config


class MountLimitsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='warehouse-limits-')
        app = web.Application(client_max_size=1024 * 1024,
                              middlewares=[security_headers, transport_and_limits])
        app[CONFIG] = config(self.directory.name)
        app[LIMITER] = RateLimiter()

        async def read(request):
            data = await request.read()
            return web.json_response({'bytes': len(data)})

        async def document(_request):
            return web.Response(text='isolated document')

        app.add_routes([web.put('/dbprog/api/upload-fixture', read),
                        web.put('/observer-fixture', read),
                        web.get('/dbprog/', document), web.get('/observer', document)])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_full_eight_megabyte_upload_chunk_is_accepted(self):
        size = 8 * 1024 * 1024
        response = await self.client.put('/dbprog/api/upload-fixture', data=b'x' * size)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())['bytes'], size)

    async def test_existing_observer_limit_and_warehouse_upper_bound_remain_enforced(self):
        response = await self.client.put('/observer-fixture', data=b'x' * (2 * 1024 * 1024))
        self.assertEqual(response.status, 413)
        response = await self.client.put('/dbprog/api/upload-fixture', data=b'x' * (16 * 1024 * 1024 + 1))
        self.assertEqual(response.status, 413)

    async def test_wasm_permission_is_limited_to_warehouse(self):
        warehouse = await self.client.get('/dbprog/')
        observer = await self.client.get('/observer')
        self.assertIn("'wasm-unsafe-eval'", warehouse.headers['Content-Security-Policy'])
        self.assertNotIn('wasm-unsafe-eval', observer.headers['Content-Security-Policy'])
        for response in (warehouse, observer):
            self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])


def test_nginx_warehouse_streaming_location_preserves_global_observer_limit():
    text = (Path(__file__).parents[1] / 'deploy/nginx/https.conf.template').read_text()
    assert 'client_max_body_size 1m;' in text
    block = text.split('location ^~ /dbprog/api/ {', 1)[1].split('}', 1)[0]
    for directive in ('client_max_body_size 16m;', 'proxy_request_buffering off;',
                      'proxy_buffering off;', 'proxy_read_timeout 180s;', 'proxy_send_timeout 180s;'):
        assert directive in block
