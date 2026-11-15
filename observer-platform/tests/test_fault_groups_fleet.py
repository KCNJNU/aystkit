"""Fault grouping and timestamp evidence keep the selected robot boundary."""
import unittest

from cloud.backend.app import FLEET_STORES
from tests import test_multi_robot as fleet
from tests.test_fault_backend import fault


class FaultGroupFleetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fleet.FleetTests.asyncSetUp
    asyncTearDown = fleet.FleetTests.asyncTearDown
    get = fleet.FleetTests.get

    async def test_identical_fault_payloads_and_timestamp_routes_remain_per_robot(self):
        event = fault('Same fault on both robots')
        identities = {}
        for robot, count in [('test-robot', 1), ('robot-two', 4)]:
            store = self.app[FLEET_STORES][robot]
            with store.db:
                store.faults.add_batch(robot, store.primary_source,
                                      [dict(event, details={'occurrences': count})])
            page = await self.get('/api/fault-logs?grouped=1', robot)
            self.assertEqual((page['total'], page['items'][0]['count']), (1, count))
            identities[robot] = page['items'][0]['group_id']
            own_path = '/api/fault-logs/groups/' + identities[robot] + '/timestamps'
            own = await self.get(own_path, robot)
            self.assertEqual((own['total'], own['occurrence_count']), (1, count))
        self.assertNotEqual(identities['test-robot'], identities['robot-two'])
        for robot, other in [('test-robot', 'robot-two'), ('robot-two', 'test-robot')]:
            foreign = '/api/fault-logs/groups/' + identities[other] + '/timestamps'
            response = await self.client.get(foreign, headers={'X-Observer-Robot': robot})
            self.assertEqual(response.status, 404)
        summary = await self.get('/api/fault-logs/summary?grouped=1', 'robot-two')
        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['monitoring']['source_id'], self.app[FLEET_STORES]['robot-two'].primary_source)

    async def test_duplicate_or_unknown_robot_selection_is_rejected_on_new_endpoints(self):
        paths = ('/api/fault-logs?grouped=1', '/api/fault-logs/summary?grouped=1',
                 '/api/fault-logs/groups/' + 'a' * 64 + '/timestamps')
        for path in paths:
            self.assertEqual((await self.client.get(path, headers={'X-Observer-Robot': 'unknown'})).status, 404)
            headers = [('X-Observer-Robot', 'test-robot'), ('X-Observer-Robot', 'robot-two')]
            self.assertEqual((await self.client.get(path, headers=headers)).status, 400)
