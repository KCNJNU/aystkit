"""Serve the original warehouse application through authenticated private transports."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
from uuid import UUID

import aiohttp
from aiohttp import web
from yarl import URL

from .dbprog import DbprogError, MAX_JSON, TOKEN

APP_COOKIE = 'observer_dbprog_app'
MAX_BODY = 16 * 1024 * 1024
API_TIMEOUT = aiohttp.ClientTimeout(total=180, connect=8, sock_read=120)
METHODS = frozenset(('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'))
REQUEST_HEADERS = ('Accept', 'Content-Type', 'Content-Range', 'X-Part-SHA256',
                   'X-CSRF-Token', 'Idempotency-Key', 'X-Request-ID', 'X-Trace-ID',
                   'traceparent', 'Last-Event-ID', 'If-None-Match', 'If-Modified-Since',
                   'Range', 'If-Range', 'User-Agent')
RESPONSE_HEADERS = ('Content-Type', 'Content-Disposition', 'Content-Range', 'Accept-Ranges',
                    'ETag', 'Last-Modified', 'X-Request-ID', 'X-Trace-ID', 'traceparent', 'Retry-After')
STATIC_SUFFIXES = frozenset(('.html', '.js', '.css', '.json', '.svg', '.png', '.jpg', '.jpeg',
                            '.webp', '.gif', '.ico', '.woff', '.woff2', '.ttf', '.wasm'))
INLINE_TYPES = frozenset(('image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif',
                          'image/bmp', 'image/x-icon', 'application/json', 'text/plain', 'text/csv'))
DOWNLOAD = re.compile(r'v1/files/([0-9a-fA-F-]{36})/download\Z')
PACKAGE_DOWNLOAD = re.compile(r'v1/datasets/([0-9a-fA-F-]{36})/recordings/([a-f0-9]{32,64})/package\Z')
FOLDER_PACKAGE_DOWNLOAD = re.compile(r'v1/datasets/([0-9a-fA-F-]{36})/folder-package\Z')
CAPTURE_PACKAGE_DOWNLOAD = re.compile(r'v1/datasets/([^/]+)/capture-sessions/([^/]+)/package\Z')
CANONICAL_UUID = re.compile(r'[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}\Z')
CAPTURE_SESSION_ID = re.compile(r'(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\Z')


def install_full_app(app, config, viewer, same_origin, gateway):
    root = (Path(config.frontend_dir) / 'warehouse').resolve()

    def error(status, code, message):
        return web.json_response({'error': code, 'message': message}, status=status)

    def identity(request):
        try:
            return viewer(request)
        except web.HTTPUnauthorized:
            raise DbprogError(401, 'observer_auth_required', '请先登录观测平台') from None
        except web.HTTPForbidden:
            raise DbprogError(403, 'observer_access_required', '当前观测账号尚未获准访问') from None

    def sign(token, owner):
        return hmac.new(config.session_secret.encode(),
            ('warehouse:' + owner['n'] + ':' + token).encode(), hashlib.sha256).hexdigest()

    def session(request, owner):
        parts = request.cookies.get(APP_COOKIE, '').split('.')
        if (len(parts) != 2 or not TOKEN.fullmatch(parts[0])
                or not re.fullmatch(r'[0-9a-f]{64}', parts[1])):
            return ''
        return parts[0] if hmac.compare_digest(parts[1], sign(parts[0], owner)) else ''

    def cookie(response, value='', delete=False):
        options = dict(path='/dbprog', httponly=True, secure=not config.allow_insecure_local, samesite='Strict')
        if delete:
            response.del_cookie(APP_COOKIE, **options)
        else:
            response.set_cookie(APP_COOKIE, value, max_age=config.session_ttl_s, **options)

    def guarded(handler):
        async def wrapped(request):
            try:
                return await handler(request)
            except DbprogError as exc:
                return error(exc.status, exc.code, exc.message)
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                return error(503, 'DBPROG_UNAVAILABLE', '数仓连接暂不可用，请稍后重试')
        return wrapped

    def remove_session_tokens(value):
        if isinstance(value, dict):
            return {key: remove_session_tokens(item) for key, item in value.items()
                    if key.lower() != 'sessiontoken'}
        if isinstance(value, list):
            return [remove_session_tokens(item) for item in value]
        return value

    def signed_url(value):
        try:
            if not isinstance(value, str) or len(value) > 16384 or any(ord(c) <= 32 for c in value):
                raise ValueError()
            parsed = urlsplit(value)
            parsed.port
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment):
                raise ValueError()
            return URL(value, encoded=True)
        except ValueError:
            raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效文件地址') from None

    def package_identifiers(dataset, recording):
        try:
            dataset = str(UUID(dataset))
            if not re.fullmatch(r'[a-f0-9]{32,64}', recording):
                raise ValueError()
            return dataset, recording
        except (ValueError, TypeError, AttributeError):
            raise DbprogError(422, 'DBPROG_INVALID_ID', '压缩包标识无效') from None

    def package_url(value):
        url = signed_url(value)
        origins = getattr(gateway, 'object_origins', ())
        # A UnixConnector always reaches the one private MinIO socket. HTTP
        # transport must additionally preserve its deployment-owned allowlist.
        if origins and url.origin() not in origins:
            raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效文件地址')
        return url

    def package_filename(value):
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效压缩包名称')
        name = value.replace('\\', '/').rsplit('/', 1)[-1]
        name = ''.join('_' if ord(char) < 32 or ord(char) == 127 else char for char in name).strip()[:240]
        return name if name and name not in ('.', '..') else 'archive.tar.zst'

    def folder_arguments(request, dataset, *, prepare=False):
        try:
            dataset_id = str(UUID(dataset))
        except (ValueError, TypeError, AttributeError):
            raise DbprogError(422, 'DBPROG_INVALID_ID', '文件夹压缩包标识无效') from None
        allowed = {'path', 'async', 'retry', 'taskId'} if prepare else {'path', 'taskId'}
        if ('path' not in request.query or not set(request.query) <= allowed
                or any(len(request.query.getall(key)) != 1 for key in request.query)):
            raise DbprogError(422, 'DBPROG_QUERY_INVALID', '文件夹参数无效')
        if prepare:
            if (any(request.query[key] not in ('true', 'false') for key in ('async', 'retry')
                    if key in request.query)
                    or any(key in request.query for key in ('retry', 'taskId')) and request.query.get('async') != 'true'
                    or 'taskId' in request.query and 'retry' in request.query):
                raise DbprogError(422, 'DBPROG_QUERY_INVALID', '文件夹准备参数无效')
        if 'taskId' in request.query and not re.fullmatch(r'[a-f0-9]{64}', request.query['taskId']):
            raise DbprogError(422, 'DBPROG_QUERY_INVALID', '文件夹任务标识无效')
        path = request.query['path']
        # Match the API's literal folder-path format. Decode the browser query
        # once, then encode it once for the API; never normalize its scope.
        if (len(path.encode('utf-8')) > 750 or '\\' in path
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or path and any(not part or part != part.strip()
                    or part in ('.', '..', '.folder', '.dataset')
                    or len(part.encode('utf-8')) > 200 for part in path.split('/'))):
            raise DbprogError(422, 'DBPROG_QUERY_INVALID', '文件夹路径无效')
        return dataset_id, path

    def capture_arguments(request, dataset, capture_session, *, prepare=False):
        if not CANONICAL_UUID.fullmatch(dataset) or not CAPTURE_SESSION_ID.fullmatch(capture_session):
            raise DbprogError(422, 'DBPROG_INVALID_ID', '采集压缩包标识无效')
        allowed = {'robotId', 'async', 'retry', 'taskId'} if prepare else {'robotId', 'taskId'}
        if (not set(request.query) <= allowed or 'robotId' not in request.query
                or any(len(request.query.getall(key)) != 1 for key in request.query)
                or not CANONICAL_UUID.fullmatch(request.query['robotId'])):
            raise DbprogError(422, 'DBPROG_QUERY_INVALID', '采集压缩包参数无效')
        task_id = request.query.get('taskId')
        if task_id is not None and not re.fullmatch(r'[a-f0-9]{64}', task_id):
            raise DbprogError(422, 'DBPROG_QUERY_INVALID', '采集压缩包任务标识无效')
        if prepare:
            if (request.query.get('async') != 'true'
                    or 'retry' in request.query and request.query['retry'] not in ('true', 'false')
                    or task_id is not None and 'retry' in request.query
                    or (request.method == 'HEAD' or any(name in request.headers for name in ('Range', 'If-Range')))
                        and task_id is None):
                raise DbprogError(422, 'DBPROG_QUERY_INVALID', '采集压缩包准备参数无效')
        elif task_id is None:
            # Browsers and download managers may probe or resume at any time.
            # A public GET/HEAD/Range can only open an existing fixed snapshot.
            raise DbprogError(422, 'DBPROG_QUERY_INVALID', '采集压缩包下载需要已准备任务')
        return str(UUID(dataset)), capture_session, str(UUID(request.query['robotId'])), task_id

    async def read_body(request):
        if (request.content_length or 0) > MAX_BODY:
            raise DbprogError(413, 'DBPROG_REQUEST_SIZE', '单次请求不能超过 16 MiB')
        body = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise DbprogError(413, 'DBPROG_REQUEST_SIZE', '单次请求不能超过 16 MiB')
        return bytes(body)

    def response_headers(upstream):
        return {key: upstream.headers[key] for key in RESPONSE_HEADERS if key in upstream.headers}

    async def streamed(request, upstream, headers):
        response = web.StreamResponse(status=upstream.status, headers=headers)
        try:
            await response.prepare(request)
            if request.method != 'HEAD':
                async for chunk in upstream.content.iter_chunked(65536):
                    viewer(request)
                    await response.write(chunk)
            await response.write_eof()
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, web.HTTPException):
            if not response.prepared:
                raise
            response.force_close()
        return response

    async def forward(request, upstream_path, tail=''):
        owner = identity(request)
        if request.method not in METHODS:
            raise web.HTTPMethodNotAllowed(request.method, METHODS)
        if request.method not in ('GET', 'HEAD') and not same_origin(request):
            raise DbprogError(403, 'DBPROG_ORIGIN', '请从数仓页面发起此操作')
        headers = {key: request.headers[key] for key in REQUEST_HEADERS if key in request.headers}
        token = session(request, owner)
        login = tail == 'v1/auth/login' and request.method == 'POST'
        if token and not login:
            headers['Cookie'] = 'cr_session=' + token
        async with gateway.slots:
            body = await read_body(request) if request.can_read_body else None
            return await upstream_response(request, upstream_path, tail, owner, headers, body, login)

    async def upstream_response(request, upstream_path, tail, owner, headers, body, login):
        async with gateway.api.request(request.method,
                URL('http://dbprog' + upstream_path, encoded=True), data=body, headers=headers,
                allow_redirects=False, auto_decompress=True, timeout=API_TIMEOUT) as upstream:
            if 300 <= upstream.status < 400 and upstream.status != 304:
                raise DbprogError(502, 'DBPROG_REDIRECT', '数仓返回了不支持的跳转')
            outgoing = response_headers(upstream)
            content_type = upstream.headers.get('Content-Type', '').lower()
            if request.method == 'HEAD' or upstream.status in (204, 304):
                if upstream.headers.get('Content-Length', '').isdigit():
                    outgoing['Content-Length'] = upstream.headers['Content-Length']
                return await streamed(request, upstream, outgoing)
            if 'text/event-stream' in content_type:
                outgoing.update({'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})
                return await streamed(request, upstream, outgoing)
            if 'application/json' not in content_type:
                return await streamed(request, upstream, outgoing)
            raw = bytearray()
            async for chunk in upstream.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > MAX_JSON:
                    raise DbprogError(502, 'DBPROG_RESPONSE', '数仓响应过大，请缩小查询范围')
            try:
                data = json.loads(raw)
                if tail in ('v1/auth/login', 'v1/auth/session'):
                    data = remove_session_tokens(data)
            except (ValueError, RecursionError):
                raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效响应') from None
            match = DOWNLOAD.fullmatch(tail)
            if match and upstream.status == 200:
                if not isinstance(data, dict):
                    raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效文件响应')
                signed_url(data.get('url'))
                data['url'] = '/dbprog/objects/' + str(UUID(match[1]))
            package_match = PACKAGE_DOWNLOAD.fullmatch(tail)
            if package_match and request.method == 'GET' and upstream.status == 200:
                if not isinstance(data, dict):
                    raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效压缩包响应')
                dataset_id, recording_id = package_identifiers(*package_match.groups())
                package_url(data.get('url'))
                data['fileName'] = package_filename(data.get('fileName'))
                data['url'] = '/dbprog/packages/' + dataset_id + '/' + recording_id
            folder_match = FOLDER_PACKAGE_DOWNLOAD.fullmatch(tail)
            if folder_match and request.method == 'GET' and upstream.status in (200, 202):
                identity(request)
                if not isinstance(data, dict):
                    raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效压缩包响应')
                dataset_id, folder_path = folder_arguments(request, folder_match[1], prepare=True)
                if upstream.status == 202:
                    retry_after_ms = data.get('retryAfterMs')
                    task_id = data.get('taskId')
                    if (request.query.get('async') != 'true' or data.get('state') != 'processing'
                            or type(retry_after_ms) is not int or not 100 <= retry_after_ms <= 30000
                            or not isinstance(task_id, str) or not re.fullmatch(r'[a-f0-9]{64}', task_id)
                            or request.query.get('taskId', task_id) != task_id
                            or 'url' in data or 'fileName' in data):
                        raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效压缩包准备状态')
                    data = {'state': 'processing', 'retryAfterMs': retry_after_ms, 'taskId': task_id}
                else:
                    if 'state' in data and data['state'] != 'ready':
                        raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效压缩包状态')
                    task_id = data.get('taskId')
                    if request.query.get('async') == 'true' and (
                            data.get('state') != 'ready' or not isinstance(task_id, str)
                            or not re.fullmatch(r'[a-f0-9]{64}', task_id)
                            or request.query.get('taskId', task_id) != task_id):
                        raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效压缩包任务')
                    package_url(data.get('url'))
                    data['fileName'] = package_filename(data.get('fileName'))
                    data['url'] = '/dbprog/folder-packages/' + dataset_id + '?path=' + quote(folder_path, safe='')
                    if request.query.get('async') == 'true':
                        data['url'] += '&taskId=' + task_id
            capture_match = CAPTURE_PACKAGE_DOWNLOAD.fullmatch(tail)
            if capture_match and request.method == 'GET' and upstream.status in (200, 202):
                identity(request)
                dataset_id, capture_session, robot_id, requested_task = capture_arguments(request, *capture_match.groups(), prepare=True)
                if not isinstance(data, dict):
                    raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效采集压缩包响应')
                task_id = data.get('taskId')
                if (not isinstance(task_id, str) or not re.fullmatch(r'[a-f0-9]{64}', task_id)
                        or requested_task is not None and requested_task != task_id):
                    raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效采集压缩包任务')
                if upstream.status == 202:
                    retry_after_ms = data.get('retryAfterMs')
                    if (data.get('state') != 'processing' or type(retry_after_ms) is not int
                            or not 100 <= retry_after_ms <= 30000 or 'url' in data or 'fileName' in data):
                        raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效采集压缩包准备状态')
                    data = {'state': 'processing', 'taskId': task_id, 'retryAfterMs': retry_after_ms}
                else:
                    if data.get('state') != 'ready':
                        raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效采集压缩包状态')
                    package_url(data.get('url'))
                    data = {'state': 'ready', 'taskId': task_id, 'fileName': package_filename(data.get('fileName')),
                        'url': '/dbprog/capture-packages/' + dataset_id + '/' + capture_session
                            + '?robotId=' + robot_id + '&taskId=' + task_id}
            response = web.json_response(data, status=upstream.status,
                headers={key: value for key, value in outgoing.items() if key != 'Content-Type'})
            if login and upstream.status < 300:
                resolved = ''
                for header in upstream.headers.getall('Set-Cookie', []):
                    parsed = SimpleCookie()
                    try:
                        parsed.load(header)
                    except CookieError:
                        continue
                    if 'cr_session' in parsed and TOKEN.fullmatch(parsed['cr_session'].value):
                        resolved = parsed['cr_session'].value
                        break
                if not resolved:
                    raise DbprogError(502, 'DBPROG_RESPONSE', '数仓未返回有效登录会话')
                cookie(response, resolved + '.' + sign(resolved, owner))
            elif tail in ('v1/auth/logout', 'v1/auth/change-password') and upstream.status < 300:
                cookie(response, delete=True)
            return response

    @guarded
    async def api(request):
        identity(request)
        tail = request.match_info['tail']
        parts = tail.split('/')
        if (not tail or parts[0] not in ('v1', 'ai', 'warehouse', 'system')
                or any(part in ('', '.', '..', 'internal') for part in parts)
                or '\\' in tail or any(ord(c) < 32 for c in tail)):
            raise web.HTTPNotFound()
        folder_match = FOLDER_PACKAGE_DOWNLOAD.fullmatch(tail)
        if folder_match and request.method == 'GET':
            folder_arguments(request, folder_match[1], prepare=True)
        capture_match = CAPTURE_PACKAGE_DOWNLOAD.fullmatch(tail)
        if capture_match:
            if request.method not in ('GET', 'HEAD'):
                raise web.HTTPMethodNotAllowed(request.method, ('GET', 'HEAD'))
            capture_arguments(request, *capture_match.groups(), prepare=True)
        raw_tail = request.raw_path.partition('/dbprog/api/')[2]
        return await forward(request, '/api/' + raw_tail, tail)

    @guarded
    async def system_endpoint(request):
        identity(request)
        endpoint = request.match_info.get('endpoint', 'version')
        if endpoint not in ('health', 'ready', 'version'):
            raise web.HTTPNotFound()
        return await forward(request, '/' + endpoint)

    @guarded
    async def objects(request):
        owner = identity(request)
        token = session(request, owner)
        if not token:
            return error(401, 'DBPROG_AUTH_REQUIRED', '请先登录数仓')
        try:
            file_id = str(UUID(request.match_info['file_id']))
        except ValueError:
            return error(422, 'DBPROG_INVALID_ID', '文件标识无效')
        metadata, _ = await gateway.request('/api/v1/files/' + file_id, token)
        data, _ = await gateway.request('/api/v1/files/' + file_id + '/download', token)
        url = signed_url(data.get('url'))
        headers = {name: request.headers[name] for name in
                   ('Range', 'If-Range', 'If-None-Match', 'If-Modified-Since') if name in request.headers}
        # Download URLs are signed for GET. HEAD must use that same method at
        # storage; streamed() preserves headers and omits the client body.
        async with gateway.download_slots, gateway.objects.request('GET', url,
                headers=headers, allow_redirects=False, auto_decompress=False) as upstream:
            if upstream.status not in (200, 206, 304, 416):
                raise DbprogError(502, 'DBPROG_DOWNLOAD', '文件暂时无法读取，请重试')
            outgoing = response_headers(upstream)
            if upstream.status == 416 and 'Content-Range' not in outgoing:
                # S3 may omit the current length on InvalidRange. The file
                # metadata was authorized above; never trust request values.
                size = metadata.get('fileSize')
                if isinstance(size, str) and re.fullmatch(r'(?:0|[1-9][0-9]{0,19})', size):
                    outgoing['Content-Range'] = 'bytes */' + size
            mime = upstream.headers.get('Content-Type') or metadata.get('mimeType') or 'application/octet-stream'
            if not isinstance(mime, str) or mime.split(';', 1)[0].strip().lower() not in INLINE_TYPES:
                filename = quote(str(metadata.get('originalName') or 'dbprog-file')[:240], safe='')
                outgoing['Content-Type'] = 'application/octet-stream'
                outgoing['Content-Disposition'] = "attachment; filename=\"dbprog-file\"; filename*=UTF-8''" + filename
            else:
                outgoing['Content-Type'] = mime
            for name in ('Content-Length', 'Content-Encoding'):
                if name in upstream.headers:
                    outgoing[name] = upstream.headers[name]
            return await streamed(request, upstream, outgoing)

    async def package_details(endpoint, token, *, task_id=None):
        # Package preparation may legitimately take longer than the small JSON
        # helper's eight-second budget. Authorization is still the API's job.
        async with gateway.slots, gateway.api.request('GET', URL(endpoint, encoded=True),
                headers={'Accept': 'application/json', 'Cookie': 'cr_session=' + token},
                allow_redirects=False, auto_decompress=True, timeout=API_TIMEOUT) as upstream:
            status = upstream.status
            if status != 200:
                if status == 401:
                    raise DbprogError(401, 'DBPROG_AUTH_REQUIRED', '数仓登录已过期，请重新登录')
                if status == 403:
                    raise DbprogError(403, 'DBPROG_FORBIDDEN', '当前数仓账号无权访问这个压缩包')
                if status == 404:
                    raise DbprogError(404, 'DBPROG_NOT_FOUND', '压缩包不存在或当前账号无权访问')
                if status in (202, 409):
                    raise DbprogError(409, 'DBPROG_PACKAGE_PENDING', '压缩包尚未准备完成，请稍后重试')
                if status == 429:
                    raise DbprogError(429, 'DBPROG_RATE_LIMITED', '请求过于频繁，请稍后重试')
                if status == 503:
                    raise DbprogError(503, 'DBPROG_PACKAGE_BUSY', '压缩包正在准备或暂不可用，请稍后重试')
                raise DbprogError(502, 'DBPROG_UPSTREAM', '数仓暂时无法准备压缩包，请稍后重试')
            raw = bytearray()
            async for chunk in upstream.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > MAX_JSON:
                    raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回的压缩包响应过大')
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, RecursionError):
                raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效压缩包响应') from None
            if task_id is not None and (data.get('state') != 'ready' or data.get('taskId') != task_id):
                raise DbprogError(502, 'DBPROG_RESPONSE', '数仓返回了无效压缩包任务')
            return package_url(data.get('url')), package_filename(data.get('fileName'))

    @guarded
    async def packages(request):
        owner = identity(request)
        token = session(request, owner)
        if not token:
            raise DbprogError(401, 'DBPROG_AUTH_REQUIRED', '请先登录数仓')
        if request.headers.get('Origin') and not same_origin(request):
            raise DbprogError(403, 'DBPROG_ORIGIN', '请从数仓页面下载压缩包')
        if request.query:
            raise DbprogError(422, 'DBPROG_QUERY_INVALID', '压缩包下载不接受自定义地址或参数')
        dataset_id, recording_id = package_identifiers(request.match_info['dataset_id'], request.match_info['recording_id'])
        endpoint = f'http://dbprog/api/v1/datasets/{dataset_id}/recordings/{recording_id}/package'
        url, filename = await package_details(endpoint, token)
        return await stream_package(request, url, filename)

    @guarded
    async def folder_packages(request):
        owner = identity(request)
        token = session(request, owner)
        if not token:
            raise DbprogError(401, 'DBPROG_AUTH_REQUIRED', '请先登录数仓')
        if request.headers.get('Origin') and not same_origin(request):
            raise DbprogError(403, 'DBPROG_ORIGIN', '请从数仓页面下载压缩包')
        dataset_id, folder_path = folder_arguments(request, request.match_info['dataset_id'])
        endpoint = f'http://dbprog/api/v1/datasets/{dataset_id}/folder-package?path=' + quote(folder_path, safe='')
        task_id = request.query.get('taskId')
        if task_id is not None:
            endpoint += '&async=true&taskId=' + task_id
        # Authorization and snapshot validation run as the original OP account
        # on every download, including HEAD and subsequent Range requests.
        url, filename = await package_details(endpoint, token, task_id=task_id)
        return await stream_package(request, url, filename)

    @guarded
    async def capture_packages(request):
        owner = identity(request)
        token = session(request, owner)
        if not token:
            raise DbprogError(401, 'DBPROG_AUTH_REQUIRED', '请先登录数仓')
        if request.headers.get('Origin') and not same_origin(request):
            raise DbprogError(403, 'DBPROG_ORIGIN', '请从数仓页面下载压缩包')
        dataset_id, capture_session, robot_id, task_id = capture_arguments(request,
            request.match_info['dataset_id'], request.match_info['session_id'])
        endpoint = f'http://dbprog/api/v1/datasets/{dataset_id}/capture-sessions/{capture_session}/package'
        endpoint += '?robotId=' + robot_id + '&async=true&taskId=' + task_id
        # Recheck warehouse ACL and the exact robot/session/snapshot using the
        # original account before every GET, HEAD, Range or conditional fetch.
        url, filename = await package_details(endpoint, token, task_id=task_id)
        return await stream_package(request, url, filename)

    async def stream_package(request, url, filename):
        identity(request)  # The observer session may have been revoked during preparation.
        headers = {name: request.headers[name] for name in
                   ('Range', 'If-Range', 'If-None-Match', 'If-Modified-Since') if name in request.headers}
        async with gateway.download_slots:
            identity(request)  # Approval may also change while waiting for a download slot.
            async with gateway.objects.request('GET', url,
                    headers=headers, allow_redirects=False, auto_decompress=False) as upstream:
                if upstream.status not in (200, 206, 304, 416):
                    raise DbprogError(502, 'DBPROG_DOWNLOAD', '压缩包暂时无法读取，请重试')
                outgoing = response_headers(upstream)
                outgoing['Content-Type'] = 'application/octet-stream'
                outgoing['Content-Disposition'] = "attachment; filename=\"archive.tar.zst\"; filename*=UTF-8''" + quote(filename, safe='')
                for name in ('Content-Length', 'Content-Encoding'):
                    if name in upstream.headers:
                        outgoing[name] = upstream.headers[name]
                return await streamed(request, upstream, outgoing)

    async def static(request):
        try:
            viewer(request)
        except web.HTTPUnauthorized:
            raise web.HTTPFound('/login')
        except web.HTTPForbidden:
            raise web.HTTPFound('/pending')
        if request.path == '/dbprog':
            raise web.HTTPFound('/dbprog/')
        tail = request.match_info.get('tail', '')
        parts = tail.split('/')
        if any(part.startswith('.') or '\\' in part for part in parts):
            raise web.HTTPNotFound()
        target = (root / tail).resolve()
        if root not in target.parents and target != root:
            raise web.HTTPNotFound()
        if target.is_file() and target.suffix.lower() in STATIC_SUFFIXES:
            pass
        elif not Path(tail).suffix:
            target = root / 'index.html'
        else:
            raise web.HTTPNotFound()
        if not target.is_file():
            raise web.HTTPServiceUnavailable(reason='数仓前端尚未安装')
        mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        return web.Response(body=target.read_bytes(), content_type=mime)

    app.add_routes([
        web.route('*', '/dbprog/api/{tail:.*}', api),
        web.get('/dbprog/nest/{endpoint}', system_endpoint),
        web.get('/dbprog/version.json', system_endpoint),
        web.get('/dbprog/objects/{file_id}', objects),
        web.get('/dbprog/packages/{dataset_id}/{recording_id}', packages),
        web.get('/dbprog/folder-packages/{dataset_id}', folder_packages),
        web.get('/dbprog/capture-packages/{dataset_id}/{session_id}', capture_packages),
        web.get('/dbprog', static), web.get('/dbprog/', static),
        web.get('/dbprog/{tail:.*}', static),
    ])
