"""Cloud-only authenticated Observer endpoints. Never subscribes or controls ROS."""
from __future__ import annotations

import asyncio
import csv
import io
import time
from datetime import datetime, timezone
from dataclasses import replace
import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from aiohttp import WSMsgType, web

from shared.protocol import (CAMERA_KEYS, MAP_KEYS, MAX_REQUEST_BYTES,
                             ProtocolError, decode_envelope)
from .security import Config, RateLimiter, Sessions, hash_password, verify_password
from .accounts import Accounts, AccountCapacity, AccountConflict, normalize_username
from .store import ReplayError, Store, VISUAL_GROUPS
from .fault_logs import (RETENTION_DAYS, MAX_RECORDS, MAX_OCCURRENCES, parse_filters,
                         CSV_HEADER, csv_row, csv_cell)
from .test_stats import TestConflict, parse_days, validate_tester_name
from .test_issues import normalize_review, validate_issue
from .native_video import NATIVE_VIDEO, NativeVideo, accepts_detection_gzip, split_socket_option
from .dbprog import install_dbprog

CONFIG = web.AppKey('config', Config)
STORE = web.AppKey('store', Store)
FLEET_STORES = web.AppKey('fleet_stores', dict)
FLEET_NATIVE = web.AppKey('fleet_native', dict)
ACCOUNTS = web.AppKey('accounts', Accounts)
SESSIONS = web.AppKey('sessions', Sessions)
LIMITER = web.AppKey('limiter', RateLimiter)
SOCKETS = web.AppKey('sockets', dict)
HASH_SLOTS = web.AppKey('hash_slots', asyncio.Semaphore)
EXPORT_SLOTS = web.AppKey('export_slots', asyncio.Semaphore)
COOKIE = 'observer_session'
PUBLIC_ASSETS = frozenset({'fleet.js', 'app.css', 'app.js', 'formatters.js', 'charts.js', 'media.js',
                           'cloud.js', 'cloud.css', 'login.js', 'native-player.js',
                           'access.css', 'access.js', 'detection-overlay.js', 'video-lease.js',
                           'fault-logs.js', 'fault-logs.css', 'daily-tests.js', 'daily-tests.css',
                           'dbprog.js', 'dbprog.css'})


def json_response(value, status=200):
    return web.json_response(value, status=status,
        dumps=lambda data: json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False))



def _observation_response(request, value, *, group=None):
    """Compress observed data and validate frame semantics without renewing age."""
    response = json_response(value)
    response.headers['Vary'] = 'Accept-Encoding'
    if group is not None:
        # Ages necessarily change between reads of the same frame. A weak
        # validator identifies the same preview, not byte-identical JSON.
        stable = {key: {name: item for name, item in fields.items()
                        if name not in ('ageS', 'uploadAgeS')}
                  for key, fields in value.items()}
        digest = hashlib.sha256(json.dumps([group, stable], ensure_ascii=False,
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        tag = 'W/"' + digest + '"'
        response.headers['ETag'] = tag
        supplied = request.headers.get('If-None-Match', '')
        # Bound parsing even if a client supplies a long validator list. Wildcard
        # conditionals never mean "the displayed sample is still current".
        candidates = supplied.split(',') if len(supplied) <= 4096 else []
        if request.method == 'GET' and tag in (item.strip() for item in candidates):
            return web.Response(status=304, headers={'ETag': tag, 'Vary': 'Accept-Encoding'})
    if request.method == 'GET' and accepts_detection_gzip(request.headers):
        response.body = gzip.compress(response.body, compresslevel=4, mtime=0)
        response.headers['Content-Encoding'] = 'gzip'
    return response


def _robot_store(request):
    selected = request.headers.getall('X-Observer-Robot', [])
    if len(selected) > 1:
        raise web.HTTPBadRequest(reason='duplicate robot selection')
    robot = selected[0] if selected else request.app[CONFIG].robot_id
    if request.path == '/ws' and 'robotId' in request.query:
        values = request.query.getall('robotId')
        if len(values) != 1 or selected and selected[0] != values[0]:
            raise web.HTTPBadRequest(reason='conflicting robot selection')
        robot = values[0]
    stores = request.app.get(FLEET_STORES, {request.app[CONFIG].robot_id: request.app[STORE]})
    if robot not in stores:
        raise web.HTTPNotFound(reason='robot is not registered')
    return stores[robot]


def _all_native(app):
    return app.get(FLEET_NATIVE, {app[CONFIG].robot_id: app[NATIVE_VIDEO]}).values()


async def _native_status(request):
    return await request.app[FLEET_NATIVE][_robot_store(request).config.robot_id].status(request)


async def _native_detections(request):
    return await request.app[FLEET_NATIVE][_robot_store(request).config.robot_id].detections(request)


async def _native_stream(request):
    return await request.app[FLEET_NATIVE][_robot_store(request).config.robot_id].video(request)


def _same_origin(request):
    return request.headers.get('Origin') == request.app[CONFIG].public_origin


def _identity_for_cookie(app, cookie):
    value = app[SESSIONS].read(cookie)
    if not value:
        return None
    if value['u'] == app[CONFIG].viewer_username:
        return {**value, 'role': 'admin', 'approved': True, 'status': 'approved'}
    account = app[ACCOUNTS].find(value['u'])
    if account is None or type(value.get('a')) is not int or value['a'] != account['id']:
        return None
    return {**value, 'role': 'viewer', 'approved': account['status'] == 'approved',
            'status': account['status']}


def _identity(request):
    value = _identity_for_cookie(request.app, request.cookies.get(COOKIE))
    if value is None:
        raise web.HTTPUnauthorized(text='viewer session required')
    return value


def _viewer(request):
    value = _identity(request)
    if not value['approved']:
        raise web.HTTPForbidden(reason='当前未获取权限')
    return value


def _administrator(request):
    value = _viewer(request)
    if value['role'] != 'admin':
        raise web.HTTPForbidden(reason='仅管理员可管理账号')
    return value


def _approved_cookie(app, cookie):
    try:
        value = _identity_for_cookie(app, cookie)
    except sqlite3.Error:
        # Stream watchdogs must stop rather than lose their authorization check.
        logging.getLogger(__name__).error('Cloud account authorization storage unavailable')
        return None
    return value if value is not None and value['approved'] else None


def _session_value(value):
    return {'authenticated': True, 'username': value['u'], 'role': value['role'],
            'approved': value['approved'], 'status': value['status']}


def _response_security_headers(request, response):
    if isinstance(response, web.WebSocketResponse):
        return
    response.headers.update({'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        'Permissions-Policy': 'camera=(), microphone=(), geolocation=()'})
    if request.path == '/dbprog' or request.path.startswith('/dbprog/'):
        # The original warehouse hashes upload chunks with WebAssembly. Keep
        # this permission scoped to its mounted application.
        response.headers['Content-Security-Policy'] = response.headers['Content-Security-Policy'].replace(
            "script-src 'self';", "script-src 'self' 'wasm-unsafe-eval';")
    if request.app[CONFIG].public_origin.startswith('https://'):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'


async def _prepare_security_headers(request, response):
    # Streaming responses prepare inside their handler. Middleware runs too
    # late to add headers to bytes already on the wire, so cover that path too.
    _response_security_headers(request, response)


@web.middleware
async def security_headers(request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        if exc.status == 302:
            response = web.Response(status=exc.status, headers=exc.headers, text=exc.text)
        else:
            response = json_response({'error': exc.reason,
                **({'authenticated': False} if exc.status == 401 else {})}, status=exc.status)
    except (ProtocolError, ValueError) as exc:
        # Validation errors have no request/body interpolation.
        response = json_response({'error': str(exc)}, status=400)
    except sqlite3.Error:
        logging.getLogger(__name__).error('Cloud SQLite operation failed')
        response = json_response({'error': 'storage unavailable'}, status=503)
    _response_security_headers(request, response)
    return response


@web.middleware
async def transport_and_limits(request, handler):
    config = request.app[CONFIG]
    path = request.path
    if path not in ('/healthz', '/readyz') and not config.is_secure(request):
        raise web.HTTPForbidden(text='HTTPS is required')
    limiter = request.app[LIMITER]
    peer = config.client_ip(request)
    if path in ('/api/login', '/api/dbprog/login', '/dbprog/api/v1/auth/login') and request.method == 'POST':
        if not limiter.allow(('login', peer), 5 / 60, 5) or not limiter.allow(('login-global',), 20 / 60, 10):
            raise web.HTTPTooManyRequests()
    elif path == '/api/register' and request.method == 'POST':
        if not limiter.allow(('register', peer), 3 / 60, 3) or not limiter.allow(('register-global',), 10 / 60, 10):
            raise web.HTTPTooManyRequests()
    elif path == '/api/v1/ingest':
        if not limiter.allow(('ingest-ip', peer), 60, 100):
            raise web.HTTPTooManyRequests()
    elif not limiter.allow(('read', peer), 60, 120):
        raise web.HTTPTooManyRequests()
    if path.startswith('/dbprog/api/'):
        maximum = 16 * 1024 * 1024
        if (request.content_length or 0) > maximum:
            raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=request.content_length)
        request = request.clone(client_max_size=maximum)
    return await handler(request)


async def _json_object(request, fields, maximum=8192):
    if not _same_origin(request):
        raise web.HTTPForbidden(text='origin mismatch')
    if request.content_type != 'application/json' or (request.content_length or 0) > maximum:
        raise web.HTTPBadRequest()
    raw = await request.read()
    if len(raw) > maximum:
        raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=len(raw))
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise web.HTTPBadRequest() from exc
    if not isinstance(value, dict) or set(value) != fields:
        raise web.HTTPBadRequest()
    return value


async def _credentials(request):
    value = await _json_object(request, {'username', 'password'})
    if not all(isinstance(v, str) for v in value.values()):
        raise web.HTTPBadRequest()
    if not value['password']:
        raise web.HTTPBadRequest(reason='密码不能为空')
    try:
        value['password'].encode('utf-8')
    except UnicodeError as exc:
        raise web.HTTPBadRequest(reason='密码包含无效字符') from exc
    return value


def _administrator_password(app):
    config = app[CONFIG]
    return app[ACCOUNTS].administrator_password(config.viewer_username, config.viewer_password_hash)


def _signed_in(request, username, status=200, *, account_id=None, password_hash=None):
    config = request.app[CONFIG]
    if username != config.viewer_username:
        current = request.app[ACCOUNTS].find(username)
        if (current is None or current['id'] != account_id
                or password_hash is not None and current['password_hash'] != password_hash):
            # Password verification yields to a worker thread. An account may
            # have been deleted/re-created while that work was in progress.
            raise web.HTTPUnauthorized()
    elif password_hash is not None and _administrator_password(request.app) != password_hash:
        raise web.HTTPUnauthorized()
    old_cookie = request.cookies.get(COOKIE)
    previous = request.app[SESSIONS].read(old_cookie)
    request.app[SESSIONS].revoke(old_cookie)
    if previous:
        [native.revoke(previous['n']) for native in _all_native(request.app)]
    cookie = request.app[SESSIONS].issue(username, account_id=account_id)
    response = json_response(_session_value(_identity_for_cookie(request.app, cookie)), status=status)
    response.set_cookie(COOKIE, cookie, httponly=True, secure=not config.allow_insecure_local,
                        samesite='Strict', max_age=config.session_ttl_s, path='/')
    return response


async def _login(request):
    value = await _credentials(request)
    config = request.app[CONFIG]
    candidate = value['username'].strip()
    administrator = candidate.lower() == config.viewer_username.lower()
    account = None
    if not administrator:
        try:
            account = request.app[ACCOUNTS].find(normalize_username(candidate))
        except ValueError:
            pass
    # Unknown usernames perform the same bounded scrypt work as existing ones.
    encoded = _administrator_password(request.app) if administrator or account is None else account['password_hash']
    slots = request.app[HASH_SLOTS]
    if slots.locked():
        raise web.HTTPTooManyRequests()
    async with slots:
        valid = await asyncio.to_thread(verify_password, value['password'], encoded)
    if not valid or (not administrator and account is None):
        raise web.HTTPUnauthorized()
    return _signed_in(request, config.viewer_username if administrator else account['username'],
                      account_id=None if administrator else account['id'], password_hash=encoded)


async def _register(request):
    value = await _credentials(request)
    username = normalize_username(value['username'])
    if username == request.app[CONFIG].viewer_username.lower():
        raise web.HTTPConflict(reason='用户名已被使用')
    slots = request.app[HASH_SLOTS]
    if slots.locked():
        raise web.HTTPTooManyRequests()
    async with slots:
        encoded = await asyncio.to_thread(hash_password, value['password'])
    try:
        account = request.app[ACCOUNTS].register(username, encoded)
    except AccountConflict as exc:
        raise web.HTTPConflict(reason=str(exc)) from exc
    except AccountCapacity as exc:
        raise web.HTTPServiceUnavailable(reason=str(exc)) from exc
    return _signed_in(request, username, status=201, account_id=account['id'], password_hash=encoded)


async def _users(request):
    _administrator(request)
    return json_response({'users': request.app[ACCOUNTS].list_users()})


async def _approve(request):
    administrator = _administrator(request)
    await _json_object(request, set(), maximum=64)
    value = request.match_info['id']
    if not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) < 1:
        raise web.HTTPNotFound()
    user = request.app[ACCOUNTS].approve(int(value), administrator['u'])
    if user is None:
        raise web.HTTPNotFound()
    return json_response({'user': user})


async def _delete_user(request):
    administrator = _administrator(request)
    await _json_object(request, set(), maximum=64)
    value = request.match_info['id']
    if not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) < 1:
        raise web.HTTPNotFound()
    user = request.app[ACCOUNTS].delete(int(value), administrator['u'])
    if user is None:
        raise web.HTTPNotFound()
    await _revoke_user_sessions(request.app, user['username'])
    return json_response({'deleted': True, 'user': user})


async def _revoke_user_sessions(app, username):
    sockets = []
    for nonce in app[SESSIONS].revoke_user(username):
        [native.revoke(nonce) for native in _all_native(app)]
        sockets.extend(list(app[SOCKETS].get(nonce, ())))
    if sockets:
        # Authorization is already revoked. Slow/abandoned tabs must not turn
        # a committed deletion into a browser timeout or delay other tabs.
        try:
            await asyncio.wait_for(asyncio.gather(*[
                socket.close(code=1008, message=b'account deleted') for socket in sockets],
                return_exceptions=True), timeout=2)
        except asyncio.TimeoutError:
            pass  # Existing per-stream watchdogs also fail closed.


def _password_value(value):
    if not isinstance(value, str) or not value:
        raise web.HTTPBadRequest(reason='密码不能为空')
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise web.HTTPBadRequest(reason='密码包含无效字符') from error
    return value


def _password_limit(request, username):
    limiter = request.app[LIMITER]
    if (not limiter.allow(('password-change', username), 5 / 60, 5)
            or not limiter.allow(('password-change-global',), 20 / 60, 10)):
        raise web.HTTPTooManyRequests()


async def _change_password(request):
    session = _identity(request)
    value = await _json_object(request, {'currentPassword', 'newPassword'})
    session = _identity(request)
    old_password = _password_value(value['currentPassword'])
    new_password = _password_value(value['newPassword'])
    _password_limit(request, session['u'])
    admin = session['role'] == 'admin'
    account = None if admin else request.app[ACCOUNTS].find(session['u'])
    encoded = _administrator_password(request.app) if admin else account['password_hash']
    slots = request.app[HASH_SLOTS]
    if slots.locked():
        raise web.HTTPTooManyRequests()
    async with slots:
        valid = await asyncio.to_thread(verify_password, old_password, encoded)
        if not valid:
            raise web.HTTPBadRequest(reason='当前密码不正确')
        new_hash = await asyncio.to_thread(hash_password, new_password)
    # Hashing yields control: deletion or another password change wins safely.
    session = _identity(request)
    if admin:
        changed = request.app[ACCOUNTS].change_administrator_password(session['u'], encoded,
            new_hash, request.app[CONFIG].viewer_password_hash)
    else:
        changed = request.app[ACCOUNTS].change_password(account['id'], encoded, new_hash)
    if not changed:
        raise web.HTTPConflict(reason='密码已更新，请重新登录后再试')
    await _revoke_user_sessions(request.app, session['u'])
    response = json_response({'changed': True, 'reauthenticate': True})
    response.del_cookie(COOKIE, path='/', secure=not request.app[CONFIG].allow_insecure_local,
                        httponly=True, samesite='Strict')
    return response


async def _reset_user_password(request):
    administrator = _administrator(request)
    value = await _json_object(request, {'newPassword'})
    administrator = _administrator(request)
    user_id = request.match_info['id']
    if not user_id.isascii() or not user_id.isdecimal() or len(user_id) > 18 or int(user_id) < 1:
        raise web.HTTPNotFound()
    password = _password_value(value['newPassword'])
    account = next((row for row in request.app[ACCOUNTS].list_users() if row['id'] == int(user_id)), None)
    if account is None:
        raise web.HTTPNotFound()
    if account['username'].casefold() == administrator['u'].casefold():
        raise web.HTTPConflict(reason='请使用修改密码入口更改自己的密码')
    existing = request.app[ACCOUNTS].find(account['username'])
    _password_limit(request, administrator['u'])
    slots = request.app[HASH_SLOTS]
    if slots.locked():
        raise web.HTTPTooManyRequests()
    async with slots:
        encoded = await asyncio.to_thread(hash_password, password)
    _administrator(request)
    if not request.app[ACCOUNTS].change_password(account['id'], existing['password_hash'], encoded):
        raise web.HTTPConflict(reason='账号或密码已更新，请刷新后重试')
    await _revoke_user_sessions(request.app, account['username'])
    return json_response({'changed': True, 'user': account})


async def _logout(request):
    session = _identity(request)
    if not _same_origin(request):
        raise web.HTTPForbidden()
    request.app[SESSIONS].revoke(request.cookies.get(COOKIE))
    [native.revoke(session['n']) for native in _all_native(request.app)]
    response = json_response({'authenticated': False})
    response.del_cookie(COOKIE, path='/', secure=not request.app[CONFIG].allow_insecure_local,
                        httponly=True, samesite='Strict')
    return response


async def _session(request):
    return json_response(_session_value(_identity(request)))


async def _index(request):
    try:
        value = _identity(request)
    except web.HTTPUnauthorized:
        raise web.HTTPFound('/login')
    if not value['approved']:
        raise web.HTTPFound('/pending')
    return _frontend(request, 'index.html')


def _frontend(request, name):
    path = Path(request.app[CONFIG].frontend_dir) / name
    if not path.is_file():
        raise web.HTTPNotFound()
    # Read content rather than FileResponse preparation so security headers survive.
    mime = 'text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'application/javascript'
    return web.Response(body=path.read_bytes(), content_type=mime, charset='utf-8')


async def _login_page(request):
    return _frontend(request, 'login.html')


async def _register_page(request):
    return _frontend(request, 'register.html')


async def _pending_page(request):
    try:
        value = _identity(request)
    except web.HTTPUnauthorized:
        raise web.HTTPFound('/login')
    if value['approved']:
        raise web.HTTPFound('/')
    return _frontend(request, 'pending.html')


async def _users_page(request):
    try:
        _administrator(request)
    except web.HTTPUnauthorized:
        raise web.HTTPFound('/login')
    return _frontend(request, 'users.html')


async def _password_page(request):
    try:
        _identity(request)
    except web.HTTPUnauthorized:
        raise web.HTTPFound('/login')
    return _frontend(request, 'password.html')


async def _coverage_page(request):
    try:
        identity = _identity(request)
    except web.HTTPUnauthorized:
        raise web.HTTPFound('/login')
    if not identity['approved']:
        raise web.HTTPFound('/pending')
    return _frontend(request, 'coverage/index.html')


async def _coverage_asset(request):
    _viewer(request)
    # A flat build directory only; never expose source or arbitrary paths.
    response = _frontend(request, 'coverage/assets/' + request.match_info['filename'])
    response.headers['Vary'] = 'Accept-Encoding'
    if accepts_detection_gzip(request.headers):
        response.body = gzip.compress(response.body, compresslevel=6, mtime=0)
        response.headers['Content-Encoding'] = 'gzip'
    return response


SENSOR_LIBRARY_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.svg': 'image/svg+xml',
    '.pdf': 'application/pdf',
    '.md': 'text/plain; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.zip': 'application/zip',
}


def _sensor_library_file(request, name):
    # The library contains nested vendor documents, unlike the flat coverage
    # build. Validate decoded components before resolving; never serve hidden
    # files, encoded traversal, links or files outside this dedicated directory.
    parts = name.split('/')
    if (not name or any(not part or part.startswith('.') for part in parts)
            or '%' in name or '\\' in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise web.HTTPNotFound()
    mime = SENSOR_LIBRARY_TYPES.get(Path(name).suffix.lower())
    if mime is None:
        raise web.HTTPNotFound()
    try:
        root = Path(request.app[CONFIG].frontend_dir).resolve(strict=True) / 'sensor-library'
        path = root
        if root.is_symlink():
            raise web.HTTPNotFound()
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise web.HTTPNotFound()
        path = path.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise web.HTTPNotFound()
    except (OSError, RuntimeError):
        raise web.HTTPNotFound()
    headers = {'Content-Type': mime}
    if path.suffix.lower() in {'.pdf', '.zip'}:
        # RFC 5987 preserves Chinese vendor filenames without placing Unicode
        # or control characters directly in the HTTP header.
        headers['Content-Disposition'] = (
            'attachment; filename="document' + path.suffix.lower()
            + '"; filename*=UTF-8\'\'' + quote(path.name, safe=''))
    # on_response_prepare applies the same security headers to FileResponse,
    # including range responses, without loading complete PDFs/ZIPs into RAM.
    return web.FileResponse(path, headers=headers)


async def _sensor_library_page(request):
    try:
        identity = _identity(request)
    except web.HTTPUnauthorized:
        raise web.HTTPFound('/login')
    if not identity['approved']:
        raise web.HTTPFound('/pending')
    if request.path == '/sensor-library':
        location = request.rel_url.with_path('/sensor-library/').with_query(request.rel_url.query)
        raise web.HTTPFound(location=str(location))
    return _sensor_library_file(request, 'index.html')


async def _sensor_library_asset(request):
    _viewer(request)
    return _sensor_library_file(request, request.match_info['filename'])


async def _asset(request):
    name = request.match_info['name']
    if name not in PUBLIC_ASSETS:
        raise web.HTTPNotFound()
    return _frontend(request, name)


async def _health(request):
    return json_response({'status': 'ok'})


async def _favicon(request):
    return web.Response(status=204)


async def _ready(request):
    return json_response({'status': 'ready' if all(store.ready() for store in request.app[FLEET_STORES].values()) and request.app[ACCOUNTS].ready() else 'not_ready'})


async def _robots(request):
    _viewer(request)
    return json_response({'defaultRobotId': request.app[CONFIG].robot_id, 'robots': [
        dict(store.cloud_status(), name=request.app[CONFIG].fleet.get(robot, {}).get('name', robot),
             warehouseId=request.app[CONFIG].fleet.get(robot, {}).get('warehouseId'))
        for robot, store in request.app[FLEET_STORES].items()]})


async def _snapshot(request):
    _viewer(request)
    return _observation_response(request, _robot_store(request).snapshot())


def _visual_preview_seen(request, group):
    """A bounded, explicit cursor contains only public preview digests."""
    values = request.headers.getall('X-Visual-Seen', [])
    if not values:
        return {}
    if len(values) != 1:
        raise ValueError('Invalid visual preview cursor')
    try:
        size = len(values[0].encode('utf-8'))
    except UnicodeError:
        raise ValueError('Invalid visual preview cursor') from None
    if size > 2048:
        raise ValueError('Invalid visual preview cursor')

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Invalid visual preview cursor')
            value[key] = item
        return value

    try:
        value = json.loads(values[0], object_pairs_hook=unique)
    except (ValueError, RecursionError):
        raise ValueError('Invalid visual preview cursor') from None
    allowed = VISUAL_GROUPS[group]
    if (not isinstance(value, dict) or not value.keys() <= allowed
            or any(not isinstance(tag, str) or re.fullmatch(r'[0-9a-f]{64}', tag) is None
                   for tag in value.values())):
        raise ValueError('Invalid visual preview cursor')
    return value


def _visual_preview_delta(items, seen):
    """Omit only points the caller identifies exactly; never omit live status."""
    result = {}
    for key, item in items.items():
        value = dict(item, pointsUnchanged=False)
        value.pop('previewTag', None)
        points = value.get('points')
        if value.get('status') == 'AVAILABLE' and isinstance(points, list) and points:
            count = min(768, len(points))
            selected = points if len(points) <= count else [
                points[index * (len(points) - 1) // (count - 1)] for index in range(count)]
            value.update(points=selected, previewPointCount=count, previewInputPoints=len(points))
            # Bind geometry and its coordinate interpretation, including source
            # and session through frameIdentity. A reused frameSeq cannot hide
            # changed coordinates, a changed topic or a changed frameId.
            payload = [key, value.get('frameIdentity'), value.get('frameId'),
                       value.get('topic'), selected]
            tag = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            value['previewTag'] = tag
            if seen.get(key) == tag:
                value.pop('points')
                value['pointsUnchanged'] = True
        elif value.get('status') != 'AVAILABLE':
            value.pop('points', None)
        result[key] = value
    return result


async def _visuals(request):
    _viewer(request)
    query = request.query
    if (any(key not in ('group', 'delta') for key in query)
            or any(len(query.getall(key, [])) > 1 for key in ('group', 'delta'))):
        raise ValueError('Invalid visual group query')
    group = query.get('group')
    if group is not None and group not in (*VISUAL_GROUPS, 'overview'):
        raise ValueError('Unknown visual group')
    delta = query.get('delta')
    if delta is not None and (delta != '1' or group not in ('clouds', 'scans')):
        raise ValueError('Invalid visual delta query')
    seen = _visual_preview_seen(request, group) if delta is not None else None
    items = _robot_store(request).visuals(keys=VISUAL_GROUPS[group] if group in VISUAL_GROUPS else None)
    if delta is not None:
        # Always return the small current metadata, even when all arrays match.
        # A group-level validator cannot describe independently updated streams.
        return _observation_response(request, _visual_preview_delta(items, seen))
    if group != 'overview':
        return _observation_response(request, items, group=group or 'all')
    # The overview is a bounded preview of one real source, never a second
    # full-resolution subscription to every cloud, scan and map on the robot.
    keys = ('lidar', 'registered', 'depthPoints', 'bluesea', 'scan')
    usable = lambda key: items.get(key, {}).get('status') == 'AVAILABLE' and bool(items[key].get('points'))
    # Keep the preferred real source while it is available. Choosing by age
    # switches coordinate frames whenever asynchronous publishers alternate.
    selected = next((key for key in keys if usable(key)), None)
    fields = ('status', 'kind', 'ageS', 'totalPoints', 'frameId', 'source', 'topic',
              'hz', 'sourceHz', 'previewHz', 'message_timestamp', 'received_timestamp',
              'source_id', 'reason', 'label', 'frameIdentity', 'ttlS', 'sourceAgeS', 'uploadAgeS')
    summary = {}
    for key in (selected,) if selected else keys:
        item = items.get(key, {})
        summary[key] = {name: (value[:256] if isinstance(value, str) else value)
                        for name in fields if isinstance((value := item.get(name)), (str, int, float, bool))}
    original = items[selected]['points'] if selected else []
    count = min(768, len(original))
    while True:
        if selected:
            points = original[:count]
            if len(original) > count > 1:
                points = [original[index * (len(original) - 1) // (count - 1)] for index in range(count)]
            summary[selected].update(points=points, previewPointCount=len(points), previewInputPoints=len(original))
        response = json_response(summary)
        if len(response.body) <= 48 * 1024 or count <= 1:
            break
        count //= 2
    return _observation_response(request, summary, group='overview')


async def _image(request):
    _viewer(request)
    key = request.match_info['key']
    camera = request.path.startswith('/camera/')
    if key not in (CAMERA_KEYS if camera else MAP_KEYS):
        raise web.HTTPNotFound()
    frame = _robot_store(request).image_frame(key)
    if frame is None or frame['binary'] is None:
        return web.Response(status=204)
    headers = {'X-Frame-Sequence': frame['identity'],
               'X-Frame-Age-S': str(frame['metadata']['ageS']),
               'X-Frame-TTL-S': 'event' if key == 'map' else '5.0' if not camera else str(request.app[CONFIG].upstream_timeout_s)}
    if camera:
        headers['X-Frame-Image-SHA256'] = hashlib.sha256(frame['binary']).hexdigest()
    # A validator binds the key, source frame and exact JPEG/PNG bytes.
    # Authentication and freshness were checked before any conditional reply.
    digest = hashlib.sha256((key + '\0' + frame['identity'] + '\0').encode('utf-8'))
    digest.update(frame['binary'])
    headers['ETag'] = '"' + digest.hexdigest() + '"'
    conditions = request.headers.getall('If-None-Match', [])
    if len(conditions) == 1 and conditions[0] == headers['ETag']:
        return web.Response(status=304, headers=headers)
    return web.Response(body=frame['binary'], content_type='image/jpeg' if camera else 'image/png',
                        headers=headers)


async def _ingest(request):
    # Never accept credentials from URL/query, viewer cookie, or a plaintext path.
    if request.query:
        raise web.HTTPBadRequest()
    authorization = request.headers.get('Authorization', '')
    if not authorization.startswith('Bearer ') or not 32 <= len(authorization[7:]) <= 512:
        raise web.HTTPUnauthorized()
    device = request.app[CONFIG].device_for_token(authorization[7:])
    if device is None:
        raise web.HTTPUnauthorized()
    if not request.app[LIMITER].allow(('device', device.robot_id, device.source_id), 30, 45):
        raise web.HTTPTooManyRequests()
    if request.content_type != 'application/json':
        raise web.HTTPUnsupportedMediaType()
    if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_REQUEST_BYTES, actual_size=request.content_length)
    # aiohttp enforces the same limit while reading chunked bodies.
    envelope = decode_envelope(await request.read())
    if envelope['robot_id'] != device.robot_id or envelope['source_id'] != device.source_id:
        raise web.HTTPForbidden()
    if envelope['kind'] in ('observation','test_metrics') and device.role != 'primary':
        raise web.HTTPForbidden()
    if envelope['kind'] == 'media' and envelope['payload']['key'] not in device.media_keys:
        raise web.HTTPForbidden()
    try:
        result = request.app[FLEET_STORES][device.robot_id].ingest(envelope)
    except ReplayError as exc:
        return json_response({'error': str(exc), 'reason': exc.reason,
                              'expected_sequence': exc.expected_sequence}, status=409)
    return json_response(result)


async def _test_stats(request):
    session = _viewer(request)
    store = _robot_store(request)
    snapshot = store.tests_snapshot(parse_days(request.query),
        actor=session['u'], read_all=session['role'] == 'admin')
    # Keep the daily-test page to one authoritative read.  This prevents the
    # issue handoff list from lagging behind the statistics snapshot and avoids
    # an extra polling request every five seconds.
    snapshot['test_issues'] = store.test_issues.list(
        # Keep all carry-forward items visible even when an issue has remained
        # unresolved for more than the chart's default 30-day window.
        actor=session['u'], read_all=session['role'] == 'admin', days=365)
    return json_response(snapshot)


async def _delete_test_run(request):
    session = _administrator(request)
    if request.query:
        raise web.HTTPBadRequest()
    await _json_object(request, set(), maximum=64)
    session = _administrator(request)
    if not request.app[LIMITER].allow(('test-delete', session['u']), 2, 8):
        raise web.HTTPTooManyRequests()
    try:
        deleted = _robot_store(request).tests.delete_run(request.match_info['id'], administrator=session['u'])
    except ValueError as exc:
        if isinstance(exc, TestConflict):
            return json_response({'error': str(exc), 'revision': exc.revision}, status=409)
        raise web.HTTPBadRequest() from exc
    if not deleted:
        raise web.HTTPNotFound()
    return json_response({'deleted': True, 'id': request.match_info['id']})


async def _test_issues(request):
    """List operator notes and the reviews required before today's test."""
    session = _viewer(request)
    days = parse_days(request.query)
    return json_response(_robot_store(request).test_issues.list(
        actor=session['u'], read_all=session['role'] == 'admin', days=days))


async def _issue_json(request, *, review=False):
    session = _viewer(request)
    if request.query or not _same_origin(request):
        raise web.HTTPBadRequest() if request.query else web.HTTPForbidden(reason='origin mismatch')
    if request.content_type != 'application/json' or (request.content_length or 0) > 8192:
        raise web.HTTPBadRequest()
    raw = await request.read()
    if len(raw) > 8192:
        raise web.HTTPRequestEntityTooLarge(max_size=8192, actual_size=len(raw))
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise web.HTTPBadRequest() from exc
    try:
        if review:
            normalize_review(value)
        else:
            validate_issue(value)
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    # Reading a request body can await a slow client while an administrator
    # deletes its account. Recheck immediately before any business mutation.
    return _viewer(request), value


async def _test_issue_create(request):
    session, value = await _issue_json(request)
    if not request.app[LIMITER].allow(('test-issues-write', session['u']), 20 / 60, 20):
        raise web.HTTPTooManyRequests()
    return json_response(_robot_store(request).test_issues.add(value, actor=session['u']), status=201)


async def _test_issue_review(request):
    session, value = await _issue_json(request, review=True)
    if not request.app[LIMITER].allow(('test-issues-review', session['u']), 30 / 60, 30):
        raise web.HTTPTooManyRequests()
    result = _robot_store(request).test_issues.review(
        request.match_info['issue_id'], value, actor=session['u'], read_all=session['role'] == 'admin')
    if result is None:
        raise web.HTTPNotFound(reason='test issue not found')
    return json_response(result)


async def _test_control(request):
    session = _viewer(request)
    if request.query:
        raise web.HTTPBadRequest(reason='invalid test control parameters')
    if not request.app[LIMITER].allow(('test-control',session['u']),2,8):
        raise web.HTTPTooManyRequests()
    if not _same_origin(request):
        raise web.HTTPForbidden(reason='origin mismatch')
    if request.content_type != 'application/json' or (request.content_length or 0)>8192:
        raise web.HTTPBadRequest()
    raw=await request.read()
    if len(raw)>8192:
        raise web.HTTPRequestEntityTooLarge(max_size=8192,actual_size=len(raw))
    session = _viewer(request)
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate test control field')
            result[key]=value
        return result
    try:
        value=json.loads(raw,object_pairs_hook=unique)
    except (UnicodeError,ValueError):
        raise web.HTTPBadRequest()
    required={'action','revision','command_id'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'name','tester_name'}:
        raise web.HTTPBadRequest()
    if 'name' in value and value['action'] != 'start':
        raise web.HTTPBadRequest(reason='test name is only valid when starting')
    if 'tester_name' in value and value['action'] != 'start':
        raise web.HTTPBadRequest(reason='测试人员姓名只能在开始测试时登记')
    tester = validate_tester_name(value.get('tester_name')) if value['action'] == 'start' else None
    if value['action'] == 'start':
        pending = _robot_store(request).test_issues.review_required(actor=session['u'])
        if pending:
            return json_response({'error': '请先处理前一天的测试问题',
                                  'reason': 'test_issue_review_required',
                                  'pending_issues': pending}, status=409)
    try:
        _robot_store(request).tests.control(value['action'],value['revision'],value['command_id'],
                                        name=value.get('name'),tester_name=tester,actor=session['u'])
    except TestConflict as exc:
        return json_response({'error':str(exc),'reason':'revision_conflict','revision':exc.revision},status=409)
    store = _robot_store(request)
    snapshot = store.tests_snapshot(actor=session['u'], read_all=session['role'] == 'admin')
    snapshot['test_issues'] = store.test_issues.list(
        actor=session['u'], read_all=session['role'] == 'admin', days=365)
    return json_response(snapshot)


async def _test_export(request):
    session=_viewer(request)
    days=parse_days(request.query,export=True)
    kind=request.query.get('format','csv')
    if kind not in ('csv','json'):
        raise web.HTTPBadRequest(reason='invalid test export format')
    if not request.app[LIMITER].allow(('test-export',session['u']),6/60,4):
        raise web.HTTPTooManyRequests()
    slots=request.app[EXPORT_SLOTS]
    if slots.locked():raise web.HTTPServiceUnavailable(reason='export busy')
    async with slots:
        store=_robot_store(request)
        db=sqlite3.connect('file:'+str(store.path)+'?mode=ro',uri=True)
        try:
            db.execute('BEGIN')
            data=store.tests_snapshot(days, connection=db,
                actor=session['u'], read_all=session['role'] == 'admin')
        finally:db.close()
        if kind=='json':
            body=json.dumps(data,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
            mime='application/json'
        else:
            output=io.StringIO(newline='');writer=csv.writer(output)
            writer.writerow(('日期（北京时间）','测试时长（秒）','测试移动距离（米）','里程有效时间（秒）','测试次数','记录状态','测试人员'))
            qualities={'none':'无测试','waiting':'等待里程数据','partial':'部分记录','available':'完整记录'}
            for row in data['days']:
                writer.writerow((row['date'],format(row['duration_s'],'.3f'),
                    '' if row['distance_m'] is None else format(row['distance_m'],'.3f'),
                    format(row['valid_seconds'],'.3f'),row['sessions'],qualities[row['quality']],
                    csv_cell('、'.join(name or '未登记' for name in row['tester_names']))))
            body=b'\xef\xbb\xbf'+output.getvalue().encode('utf-8');mime='text/csv'
        return web.Response(body=body,headers={
            'Content-Type':mime+'; charset=utf-8',
            'Content-Disposition':'attachment; filename="test-statistics-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.'+kind+'"'})


async def _fault_logs(request):
    _viewer(request)
    filters = parse_filters(request.query, grouping=True)
    with _robot_store(request).db:
        _robot_store(request).faults.prune()
    return json_response(_robot_store(request).faults.query(filters))


async def _fault_timestamps(request):
    _viewer(request)
    group_id = request.match_info['group_id']
    if not re.fullmatch(r'[a-f0-9]{64}', group_id):
        raise web.HTTPBadRequest(reason='invalid fault group')
    filters = parse_filters(request.query, max_offset=MAX_RECORDS + MAX_OCCURRENCES)
    store = _robot_store(request)
    with store.db:
        store.faults.prune()
    result = store.faults.timestamps(group_id, filters)
    if result is None:
        raise web.HTTPNotFound()
    return json_response(result)


async def _fault_summary(request):
    _viewer(request)
    filters = parse_filters(request.query, pagination=False, grouping=True)
    if request.query and not filters.get('grouped'):
        raise web.HTTPBadRequest()
    store = _robot_store(request)
    with store.db:
        store.faults.prune()
    result = store.faults.summary(filters)
    source = store.sources.get((store.config.robot_id, store.primary_source))
    result['monitoring'] = {'online': store.source_online(store.primary_source),
                            'last_seen': source['last_seen'] if source else None,
                            'source_id': store.primary_source}
    return json_response(result)


async def _fault_export(request):
    session = _viewer(request)
    filters = parse_filters(request.query, pagination=False)
    format_name = request.query.get('format', 'csv')
    if format_name not in ('csv', 'json'):
        raise web.HTTPBadRequest(reason='invalid export format')
    if not request.app[LIMITER].allow(('fault-export', session['u']), 6 / 60, 4):
        raise web.HTTPTooManyRequests()
    slots = request.app[EXPORT_SLOTS]
    if slots.locked():
        raise web.HTTPServiceUnavailable(reason='export busy')
    async with slots:
        store = _robot_store(request)
        with store.db:
            store.faults.prune()
        # Independent read transaction gives a stable download while ingestion continues.
        db = sqlite3.connect('file:' + str(store.path) + '?mode=ro', uri=True)
        cursor = None
        response = web.StreamResponse(headers={
            'Content-Type': ('text/csv' if format_name == 'csv' else 'application/json') + '; charset=utf-8',
            'Content-Disposition': 'attachment; filename="fault-logs-' +
                datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.' + format_name + '"'})
        try:
            db.execute('BEGIN')
            cursor = store.faults.select(filters, export=True, connection=db)
            await response.prepare(request)

            async def write(data):
                await asyncio.wait_for(response.write(data), timeout=5)

            if format_name == 'csv':
                output = io.StringIO(newline='')
                csv.writer(output).writerow(CSV_HEADER)
                await write(b'\xef\xbb\xbf' + output.getvalue().encode('utf-8'))
            else:
                prefix = json.dumps({'exported_at': time.time(), 'retention_days': RETENTION_DAYS,
                                     'max_records': store.faults.max_records}, separators=(',', ':'))
                await write((prefix[:-1] + ',"items":[').encode())
            first = True
            while True:
                rows = cursor.fetchmany(20)
                if not rows:
                    break
                if not _approved_cookie(request.app, request.cookies.get(COOKIE)):
                    raise ConnectionError('viewer session expired')
                chunk = bytearray()
                for row in rows:
                    item = store.faults._item(row)
                    if format_name == 'csv':
                        chunk.extend(csv_row(item))
                    else:
                        if not first:
                            chunk.extend(b',')
                        chunk.extend(json.dumps(item, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))
                        first = False
                await write(bytes(chunk))
            if format_name == 'json':
                await write(b']}')
            await response.write_eof()
        except (ConnectionError, asyncio.TimeoutError):
            if request.transport:
                request.transport.close()
        finally:
            if cursor is not None:
                cursor.close()
            db.close()
        return response


async def _fault_monitor(app):
    async def monitor():
        while True:
            try:
                for store in app[FLEET_STORES].values():
                    store.poll_faults()
                    with store.db:
                        store.faults.prune()
            except sqlite3.Error:
                logging.getLogger(__name__).error('Fault history persistence unavailable')
            await asyncio.sleep(2)
    task = asyncio.create_task(monitor())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _websocket(request):
    session = _viewer(request)
    selected_store = _robot_store(request)
    if not _same_origin(request) or set(request.query) - {'robotId'}:
        raise web.HTTPForbidden()
    sockets = request.app[SOCKETS]
    if sum(len(group) for group in sockets.values()) >= 64 or len(sockets.get(session['n'], ())) >= 4:
        raise web.HTTPServiceUnavailable()
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096, receive_timeout=65)
    await ws.prepare(request)
    # One WS per page is a frontend invariant. Shared cookies can legitimately
    # have several tabs, bounded independently to avoid reconnection fights.
    sockets.setdefault(session['n'], set()).add(ws)
    async def publish():
        try:
            while not ws.closed:
                if not _approved_cookie(request.app, request.cookies.get(COOKIE)):
                    await ws.close(code=1008, message=b'session expired')
                    break
                await asyncio.wait_for(ws.send_json(selected_store.snapshot(),
                    dumps=lambda d: json.dumps(d, ensure_ascii=False, separators=(',', ':'), allow_nan=False)), timeout=5)
                await asyncio.sleep(1)
        except (ConnectionError, asyncio.TimeoutError, RuntimeError):
            await ws.close()
    task = asyncio.create_task(publish())
    try:
        async for message in ws:
            if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                await ws.close(code=1008, message=b'read-only stream')
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        group = sockets.get(session['n'])
        if group is not None:
            group.discard(ws)
            if not group:
                del sockets[session['n']]
    return ws


def create_app(config=None, dbprog=None):
    """Build an isolated app from Config/mapping/env; no sockets opened here."""
    if config is None:
        config = Config.from_env()
    elif isinstance(config, dict):
        config = Config(**config)
    config.validate()
    app = web.Application(client_max_size=MAX_REQUEST_BYTES,
                          middlewares=[security_headers, transport_and_limits])
    app.on_response_prepare.append(_prepare_security_headers)
    # Validate opt-in settings before allocating SQLite or client resources.
    app[CONFIG] = config
    native_map = {}
    default_path = os.environ.get('NATIVE_VIDEO_SOCKET', '')
    paths = set()
    for robot in dict.fromkeys([config.robot_id, *[d.robot_id for d in config.device_credentials]]):
        path = config.fleet.get(robot, {}).get('nativeVideoSocket', default_path if robot == config.robot_id else '')
        if path and path in paths:
            raise ValueError('robots must use distinct native video sockets')
        if path:
            paths.add(path)
        native_map[robot] = NativeVideo(path, _viewer,
            lambda cookie: _approved_cookie(app, cookie),
            int(os.environ.get('NATIVE_VIDEO_MAX_PLAYING', '4')),
            split_socket_option(os.environ.get('NATIVE_VIDEO_SPLIT_SOCKETS', 'false')))
    app[ACCOUNTS] = Accounts(config.data_dir)
    stores = {}
    try:
        for robot in native_map:
            scoped = replace(config, robot_id=robot,
                device_credentials=[d for d in config.device_credentials if d.robot_id == robot],
                data_dir=config.data_dir if robot == config.robot_id else str(Path(config.data_dir) / 'robots' / robot))
            stores[robot] = Store(scoped)
    except Exception:
        for store in stores.values():
            store.close()
        app[ACCOUNTS].close()
        raise
    app[STORE], app[FLEET_STORES] = stores[config.robot_id], stores
    app[FLEET_NATIVE] = native_map
    native = native_map[config.robot_id]
    app[SESSIONS], app[LIMITER], app[SOCKETS] = Sessions(config.session_secret, config.session_ttl_s), RateLimiter(), {}
    app[HASH_SLOTS] = asyncio.Semaphore(2)
    app[EXPORT_SLOTS] = asyncio.Semaphore(2)
    app.cleanup_ctx.append(_fault_monitor)
    app[NATIVE_VIDEO] = native
    for camera in native_map.values():
        app.on_startup.append(camera.start)
    # End native media before aiohttp waits for long-lived handlers to drain.
    # Cleanup repeats close safely after the server has finished shutdown.
    for camera in native_map.values():
        app.on_shutdown.append(camera.close)
    install_dbprog(app, config, _viewer, _same_origin, _frontend, dbprog)
    app.add_routes([
        web.get('/healthz', _health), web.get('/readyz', _ready),
        web.get('/favicon.ico', _favicon),
        web.post('/api/login', _login), web.post('/api/register', _register),
        web.post('/api/logout', _logout),
        web.post('/api/account/password', _change_password),
        web.get('/api/admin/users', _users),
        web.post('/api/admin/users/{id}/approve', _approve),
        web.delete('/api/admin/users/{id}', _delete_user),
        web.post('/api/admin/users/{id}/password', _reset_user_password),
        web.get('/api/session', _session), web.get('/api/robots', _robots),
        web.get('/api/tests', _test_stats),
        web.delete('/api/admin/tests/{id}', _delete_test_run),
        web.get('/api/tests/issues', _test_issues),
        web.post('/api/tests/issues', _test_issue_create),
        web.post('/api/tests/issues/{issue_id}/review', _test_issue_review),
        # Short alias kept for desktop tools and older local pages.
        web.get('/api/test-issues', _test_issues),
        web.post('/api/test-issues', _test_issue_create),
        web.post('/api/test-issues/{issue_id}/review', _test_issue_review),
        web.post('/api/tests/control', _test_control),
        web.get('/api/tests/export', _test_export),
        web.get('/api/fault-logs', _fault_logs),
        web.get('/api/fault-logs/summary', _fault_summary),
        web.get('/api/fault-logs/export', _fault_export),
        web.get('/api/fault-logs/groups/{group_id}/timestamps', _fault_timestamps),
        web.post('/api/v1/ingest', _ingest), web.get('/snapshot.json', _snapshot),
        web.get('/visuals.json', _visuals), web.get('/camera/{key}.jpg', _image),
        web.get('/visual/{key}.png', _image), web.get('/ws', _websocket),
        web.get('/native-camera/status', _native_status, allow_head=False),
        web.get('/native-camera/detections', _native_detections, allow_head=False),
        web.get('/native-camera/{key}/video.mp4', _native_stream, allow_head=False),
        web.get('/', _index), web.get('/index.html', _index),
        web.get('/login', _login_page), web.get('/login.html', _login_page),
        web.get('/register', _register_page), web.get('/register.html', _register_page),
        web.get('/pending', _pending_page), web.get('/pending.html', _pending_page),
        web.get('/users', _users_page), web.get('/users.html', _users_page),
        web.get('/password', _password_page), web.get('/password.html', _password_page),
        web.get('/coverage', _coverage_page), web.get('/coverage/', _coverage_page),
        web.get('/sensor-library', _sensor_library_page),
        web.get('/sensor-library/', _sensor_library_page),
        web.get('/sensor-library/index.html', _sensor_library_page),
        web.get('/sensor-library/{filename:.*}', _sensor_library_asset),
        web.get(r'/coverage-assets/assets/{filename:[A-Za-z0-9_-]+\.(?:js|css)}', _coverage_asset),
        web.get('/{name}', _asset)])
    async def cleanup(application):
        for camera in application[FLEET_NATIVE].values():
            await camera.close(application)
        for group in list(application[SOCKETS].values()):
            for ws in list(group):
                await ws.close(code=1001, message=b'server shutdown')
        for store in application[FLEET_STORES].values():
            store.close()
        application[ACCOUNTS].close()
    app.on_cleanup.append(cleanup)
    return app


def main():
    try:
        app = create_app()
    except (ValueError, KeyError, OSError, sqlite3.Error):
        raise SystemExit('Invalid or unavailable cloud configuration/storage; see deployment documentation')
    web.run_app(app, host='0.0.0.0', port=app[CONFIG].port, access_log=None)


if __name__ == '__main__':
    main()
