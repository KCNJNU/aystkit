"""Synthetic two-robot isolation, shared logins and independent physical sources."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from aiohttp import CookieJar, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from cloud.backend.app import create_app, FLEET_STORES, FLEET_NATIVE, STORE
from tests.test_backend import config, TOKEN, PASSWORD, ORIGIN
from tests.test_protocol import envelope

OTHER_TOKEN='b'*48

class FleetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.cfg=config(self.temp.name)
        second=copy.deepcopy(self.cfg.device_credentials[0]);second.update(robot_id='robot-two',token=OTHER_TOKEN)
        self.cfg.device_credentials.append(second)
        self.cfg.fleet={'test-robot':{'name':'一号车','warehouseId':'00000000-0000-4000-8000-000000000001'},'robot-two':{'name':'二号车'}}
        self.app=create_app(self.cfg)
        self.client=TestClient(TestServer(self.app),cookie_jar=CookieJar(unsafe=True));await self.client.start_server()
        r=await self.client.post('/api/login',json={'username':'viewer','password':PASSWORD},headers={'Origin':ORIGIN});self.assertEqual(r.status,200)
    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()
    async def get(self,path,robot):
        r=await self.client.get(path,headers={'X-Observer-Robot':robot});self.assertEqual(r.status,200,await r.text());return await r.json()
    async def ingest(self,robot,token,kind='observation'):
        value=envelope(kind);value['robot_id']=robot;value['payload'].update({'robot':{'testIdentity':robot}} if kind=='observation' else {})
        r=await self.client.post('/api/v1/ingest',json=value,headers={'Authorization':'Bearer '+token});return r
    async def test_list_includes_offline_configured_robots_without_secrets(self):
        r=await self.client.get('/api/robots');data=await r.json()
        self.assertEqual(data['defaultRobotId'],'test-robot');self.assertEqual(len(data['robots']),2)
        self.assertEqual([x['name'] for x in data['robots']],['一号车','二号车'])
        self.assertNotIn(TOKEN,json.dumps(data));self.assertFalse(data['robots'][1]['online'])
    async def test_ingestion_and_same_media_keys_are_isolated(self):
        for robot,token in [('test-robot',TOKEN),('robot-two',OTHER_TOKEN)]:
            self.assertEqual((await self.ingest(robot,token)).status,200)
            self.assertEqual((await self.ingest(robot,token,'media')).status,200)
        for robot in ['test-robot','robot-two']:
            value=await self.get('/snapshot.json',robot);self.assertEqual(value['cloud']['robotId'],robot)
            self.assertEqual(value['robot']['testIdentity'],robot)
        self.assertIsNot(self.app[FLEET_STORES]['test-robot'].media,self.app[FLEET_STORES]['robot-two'].media)
        self.assertEqual((await self.ingest('robot-two',TOKEN)).status,403)
        self.assertEqual((await self.client.get('/snapshot.json',headers={'X-Observer-Robot':'unknown'})).status,404)
    async def test_tests_issues_logs_and_delete_cannot_cross_robot(self):
        body={'action':'start','revision':0,'command_id':str(uuid.uuid4()),'tester_name':'隔离测试'}
        r=await self.client.post('/api/tests/control',json=body,headers={'Origin':ORIGIN,'X-Observer-Robot':'robot-two'})
        self.assertEqual(r.status,200,await r.text());run=(await r.json())['active']['id']
        self.assertIsNone((await self.get('/api/tests','test-robot'))['active'])
        self.assertEqual((await self.get('/api/tests','robot-two'))['active']['id'],run)
        r=await self.client.delete('/api/admin/tests/'+run,json={},headers={'Origin':ORIGIN,'X-Observer-Robot':'test-robot'})
        self.assertEqual(r.status,404)
        self.assertEqual((await self.get('/api/tests','robot-two'))['active']['id'],run)
    async def test_ws_tabs_and_disabled_native_camera_do_not_show_default_robot(self):
        a=await self.client.ws_connect('/ws?robotId=test-robot',headers={'Origin':ORIGIN})
        b=await self.client.ws_connect('/ws?robotId=robot-two',headers={'Origin':ORIGIN})
        self.assertEqual((await a.receive_json())['cloud']['robotId'],'test-robot')
        self.assertEqual((await b.receive_json())['cloud']['robotId'],'robot-two')
        await a.close();await b.close()
        self.assertFalse((await self.get('/native-camera/status','robot-two'))['enabled'])
        self.assertIsNot(self.app[FLEET_NATIVE]['test-robot'],self.app[FLEET_NATIVE]['robot-two'])
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect('/ws?robotId=unknown',headers={'Origin':ORIGIN})
    async def test_each_robot_uses_distinct_persistent_store_and_restart_retains_default(self):
        self.assertEqual(self.app[STORE].path,Path(self.temp.name)/'latest.sqlite3')
        self.assertEqual(self.app[FLEET_STORES]['robot-two'].path,Path(self.temp.name)/'robots/robot-two/latest.sqlite3')
        self.assertEqual((await self.ingest('robot-two',OTHER_TOKEN)).status,200)
        await self.client.close()
        self.app=create_app(self.cfg);self.client=TestClient(TestServer(self.app),cookie_jar=CookieJar(unsafe=True));await self.client.start_server()
        self.assertIn(('robot-two','rk3588'),self.app[FLEET_STORES]['robot-two'].sources)
        self.assertNotIn(('robot-two','rk3588'),self.app[STORE].sources)

class FleetConfigTests(unittest.TestCase):
    def test_per_robot_primary_and_token_uniqueness(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=config(directory);second=copy.deepcopy(cfg.device_credentials[0]);second['robot_id']='second'
            cfg.device_credentials.append(second)
            with self.assertRaises(ValueError):cfg.validate()
            second['token']='d'*48
            cfg.validate()
            cfg.fleet={'second':{'warehouseId':'not-a-uuid'}}
            with self.assertRaises(ValueError):cfg.validate()
            cfg.fleet={'second':{'nativeVideoSocket':'/run/native-preview/shared.sock'},'test-robot':{'nativeVideoSocket':'/run/native-preview/shared.sock'}}
            with self.assertRaises(ValueError):cfg.validate()
