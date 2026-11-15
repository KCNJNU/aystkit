"""Authenticated, same-origin DBProg access over the existing private socket mount."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
import os
import re
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

import aiohttp
from aiohttp import web
from yarl import URL

COOKIE = 'observer_dbprog_session'
TOKEN = re.compile(r'[A-Za-z0-9_-]{20,256}\Z')
MAX_JSON = 4 * 1024 * 1024


class DbprogError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _http_origin(value):
    """A deployment-owned fixed origin, never a browser-selected upstream."""
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
            raise ValueError()
        url = URL(value)
        if (url.scheme not in ('http', 'https') or not url.host or url.user is not None
                or url.password is not None or url.query_string or url.fragment
                or url.path not in ('', '/')):
            raise ValueError()
        url.port
        return url.origin()
    except (ValueError, TypeError):
        raise ValueError('DBProg HTTP transport requires a fixed HTTP(S) origin') from None


class _HttpTransport:
    """Adapt the existing socket gateway to explicitly configured cloud origins.

    API routes always use one fixed server. Object URLs must match an exact
    configured origin so signed downloads cannot become an unrestricted proxy.
    """
    def __init__(self, session, origins, *, api=False):
        self.session, self.origins, self.api = session, tuple(origins), api

    def request(self, method, value, **options):
        url = URL(value, encoded=True)
        if self.api:
            url = URL(str(self.origins[0]) + url.raw_path_qs, encoded=True)
        elif url.user is not None or url.password is not None or url.origin() not in self.origins:
            raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了未配置的文件存储地址')
        # Authentication is sent explicitly by the gateway, never retained by
        # either session, and redirects are disallowed by every caller.
        return self.session.request(method, url, **options)

    def get(self, value, **options):
        return self.request('GET', value, **options)

    async def close(self):
        await self.session.close()


class DbprogGateway:
    def __init__(self, api_socket=None, object_socket=None, *, api_url=None, object_urls=None):
        self.api_socket = api_socket or os.environ.get('DBPROG_API_SOCKET', '/run/native-preview/dbprog-api.sock')
        self.object_socket = object_socket or os.environ.get('DBPROG_OBJECT_SOCKET', '/run/native-preview/dbprog-objects.sock')
        self.api_url = api_url if api_url is not None else os.environ.get('DBPROG_API_URL', '')
        selected_objects = object_urls if object_urls is not None else os.environ.get('DBPROG_OBJECT_URLS', '')
        self.object_urls = selected_objects.split(',') if isinstance(selected_objects, str) else selected_objects
        self.api_origin = _http_origin(self.api_url) if self.api_url else None
        self.object_origins = tuple(_http_origin(value.strip()) for value in (self.object_urls or ()) if value.strip())
        if bool(self.api_origin) != bool(self.object_origins):
            raise ValueError('Configure both DBPROG_API_URL and DBPROG_OBJECT_URLS for cloud transport')
        self.api = self.objects = None
        self.slots = asyncio.Semaphore(6)
        self.download_slots = asyncio.Semaphore(3)

    async def start(self, _app):
        if self.api_origin:
            self.api = _HttpTransport(aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=8)), (self.api_origin,), api=True)
            self.objects = _HttpTransport(aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=None, connect=8, sock_read=300)), self.object_origins)
            return
        self.api = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=self.api_socket),
            cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=8))
        self.objects = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=self.object_socket),
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=None, connect=8, sock_read=300))

    async def close(self, _app):
        for session in (self.api, self.objects):
            if session is not None:
                await session.close()

    async def request(self, path, token='', method='GET', payload=None, csrf=''):
        headers = {'Accept': 'application/json'}
        if token:
            headers['Cookie'] = 'cr_session=' + token
        if csrf:
            headers['X-CSRF-Token'] = csrf
        try:
            async with self.slots, self.api.request(method, 'http://dbprog' + path,
                    headers=headers, json=payload, allow_redirects=False) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_JSON:
                        raise DbprogError(502, 'DBPROG_RESPONSE', 'DBProg 响应过大，请缩小查询范围')
                status = response.status
                cookies = response.headers.getall('Set-Cookie', [])
                try:
                    data = json.loads(raw)
                    expected = list if path.endswith('/folders') and status < 300 else dict
                    if not isinstance(data, expected):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise DbprogError(502, 'DBPROG_RESPONSE', 'DBProg 返回了无效响应') from None
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            message = ('云端数仓暂不可用，请检查云端数仓服务和连接' if self.api_origin else
                       '数仓暂不可用，请检查工控机 DBProg 服务和连接')
            raise DbprogError(503, 'DBPROG_UNAVAILABLE', message) from None
        if status >= 300:
            if status == 401:
                raise DbprogError(401, 'DBPROG_AUTH_REQUIRED', 'DBProg 账号或密码错误，或登录已过期')
            if status == 403:
                change = data.get('message') == 'PASSWORD_CHANGE_REQUIRED'
                raise DbprogError(403, 'PASSWORD_CHANGE_REQUIRED' if change else 'DBPROG_FORBIDDEN',
                    '请先在 DBProg 原页面修改初始密码' if change else '当前 DBProg 账号没有访问这些数据的权限')
            if status == 404:
                raise DbprogError(404, 'DBPROG_NOT_FOUND', '数据不存在或当前账号无权访问')
            if status == 429:
                raise DbprogError(429, 'DBPROG_RATE_LIMITED', '请求过于频繁，请稍后重试')
            raise DbprogError(502, 'DBPROG_UPSTREAM', '数仓暂时无法处理请求，请稍后重试')
        return data, cookies


def install_dbprog(app, config, viewer, same_origin, frontend, gateway=None):
    gateway = gateway or DbprogGateway()
    app.on_startup.append(gateway.start)
    app.on_cleanup.append(gateway.close)
    from .dbprog_full import install_full_app
    install_full_app(app, config, viewer, same_origin, gateway)

    def result(data, status=200):
        return web.json_response(data, status=status)

    def identity(request):
        try:
            return viewer(request)
        except web.HTTPUnauthorized:
            raise DbprogError(401, 'observer_auth_required', '请先登录观测平台') from None
        except web.HTTPForbidden:
            raise DbprogError(403, 'observer_access_required', '当前观测账号尚未获准访问') from None

    def signature(token, owner):
        return hmac.new(config.session_secret.encode(), (owner['n'] + ':' + token).encode(), hashlib.sha256).hexdigest()

    def session_token(request, owner):
        cookie = request.cookies.get(COOKIE, '')
        parts = cookie.split('.')
        if len(parts) != 2 or not TOKEN.fullmatch(parts[0]) or not re.fullmatch(r'[0-9a-f]{64}', parts[1]):
            return ''
        return parts[0] if hmac.compare_digest(parts[1], signature(parts[0], owner)) else ''

    def authenticated_token(request):
        token = session_token(request, identity(request))
        if not token:
            raise DbprogError(401, 'DBPROG_AUTH_REQUIRED', '请使用 DBProg 原账号登录数仓')
        return token

    def secure_cookie(response, value='', delete=False):
        options = dict(path='/api/dbprog', httponly=True, secure=not config.allow_insecure_local, samesite='Strict')
        if delete:
            response.del_cookie(COOKIE, **options)
        else:
            response.set_cookie(COOKIE, value, max_age=config.session_ttl_s, **options)

    def require_origin(request):
        if not same_origin(request):
            raise DbprogError(403, 'DBPROG_ORIGIN', '请从观测平台发起此操作')

    def identifier(value):
        try:
            return str(UUID(value))
        except (ValueError, TypeError):
            raise DbprogError(422, 'DBPROG_INVALID_ID', '数据标识无效') from None

    def pagination(request):
        query = request.query
        try:
            page, size = int(query.get('page', '1')), int(query.get('page_size', '20'))
            keyword = query.get('keyword', '').strip()
            if not 1 <= page <= 1000000 or not 1 <= size <= 100 or len(keyword) > 500:
                raise ValueError()
        except ValueError:
            raise DbprogError(422, 'DBPROG_QUERY_INVALID', '分页或搜索参数无效') from None
        return {'page': page, 'pageSize': size, 'search': keyword}

    def guarded(handler):
        async def wrapped(request):
            try:
                return await handler(request)
            except DbprogError as error:
                return result({'error': error.code, 'message': error.message}, error.status)
        return wrapped

    async def page(request):
        try:
            viewer(request)
        except web.HTTPUnauthorized:
            raise web.HTTPFound('/login')
        except web.HTTPForbidden:
            raise web.HTTPFound('/pending')
        return frontend(request, 'dbprog.html')

    @guarded
    async def status(request):
        owner = identity(request)
        data = {'configured': True, 'available': False, 'authenticated': False,
                'state': 'ERROR', 'message': '正在等待数仓连接', 'health': None}
        try:
            ready, _ = await gateway.request('/ready')
            data.update(available=ready.get('status') == 'healthy', state='NO_DATA', message='数仓已连接，请登录 DBProg')
            token = session_token(request, owner)
            if token:
                try:
                    health, _ = await gateway.request('/api/system/status', token)
                    data.update(authenticated=True, health=health, state='AVAILABLE', message='DBProg 已连接')
                except DbprogError as error:
                    if error.status != 401:
                        data.update(authenticated=error.status == 403, state='ERROR',
                                    message=error.message, error=error.code)
        except DbprogError as error:
            data.update(error=error.code, message=error.message)
        return result(data)

    @guarded
    async def login(request):
        owner = identity(request)
        require_origin(request)
        if request.content_type != 'application/json' or (request.content_length or 0) > 8192:
            raise DbprogError(422, 'DBPROG_LOGIN_INVALID', '请输入 DBProg 账号和密码')
        raw = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            raw.extend(chunk)
            if len(raw) > 8192:
                raise DbprogError(413, 'DBPROG_REQUEST_SIZE', '登录请求过大')
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if (not isinstance(payload, dict) or not isinstance(payload.get('email'), str)
                or not 1 <= len(payload['email'].strip()) <= 320
                or not isinstance(payload.get('password'), str) or not 1 <= len(payload['password']) <= 4096):
            raise DbprogError(422, 'DBPROG_LOGIN_INVALID', '请输入 DBProg 账号和密码')
        data, cookies = await gateway.request('/api/v1/auth/login', method='POST',
            payload={'email': payload['email'].strip(), 'password': payload['password']})
        if not isinstance(data.get('user'), dict):
            raise DbprogError(502, 'DBPROG_RESPONSE', 'DBProg 返回了无效登录响应')
        for header in cookies:
            cookie = SimpleCookie()
            cookie.load(header)
            if 'cr_session' in cookie and TOKEN.fullmatch(cookie['cr_session'].value):
                token = cookie['cr_session'].value
                response = result({'authenticated': True, 'user': data['user']})
                secure_cookie(response, token + '.' + signature(token, owner))
                return response
        raise DbprogError(502, 'DBPROG_RESPONSE', 'DBProg 未返回有效登录会话')

    @guarded
    async def logout(request):
        owner = identity(request)
        require_origin(request)
        token = session_token(request, owner)
        if token:
            try:
                session, _ = await gateway.request('/api/v1/auth/session', token)
                csrf = session.get('csrfToken')
                if not isinstance(csrf, str) or not TOKEN.fullmatch(csrf):
                    raise DbprogError(502, 'DBPROG_RESPONSE', 'DBProg 返回了无效会话响应')
                await gateway.request('/api/v1/auth/logout', token, method='POST', payload={}, csrf=csrf)
            except DbprogError as error:
                if error.status != 401:
                    raise
        response = result({'authenticated': False})
        secure_cookie(response, delete=True)
        return response

    @guarded
    async def overview(request):
        data, _ = await gateway.request('/api/warehouse/overview', authenticated_token(request))
        return result(data)

    @guarded
    async def datasets(request):
        token = authenticated_token(request)
        data, _ = await gateway.request('/api/v1/datasets/page?' + urlencode(pagination(request)), token)
        return result(data)

    @guarded
    async def folders(request):
        token = authenticated_token(request)
        dataset_id = identifier(request.match_info['dataset_id'])
        data, _ = await gateway.request('/api/v1/datasets/' + dataset_id + '/folders', token)
        return result({'items': data})

    @guarded
    async def files(request):
        token = authenticated_token(request)
        query = pagination(request)
        if request.query.get('dataset_id'):
            query['datasetId'] = identifier(request.query['dataset_id'])
        if 'folder_path' in request.query:
            path = request.query['folder_path']
            if len(path) > 750:
                raise DbprogError(422, 'DBPROG_QUERY_INVALID', '目录路径过长')
            query['folderPath'] = path
        data, _ = await gateway.request('/api/v1/files?' + urlencode(query), token)
        return result(data)

    @guarded
    async def download(request):
        token = authenticated_token(request)
        file_id = identifier(request.match_info['file_id'])
        metadata, _ = await gateway.request('/api/v1/files/' + file_id, token)
        data, _ = await gateway.request('/api/v1/files/' + file_id + '/download', token)
        try:
            if not isinstance(data.get('url'), str) or len(data['url']) > 16384:
                raise ValueError()
            url = urlsplit(data['url'])
            url.port
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError()
        except ValueError:
            raise DbprogError(502, 'DBPROG_RESPONSE', 'DBProg 返回了无效下载地址') from None
        filename = str(metadata.get('originalName') or 'dbprog-file')[:240]
        headers = {'Content-Type': 'application/octet-stream',
                   'Content-Disposition': "attachment; filename=\"dbprog-file\"; filename*=UTF-8''" + quote(filename, safe='')}
        response = None
        try:
            # The connector always reaches the fixed private MinIO socket. Keep
            # the original signed Host and path; never expose the LAN URL.
            async with gateway.download_slots, gateway.objects.get(URL(data['url'], encoded=True), allow_redirects=False) as upstream:
                if upstream.status != 200:
                    raise DbprogError(502, 'DBPROG_DOWNLOAD', '文件暂时无法下载，请重试')
                if upstream.headers.get('Content-Length', '').isdigit():
                    headers['Content-Length'] = upstream.headers['Content-Length']
                response = web.StreamResponse(headers=headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    viewer(request)
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, web.HTTPException):
            if response is not None and response.prepared:
                response.force_close()
                return response
            raise DbprogError(503, 'DBPROG_DOWNLOAD', '文件下载连接暂时不可用，请稍后重试') from None

    app.add_routes([
        web.get('/api/dbprog/status', status), web.post('/api/dbprog/login', login),
        web.post('/api/dbprog/logout', logout), web.get('/api/dbprog/overview', overview),
        web.get('/api/dbprog/datasets', datasets),
        web.get('/api/dbprog/datasets/{dataset_id}/folders', folders),
        web.get('/api/dbprog/files', files),
        web.get('/api/dbprog/files/{file_id}/download', download, allow_head=False),
    ])

    return gateway
