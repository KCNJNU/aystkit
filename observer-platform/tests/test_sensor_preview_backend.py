"""Bounded sensor previews preserve real frame age, source and permissions."""
import gzip
import json
import time
import unittest
import uuid

from cloud.backend.app import STORE
from cloud.backend.store import VISUAL_GROUPS
from shared.protocol import CLOUD_KEYS, MAP_KEYS
from tests import test_backend as fixture


class SensorPreviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.BackendTests.asyncSetUp
    asyncTearDown = fixture.BackendTests.asyncTearDown
    login = fixture.BackendTests.login
    ingest = fixture.BackendTests.ingest

    async def upload(self, key='lidar', *, age=.15, frame=7, sequence=None, timestamp=None):
        if not hasattr(self, 'clock'):
            self.clock = [time.time()]
            self.app[STORE].clock = lambda: self.clock[0]
            self.session = str(uuid.uuid4())
            self.sequence = 0
        self.sequence = self.sequence + 1 if sequence is None else sequence
        obj = fixture.envelope('media', timestamp=self.clock[0] if timestamp is None else timestamp,
                               sequence=self.sequence, session=self.session)
        obj['payload'] = {'key':key, 'kind':'cloud', 'status':'AVAILABLE', 'ageS':age,
            'points':[[i / 100, .125, -.25] for i in range(1500)],
            'totalPoints':90000, 'frameId':'sensor_fixture', 'frameSeq':frame}
        self.assertEqual((await self.ingest(obj)).status, 200)

    async def test_groups_filter_before_copy_and_keep_legacy_full_response(self):
        await self.upload('lidar')
        await self.upload('bluesea')
        await self.login()
        for group, keys in VISUAL_GROUPS.items():
            response = await self.client.get('/visuals.json?group=' + group)
            self.assertEqual(response.status, 200)
            value = await response.json()
            self.assertEqual(set(value), keys)
            self.assertEqual(value, self.app[STORE].visuals(keys=keys))
        full = await (await self.client.get('/visuals.json')).json()
        self.assertEqual(set(full), CLOUD_KEYS | MAP_KEYS)
        self.assertEqual(len(full['lidar']['points']), 1500)
        with self.assertRaises(ValueError):
            self.app[STORE].visuals(keys={'untrusted'})

    async def test_authorization_precedes_query_and_conditional_processing(self):
        for path in ('/visuals.json?group=clouds', '/visuals.json?group=unknown',
                     '/visuals.json?group=scans&group=maps', '/snapshot.json'):
            response = await self.client.get(path, headers={'Accept-Encoding':'gzip', 'If-None-Match':'*'})
            self.assertEqual(response.status, 401)
            self.assertNotIn('Content-Encoding', response.headers)
            self.assertNotIn('ETag', response.headers)
        response = await self.client.post('/api/register',
            json={'username':'pending.sensor', 'password':fixture.PASSWORD},
            headers={'Origin':fixture.ORIGIN})
        self.assertEqual(response.status, 201)
        for path in ('/visuals.json?group=clouds', '/visuals.json?group=unknown', '/snapshot.json'):
            response = await self.client.get(path, headers={'Accept-Encoding':'gzip'})
            self.assertEqual(response.status, 403)
            self.assertNotIn('Content-Encoding', response.headers)

    async def test_invalid_group_queries_rejected_without_compression(self):
        await self.login()
        for query in ('group=', 'group=unknown', 'group=scans&group=scans', 'group=Scans',
                      'group=scans&group=maps', 'keys=lidar', 'group=maps&extra=1'):
            response = await self.client.get('/visuals.json?' + query, headers={'Accept-Encoding':'gzip'})
            self.assertEqual(response.status, 400, query)
            self.assertNotIn('Content-Encoding', response.headers)

    async def test_gzip_is_exact_negotiated_and_reduces_wire_bytes(self):
        await self.upload()
        await self.login()
        for path in ('/snapshot.json', '/visuals.json', '/visuals.json?group=clouds', '/visuals.json?group=overview'):
            plain = await self.client.get(path, headers={'Accept-Encoding':'identity'}, auto_decompress=False)
            raw = await plain.read()
            zipped = await self.client.get(path, headers={'Accept-Encoding':'gzip'}, auto_decompress=False)
            wire = await zipped.read()
            self.assertEqual(zipped.headers['Content-Encoding'], 'gzip')
            self.assertEqual(zipped.headers['Vary'], 'Accept-Encoding')
            self.assertEqual(zipped.headers['Cache-Control'], 'no-store')
            self.assertEqual(gzip.decompress(wire), raw)
            if path.startswith('/visuals.json'):
                self.assertLess(len(wire), len(raw) / 3)
        head = await self.client.head('/visuals.json?group=clouds', headers={'Accept-Encoding':'gzip'})
        self.assertNotIn('Content-Encoding', head.headers)
        self.assertEqual(await head.read(), b'')

    async def test_gzip_qzero_and_malformed_values_remain_identity(self):
        await self.login()
        for value in ('gzip;q=0', 'gzip;q=NaN', 'gzip;q=1.001', 'xgzip', 'gzip,gzip'):
            response = await self.client.get('/visuals.json?group=scans', headers={'Accept-Encoding':value})
            self.assertNotIn('Content-Encoding', response.headers)
        response = await self.client.get('/visuals.json?group=scans', headers={'Accept-Encoding':'GZip;Q=0.5'})
        self.assertEqual(response.headers['Content-Encoding'], 'gzip')

    async def test_frame_identity_and_age_breakdown_do_not_renew_on_get(self):
        await self.upload(timestamp=time.time() - .25)
        await self.login()
        first = (await (await self.client.get('/visuals.json?group=clouds')).json())['lidar']
        self.assertEqual(first['frameIdentity'], 'rk3588:' + self.session + ':7')
        self.assertEqual(first['ttlS'], 2)
        self.assertEqual(first['sourceAgeS'], .15)
        self.assertGreaterEqual(first['uploadAgeS'], .24)
        self.clock[0] += .4
        second = (await (await self.client.get('/visuals.json?group=clouds')).json())['lidar']
        self.assertEqual(first['frameIdentity'], second['frameIdentity'])
        self.assertEqual(second['sourceAgeS'], first['sourceAgeS'])
        self.assertAlmostEqual(second['ageS'] - first['ageS'], .4, places=5)
        self.assertAlmostEqual(second['uploadAgeS'] - first['uploadAgeS'], .4, places=5)
        overview = await (await self.client.get('/visuals.json?group=overview')).json()
        for key in ('frameIdentity','ttlS','sourceAgeS','uploadAgeS'):
            self.assertEqual(overview['lidar'][key], second[key])

    async def test_conditional_same_frame_has_no_body_and_expiry_changes_validator(self):
        await self.upload()
        await self.login()
        response = await self.client.get('/visuals.json?group=clouds')
        tag = response.headers['ETag']
        self.assertRegex(tag, '^W/"[0-9a-f]{64}"$')
        first = await response.json()
        self.clock[0] += .5
        repeated = await self.client.get('/visuals.json?group=clouds', headers={'If-None-Match':tag})
        self.assertEqual(repeated.status, 304)
        self.assertEqual(await repeated.read(), b'')
        self.assertEqual(repeated.headers['Cache-Control'], 'no-store')
        self.assertNotIn('Content-Encoding', repeated.headers)
        self.assertNotIn('X-Frame-Age-S', repeated.headers)
        self.clock[0] += 2
        expired = await self.client.get('/visuals.json?group=clouds', headers={'If-None-Match':tag})
        self.assertEqual(expired.status, 200)
        self.assertNotEqual(expired.headers['ETag'], tag)
        value = await expired.json()
        self.assertEqual(value['lidar']['status'], 'STALE')
        self.assertNotIn('points', value['lidar'])
        self.assertGreater(value['lidar']['ageS'], first['lidar']['ageS'])

    async def test_new_frame_same_coordinates_and_new_session_change_validator(self):
        await self.upload()
        await self.login()
        first = await self.client.get('/visuals.json?group=clouds')
        tag = first.headers['ETag'];value = await first.json()
        self.clock[0] += .1
        await self.upload(frame=8)
        second = await self.client.get('/visuals.json?group=clouds', headers={'If-None-Match':tag})
        self.assertEqual(second.status, 200)
        second_tag = second.headers['ETag'];second_value = await second.json()
        self.assertNotEqual(second_tag, tag)
        self.assertNotEqual(value['lidar']['frameIdentity'], second_value['lidar']['frameIdentity'])
        self.clock[0] += .1
        self.session = str(uuid.uuid4())
        await self.upload(frame=8, sequence=1)
        third = await self.client.get('/visuals.json?group=clouds', headers={'If-None-Match':second_tag})
        self.assertEqual(third.status, 200)
        third_value = await third.json()
        self.assertNotEqual(third_value['lidar']['frameIdentity'], second_value['lidar']['frameIdentity'])

    async def test_validator_is_bound_to_group_and_full_preview_bytes(self):
        await self.upload()
        await self.login()
        clouds = await self.client.get('/visuals.json?group=clouds')
        tag = clouds.headers['ETag'];await clouds.read()
        scans = await self.client.get('/visuals.json?group=scans', headers={'If-None-Match':tag})
        self.assertEqual(scans.status, 200)
        self.assertNotEqual(scans.headers['ETag'], tag)
        # A buggy upstream reusing frameSeq must still invalidate changed pixels.
        self.app[STORE].media['lidar']['metadata']['points'][0][0] = 12.5
        changed = await self.client.get('/visuals.json?group=clouds', headers={'If-None-Match':tag})
        self.assertEqual(changed.status, 200)
        self.assertNotEqual(changed.headers['ETag'], tag)

    async def test_same_frame_number_from_different_authorities_cannot_collide(self):
        await self.upload()
        obj = fixture.envelope('media', timestamp=self.clock[0], session=self.session)
        obj['source_id'] = 'jetson'
        obj['payload'] = {'key':'depthPoints', 'kind':'cloud', 'status':'AVAILABLE', 'ageS':0,
            'points':[[1,2,3]], 'totalPoints':1, 'frameId':'depth_fixture', 'frameSeq':7}
        self.assertEqual((await self.ingest(obj, fixture.CAMERA_TOKEN)).status, 200)
        await self.login()
        value = await (await self.client.get('/visuals.json?group=clouds')).json()
        self.assertEqual(value['depthPoints']['frameIdentity'], 'jetson:' + self.session + ':7')
        self.assertNotEqual(value['depthPoints']['frameIdentity'], value['lidar']['frameIdentity'])

    async def test_heartbeat_does_not_resurrect_stale_frame(self):
        await self.upload()
        await self.login()
        first = await self.client.get('/visuals.json?group=clouds');await first.read()
        self.clock[0] += 3
        self.sequence += 1
        heartbeat = fixture.envelope('heartbeat', timestamp=self.clock[0], session=self.session, sequence=self.sequence)
        self.assertEqual((await self.ingest(heartbeat)).status, 200)
        value = await (await self.client.get('/visuals.json?group=clouds')).json()
        self.assertEqual(value['lidar']['status'], 'STALE')
        self.assertNotIn('points', value['lidar'])
