"""HTTP byte savings preserve observed values, identity, freshness and authorization."""
import base64
import gzip
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from cloud.backend.app import ACCOUNTS, STORE, create_app
from cloud.backend.store import VISUAL_GROUPS
from shared.protocol import CLOUD_KEYS, MAP_KEYS
from tests.test_backend import ORIGIN, PASSWORD, PASSWORD_HASH, TOKEN, config
from tests.test_protocol import envelope, jpeg, png


class ObservationBandwidthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-observation-bandwidth-')
        with patch.dict(os.environ, {'NATIVE_VIDEO_SOCKET': ''}):
            self.app = create_app(config(self.temp.name))
        self.clock = [time.time()]
        self.app[STORE].clock = lambda: self.clock[0]
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.session = None
        self.sequence = 0

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def login(self, username='viewer', headers=None):
        response = await self.client.post('/api/login',
            json={'username': username, 'password': PASSWORD},
            headers={'Origin': ORIGIN, **(headers or {})}, auto_decompress=False)
        self.assertEqual(response.status, 200)
        return response

    async def ingest(self, kind='media', payload=None):
        self.sequence += 1
        obj = envelope(kind, sequence=self.sequence, timestamp=self.clock[0], session=self.session)
        self.session = obj['session_id']
        if payload is not None:
            obj['payload'] = payload
        result = await self.client.post('/api/v1/ingest', json=obj,
                                        headers={'Authorization': 'Bearer ' + TOKEN})
        self.assertEqual(result.status, 200, await result.text())
        return obj

    async def cloud(self, key='lidar'):
        return await self.ingest(payload={'key': key, 'kind': 'cloud', 'status': 'AVAILABLE',
            'ageS': 0, 'points': [[index / 100, 0.125, -0.25] for index in range(1500)],
            'totalPoints': 1500, 'frameId': 'isolated_fixture'})

    async def test_gzip_roundtrip_is_exact_and_larger_preview_saves_bytes(self):
        await self.cloud()
        await self.login()
        for path in ('/snapshot.json', '/visuals.json', '/visuals.json?group=clouds'):
            with self.subTest(path=path):
                original = await self.client.get(path, headers={'Accept-Encoding': 'identity'}, auto_decompress=False)
                plain = await original.read()
                compressed = await self.client.get(path, headers={'Accept-Encoding': 'gzip'}, auto_decompress=False)
                wire = await compressed.read()
                self.assertEqual(compressed.status, 200)
                self.assertEqual(compressed.headers['Content-Encoding'], 'gzip')
                self.assertEqual(compressed.headers['Vary'], 'Accept-Encoding')
                self.assertEqual(compressed.headers['Cache-Control'], 'no-store')
                self.assertEqual(len(wire), compressed.content_length)
                self.assertEqual(gzip.decompress(wire), plain)
                self.assertEqual(json.loads(gzip.decompress(wire)), json.loads(plain))
                if path.startswith('/visuals.json'):
                    self.assertLess(len(wire), len(plain) / 3)

    async def test_qzero_malformed_and_identity_responses_are_not_compressed(self):
        await self.login()
        for encoding in ('identity', 'gzip;q=0', 'gzip;q=0.000', 'br,gzip;q=0',
                         'xgzip', 'gzip;q=NaN', 'gzip;q=1.001', 'gzip,gzip'):
            with self.subTest(encoding=encoding):
                result = await self.client.get('/visuals.json?group=scans',
                    headers={'Accept-Encoding': encoding}, auto_decompress=False)
                self.assertEqual(result.status, 200)
                self.assertNotIn('Content-Encoding', result.headers)
                self.assertEqual(result.headers['Vary'], 'Accept-Encoding')
                self.assertEqual(set(await result.json()), VISUAL_GROUPS['scans'])
        for encoding in ('GZip;Q=0.5', 'br, gzip;q=0.001', 'gzip;q=1.000'):
            result = await self.client.get('/snapshot.json', headers={'Accept-Encoding': encoding}, auto_decompress=False)
            self.assertEqual(result.headers['Content-Encoding'], 'gzip')
            self.assertIsInstance(json.loads(gzip.decompress(await result.read())), dict)

    async def test_permissions_precede_group_validation_and_no_auth_error_compression(self):
        paths = ('/snapshot.json', '/visuals.json?group=scans', '/visuals.json?group=unknown',
                 '/visuals.json?group=scans&group=maps', '/native-camera/status')
        for path in paths:
            response = await self.client.get(path, headers={'Accept-Encoding': 'gzip'}, auto_decompress=False)
            self.assertEqual(response.status, 401)
            self.assertNotIn('Content-Encoding', response.headers)
        self.app[ACCOUNTS].register('pending_user', PASSWORD_HASH)
        await self.login('pending_user')
        for path in paths:
            response = await self.client.get(path, headers={'Accept-Encoding': 'gzip'}, auto_decompress=False)
            self.assertEqual(response.status, 403)
            self.assertNotIn('Content-Encoding', response.headers)

    async def test_visual_groups_contain_only_requested_data_without_mutating_store(self):
        await self.cloud('lidar')
        await self.cloud('bluesea')
        await self.login()
        for group, expected in VISUAL_GROUPS.items():
            response = await self.client.get('/visuals.json?group=' + group)
            self.assertEqual(response.status, 200)
            value = await response.json()
            self.assertEqual(set(value), expected)
            self.assertEqual(value, self.app[STORE].visuals(keys=expected))
            if group == 'scans':
                self.assertEqual(value['bluesea']['points'][10], [0.1, 0.125, -0.25])
                self.assertNotIn('lidar', value)
        full = await (await self.client.get('/visuals.json')).json()
        self.assertEqual(set(full), MAP_KEYS | CLOUD_KEYS)
        self.assertEqual(len(full['lidar']['points']), 1500)

    async def test_unknown_and_duplicate_visual_parameters_rejected_after_authorization(self):
        await self.login()
        for query in ('group=unknown', 'group=', 'group=scans&group=scans', 'group=scans&group=maps',
                      'group=Scans', 'group=%20scans', 'keys=lidar', 'group=clouds&url=http://unsafe',
                      'extra=1', 'Group=maps'):
            with self.subTest(query=query):
                response = await self.client.get('/visuals.json?' + query,
                    headers={'Accept-Encoding': 'gzip'}, auto_decompress=False)
                self.assertEqual(response.status, 400)
                self.assertNotIn('Content-Encoding', response.headers)
        with self.assertRaises(ValueError):
            self.app[STORE].visuals(keys={'untrusted'})

    async def test_sensitive_account_endpoints_and_binary_images_never_compress(self):
        result = await self.login(headers={'Accept-Encoding': 'gzip'})
        self.assertIn('Set-Cookie', result.headers)
        self.assertNotIn('Content-Encoding', result.headers)
        for path in ('/api/session', '/api/admin/users', '/api/robots', '/healthz'):
            response = await self.client.get(path, headers={'Accept-Encoding': 'gzip'}, auto_decompress=False)
            self.assertEqual(response.status, 200)
            self.assertNotIn('Content-Encoding', response.headers)
        await self.ingest()
        await self.ingest(payload={'key': 'usbFront', 'kind': 'image', 'status': 'AVAILABLE',
            'ageS': 0, 'data': base64.b64encode(jpeg()).decode(), 'mime_type': 'image/jpeg'})
        for path, expected in (('/visual/map.png', png()), ('/camera/usbFront.jpg', jpeg())):
            response = await self.client.get(path, headers={'Accept-Encoding': 'gzip'}, auto_decompress=False)
            self.assertEqual(await response.read(), expected)
            self.assertNotIn('Content-Encoding', response.headers)
        head = await self.client.head('/snapshot.json', headers={'Accept-Encoding': 'gzip'}, auto_decompress=False)
        self.assertEqual(head.status, 200)
        self.assertEqual(await head.read(), b'')
        self.assertNotIn('Content-Encoding', head.headers)

    async def test_map_conditional_bytes_identity_and_age_without_ttl_extension(self):
        await self.ingest()
        await self.login()
        response = await self.client.get('/visual/map.png')
        self.assertEqual(await response.read(), png())
        tag = response.headers['ETag']
        self.assertRegex(tag, '^"[0-9a-f]{64}"$')
        identity = response.headers['X-Frame-Sequence']
        self.assertEqual(response.headers['X-Frame-TTL-S'], 'event')
        self.clock[0] += 1
        response = await self.client.get('/visual/map.png', headers={'If-None-Match': tag})
        self.assertEqual(response.status, 304)
        self.assertEqual(await response.read(), b'')
        self.assertEqual(response.headers['X-Frame-Sequence'], identity)
        self.assertEqual(response.headers['ETag'], tag)
        self.assertEqual(float(response.headers['X-Frame-Age-S']), 1.0)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        # Static maps are event data: a source heartbeat keeps the map valid,
        # without inventing a new map frame or resetting its age.
        self.clock[0] += 7
        await self.ingest('heartbeat')
        response = await self.client.get('/visual/map.png', headers={'If-None-Match': tag})
        self.assertEqual(response.status, 304)
        self.assertEqual(response.headers['X-Frame-Sequence'], identity)
        self.assertEqual(float(response.headers['X-Frame-Age-S']), 8.0)
        self.clock[0] += 11
        response = await self.client.get('/visual/map.png', headers={'If-None-Match': tag})
        self.assertEqual(response.status, 204)
        self.assertNotIn('ETag', response.headers)

    async def test_map_conditional_authorization_and_costmap_staleness(self):
        obj = await self.ingest()
        guessed = '"' + 'a' * 64 + '"'
        anonymous = await self.client.get('/visual/map.png', headers={'If-None-Match': guessed})
        self.assertEqual(anonymous.status, 401)
        self.assertNotIn('ETag', anonymous.headers)
        await self.login()
        response = await self.client.get('/visual/map.png')
        tag = response.headers['ETag'];await response.read()
        obj['payload']['key'] = 'localCostmap'
        await self.ingest(payload=obj['payload'])
        costmap = await self.client.get('/visual/localCostmap.png', headers={'If-None-Match': tag})
        self.assertEqual(costmap.status, 200)
        self.assertNotEqual(costmap.headers['ETag'], tag)
        self.assertEqual(costmap.headers['X-Frame-TTL-S'], '5.0')
        costmap_tag = costmap.headers['ETag'];await costmap.read()
        self.clock[0] += 5.1
        stale = await self.client.get('/visual/localCostmap.png', headers={'If-None-Match': costmap_tag})
        self.assertEqual(stale.status, 204)
        self.assertNotIn('ETag', stale.headers)
        await self.client.post('/api/logout', headers={'Origin': ORIGIN})
        denied = await self.client.get('/visual/map.png', headers={'If-None-Match': tag})
        self.assertEqual(denied.status, 401)
        self.assertNotIn('ETag', denied.headers)
