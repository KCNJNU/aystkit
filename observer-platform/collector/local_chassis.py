"""Receive-only client for the existing loopback chassis broadcast.

The 17-byte STM32 decoder is independent of the Observer package. Only status
and received frames are consumed; command broadcasts never become feedback.
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from collections import deque

import aiohttp

from .legacy.telemetry import KNOWN_PAGES, SlidingRate

SOURCE = 'ws://127.0.0.1:8765/ws'
SOURCE_DESCRIPTION = '本机底盘服务只读广播 · ' + SOURCE
MAX_MESSAGE_BYTES = 256 * 1024
RECONNECT_SECONDS = 2.0
BRUSHES = ('frontRollerState', 'rearRollerState', 'leftSideBrushState', 'rightSideBrushState')


def decode_frame(text):
    if not isinstance(text, str) or len(text) > 128:
        raise ValueError('Invalid frame text')
    frame = bytes.fromhex(text)
    if len(frame) != 17 or frame[0] != 0x7b or frame[-1] != 0x7d:
        raise ValueError('Invalid frame length/header/tail')
    checksum = 0
    for value in frame[:15]:
        checksum ^= value
    if checksum != frame[15]:
        raise ValueError('Invalid BCC')
    return frame


def bounded_rate(clock):
    rate = SlidingRate(clock=clock)
    rate.samples = deque(maxlen=2048)
    return rate


class LocalChassis:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.connected = False
        self.port_open = None
        self.port = None
        self.error = None
        self.count = self.invalid = self.unknown = self.connection_errors = 0
        self.last = None
        self.fields = {}
        self.pages = {key: {'count': 0, 'last': None, 'rate': bounded_rate(clock)}
                      for key in KNOWN_PAGES}
        self.rate = bounded_rate(clock)
        self.thread = self.loop = self.task = None
        self.stopping = threading.Event()

    def receive(self, msg):
        with self.lock:
            if not isinstance(msg, dict):
                self.invalid += 1
                return
            if msg.get('type') == 'status':
                self.port_open = msg.get('connected') if isinstance(msg.get('connected'), bool) else None
                port = msg.get('port')
                self.port = port if isinstance(port, str) and len(port) <= 256 else None
                return
            if msg.get('type') != 'rx':
                return  # In particular: ignore tx, drive, nav, commands.
            try:
                frame = decode_frame(msg.get('hex'))
            except (ValueError, TypeError):
                self.invalid += 1
                return
            now = self.clock()
            self.count += 1
            self.last = now
            self.rate.add(now)
            page = f'0x{frame[1]:02x}'
            if page not in self.pages:
                self.unknown += 1
                return
            item = self.pages[page]
            item['count'] += 1
            item['last'] = now
            item['rate'].add(now)

            def field(key, value, unit=None):
                self.fields[key] = (value, unit, now, page)

            def word(offset, signed=False):
                return int.from_bytes(frame[offset:offset + 2], 'big', signed=signed)

            voltage = word(9)
            # Voltage zero is the contract's explicit unavailable sentinel.
            field('busVoltage', voltage / 100 if voltage else None, 'V')
            if page == '0x10':
                for key, offset, unit in [('leftWheelMps', 3, 'm/s'), ('rightWheelMps', 5, 'm/s'),
                                          ('actualVxMps', 7, 'm/s'), ('actualWzRadps', 11, 'rad/s')]:
                    field(key, word(offset, True) / 1000, unit)
            elif page == '0x11':
                for index, (name, offset) in enumerate(zip(BRUSHES, (3, 5, 7, 11))):
                    field(name + 'ActualRpm', word(offset, True), 'RPM')
                    field(name + '.online', bool(frame[13] & (1 << index)))
                    field(name + '.commLost', bool(frame[14] & (1 << (index + 4))))
            elif page == '0x12':
                for index in range(2):
                    flags = frame[7 + index]
                    field(f'lift{index + 1}State', {
                        'hallCount': word(3 + index * 2),
                        'direction': ('coast', 'extend', 'retract', 'unknown')[flags & 3],
                        'atMin': bool(flags & 4), 'atMax': bool(flags & 8)})
            elif page == '0x13':
                for index, name in enumerate(BRUSHES):
                    field(name + '.faultCode', frame[3 + index])
                    field(name + '.online', bool(frame[8] & (1 << index)))
                    field(name + '.commLost', bool(frame[7] & (1 << index)))

    def snapshot(self):
        with self.lock:
            now = self.clock()
            age = None if self.last is None else max(0, now - self.last)
            status = ('WAITING_UPSTREAM' if age is None else
                      'AVAILABLE' if self.connected and self.port_open is not False and age <= 2 else 'STALE')
            fields = {}
            payload = {'chassisAlive': True if status == 'AVAILABLE' else None,
                       'debug': {'fields': fields}, 'serialDebug': {
                           'actualOpenDevice': self.port, 'portOpen': self.port_open if self.connected else None,
                           'rxActive': status == 'AVAILABLE', 'frameValid': status == 'AVAILABLE',
                           'validFrameCount': self.count, 'validFramesPerSec': self.rate.value(now),
                           # Upstream discards invalid serial frames before broadcasting.
                           'invalidFrameCount': None, 'checksumErrorCount': None,
                           'collectorRejectedFrames': self.invalid, 'unknownPageCount': self.unknown,
                           'conclusion': '本机底盘服务只读广播；计数仅为 Collector 接收量，上游串口错误计数不可用'}}
            for key, (value, unit, stamp, page) in self.fields.items():
                fresh = status == 'AVAILABLE' and now - stamp <= 2
                state = 'STALE' if not fresh else 'NO_DATA' if value is None else 'AVAILABLE'
                displayed = copy.deepcopy(value) if fresh else None
                fields[key] = {'value': displayed, 'state': state, 'source': f'本机底盘广播 Page {page}',
                               'ageMs': max(0, now - stamp) * 1000, 'unit': unit, 'type': 'ACTUAL'}
                if '.' in key:
                    obj, prop = key.split('.', 1)
                    payload.setdefault(obj, {})[prop] = displayed
                elif key.startswith('lift'):
                    payload[key] = displayed
            pages = {}
            for key, item in self.pages.items():
                page_age = None if item['last'] is None else max(0, now - item['last'])
                pages[key] = {'name': KNOWN_PAGES[key], 'count': item['count'],
                              'hz': item['rate'].value(now), 'ageMs': None if page_age is None else page_age * 1000,
                              'status': 'WAITING_UPSTREAM' if page_age is None else
                              'AVAILABLE' if status == 'AVAILABLE' and page_age <= 2 else 'STALE'}
            return {'status': status, 'source': SOURCE_DESCRIPTION, 'transport': 'LOCAL_WS_RX',
                    'connected': self.connected, 'reason': self.error, 'ageS': age,
                    'hz': self.rate.value(now), 'messageCount': self.count,
                    'parseErrorCount': self.invalid, 'connectionErrorCount': self.connection_errors,
                    'unknownPageCount': self.unknown, 'pages': pages,
                    'payload': payload if self.last is not None else None}

    async def _run(self):
        # A redirect must never turn a fixed loopback receiver into a remote client.
        async def reject_redirect(session, context, params):
            raise aiohttp.ClientConnectionError('Redirect rejected')

        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(reject_redirect)
        timeout = aiohttp.ClientTimeout(total=3, connect=3)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False, trace_configs=[trace],
                                         connector=aiohttp.TCPConnector(limit=1)) as session:
            while not self.stopping.is_set():
                try:
                    # Only passive protocol Pong/Close replies are allowed; no
                    # application messages or active heartbeat are emitted.
                    async with session.ws_connect(SOURCE, autoping=True, heartbeat=None,
                                                  timeout=aiohttp.ClientWSTimeout(ws_close=1),
                                                  max_msg_size=MAX_MESSAGE_BYTES) as ws:
                        with self.lock:
                            self.connected, self.error = True, None
                            self.port_open, self.port = None, None
                        async for raw in ws:
                            if self.stopping.is_set():
                                break
                            if raw.type == aiohttp.WSMsgType.ERROR:
                                raise ws.exception() or RuntimeError('WebSocket receive failed')
                            if raw.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                                continue
                            try:
                                self.receive(json.loads(raw.data))
                            except (ValueError, TypeError, RecursionError):
                                with self.lock:
                                    self.invalid += 1
                except Exception as exc:
                    with self.lock:
                        self.error = type(exc).__name__
                        self.connection_errors += 1
                finally:
                    with self.lock:
                        self.connected = False
                if not self.stopping.is_set():
                    await asyncio.sleep(RECONNECT_SECONDS)

    def start(self):
        def worker():
            async def run():
                with self.lock:
                    self.loop = asyncio.get_running_loop()
                    self.task = asyncio.current_task()
                try:
                    await self._run()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    with self.lock:
                        self.error = type(exc).__name__
                        self.connection_errors += 1
                finally:
                    with self.lock:
                        self.connected = False
            asyncio.run(run())
        with self.lock:
            if self.thread is not None or self.stopping.is_set():
                return
            self.thread = threading.Thread(target=worker, name='collector-passive-chassis', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        with self.lock:
            loop, task, thread = self.loop, self.task, self.thread
        if loop and not loop.is_closed() and task:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker may have closed its loop between these checks.
        if thread:
            thread.join(timeout=3)
