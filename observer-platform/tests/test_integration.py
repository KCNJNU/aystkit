"""Real isolated HTTP upload → authenticated WS, never production/ROS injection."""
import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from cloud.backend.app import STORE, create_app
from cloud.backend.security import Config as CloudConfig, hash_password
from collector.config import Config as CollectorConfig
from collector.state import CollectorState
from collector.transport import Uploader


class FixtureSystem:
    def collect(self):
        return {'status': 'AVAILABLE', 'hostname': 'TEST FIXTURE ONLY',
                'cpu': {'status': 'AVAILABLE', 'value': 0, 'unit': '%'},
                'primaryThermal': {'status': 'NO_DATA', 'temperatureC': None}}


class UploadIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_actual_uploader_auth_broadcast_and_disconnection(self):
        with tempfile.TemporaryDirectory(prefix='robot-cloud-integration-') as directory:
            token = 'test-only-device-token-' + 'x' * 32
            config = CloudConfig(robot_id='test-robot', device_credentials=[{
                'token': token, 'robot_id': 'test-robot', 'source_id': 'test-rk', 'role': 'primary'}],
                viewer_username='test-viewer', viewer_password_hash=hash_password('TestFixturePasswordOnly'),
                session_secret='test-only-session-' + 'x' * 32, public_origin='http://127.0.0.1',
                data_dir=directory, allow_insecure_local=True)
            app = create_app(config)
            async with TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True)) as viewer:
                base = str(viewer.make_url('/')).rstrip('/')
                config.public_origin = base
                origin = {'Origin': base}
                rejected = await viewer.get('/snapshot.json', headers={'Authorization': 'Bearer ' + token})
                self.assertEqual(rejected.status, 401)
                logged = await viewer.post('/api/login', json={'username': 'test-viewer', 'password': 'TestFixturePasswordOnly'}, headers=origin)
                self.assertEqual(logged.status, 200)
                websocket = await viewer.ws_connect('/ws', headers=origin)
                try:
                    self.assertFalse((await websocket.receive_json())['cloud']['online'])
                    cfg = CollectorConfig('test-robot', 'test-rk', cloud_url=base + '/api/v1/ingest', token=token,
                                          allow_insecure_local=True)
                    state = CollectorState(cfg, collector=FixtureSystem())
                    self.assertTrue(state.telemetry.receive(json.dumps({
                        'telemetryTransportSchema': 1, 'telemetrySourceNode': '/chassis_host',
                        'debug': {'fields': {'actualVxMps': {'value': 0, 'state': 'AVAILABLE', 'type': 'ACTUAL',
                                                         'source': 'TEST FIXTURE ONLY', 'ageMs': 0, 'unit': 'm/s'},
                                             'busVoltageV': {'value': None, 'state': 'WAITING_UPSTREAM', 'type': 'ACTUAL'}}},
                        'serialDebug': {'telemetryPages': {'0x00': {'count': 1, 'ageMs': 0}}}})))
                    state.telemetry.set_graph(1, ['std_msgs/msg/String'])
                    uploader = Uploader(cfg)
                    with patch.dict('os.environ', {'NO_PROXY': '127.0.0.1,localhost'}):
                        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 0})
                        uploader.enqueue('observation', '/observer/observation', state.observation())
                        uploader.start()
                        try:
                            deadline = time.monotonic() + 6
                            while uploader.sent < 2 and time.monotonic() < deadline:
                                await asyncio.sleep(.05)
                            self.assertEqual(uploader.sent, 2)
                            snap = await (await viewer.get('/snapshot.json')).json()
                            self.assertTrue(snap['cloud']['online'])
                            self.assertEqual(snap['rk3588']['cpu']['value'], 0)
                            self.assertIsNone(snap['rk3588']['primaryThermal']['temperatureC'])
                            fields = snap['robot']['data']['debug']['fields']
                            self.assertEqual(fields['actualVxMps']['value'], 0)
                            self.assertEqual(fields['actualVxMps']['type'], 'ACTUAL')
                            self.assertIsNone(fields['busVoltageV']['value'])
                            self.assertEqual(snap['pages']['0x10']['status'], 'WAITING_UPSTREAM')
                            deadline = time.monotonic() + 4
                            while time.monotonic() < deadline:
                                pushed = await asyncio.wait_for(websocket.receive_json(), 2)
                                if pushed['cloud']['online']:
                                    break
                            self.assertEqual(pushed['rk3588']['hostname'], 'TEST FIXTURE ONLY')
                            now = time.time()
                            app[STORE].clock = lambda: now + 20
                            offline = await (await viewer.get('/snapshot.json')).json()
                            self.assertFalse(offline['cloud']['online'])
                            self.assertEqual(offline['cloud']['status'], 'OFFLINE')
                            self.assertEqual(offline['robot']['data']['debug']['fields']['actualVxMps']['state'], 'STALE')
                            self.assertEqual(offline['robot']['data']['debug']['fields']['actualVxMps']['value'], 0)
                            self.assertEqual((await viewer.get('/healthz')).status, 200)
                        finally:
                            await asyncio.to_thread(uploader.stop)
                finally:
                    await websocket.close()
