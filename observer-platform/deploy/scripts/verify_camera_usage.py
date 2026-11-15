#!/usr/bin/env python3
"""Read-only production occupancy acceptance; credentials stay inside ECS.

No capture is started and no camera device is opened. Login/logout only change
the verifier's session. TLS hostname validation is retained on the loopback hop.
"""
import asyncio
import math
import ssl
import sys

import aiohttp

from verify_live import COOKIE, CREDENTIALS, ORIGIN, VerificationError, credentials, read_json, report, require
from verify_native_video import KEYS, LoopbackResolver


async def main():
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(), resolver=LoopbackResolver())
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=20), headers={'Origin': ORIGIN},
            cookie_jar=aiohttp.CookieJar()) as session:
        for path in ('/native-camera/status', *(f'/native-camera/{key}/video.mp4' for key in KEYS)):
            async with session.get(ORIGIN + path, allow_redirects=False) as response:
                require(response.status == 401, 'camera_usage_anonymous_denied')
        login, issued = credentials(CREDENTIALS), None
        try:
            async with session.post(ORIGIN + '/api/login', json=login, allow_redirects=False) as response:
                require(response.status == 200, 'camera_usage_login')
                cookie = response.cookies.get(COOKIE)
                require(cookie is not None, 'camera_usage_cookie')
                issued = cookie.value
        finally:
            login.clear()
        try:
            async with session.get(ORIGIN + '/', allow_redirects=False) as response:
                require(response.status == 200, 'camera_usage_site_accessible')
            async with session.get(ORIGIN + '/native-camera/status', allow_redirects=False) as response:
                require(response.status == 200, 'camera_usage_status_response')
                value = await read_json(response)
            require(value.get('enabled') is True, 'camera_usage_enabled')
            usage = value.get('cameraUsage')
            require(isinstance(usage, dict) and set(usage) == set(KEYS), 'camera_usage_fixed_keys')
            for key in KEYS:
                row = usage[key]
                require(isinstance(row, dict) and set(row) == {'owner', 'connected', 'checkedAgeMs'},
                        'camera_usage_redacted_fields')
                owner, connected, age = row['owner'], row['connected'], row['checkedAgeMs']
                require(owner in ('WEB', 'OTHER', 'NONE', 'UNKNOWN'), 'camera_usage_owner')
                require(connected is None or type(connected) is bool, 'camera_usage_connected')
                finite = type(age) in (int, float) and math.isfinite(age) and age >= 0
                require((finite and age < 6000) or (owner == 'UNKNOWN' and connected is None),
                        'camera_usage_no_expired_assertion')
                require(finite and age < 6000 and owner != 'UNKNOWN', 'camera_usage_live_probe_required')
                report('camera_usage:' + key, owner=owner, connected=connected, checkedAgeMs=round(age))
            # Off/occupied sources should not start a stream or capture as a
            # side effect of a viewer checking the current state.
            for key in KEYS:
                if usage[key]['owner'] == 'OTHER' or usage[key]['connected'] is False or key not in value['cameras']:
                    async with session.get(ORIGIN + f'/native-camera/{key}/video.mp4', allow_redirects=False) as response:
                        require(response.status == 503, 'camera_usage_unavailable_video_denied')
        finally:
            async with session.post(ORIGIN + '/api/logout', allow_redirects=False) as response:
                require(response.status == 200, 'camera_usage_logout')
            session.cookie_jar.clear()
            async with session.get(ORIGIN + '/native-camera/status', cookies={COOKIE: issued},
                                   allow_redirects=False) as response:
                require(response.status == 401, 'camera_usage_revoked_session')
        report('camera_usage_acceptance', status='PASS', scope='ECS_LOOPBACK_TLS_NOT_VIEWER_WAN',
               siteAccessible=True, captureStarted=False, pixelsSaved=False)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except VerificationError as error:
        report('camera_usage_acceptance', status='FAIL', reason=str(error))
        sys.exit(1)
    except Exception as error:
        report('camera_usage_acceptance', status='FAIL', exception=type(error).__name__)
        sys.exit(1)
