#!/usr/bin/env python3
"""ECS-only HTTPS account acceptance; removes only its own random test account.

Mount initial-access.json read-only at /run/observer-initial-access.json and the
server data directory at /data. Run with explicit --run-account-check. Secrets
remain inside ECS memory and are never printed. No device ingestion or control.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3

import aiohttp
from verify_live import credentials, require, ORIGIN


class LoopbackResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        require(host == 'aystkit.cn', 'resolver_hostname')
        return [{'hostname': host, 'host': '127.0.0.1', 'port': port, 'family': socket.AF_INET,
                 'proto': socket.IPPROTO_TCP, 'flags': socket.AI_NUMERICHOST}]

    async def close(self):
        pass


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-account-check', action='store_true', required=True)
    parser.parse_args()
    admin_credentials = credentials('/run/observer-initial-access.json')
    data = Path('/data/accounts.sqlite3')
    require(data.is_file(), 'account_database_exists')
    # Keep SQLite side files owned by the running backend, even though the
    # private bootstrap credential must initially be read by root.
    owner = data.stat()
    if os.getuid() == 0:
        os.setgroups([])
        os.setgid(owner.st_gid)
        os.setuid(owner.st_uid)
    username = 'check_' + secrets.token_hex(10)
    password = secrets.token_urlsafe(32)
    created = False
    checks = []
    connector = aiohttp.TCPConnector(resolver=LoopbackResolver())
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {'Origin': ORIGIN}
    try:
        async with aiohttp.ClientSession(connector=connector, connector_owner=False, timeout=timeout) as admin, \
                   aiohttp.ClientSession(connector=connector, connector_owner=False, timeout=timeout) as member:
            async def get(client, path, status):
                async with client.get(ORIGIN + path, allow_redirects=False) as response:
                    require(response.status == status, 'status_' + path)
                    return await response.read()

            async def post(client, path, body, status):
                async with client.post(ORIGIN + path, json=body, headers=headers) as response:
                    require(response.status == status, 'status_' + path)
                    return await response.json()

            require('注册'.encode() in await get(member, '/register', 200), 'register_page')
            registered = await post(member, '/api/register', {'username': username, 'password': password}, 201)
            created = True
            require(registered.get('approved') is False and registered.get('role') == 'viewer', 'pending_default')
            require('当前未获取权限'.encode() in await get(member, '/pending', 200), 'pending_page')
            for path in ['/snapshot.json', '/visuals.json', '/api/robots', '/camera/usbFront.jpg',
                         '/visual/map.png', '/native-camera/status', '/native-camera/detections', '/native-camera/usbFront/video.mp4',
                         '/api/admin/users', '/users']:
                await get(member, path, 403)
            try:
                async with member.ws_connect(ORIGIN.replace('https:', 'wss:') + '/ws', headers=headers):
                    raise AssertionError('pending_websocket_opened')
            except aiohttp.WSServerHandshakeError as exc:
                require(exc.status == 403, 'pending_websocket_denied')
            checks.append('pending_denied_data_media_websocket_admin')
            admin_auth = await post(admin, '/api/login', admin_credentials, 200)
            require(admin_auth.get('role') == 'admin' and admin_auth.get('approved') is True, 'observer_admin')
            listing = json.loads(await get(admin, '/api/admin/users', 200))['users']
            account = next(row for row in listing if row['username'] == username)
            require(account['status'] == 'pending' and not any('password' in key for key in account), 'admin_pending_list')
            await post(admin, f"/api/admin/users/{account['id']}/approve", {}, 200)
            require(json.loads(await get(member, '/api/session', 200)).get('approved') is True, 'approval_current_cookie')
            await get(member, '/', 200)
            await get(member, '/snapshot.json', 200)
            await get(member, '/native-camera/detections', 200)
            await get(member, '/api/admin/users', 403)
            async with member.ws_connect(ORIGIN.replace('https:', 'wss:') + '/ws', headers=headers) as ws:
                require((await ws.receive(timeout=8)).type == aiohttp.WSMsgType.TEXT, 'approved_websocket')
            checks.append('observer_approval_existing_session_viewer_only')
            await post(member, '/api/logout', {}, 200)
            await get(member, '/snapshot.json', 401)
            await get(member, '/native-camera/detections', 401)
            await post(admin, '/api/logout', {}, 200)
            checks.append('logout_revokes_access')
    finally:
        await connector.close()
        if created:
            with sqlite3.connect(data) as db:
                # Match the unguessable username generated only by this run.
                removed = db.execute('DELETE FROM users WHERE username=?', (username,)).rowcount
                require(removed == 1, 'own_test_account_cleanup')
            checks.append('own_test_account_removed')
    print(json.dumps({'pass': True, 'checks': checks}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
