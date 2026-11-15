"""Transport routing recovery without real cloud traffic or device credentials."""
import asyncio
import json
import ssl

import aiohttp
import pytest

from collector.config import Config
from collector.transport import (PROXY_FAILURE_COOLDOWN_SECONDS, PROXY_ROUTE_HOLD_SECONDS,
                                 UploadNetworkRoute, Uploader)

URL = 'https://isolated-network-fixture.invalid/api/v1/ingest'
PROXY = 'http://127.0.0.1:17890'


def test_direct_is_preferred_even_when_environment_proxy_is_dead():
    route = UploadNetworkRoute(URL, {'HTTPS_PROXY': PROXY})
    assert route.proxy_for_request() is None
    route.succeeded(None)
    assert route.proxy_for_request() is None


def test_proxy_fallback_is_cached_instead_of_retrying_direct_for_every_frame():
    now = [0.0]
    route = UploadNetworkRoute(URL, {'HTTPS_PROXY': PROXY}, clock=lambda: now[0])
    route.failed(None, OSError('fixture network unavailable'))
    for _ in range(100):
        assert route.proxy_for_request() == PROXY
        route.succeeded(PROXY)
    now[0] = PROXY_ROUTE_HOLD_SECONDS + 1
    assert route.proxy_for_request() is None
    route.succeeded(None)
    assert route.proxy_for_request() is None


def test_broken_proxy_returns_to_direct_on_next_normal_retry():
    route = UploadNetworkRoute(URL, {'HTTPS_PROXY': PROXY})
    route.failed(None, OSError())
    assert route.proxy_for_request() == PROXY
    route.failed(PROXY, aiohttp.ClientProxyConnectionError(None, OSError('fixture unavailable')))
    assert route.proxy_for_request() is None


def test_failed_proxy_is_not_retried_during_repeated_direct_failures_and_can_recover():
    now = [0.0]
    route = UploadNetworkRoute(URL, {'HTTPS_PROXY': PROXY}, clock=lambda: now[0])
    route.failed(None, asyncio.TimeoutError())
    assert route.proxy_for_request() == PROXY
    route.failed(PROXY, aiohttp.ClientProxyConnectionError(None, OSError('fixture unavailable')))
    for second in range(int(PROXY_FAILURE_COOLDOWN_SECONDS)):
        now[0] = float(second)
        route.failed(None, asyncio.TimeoutError())
        assert route.proxy_for_request() is None
    now[0] = PROXY_FAILURE_COOLDOWN_SECONDS
    route.failed(None, asyncio.TimeoutError())
    assert route.proxy_for_request() == PROXY
    route.succeeded(PROXY)
    now[0] += PROXY_ROUTE_HOLD_SECONDS - .1
    assert route.proxy_for_request() == PROXY
    # Continued proxy success renews its hold rather than forcing a direct
    # attempt in the middle of a working recovered connection.
    route.succeeded(PROXY)
    now[0] += .2
    assert route.proxy_for_request() == PROXY
    now[0] += PROXY_ROUTE_HOLD_SECONDS
    assert route.proxy_for_request() is None


def test_direct_success_does_not_forget_a_failed_proxy_cooldown():
    now = [0.0]
    route = UploadNetworkRoute(URL, {'HTTPS_PROXY': PROXY}, clock=lambda: now[0])
    route.failed(None, OSError())
    route.failed(PROXY, OSError())
    route.succeeded(None)
    now[0] = 1.0
    route.failed(None, asyncio.TimeoutError())
    assert route.proxy_for_request() is None


@pytest.mark.parametrize('error', [ssl.SSLError('fixture trust failed'),
    aiohttp.ClientConnectorCertificateError(None, ssl.CertificateError('fixture trust failed')),
    aiohttp.ServerFingerprintMismatch(b'expected', b'actual', 'fixture.invalid', 443)])
def test_tls_trust_failures_never_switch_to_proxy(error):
    route = UploadNetworkRoute(URL, {'HTTPS_PROXY': PROXY})
    route.failed(None, error)
    assert route.proxy_for_request() is None


@pytest.mark.parametrize('proxy', ['socks5://127.0.0.1:1080', 'http://localhost:bad',
    'http://localhost/?secret=fixture', 'http://localhost/#fixture', 'http://localhost/path', 'http://bad host/'])
def test_invalid_or_unsupported_proxy_does_not_break_healthy_direct_route(proxy):
    route = UploadNetworkRoute(URL, {'HTTPS_PROXY': proxy})
    route.failed(None, OSError())
    assert route.proxy_for_request() is None


def test_loopback_and_explicit_direct_ignore_all_proxy_environment():
    route = UploadNetworkRoute('http://127.0.0.1:12345/api/v1/ingest', {'HTTPS_PROXY': PROXY})
    route.failed(None, OSError())
    assert route.proxy_for_request() is None
    route = UploadNetworkRoute(URL, {'HTTPS_PROXY': PROXY, 'COLLECTOR_NETWORK_MODE': 'direct'})
    route.failed(None, OSError())
    assert route.proxy_for_request() is None
    route = UploadNetworkRoute(URL, {'ALL_PROXY': PROXY, 'HTTP_PROXY': PROXY})
    route.failed(None, OSError())
    assert route.proxy_for_request() is None


def test_explicit_proxy_mode_is_validated_and_does_not_fallback_silently():
    with pytest.raises(ValueError, match='valid explicit HTTPS_PROXY'):
        UploadNetworkRoute(URL, {'COLLECTOR_NETWORK_MODE': 'proxy'})
    with pytest.raises(ValueError, match='COLLECTOR_NETWORK_MODE'):
        UploadNetworkRoute(URL, {'COLLECTOR_NETWORK_MODE': 'typo'})
    route = UploadNetworkRoute(URL, {'COLLECTOR_NETWORK_MODE': 'proxy', 'HTTPS_PROXY': PROXY})
    assert route.proxy_for_request() == PROXY
    route.failed(PROXY, OSError())
    assert route.proxy_for_request() == PROXY


def test_route_failure_logs_do_not_disclose_proxy_credentials_or_error_text(caplog):
    secret_proxy = 'http://fixture-user:fixture-private-password@127.0.0.1:17890'
    route = UploadNetworkRoute(URL, {'HTTPS_PROXY': secret_proxy})
    route.failed(None, OSError('fixture-private-error'))
    route.failed(secret_proxy, OSError('fixture-private-error'))
    assert 'fixture-private' not in caplog.text and secret_proxy not in caplog.text


def test_uploader_reuses_sequence_on_fallback_and_sends_next_packet_only_once(monkeypatch, caplog):
    async def scenario():
        monkeypatch.setenv('HTTPS_PROXY', PROXY)
        monkeypatch.setenv('COLLECTOR_NETWORK_MODE', 'auto')
        uploader = Uploader(Config('fixture-robot', 'fixture-source', cloud_url=URL,
                                   token='fixture-device-private-token-'+'x'*32))
        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 1})
        calls, session_options = [], []

        class Response:
            status = 200

            def __init__(self):
                self.content = self

            async def read(self, _size):
                return b'{}'

        class Attempt:
            def __init__(self, number):
                self.number = number

            async def __aenter__(self):
                if self.number == 1:
                    raise OSError('fixture-private-error-must-not-leak')
                return Response()

            async def __aexit__(self, *_args):
                if uploader.sent == 1:
                    uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 2})
                elif uploader.sent == 2:
                    uploader.stop_event.set()

        class Session:
            def __init__(self, **kwargs):
                self.connector = kwargs['connector']
                session_options.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                await self.connector.close()

            def post(self, url, **kwargs):
                assert url == URL
                assert kwargs['allow_redirects'] is False
                calls.append({'proxy': kwargs['proxy'], 'packet': json.loads(kwargs['data'])})
                return Attempt(len(calls))

        async def no_delay(_seconds):
            await asyncio.sleep(0)

        monkeypatch.setattr(aiohttp, 'ClientSession', Session)
        monkeypatch.setattr(uploader, '_pause', no_delay)
        await asyncio.wait_for(uploader.run(), 2)
        assert [call['proxy'] for call in calls] == [None, PROXY, PROXY]
        assert [call['packet']['sequence'] for call in calls] == [1, 1, 2]
        assert [call['packet']['payload']['uptime_s'] for call in calls] == [1, 1, 2]
        assert session_options[0]['trust_env'] is False
        assert session_options[0]['connector']._ssl is True
        assert uploader.sent == 2 and uploader.sequence == 2

    asyncio.run(scenario())
    assert 'fixture-device-private-token' not in caplog.text
    assert 'fixture-private-error' not in caplog.text
