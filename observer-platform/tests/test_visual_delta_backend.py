"""Independent stream cursors reduce bytes without concealing stale data."""
import gzip
import json
import time
import unittest
import uuid

from cloud.backend.app import STORE
from shared.protocol import CLOUD_KEYS, MAP_KEYS
from tests import test_backend as fixture


class VisualDeltaTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.BackendTests.asyncSetUp
    asyncTearDown = fixture.BackendTests.asyncTearDown
    login = fixture.BackendTests.login
    ingest = fixture.BackendTests.ingest

    async def upload(self, key='lidar', *, age=.1, frame=1, status='AVAILABLE'):
        if not hasattr(self, 'clock'):
            self.clock = [time.time()]
            self.app[STORE].clock = lambda: self.clock[0]
            self.session = str(uuid.uuid4())
            self.sequence = 0
        self.sequence += 1
        obj = fixture.envelope('media', timestamp=self.clock[0],
            session=self.session, sequence=self.sequence)
        obj['payload'] = {'key':key,'kind':'cloud','status':status,'ageS':age,
            'frameSeq':frame,'frameId':'fixture_'+key,'topic':'/fixture/'+key,
            'totalPoints':1500}
        if status == 'AVAILABLE':
            obj['payload']['points'] = [[i*.01, .125, -.25] for i in range(1500)]
        self.assertEqual((await self.ingest(obj)).status, 200)

    async def delta(self, seen=None, *, group='clouds', headers=None, **kwargs):
        request_headers = dict(headers or {})
        if seen is not None:
            request_headers['X-Visual-Seen'] = json.dumps(seen, separators=(',', ':'))
        return await self.client.get('/visuals.json?group='+group+'&delta=1',
            headers=request_headers, **kwargs)

    async def test_same_frame_omits_only_points_and_preserves_live_metadata(self):
        await self.upload()
        await self.login()
        first = await self.delta()
        value = await first.json();tag = value['lidar']['previewTag']
        self.assertRegex(tag, '^[0-9a-f]{64}$')
        self.assertFalse(value['lidar']['pointsUnchanged'])
        self.assertEqual(len(value['lidar']['points']), 768)
        self.assertEqual(value['lidar']['previewPointCount'], 768)
        self.assertEqual(value['lidar']['previewInputPoints'], 1500)
        self.clock[0] += .4
        repeated = await self.delta({'lidar':tag}, headers={'If-None-Match':'*'})
        self.assertEqual(repeated.status, 200)
        self.assertNotIn('ETag', repeated.headers)
        again = await repeated.json()
        self.assertTrue(again['lidar']['pointsUnchanged'])
        self.assertNotIn('points', again['lidar'])
        self.assertEqual(again['lidar']['frameIdentity'], value['lidar']['frameIdentity'])
        self.assertAlmostEqual(again['lidar']['ageS']-value['lidar']['ageS'], .4, places=5)
        self.assertEqual(again['lidar']['ttlS'], value['lidar']['ttlS'])
        self.assertEqual(again['lidar']['sourceAgeS'], value['lidar']['sourceAgeS'])
        self.assertGreater(again['lidar']['uploadAgeS'], value['lidar']['uploadAgeS'])

    async def test_asynchronous_sources_do_not_resend_other_point_arrays(self):
        await self.upload(frame=1)
        await self.login()
        origin = self.clock[0]
        events = [(.25,'registered',1),(1,'lidar',2),(1.25,'registered',2),
                  (2,'lidar',3),(2.25,'registered',3)]
        seen = {};delivered = {};delta_bytes = 0;full_bytes = 0;one_preview_size = None
        for poll in (0,.5,1,1.5,2,2.5):
            while events and events[0][0] <= poll:
                when,key,frame = events.pop(0)
                self.clock[0] = origin+when
                await self.upload(key,frame=frame)
            self.clock[0] = origin+poll
            values = await (await self.delta(seen)).json()
            for key,item in values.items():
                if 'previewTag' in item:
                    seen[key] = item['previewTag']
                if 'points' in item:
                    identity = (key,item['frameIdentity'])
                    delivered[identity] = delivered.get(identity,0)+1
                    size = len(json.dumps(item['points'],separators=(',',':')))
                    delta_bytes += size
                    if one_preview_size is None:
                        one_preview_size = size
            full = await (await self.client.get('/visuals.json?group=clouds')).json()
            for item in full.values():
                if 'points' not in item:
                    continue
                points=item['points'];count=min(768,len(points))
                preview=points if len(points)<=count else [
                    points[index*(len(points)-1)//(count-1)] for index in range(count)]
                full_bytes += len(json.dumps(preview,separators=(',',':')))
        self.assertEqual(len(delivered), 6)
        self.assertTrue(all(count==1 for count in delivered.values()))
        self.assertEqual(delta_bytes, 6 * one_preview_size)
        self.assertLessEqual(delta_bytes, full_bytes * 6/11)

    async def test_bounded_preview_preserves_original_coordinates_and_endpoints(self):
        await self.upload()
        await self.login()
        original=self.app[STORE].media['lidar']['metadata']['points']
        item=(await (await self.delta()).json())['lidar']
        self.assertLessEqual(len(item['points']),768)
        self.assertEqual(item['points'][0],original[0])
        self.assertEqual(item['points'][-1],original[-1])
        self.assertTrue(all(point in original for point in item['points']))
        self.assertEqual(item['previewPointCount'],len(item['points']))
        self.assertEqual(item['previewInputPoints'],len(original))
        legacy=(await (await self.client.get('/visuals.json?group=clouds')).json())['lidar']
        self.assertEqual(legacy['points'],original)
        self.assertEqual(len(legacy['points']),1500)
        tag=item['previewTag']
        repeated=(await (await self.delta({'lidar':tag})).json())['lidar']
        self.assertEqual(repeated['previewTag'],tag)
        self.assertTrue(repeated['pointsUnchanged'])
        self.assertEqual(repeated['previewPointCount'],768)
        self.assertNotIn('points',repeated)

    async def test_stale_transition_is_delivered_despite_matching_cursor_and_heartbeat(self):
        await self.upload()
        await self.login()
        first = await (await self.delta()).json()
        tag = first['lidar']['previewTag']
        self.clock[0] += 2.1
        self.sequence += 1
        heartbeat = fixture.envelope('heartbeat', timestamp=self.clock[0],
            session=self.session, sequence=self.sequence)
        self.assertEqual((await self.ingest(heartbeat)).status, 200)
        response = await self.delta({'lidar':tag})
        self.assertEqual(response.status, 200)
        value = (await response.json())['lidar']
        self.assertEqual(value['status'], 'STALE')
        self.assertFalse(value['pointsUnchanged'])
        self.assertNotIn('points', value)
        self.assertNotIn('previewTag', value)
        self.assertEqual(value['frameIdentity'], first['lidar']['frameIdentity'])

    async def test_cursor_validation_rejects_unknown_keys_types_duplicates_and_size(self):
        await self.login()
        invalid = ['[]','null','"text"','{"lidar":null}','{"lidar":1}',
            '{"lidar":"bad"}','{"scan":"'+'a'*64+'"}',
            '{"lidar":"'+'a'*64+'","lidar":"'+'b'*64+'"}',
            '{"lidar":"'+'a'*2048+'"}', '['*1000+']'*1000]
        for raw in invalid:
            response = await self.delta(headers={'X-Visual-Seen':raw})
            self.assertEqual(response.status, 400, raw[:80])
            self.assertNotIn('Content-Encoding', response.headers)
        response = await self.client.get('/visuals.json?group=clouds&delta=1',
            headers=[('X-Visual-Seen','{}'),('X-Visual-Seen','{}')])
        self.assertEqual(response.status, 400)

    async def test_delta_query_is_explicit_bounded_and_only_for_point_groups(self):
        await self.login()
        for query in ('delta=1','group=maps&delta=1','group=overview&delta=1',
            'group=clouds&delta=0','group=clouds&delta=true',
            'group=clouds&delta=1&delta=1','group=clouds&delta=1&keys=lidar'):
            response = await self.client.get('/visuals.json?'+query)
            self.assertEqual(response.status, 400, query)
        response = await self.delta(group='scans')
        self.assertEqual(response.status, 200)
        self.assertEqual(set(await response.json()), {'scan','bluesea'})

    async def test_authentication_and_pending_approval_precede_cursor_parsing(self):
        for path in ('/visuals.json?group=clouds&delta=1',
                     '/visuals.json?group=maps&delta=1'):
            response = await self.client.get(path,
                headers={'X-Visual-Seen':'invalid','Accept-Encoding':'gzip'})
            self.assertEqual(response.status, 401)
            self.assertNotIn('Content-Encoding', response.headers)
        response = await self.client.post('/api/register',
            json={'username':'pending.delta','password':fixture.PASSWORD},
            headers={'Origin':fixture.ORIGIN})
        self.assertEqual(response.status, 201)
        response = await self.delta(headers={'X-Visual-Seen':'invalid'})
        self.assertEqual(response.status, 403)

    async def test_delta_gzip_is_exact_and_small_unchanged_metadata_stays_200(self):
        await self.upload()
        await self.login()
        first = await (await self.delta()).json()
        for seen in ({},{'lidar':first['lidar']['previewTag']}):
            raw_response = await self.delta(seen,
                headers={'Accept-Encoding':'identity'},auto_decompress=False)
            raw = await raw_response.read()
            zipped = await self.delta(seen,
                headers={'Accept-Encoding':'gzip'},auto_decompress=False)
            wire = await zipped.read()
            self.assertEqual(zipped.status, 200)
            self.assertEqual(zipped.headers['Content-Encoding'], 'gzip')
            self.assertEqual(zipped.headers['Cache-Control'], 'no-store')
            self.assertEqual(zipped.headers['Vary'], 'Accept-Encoding')
            self.assertEqual(gzip.decompress(wire), raw)
            self.assertNotIn('ETag', zipped.headers)
            if seen:
                self.assertLess(len(raw), 2000)
        response = await self.delta(headers={'Accept-Encoding':'gzip;q=0'})
        self.assertNotIn('Content-Encoding', response.headers)

    async def test_legacy_routes_and_group_etag_remain_compatible(self):
        await self.upload()
        await self.login()
        response = await self.client.get('/visuals.json?group=clouds',
            headers={'X-Visual-Seen':'ignored legacy header'})
        value = await response.json();tag = response.headers['ETag']
        self.assertIn('points', value['lidar'])
        self.assertNotIn('previewTag', value['lidar'])
        self.assertNotIn('pointsUnchanged', value['lidar'])
        response = await self.client.get('/visuals.json?group=clouds',
            headers={'If-None-Match':tag})
        self.assertEqual(response.status, 304)
        self.assertEqual(await response.read(), b'')
        full = await (await self.client.get('/visuals.json')).json()
        self.assertEqual(set(full), CLOUD_KEYS|MAP_KEYS)

    async def test_preview_tag_binds_points_source_identity_frame_and_topic(self):
        await self.upload()
        await self.login()
        value = await (await self.delta()).json();previous = value['lidar']['previewTag']
        metadata = self.app[STORE].media['lidar']['metadata']
        for name,new_value in (('frameId','other_frame'),('topic','/other/topic'),
            ('frameSeq',2),('points',[[99,2,3]])):
            metadata[name] = new_value
            value = await (await self.delta({'lidar':previous})).json()
            self.assertFalse(value['lidar']['pointsUnchanged'])
            self.assertNotEqual(value['lidar']['previewTag'], previous)
            self.assertIn('points', value['lidar'])
            previous = value['lidar']['previewTag']
        self.app[STORE].media['lidar']['source_id'] = 'jetson'
        self.app[STORE].sources[('test-robot','jetson')] = dict(
            self.app[STORE].sources[('test-robot','rk3588')])
        value = await (await self.delta({'lidar':previous})).json()
        self.assertNotEqual(value['lidar']['previewTag'], previous)
        self.assertTrue(value['lidar']['frameIdentity'].startswith('jetson:'))

    async def test_unavailable_and_empty_samples_never_reuse_tags_or_arrays(self):
        await self.upload()
        await self.login()
        first = await (await self.delta()).json();seen={'lidar':first['lidar']['previewTag']}
        metadata = self.app[STORE].media['lidar']['metadata']
        for status in ('WAITING_UPSTREAM','STALE','ERROR','NO_DATA'):
            metadata.update(status=status, previewTag=seen['lidar'])
            item = (await (await self.delta(seen)).json())['lidar']
            self.assertFalse(item['pointsUnchanged'])
            self.assertNotIn('points', item)
            self.assertNotIn('previewTag', item)
        metadata.update(status='AVAILABLE',points=[])
        item = (await (await self.delta(seen)).json())['lidar']
        self.assertFalse(item['pointsUnchanged'])
        self.assertEqual(item['points'], [])
        self.assertNotIn('previewTag', item)

    async def test_delta_does_not_mutate_store_and_overview_source_priority_is_stable(self):
        await self.upload(age=.9)
        await self.upload('registered',age=.1)
        await self.upload('bluesea',age=0)
        await self.login()
        first = await (await self.delta()).json()
        seen={key:value['previewTag'] for key,value in first.items() if 'previewTag' in value}
        await (await self.delta(seen)).read()
        legacy = await (await self.client.get('/visuals.json?group=clouds')).json()
        self.assertEqual(len(legacy['lidar']['points']),1500)
        self.assertNotIn('previewTag', self.app[STORE].media['lidar']['metadata'])
        overview = await (await self.client.get('/visuals.json?group=overview')).json()
        self.assertEqual(set(overview), {'lidar'})
        self.app[STORE].media['lidar']['metadata']['status'] = 'ERROR'
        overview = await (await self.client.get('/visuals.json?group=overview')).json()
        self.assertEqual(set(overview), {'registered'})
        self.app[STORE].media['registered']['metadata']['status'] = 'NO_DATA'
        overview = await (await self.client.get('/visuals.json?group=overview')).json()
        self.assertEqual(set(overview), {'bluesea'})
