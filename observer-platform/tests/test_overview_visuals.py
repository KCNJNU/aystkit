"""The overview returns a small authenticated preview without renewing real data."""
import gzip
import json
import time
import unittest

from cloud.backend.app import STORE
from tests import test_backend as fixture


class OverviewVisualTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.BackendTests.asyncSetUp
    asyncTearDown = fixture.BackendTests.asyncTearDown
    login = fixture.BackendTests.login
    ingest = fixture.BackendTests.ingest

    async def upload(self, key='lidar', age=0, count=4000, sequence=1, metadata=None):
        now = time.time()
        obj = fixture.envelope('media', timestamp=now)
        if not hasattr(self, 'upload_session'):
            self.upload_session = obj['session_id']
        obj['session_id'] = self.upload_session
        obj['sequence'] = sequence
        obj['topic'] = '/fixture/' + key
        points = [[index * .123456789, index * -.0123456789, index % 23 * .04321]
                  for index in range(count)]
        obj['payload'] = {'key': key, 'kind': 'cloud', 'status': 'AVAILABLE', 'ageS': age,
                          'points': points, 'totalPoints': 90000, 'frameId': key + '_frame'}
        obj['payload'].update(metadata or {})
        self.assertEqual((await self.ingest(obj)).status, 200)
        return points

    async def test_authentication_and_pending_accounts_are_not_bypassed(self):
        self.assertEqual((await self.client.get('/visuals.json?group=overview')).status, 401)
        result = await self.client.post('/api/register', json={'username': 'overview.pending', 'password': fixture.PASSWORD},
                                        headers={'Origin': fixture.ORIGIN})
        self.assertEqual(result.status, 201)
        self.assertEqual((await self.client.get('/visuals.json?group=overview')).status, 403)

    async def test_one_sampled_source_preserves_coordinates_and_full_endpoint(self):
        original = await self.upload()
        await self.login()
        response = await self.client.get('/visuals.json?group=overview', headers={'Accept-Encoding': 'identity'})
        body = await response.read()
        self.assertLessEqual(len(body), 48 * 1024)
        value = json.loads(body)
        self.assertEqual(set(value), {'lidar'})
        item = value['lidar']
        self.assertLessEqual(len(item['points']), 768)
        self.assertEqual(item['points'][0], original[0])
        self.assertEqual(item['points'][-1], original[-1])
        self.assertTrue(all(point in original for point in item['points']))
        self.assertEqual(item['frameId'], 'lidar_frame')
        self.assertEqual(item['totalPoints'], 90000)
        self.assertEqual(item['previewInputPoints'], 4000)
        full = await (await self.client.get('/visuals.json')).json()
        self.assertEqual(full['lidar']['points'], original)
        self.assertIn('map', full)

    async def test_preferred_cloud_is_stable_across_publish_phases_then_scan_fallback(self):
        await self.upload('bluesea', sequence=1)
        await self.upload('lidar', age=.8, sequence=2)
        await self.upload('registered', age=.1, sequence=3)
        await self.login()
        value = await (await self.client.get('/visuals.json?group=overview')).json()
        self.assertEqual(set(value), {'lidar'})
        self.assertEqual(value['lidar']['frameId'], 'lidar_frame')
        self.app[STORE].media.pop('registered')
        self.app[STORE].media.pop('lidar')
        fallback = await (await self.client.get('/visuals.json?group=overview')).json()
        self.assertEqual(set(fallback), {'bluesea'})
        self.assertEqual(fallback['bluesea']['frameId'], 'bluesea_frame')

    async def test_gzip_is_negotiated_and_age_is_not_renewed_by_reads(self):
        await self.upload()
        await self.login()
        first = await (await self.client.get('/visuals.json?group=overview')).json()
        now = self.app[STORE].clock()
        self.app[STORE].clock = lambda: now + .4
        result = await self.client.get('/visuals.json?group=overview', headers={'Accept-Encoding': 'gzip'}, auto_decompress=False)
        self.assertEqual(result.headers['Content-Encoding'], 'gzip')
        self.assertIn('Accept-Encoding', result.headers['Vary'])
        decoded = gzip.decompress(await result.read())
        self.assertLessEqual(len(decoded), 48 * 1024)
        self.assertGreaterEqual(json.loads(decoded)['lidar']['ageS'], first['lidar']['ageS'] + .3)
        plain = await self.client.get('/visuals.json?group=overview', headers={'Accept-Encoding': 'gzip;q=0, identity'})
        self.assertNotIn('Content-Encoding', plain.headers)

    async def test_stale_and_missing_sources_do_not_expose_old_points(self):
        await self.login()
        waiting = await (await self.client.get('/visuals.json?group=overview')).json()
        self.assertTrue(all('points' not in item for item in waiting.values()))
        await self.upload()
        now = self.app[STORE].clock()
        self.app[STORE].clock = lambda: now + 3
        stale = await (await self.client.get('/visuals.json?group=overview')).json()
        self.assertEqual(stale['lidar']['status'], 'STALE')
        self.assertTrue(all('points' not in item for item in stale.values()))

    async def test_nested_metadata_cannot_bypass_the_preview_byte_limit(self):
        await self.upload(count=1, metadata={'source': {'notes': ['x' * 4096] * 128}, 'label': '相' * 5000})
        await self.login()
        response = await self.client.get('/visuals.json?group=overview', headers={'Accept-Encoding': 'identity'})
        body = await response.read()
        self.assertLessEqual(len(body), 48 * 1024)
        item = json.loads(body)['lidar']
        self.assertNotIn('source', item)
        self.assertEqual(len(item['label']), 256)
        self.assertEqual(len(item['points']), 1)
