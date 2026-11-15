"""Test-only chassis frames and isolated ephemeral-loopback WebSocket servers."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from aiohttp import WSMsgType, web

from collector import local_chassis
from collector.local_chassis import LocalChassis, decode_frame


def frame(page=0, changes=None):
    data = bytearray([0x7b, page, 0] + [0] * 12 + [0, 0x7d])
    for offset, value in (changes or {}).items():
        data[offset] = value
    for value in data[:15]:
        data[15] ^= value
    return {'type': 'rx', 'hex': data.hex()}


def store():
    now = [10.0]
    receiver = LocalChassis(clock=lambda: now[0])
    receiver.connected = True
    receiver.receive({'type': 'status', 'connected': True, 'port': 'test-only'})
    return receiver, now


def test_local_chassis_source_and_message_limit_are_fixed():
    assert local_chassis.SOURCE == 'ws://127.0.0.1:8765/ws'
    assert local_chassis.MAX_MESSAGE_BYTES == 256 * 1024
    assert local_chassis.RECONNECT_SECONDS == 2.0


def test_decode_exact_17_byte_frame_and_big_endian_signed_motion():
    message = frame(0x10, {3: 255, 4: 156, 5: 0, 6: 100, 9: 9, 10: 96,
                           11: 255, 12: 6})
    raw = decode_frame(message['hex'])
    assert raw == bytes.fromhex(message['hex'])
    assert len(raw) == 17
    assert raw[0] == 0x7b and raw[-1] == 0x7d
    checksum = 0
    for value in raw[:15]:
        checksum ^= value
    assert raw[15] == checksum

    receiver, _ = store()
    receiver.receive(message)
    fields = receiver.snapshot()['payload']['debug']['fields']
    assert fields['leftWheelMps']['value'] == -0.1
    assert fields['rightWheelMps']['value'] == 0.1
    assert fields['actualVxMps']['value'] == 0
    assert fields['actualWzRadps']['value'] == -0.25
    assert fields['busVoltage']['value'] == 24


@pytest.mark.parametrize('raw', [None, True, b'7b00', 'zz', '7b00', '00' * 17,
                                frame()['hex'][:-2], frame()['hex'] + '00',
                                '00' + frame()['hex'][2:], frame()['hex'][:-2] + '00'])
def test_decode_rejects_bad_type_length_header_tail_and_hex(raw):
    with pytest.raises((ValueError, TypeError)):
        decode_frame(raw)
    receiver, _ = store()
    receiver.receive({'type': 'rx', 'hex': raw})
    snapshot = receiver.snapshot()
    assert snapshot['messageCount'] == 0
    assert snapshot['parseErrorCount'] == 1
    assert snapshot['payload'] is None
    assert snapshot['status'] == 'WAITING_UPSTREAM'


def test_bcc_failure_cannot_refresh_accepted_frame_or_page():
    receiver, now = store()
    receiver.receive(frame())
    broken = bytearray.fromhex(frame()['hex'])
    broken[15] ^= 1
    with pytest.raises(ValueError):
        decode_frame(broken.hex())
    now[0] += 3
    receiver.receive({'type': 'rx', 'hex': broken.hex()})
    snapshot = receiver.snapshot()
    assert snapshot['status'] == 'STALE'
    assert snapshot['messageCount'] == 1
    assert snapshot['parseErrorCount'] == 1
    assert snapshot['pages']['0x00']['count'] == 1
    assert snapshot['pages']['0x00']['ageMs'] == 3000
    assert snapshot['payload']['chassisAlive'] is None


def test_zero_motion_brush_and_lift_values_are_real_but_zero_voltage_is_unknown():
    receiver, _ = store()
    for page in (0x10, 0x11, 0x12, 0x13):
        receiver.receive(frame(page))
    payload = receiver.snapshot()['payload']
    fields = payload['debug']['fields']
    for key in ('leftWheelMps', 'rightWheelMps', 'actualVxMps', 'actualWzRadps',
                'frontRollerStateActualRpm', 'frontRollerState.faultCode'):
        assert fields[key]['value'] == 0
        assert fields[key]['state'] == 'AVAILABLE'
    assert fields['frontRollerState.online']['value'] is False
    assert fields['frontRollerState.commLost']['value'] is False
    assert fields['busVoltage']['value'] is None
    assert fields['busVoltage']['state'] == 'NO_DATA'
    assert payload['lift1State'] == {
        'hallCount': 0, 'direction': 'coast', 'atMin': False, 'atMax': False,
    }


def test_missing_pages_do_not_invent_motion_commands_or_serial_error_counts():
    receiver, _ = store()
    receiver.receive(frame(changes={9: 9, 10: 96}))
    snapshot = receiver.snapshot()
    fields = snapshot['payload']['debug']['fields']
    assert set(fields) == {'busVoltage'}
    assert fields['busVoltage']['value'] == 24
    assert snapshot['payload']['serialDebug']['checksumErrorCount'] is None
    assert snapshot['payload']['serialDebug']['invalidFrameCount'] is None
    for page in ('0x10', '0x11', '0x12', '0x13'):
        assert snapshot['pages'][page]['status'] == 'WAITING_UPSTREAM'
        assert snapshot['pages'][page]['count'] == 0
        assert snapshot['pages'][page]['ageMs'] is None


@pytest.mark.parametrize('kind', ['tx', 'drive', 'nav', 'command'])
def test_command_broadcasts_cannot_become_feedback(kind):
    receiver, _ = store()
    receiver.receive(dict(frame(0x10), type=kind))
    snapshot = receiver.snapshot()
    assert snapshot['messageCount'] == 0
    assert snapshot['parseErrorCount'] == 0
    assert snapshot['payload'] is None
    assert all(page['status'] == 'WAITING_UPSTREAM' for page in snapshot['pages'].values())


def test_unknown_page_counts_without_inventing_known_fields_or_pages():
    receiver, _ = store()
    receiver.receive(frame(0xff, {9: 9, 10: 96}))
    snapshot = receiver.snapshot()
    assert snapshot['messageCount'] == 1
    assert snapshot['parseErrorCount'] == 0
    assert snapshot['payload']['debug']['fields'] == {}
    assert snapshot['payload']['serialDebug']['unknownPageCount'] == 1
    assert '0xff' not in snapshot['pages']
    assert all(page['status'] == 'WAITING_UPSTREAM' for page in snapshot['pages'].values())


def test_brush_lift_fault_decode_and_each_page_has_its_own_age():
    receiver, now = store()
    receiver.receive(frame(0x11, {3: 255, 4: 255, 13: 1, 14: 16}))
    receiver.receive(frame(0x12, {4: 42, 7: 5, 8: 10}))
    receiver.receive(frame(0x13, {3: 7, 7: 1, 8: 1}))
    payload = receiver.snapshot()['payload']
    assert payload['debug']['fields']['frontRollerStateActualRpm']['value'] == -1
    assert payload['frontRollerState'] == {'online': True, 'commLost': True, 'faultCode': 7}
    assert payload['lift1State'] == {'hallCount': 42, 'direction': 'extend', 'atMin': True, 'atMax': False}
    assert payload['lift2State'] == {'hallCount': 0, 'direction': 'retract', 'atMin': False, 'atMax': True}
    now[0] += 3
    receiver.receive(frame())
    snapshot = receiver.snapshot()
    assert snapshot['status'] == 'AVAILABLE'
    assert snapshot['pages']['0x00']['status'] == 'AVAILABLE'
    for page in ('0x11', '0x12', '0x13'):
        assert snapshot['pages'][page]['status'] == 'STALE'
    assert snapshot['pages']['0x10']['status'] == 'WAITING_UPSTREAM'
    assert snapshot['payload']['debug']['fields']['frontRollerStateActualRpm']['value'] is None
    assert snapshot['payload']['lift1State'] is None


def test_fresh_fault_page_does_not_refresh_old_rpm_and_fresh_rpm_does_not_refresh_fault():
    receiver, now = store()
    receiver.receive(frame(0x11, {4: 42, 13: 1}))
    now[0] = 11.5
    receiver.receive(frame(0x13, {3: 7, 7: 1, 8: 1}))
    now[0] = 12.8
    receiver.receive(frame())
    snapshot = receiver.snapshot()
    fields = snapshot['payload']['debug']['fields']
    assert snapshot['status'] == 'AVAILABLE'
    assert snapshot['pages']['0x11']['status'] == 'STALE'
    assert snapshot['pages']['0x13']['status'] == 'AVAILABLE'
    assert fields['frontRollerStateActualRpm']['value'] is None
    assert fields['frontRollerStateActualRpm']['state'] == 'STALE'
    assert fields['frontRollerState.faultCode']['value'] == 7
    assert fields['frontRollerState.online']['value'] is True
    assert fields['frontRollerState.commLost']['value'] is True

    now[0] = 14.5
    receiver.receive(frame(0x11, {4: 43}))
    snapshot = receiver.snapshot()
    fields = snapshot['payload']['debug']['fields']
    assert snapshot['pages']['0x11']['status'] == 'AVAILABLE'
    assert snapshot['pages']['0x13']['status'] == 'STALE'
    assert fields['frontRollerStateActualRpm']['value'] == 43
    assert fields['frontRollerState.faultCode']['value'] is None
    assert fields['frontRollerState.faultCode']['state'] == 'STALE'
    assert fields['frontRollerState.online']['value'] is False


def test_disconnect_serial_close_and_timeout_invalidate_chassis_alive():
    receiver, now = store()
    receiver.receive(frame(0x10))
    receiver.connected = False
    snapshot = receiver.snapshot()
    assert snapshot['status'] == 'STALE'
    assert snapshot['payload']['chassisAlive'] is None
    assert snapshot['payload']['debug']['fields']['actualVxMps']['value'] is None
    receiver.connected = True
    receiver.receive({'type': 'status', 'connected': False})
    assert receiver.snapshot()['status'] == 'STALE'
    receiver.receive({'type': 'status', 'connected': True})
    assert receiver.snapshot()['status'] == 'AVAILABLE'
    now[0] += 3
    assert receiver.snapshot()['status'] == 'STALE'


async def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Timed out waiting for isolated local chassis client')
        await asyncio.sleep(0.01)


async def run_server(handler):
    app = web.Application()
    app.router.add_get('/ws', handler)
    runner = web.AppRunner(app, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    assert port != 8765, 'Tests must never connect to the production chassis port'
    return runner, f'ws://127.0.0.1:{port}/ws'


async def stop_client(receiver):
    started = time.monotonic()
    await asyncio.wait_for(asyncio.to_thread(receiver.stop), timeout=2)
    assert time.monotonic() - started < 2
    assert receiver.thread is not None and not receiver.thread.is_alive()


def test_threaded_client_only_sends_passive_pong_and_no_application_messages(monkeypatch):
    async def scenario():
        received = []
        sockets = []

        async def handler(request):
            websocket = web.WebSocketResponse(autoping=False, heartbeat=None)
            await websocket.prepare(request)
            sockets.append(websocket)
            await websocket.send_json({'type': 'status', 'connected': True, 'port': 'test-only'})
            await websocket.send_json(frame(0x10))
            await websocket.ping(b'test-only-server-ping')
            async for message in websocket:
                if message.type in (WSMsgType.TEXT, WSMsgType.BINARY, WSMsgType.PING, WSMsgType.PONG):
                    received.append((message.type, message.data))
            return websocket

        runner, source = await run_server(handler)
        monkeypatch.setattr(local_chassis, 'SOURCE', source)
        monkeypatch.setattr(local_chassis, 'RECONNECT_SECONDS', 0.05)
        receiver = LocalChassis()
        receiver.start()
        try:
            await wait_until(lambda: receiver.snapshot()['messageCount'] == 1)
            assert receiver.snapshot()['status'] == 'AVAILABLE'
            assert receiver.thread.is_alive()
            await wait_until(lambda: len(received) >= 1)
            await asyncio.sleep(0.15)
            assert received == [(WSMsgType.PONG, b'test-only-server-ping')]
            await stop_client(receiver)
            assert received == [(WSMsgType.PONG, b'test-only-server-ping')]
            assert receiver.snapshot()['status'] == 'STALE'
        finally:
            if receiver.thread.is_alive():
                await stop_client(receiver)
            for websocket in sockets:
                await websocket.close()
            await runner.cleanup()
    asyncio.run(scenario())


def test_threaded_client_rejects_redirect_without_following_it(monkeypatch):
    async def scenario():
        requests = []

        async def handler(request):
            requests.append(request.path_qs)
            if request.query.get('redirected'):
                return web.Response(status=500, text='Redirect must never be followed')
            return web.Response(status=302, headers={'Location': '/ws?redirected=1'})

        runner, source = await run_server(handler)
        monkeypatch.setattr(local_chassis, 'SOURCE', source)
        monkeypatch.setattr(local_chassis, 'RECONNECT_SECONDS', 0.05)
        receiver = LocalChassis()
        receiver.start()
        try:
            await wait_until(lambda: len(requests) >= 2)
            assert all(path == '/ws' for path in requests)
            assert not receiver.snapshot()['connected']
            assert receiver.snapshot()['messageCount'] == 0
            assert receiver.snapshot()['connectionErrorCount'] >= 1
            await stop_client(receiver)
        finally:
            if receiver.thread.is_alive():
                await stop_client(receiver)
            await runner.cleanup()
    asyncio.run(scenario())


def test_threaded_disconnect_is_stale_reconnects_and_stop_interrupts_backoff(monkeypatch):
    async def scenario():
        connections = []
        recovery_allowed = asyncio.Event()

        async def handler(request):
            websocket = web.WebSocketResponse(autoping=False, heartbeat=None)
            await websocket.prepare(request)
            connections.append((websocket, request.transport))
            if len(connections) > 1:
                await recovery_allowed.wait()
            await websocket.send_json({'type': 'status', 'connected': True, 'port': 'test-only'})
            await websocket.send_json(frame(0x10))
            async for _ in websocket:
                pass
            return websocket

        runner, source = await run_server(handler)
        monkeypatch.setattr(local_chassis, 'SOURCE', source)
        monkeypatch.setattr(local_chassis, 'RECONNECT_SECONDS', 0.1)
        receiver = LocalChassis()
        receiver.start()
        try:
            await wait_until(lambda: receiver.snapshot()['messageCount'] == 1)
            connections[0][1].close()
            await wait_until(lambda: not receiver.snapshot()['connected'])
            stale = receiver.snapshot()
            assert stale['status'] == 'STALE'
            assert stale['payload']['chassisAlive'] is None
            assert stale['payload']['debug']['fields']['actualVxMps']['value'] is None
            await wait_until(lambda: len(connections) == 2)
            recovery_allowed.set()
            await wait_until(lambda: receiver.snapshot()['messageCount'] == 2)
            assert receiver.snapshot()['status'] == 'AVAILABLE'
            monkeypatch.setattr(local_chassis, 'RECONNECT_SECONDS', 60)
            connections[-1][1].close()
            await wait_until(lambda: not receiver.snapshot()['connected'])
            await stop_client(receiver)
            assert len(connections) == 2
        finally:
            recovery_allowed.set()
            if receiver.thread.is_alive():
                await stop_client(receiver)
            for websocket, _ in connections:
                await websocket.close()
            await runner.cleanup()
    asyncio.run(scenario())


def test_threaded_client_rejects_oversized_message_and_recovers(monkeypatch):
    async def scenario():
        sockets = []

        async def handler(request):
            websocket = web.WebSocketResponse(autoping=False, heartbeat=None)
            await websocket.prepare(request)
            sockets.append(websocket)
            if len(sockets) == 1:
                oversized = dict(frame(0x10), padding='x' * local_chassis.MAX_MESSAGE_BYTES)
                await websocket.send_str(json.dumps(oversized))
            else:
                await websocket.send_json(frame(0x10))
            async for _ in websocket:
                pass
            return websocket

        runner, source = await run_server(handler)
        monkeypatch.setattr(local_chassis, 'SOURCE', source)
        monkeypatch.setattr(local_chassis, 'RECONNECT_SECONDS', 0.05)
        receiver = LocalChassis()
        receiver.start()
        try:
            await wait_until(lambda: len(sockets) >= 2 and receiver.snapshot()['messageCount'] >= 1)
            assert receiver.snapshot()['messageCount'] == 1
            assert receiver.snapshot()['status'] == 'AVAILABLE'
            await stop_client(receiver)
        finally:
            if receiver.thread.is_alive():
                await stop_client(receiver)
            for websocket in sockets:
                await websocket.close()
            await runner.cleanup()
    asyncio.run(scenario())
