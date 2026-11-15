import assert from 'node:assert/strict';
import test from 'node:test';
import { managedSpawnOptions, windowsTaskkillArgs } from '../process-supervisor.mjs';

test('managed process groups and Windows tree termination stay scoped to the child PID', () => {
  assert.deepEqual(windowsTaskkillArgs(1234), ['/PID', '1234', '/T']);
  assert.deepEqual(windowsTaskkillArgs(1234, true), ['/PID', '1234', '/T', '/F']);
  assert.equal(managedSpawnOptions('linux').detached, true);
  assert.equal(managedSpawnOptions('win32').detached, false);
});
