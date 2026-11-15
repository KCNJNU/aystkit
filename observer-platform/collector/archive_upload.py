"""Durable native-recording uploads to the local PostgreSQL-backed warehouse API.

Only finalized archive.json segments are accepted. This module never removes a
recorded file and never uses an administrator identity for unattended uploads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
from datetime import datetime, timezone
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID
from zoneinfo import ZoneInfo

from collector.archive_core import atomic_json, MAX_FILE_BYTES, valid_file_size

CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_UPLOAD_BYTES_PER_SECOND = 1024 * 1024
UPLOAD_SEND_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 4 * 1024**2
TOKEN = re.compile(r'[A-Za-z0-9_-]{20,256}\Z')


class ArchiveUploadError(Exception):
    """Fixed diagnostic codes; exception text never contains credentials or API bodies."""
    def __init__(self, code, status=None, payload=None):
        super().__init__(code)
        self.code, self.status, self.payload = code, status, payload


def upload_rate(value):
    """Literal nonnegative integer, with zero an explicit unlimited choice."""
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            value = int(value)
        except ValueError:
            raise ArchiveUploadError('ARCHIVE_UPLOAD_RATE_INVALID') from None
    if type(value) is not int or not 0 <= value <= 1024 ** 3:
        raise ArchiveUploadError('ARCHIVE_UPLOAD_RATE_INVALID')
    return value


class UploadRateLimiter:
    """A shared per-client clock across files, parts and HTTP retries.

    Each bounded block pays its time before being handed to the socket. Idle
    time earns no burst allowance. Metadata and acknowledgements bypass it.
    """
    def __init__(self, bytes_per_second, *, clock=time.monotonic, wait=time.sleep,
                 stopping=None):
        self.rate = upload_rate(bytes_per_second)
        self.clock, self.wait, self.stopping = clock, wait, stopping
        self.next_send_at = None
        self.lock = threading.Lock()

    def before_send(self, size):
        with self.lock:
            if self.stopping is not None and self.stopping.is_set():
                raise ArchiveUploadError('ARCHIVE_UPLOAD_CANCELLED')
            if not self.rate:
                return
            now = self.clock()
            due = max(now, self.next_send_at if self.next_send_at is not None else now) + size / self.rate
            self.next_send_at = due
            while True:
                if self.stopping is not None and self.stopping.is_set():
                    raise ArchiveUploadError('ARCHIVE_UPLOAD_CANCELLED')
                remaining = due - self.clock()
                if remaining <= 0:
                    return
                if self.stopping is None:
                    self.wait(min(.1, remaining))
                elif self.stopping.wait(min(.1, remaining)):
                    raise ArchiveUploadError('ARCHIVE_UPLOAD_CANCELLED')


class RateLimitedBody:
    """Reiterable fixed-length HTTP data; retries resend every source byte."""
    def __init__(self, data, limiter):
        self.data, self.limiter = data, limiter

    def __len__(self):
        return len(self.data)

    def __iter__(self):
        for offset in range(0, len(self.data), UPLOAD_SEND_BYTES):
            block = self.data[offset:offset + UPLOAD_SEND_BYTES]
            self.limiter.before_send(len(block))
            yield block


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def safe_api_base(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or not ipaddress.ip_address(parsed.hostname).is_loopback
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip('/') != '/api'
                or any(ord(char) <= 32 for char in value)):
            raise ValueError()
        parsed.port
    except ValueError:
        raise ArchiveUploadError('ARCHIVE_API_MUST_BE_LOOPBACK') from None
    return value.rstrip('/')


def entry_name(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or value in ('.', '..', '.folder', '.dataset') or len(value.encode()) > 200
            or any(char in '/\\' or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ArchiveUploadError('ARCHIVE_NAME_INVALID')
    return value


class LocalWarehouseApi:
    """Bounded, fixed-loopback HTTP session; callers choose their permitted role."""
    def __init__(self, api_base='http://127.0.0.1:3000/api', *, opener=None, timeout=120,
                 upload_bytes_per_second=0, upload_limiter=None):
        self.base = safe_api_base(api_base)
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.timeout = timeout
        self.token = self.csrf = ''
        self.upload_limiter = upload_limiter or UploadRateLimiter(upload_bytes_per_second)

    def request(self, path, method='GET', payload=None, body=None, headers=None):
        if not path.startswith('/') or path.startswith('//') or '\\' in path:
            raise ArchiveUploadError('ARCHIVE_API_PATH_INVALID')
        outgoing = {'Accept': 'application/json', **(headers or {})}
        if self.token:
            outgoing['Cookie'] = 'cr_session=' + self.token
        if method not in ('GET', 'HEAD') and self.csrf:
            outgoing['X-CSRF-Token'] = self.csrf
        if payload is not None:
            body = json.dumps(payload, allow_nan=False).encode()
            outgoing['Content-Type'] = 'application/json'
        if body is not None and len(body) > CHUNK_BYTES:
            raise ArchiveUploadError('ARCHIVE_REQUEST_TOO_LARGE')
        if method == 'PUT' and payload is None and body is not None:
            if not isinstance(body, bytes):
                raise ArchiveUploadError('ARCHIVE_REQUEST_BODY_INVALID')
            # urllib/http.client iterates this body in bounded blocks. An
            # explicit length preserves the warehouse's 8MiB part contract
            # without chunked transfer encoding or an 8MiB socket burst.
            outgoing = {key: value for key, value in outgoing.items()
                        if key.lower() not in ('content-length', 'transfer-encoding')}
            outgoing['Content-Length'] = str(len(body))
            body = RateLimitedBody(body, self.upload_limiter)
        request = Request(self.base + path, data=body, headers=outgoing, method=method)
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        except (OSError, URLError, HTTPException):
            raise ArchiveUploadError('ARCHIVE_API_UNAVAILABLE') from None
        try:
            with response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status_code = response.status
                cookies = response.headers.get_all('Set-Cookie', [])
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError()
            data = json.loads(raw) if raw else {}
            if not isinstance(data, (dict, list)):
                raise ValueError()
        except (ValueError, OSError, HTTPException):
            raise ArchiveUploadError('ARCHIVE_API_RESPONSE_INVALID') from None
        if status_code >= 300:
            raise ArchiveUploadError('ARCHIVE_HTTP_' + str(status_code), status_code, data)
        for header in cookies:
            parsed = SimpleCookie()
            try:
                parsed.load(header)
            except CookieError:
                raise ArchiveUploadError('ARCHIVE_API_RESPONSE_INVALID') from None
            if 'cr_session' in parsed:
                candidate = parsed['cr_session'].value
                self.token = candidate if TOKEN.fullmatch(candidate) else ''
        return data

    def login(self, email, password):
        self.token = self.csrf = ''
        data = self.request('/v1/auth/login', 'POST', {'email': email, 'password': password})
        csrf = data.get('csrfToken')
        if not self.token or not isinstance(csrf, str) or not TOKEN.fullmatch(csrf):
            raise ArchiveUploadError('ARCHIVE_LOGIN_RESPONSE_INVALID')
        self.csrf = csrf
        return data.get('user')


@dataclass(frozen=True)
class ArchiveUploadConfig:
    email: str
    password: str = field(repr=False)
    api_url: str = 'http://127.0.0.1:3000/api'
    dataset_prefix: str = '地库_'
    robot_id: str = ''
    upload_bytes_per_second: int = DEFAULT_UPLOAD_BYTES_PER_SECOND

    @classmethod
    def from_environment(cls, path=None):
        data = {}
        if path:
            target = Path(path)
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
                raise ArchiveUploadError('ARCHIVE_CONFIG_MUST_BE_PRIVATE')
            data = json.loads(target.read_text())
        email = os.environ.get('ARCHIVE_DBPROG_EMAIL', data.get('email', ''))
        password = os.environ.get('ARCHIVE_DBPROG_PASSWORD', data.get('password', ''))
        if not email or not password or email.strip().lower() == 'admin':
            raise ArchiveUploadError('ARCHIVE_OPERATOR_CREDENTIALS_REQUIRED')
        return cls(email, password,
            os.environ.get('ARCHIVE_DBPROG_API_URL', data.get('apiUrl', cls.api_url)),
            os.environ.get('ARCHIVE_DATASET_PREFIX', data.get('datasetPrefix', cls.dataset_prefix)),
            os.environ.get('ARCHIVE_ROBOT_ID', data.get('robotId', '')),
            upload_rate(os.environ.get('ARCHIVE_UPLOAD_BYTES_PER_SECOND',
                data.get('uploadBytesPerSecond', DEFAULT_UPLOAD_BYTES_PER_SECOND))))


class ArchiveUploadClient:
    def __init__(self, config, *, opener=None):
        self.config = config
        if not config.email or not config.password or config.email.strip().lower() == 'admin':
            raise ArchiveUploadError('ARCHIVE_OPERATOR_CREDENTIALS_REQUIRED')
        self.api = LocalWarehouseApi(config.api_url, opener=opener,
            upload_bytes_per_second=config.upload_bytes_per_second)
        self.user = None

    def _validate_user(self, user):
        if (not isinstance(user, dict) or user.get('role') != 'OPERATOR'
                or user.get('mustChangePassword') is True or not user.get('tenantId')
                or not isinstance(user.get('projectIds'), list) or len(user['projectIds']) != 1):
            if self.api.token and self.api.csrf:
                try:
                    self.api.request('/v1/auth/logout', 'POST')
                except ArchiveUploadError:
                    pass
            self.api.token = self.api.csrf = ''
            raise ArchiveUploadError('ARCHIVE_SCOPED_OPERATOR_REQUIRED')
        self.user = user

    def login(self):
        self._validate_user(self.api.login(self.config.email, self.config.password))

    def ensure_day(self, day, sensors=()):
        if datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day:
            raise ArchiveUploadError('ARCHIVE_DAY_INVALID')
        robot_id = str(UUID(self.config.robot_id))
        if not self.user:
            self.login()
        dataset_id = self._dataset({'day': day, 'robotId': robot_id})
        for sensor in sensors:
            self._folders(dataset_id, entry_name(sensor))
        return dataset_id

    def _request(self, path, method='GET', payload=None, body=None, headers=None):
        if not self.user or not self.api.token:
            self.login()
        try:
            return self.api.request(path, method, payload, body, headers)
        except ArchiveUploadError as error:
            if error.status == 401:
                self.login()
            elif (error.status == 403 and isinstance(error.payload, dict)
                    and 'csrf' in str(error.payload.get('message', '')).lower()):
                current = self.api.request('/v1/auth/session')
                self._validate_user(current.get('user'))
                csrf = current.get('csrfToken')
                if not isinstance(csrf, str) or not TOKEN.fullmatch(csrf):
                    raise ArchiveUploadError('ARCHIVE_CSRF_RESPONSE_INVALID') from None
                self.api.csrf = csrf
            else:
                raise
            return self.api.request(path, method, payload, body, headers)

    def _manifest(self, segment):
        segment = Path(segment).resolve()
        path = segment / 'archive.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESPONSE_BYTES:
            raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID')
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
            day = data['day']
            datetime.strptime(day, '%Y-%m-%d')
            if data.get('schemaVersion') != 1 or not isinstance(data.get('files'), list) or not 1 <= len(data['files']) <= 4096:
                raise ValueError()
            data['robotId'] = str(UUID(data.get('robotId') or self.config.robot_id))
            if self.config.robot_id and data['robotId'] != str(UUID(self.config.robot_id)):
                raise ArchiveUploadError('ARCHIVE_ROBOT_IDENTITY_MISMATCH')
            entry_name(data['sensor']); entry_name(data['label'])
            data['id'] = entry_name(data.get('id') or segment.name)
            started = datetime.fromisoformat(data['startedAt'].replace('Z', '+00:00'))
            ended = datetime.fromisoformat(data['endAt'].replace('Z', '+00:00'))
            if started.tzinfo is None or ended.tzinfo is None or ended < started:
                raise ValueError()
            if started.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d') != day:
                raise ValueError()
            if len(set(data['files'])) != len(data['files']):
                raise ValueError()
            for name in data['files']:
                if not isinstance(name, str) or '\\' in name:
                    raise ValueError()
                relative = Path(name)
                if str(relative) != name or relative.is_absolute() or any(part in ('', '.', '..') for part in relative.parts):
                    raise ValueError()
                for part in relative.parts:
                    entry_name(part)
                current = segment
                for part in relative.parts:
                    current = current / part
                    if current.is_symlink():
                        raise ValueError()
                if segment not in current.resolve().parents or not current.is_file():
                    raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID') from None
        from .archive_policy import training_only_enabled, training_manifest_allowed
        if training_only_enabled() and not training_manifest_allowed(data):
            raise ArchiveUploadError('ARCHIVE_MANIFEST_EXCLUDED_BY_STORAGE_POLICY')
        return segment, data, hashlib.sha256(raw).hexdigest()

    def _dataset(self, manifest):
        legacy_name = entry_name(self.config.dataset_prefix + manifest['day'])
        project_id = self.user['projectIds'][0]
        # Retain existing names/links. A second robot uses a stable full UUID,
        # avoiding same-day conflicts even if operators copy the same prefix.
        names = [legacy_name, entry_name(legacy_name + '_' + manifest['robotId'])]
        for name in names:
            def existing():
                page = self._request('/v1/datasets/page?' + urlencode({'search': name, 'page': 1, 'pageSize': 100}))
                items = [item for item in page.get('items', []) if item.get('name') == name]
                if not items:
                    return None
                item = items[0]
                meta = item.get('metadata') or {}
                if (name == legacy_name and meta.get('type') == 'garage-native-archive'
                        and isinstance(meta.get('robotId'), str) and meta['robotId'] != manifest['robotId']):
                    return False
                if (item.get('projectId') != project_id or meta.get('type') != 'garage-native-archive'
                        or meta.get('robotId') != manifest['robotId'] or item.get('deletingAt')):
                    raise ArchiveUploadError('ARCHIVE_DATASET_OWNERSHIP_CONFLICT')
                return item['id']
            found = existing()
            if found is False:
                continue
            if found:
                return found
            try:
                created = self._request('/v1/datasets', 'POST', {'name': name, 'projectId': project_id,
                    'description': '地库原生传感器记录；按机器人、采集日期与传感器时间段归档',
                    'metadata': {'type': 'garage-native-archive', 'robotId': manifest['robotId'],
                        'day': manifest['day'], 'timezone': 'Asia/Shanghai'}})
                return created['id']
            except ArchiveUploadError as error:
                if error.status != 409:
                    raise
                found = existing()
                if found:
                    return found
                if found is False:
                    continue
                raise ArchiveUploadError('ARCHIVE_DATASET_OWNERSHIP_CONFLICT') from None
        raise ArchiveUploadError('ARCHIVE_DATASET_OWNERSHIP_CONFLICT')

    def _folders(self, dataset_id, path):
        folders = self._request(f'/v1/datasets/{dataset_id}/folders')
        known = {item.get('path') for item in folders}
        parent = ''
        for name in path.split('/'):
            current = '/'.join(filter(None, (parent, name)))
            if current not in known:
                try:
                    self._request(f'/v1/datasets/{dataset_id}/folders', 'POST', {'parentPath': parent, 'name': name})
                except ArchiveUploadError as error:
                    if error.status != 409 or current not in {
                            item.get('path') for item in self._request(f'/v1/datasets/{dataset_id}/folders')}:
                        raise
                known.add(current)
            parent = current

    @staticmethod
    def _digest(path):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if not valid_file_size(before.st_size):
                raise ArchiveUploadError('ARCHIVE_FILE_SIZE_INVALID')
            for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ArchiveUploadError('ARCHIVE_FILE_CHANGED')
        return digest.hexdigest(), before.st_size, before.st_mtime_ns

    def _confirmed_file(self, file_id, expected, dataset_id, folder_path, name, robot_id=None):
        if not isinstance(file_id, str):
            raise ArchiveUploadError('ARCHIVE_UPLOAD_CONTRACT_INVALID')
        try:
            item = self._request('/v1/files/' + str(UUID(file_id)))
        except (ValueError, TypeError):
            raise ArchiveUploadError('ARCHIVE_UPLOAD_CONTRACT_INVALID') from None
        expected_robot = robot_id or self.config.robot_id
        if (item.get('checksum', '').lower() != expected['sha256']
                or str(item.get('fileSize')) != str(expected['size'])
                or item.get('datasetId') != dataset_id or item.get('folderPath', '') != folder_path
                or (expected_robot and item.get('robotId') != expected_robot)):
            raise ArchiveUploadError('ARCHIVE_REMOTE_FILE_MISMATCH')
        if item.get('originalName') != name:
            # The warehouse currently deduplicates by dataset/folder/checksum,
            # including different filenames. This is a naming constraint, not
            # permission to acknowledge the other file under this local name.
            raise ArchiveUploadError('ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT')
        try:
            self._request('/v1/files/' + item['id'] + '/download')
        except ArchiveUploadError as error:
            # The API can retain a RobotFile row after its object was lost from
            # MinIO. Treat that orphan as recoverable instead of blocking every
            # later segment behind a permanent HTTP 404.
            if error.status == 404:
                raise ArchiveUploadError('ARCHIVE_REMOTE_OBJECT_MISSING', 404) from None
            raise
        return {'robotFileId': item['id'], 'sha256': expected['sha256'], 'size': expected['size'],
                'objectKey': item.get('objectKey'), 'confirmedAt': now_iso()}

    def _abort_stale_upload(self, upload_id):
        if not upload_id:
            return
        try:
            self._request('/v1/uploads/' + str(UUID(upload_id)), 'DELETE')
        except (ValueError, TypeError):
            raise ArchiveUploadError('ARCHIVE_UPLOAD_RECOVERY_FAILED') from None
        except ArchiveUploadError as error:
            if error.status == 404:
                return
            raise ArchiveUploadError('ARCHIVE_UPLOAD_RECOVERY_FAILED', error.status) from None

    def _recycle_missing_file(self, file_id):
        if not file_id:
            return
        try:
            self._request('/v1/files/' + str(UUID(file_id)), 'DELETE')
        except (ValueError, TypeError):
            raise ArchiveUploadError('ARCHIVE_UPLOAD_RECOVERY_FAILED') from None
        except ArchiveUploadError as error:
            # A concurrent repair may already have moved the row to the
            # recycle bin. Any other response must remain visible and stop the
            # queue rather than silently creating a duplicate.
            if error.status == 404:
                return
            raise ArchiveUploadError('ARCHIVE_UPLOAD_RECOVERY_FAILED', error.status) from None

    def _reset_stale_upload(self, file, state_path, state, *, upload_id=None, file_id=None):
        """Release an orphan upload/file and make the local state retryable."""
        self._abort_stale_upload(upload_id)
        self._recycle_missing_file(file_id)
        for key in ('uploadId', 'jobId', 'parts', 'previousUploadId', 'confirmed'):
            file.pop(key, None)
        atomic_json(state_path, state)

    def _reset_stale_dataset(self, state_path, state):
        """Discard references into a deleted dataset while keeping local bytes."""
        for file in (state.get('files') or {}).values():
            if not isinstance(file, dict):
                continue
            self._abort_stale_upload(file.pop('uploadId', None))
            for key in ('jobId', 'parts', 'previousUploadId', 'confirmed'):
                file.pop(key, None)
        state.pop('datasetId', None)
        state.pop('foldersReady', None)
        atomic_json(state_path, state)

    def _dataset_for_state(self, manifest, state_path, state):
        """Return a live dataset id, recovering a deleted dataset reference."""
        dataset_id = state.get('datasetId')
        if dataset_id:
            try:
                UUID(str(dataset_id))
                self._request(f'/v1/datasets/{dataset_id}/folders')
                return str(dataset_id)
            except ValueError:
                pass
            except ArchiveUploadError as error:
                if error.status != 404:
                    raise
            self._reset_stale_dataset(state_path, state)
        dataset_id = self._dataset(manifest)
        state['datasetId'] = dataset_id
        state['foldersReady'] = False
        atomic_json(state_path, state)
        return dataset_id

    def upload_segment(self, segment_dir):
        """Serialize foreground/background upload state for one sealed segment."""
        segment = Path(segment_dir).resolve()
        descriptor = os.open(segment / '.upload.lock',
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ArchiveUploadError('ARCHIVE_UPLOAD_LOCK_INVALID')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'state': 'pending', 'reason': 'ARCHIVE_SEGMENT_UPLOAD_BUSY'}
            return self._upload_segment_unlocked(segment)
        finally:
            os.close(descriptor)

    def _upload_segment_unlocked(self, segment_dir):
        segment, manifest, manifest_sha = self._manifest(segment_dir)
        ack_path, state_path = segment / 'uploaded.json', segment / 'upload-state.json'
        if ack_path.is_symlink() or state_path.is_symlink():
            raise ArchiveUploadError('ARCHIVE_STATE_INVALID')
        if ack_path.is_file():
            ack = json.loads(ack_path.read_text())
            if ack.get('manifestSha256') != manifest_sha or ack.get('state') != 'complete':
                raise ArchiveUploadError('ARCHIVE_MANIFEST_CHANGED')
            self._preserve_frame_time_index(segment, manifest)
            return ack
        state = json.loads(state_path.read_text()) if state_path.is_file() else {
            'schemaVersion': 1, 'manifestSha256': manifest_sha, 'files': {}}
        if state.get('manifestSha256') != manifest_sha:
            raise ArchiveUploadError('ARCHIVE_MANIFEST_CHANGED')
        if not self.user:
            self.login()
        dataset_id = self._dataset_for_state(manifest, state_path, state)
        base_folder = '/'.join((manifest['sensor'], manifest['label'], manifest['id']))
        if not state.get('foldersReady'):
            self._folders(dataset_id, base_folder)
            state['foldersReady'] = True
            atomic_json(state_path, state)
        pending = False
        name_conflict = False
        for relative in manifest['files']:
            file = state['files'].setdefault(relative, {})
            local = segment / relative
            info = local.stat()
            if not file.get('sha256') or (file.get('size'), file.get('mtimeNs')) != (info.st_size, info.st_mtime_ns):
                if file.get('uploadId') or file.get('confirmed'):
                    raise ArchiveUploadError('ARCHIVE_FILE_CHANGED')
                digest, size, modified = self._digest(local)
                file.update(sha256=digest, size=size, mtimeNs=modified)
                atomic_json(state_path, state)
            if file.get('confirmed'):
                continue
            remote_folder = '/'.join((base_folder, *Path(relative).parts[:-1]))
            name = Path(relative).name
            session = None
            if file.get('uploadId'):
                try:
                    session = self._request('/v1/uploads/' + file['uploadId'])
                except ArchiveUploadError as error:
                    if error.status != 404:
                        raise
                    file.pop('uploadId', None)
            if session and (session.get('checksum', '').lower() != file['sha256']
                    or str(session.get('fileSize')) != str(file['size'])
                    or session.get('datasetId') != dataset_id or session.get('folderPath', '') != remote_folder
                    or session.get('originalName') != name or session.get('robotId') != manifest['robotId']
                    or session.get('chunkSize') != CHUNK_BYTES
                    or session.get('totalParts') != (file['size'] + CHUNK_BYTES - 1) // CHUNK_BYTES):
                raise ArchiveUploadError('ARCHIVE_UPLOAD_STATE_MISMATCH')
            if session and session.get('status') == 'COMPLETED':
                file_id = session.get('robotFileId')
                try:
                    file['confirmed'] = self._confirmed_file(
                        file_id, file, dataset_id, remote_folder, name, manifest['robotId'])
                except ArchiveUploadError as error:
                    if error.code == 'ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT':
                        name_conflict = True
                        continue
                    if error.code != 'ARCHIVE_REMOTE_OBJECT_MISSING':
                        raise
                    # Keep the local bytes. Remove only the unusable remote
                    # metadata/session so the next pass can upload afresh.
                    self._reset_stale_upload(file, state_path, state, file_id=file_id)
                    pending = True
                    continue
                atomic_json(state_path, state)
                continue
            if session and session.get('status') == 'FINALIZING':
                if file.get('jobId'):
                    try:
                        job = self._request('/v1/jobs/' + file['jobId'])
                    except ArchiveUploadError as error:
                        if error.status != 404:
                            raise
                        self._reset_stale_upload(file, state_path, state,
                            upload_id=file.get('uploadId'))
                        pending = True
                        continue
                    if job.get('status') in ('FAILED', 'CANCELLED'):
                        self._reset_stale_upload(file, state_path, state,
                            upload_id=file.get('uploadId'))
                        pending = True
                        continue
                pending = True
                continue
            if session:
                expiry = datetime.fromisoformat(session['expiresAt'].replace('Z', '+00:00'))
                if session.get('status') in ('ABORTED', 'EXPIRED') or expiry <= datetime.now(timezone.utc):
                    if session.get('status') not in ('ABORTED', 'EXPIRED'):
                        self._request('/v1/uploads/' + file['uploadId'], 'DELETE')
                    file['previousUploadId'] = file.pop('uploadId')
                    file.pop('jobId', None)
                    file['parts'] = []
                    session = None
                    atomic_json(state_path, state)
            if session is None:
                page = self._request('/v1/files?' + urlencode({'datasetId': dataset_id,
                    'folderPath': remote_folder, 'checksum': file['sha256'], 'page': 1, 'pageSize': 1}))
                duplicate = next(iter(page.get('items', [])), None)
                if duplicate:
                    try:
                        file['confirmed'] = self._confirmed_file(
                            duplicate['id'], file, dataset_id, remote_folder, name, manifest['robotId'])
                    except ArchiveUploadError as error:
                        if error.code == 'ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT':
                            name_conflict = True
                            continue
                        if error.code != 'ARCHIVE_REMOTE_OBJECT_MISSING':
                            raise
                        self._reset_stale_upload(file, state_path, state, file_id=duplicate.get('id'))
                        pending = True
                        continue
                    atomic_json(state_path, state)
                    continue
                try:
                    session = self._request('/v1/uploads', 'POST', {'datasetId': dataset_id,
                        'folderPath': base_folder, 'robotId': manifest['robotId'], 'originalName': relative,
                        'mimeType': mimetypes.guess_type(name)[0] or 'application/octet-stream',
                        'fileSize': file['size'], 'checksum': file['sha256'],
                        'recognitionType': 'garage-native-' + manifest['sensor'], 'schemaVersion': '1.0',
                        'capturedAt': manifest['startedAt'], 'metadata': {**manifest.get('metadata', {}),
                            'archiveType': 'garage-native-archive', 'segmentId': manifest['id'],
                            'startedAt': manifest['startedAt'], 'endAt': manifest['endAt'],
                            'manifestSha256': manifest_sha, 'relativePath': relative}})
                except ArchiveUploadError as error:
                    duplicate_id = error.payload.get('robotFileId') if isinstance(error.payload, dict) else None
                    if error.status != 409 or not duplicate_id:
                        raise
                    try:
                        file['confirmed'] = self._confirmed_file(
                            duplicate_id, file, dataset_id, remote_folder, name, manifest['robotId'])
                    except ArchiveUploadError as duplicate_error:
                        if duplicate_error.code == 'ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT':
                            name_conflict = True
                            continue
                        if duplicate_error.code != 'ARCHIVE_REMOTE_OBJECT_MISSING':
                            raise
                        self._reset_stale_upload(file, state_path, state, file_id=duplicate_id)
                        pending = True
                        continue
                    atomic_json(state_path, state)
                    continue
                if session.get('chunkSize') != CHUNK_BYTES or not session.get('id'):
                    raise ArchiveUploadError('ARCHIVE_UPLOAD_CONTRACT_INVALID')
                file.update(uploadId=session['id'], parts=[])
                atomic_json(state_path, state)
            received = {part['partNumber']: part for part in session.get('parts', [])}
            with local.open('rb') as stream:
                for number in range(1, session['totalParts'] + 1):
                    data = stream.read(CHUNK_BYTES)
                    digest = hashlib.sha256(data).hexdigest()
                    existing = received.get(number, {})
                    if existing.get('checksum') != digest or existing.get('size') != len(data):
                        start = (number - 1) * CHUNK_BYTES
                        self._request(f'/v1/uploads/{file["uploadId"]}/parts/{number}', 'PUT', body=data,
                            headers={'Content-Type': 'application/octet-stream',
                                'Content-Range': f'bytes {start}-{start + len(data) - 1}/{file["size"]}',
                                'X-Part-SHA256': digest})
                    file['parts'] = sorted(set(file.get('parts', [])) | {number})
                    atomic_json(state_path, state)
            after = local.stat()
            if (file['size'], file['mtimeNs']) != (after.st_size, after.st_mtime_ns):
                raise ArchiveUploadError('ARCHIVE_FILE_CHANGED')
            completion = self._request('/v1/uploads/' + file['uploadId'] + '/complete', 'POST')
            if completion.get('completed') is True:
                try:
                    file['confirmed'] = self._confirmed_file(
                        completion['robotFileId'], file, dataset_id, remote_folder, name, manifest['robotId'])
                except ArchiveUploadError as error:
                    if error.code != 'ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT':
                        raise
                    name_conflict = True
                    continue
            elif completion.get('id'):
                file['jobId'] = completion['id']
                pending = True
            else:
                raise ArchiveUploadError('ARCHIVE_COMPLETE_RESPONSE_INVALID')
            atomic_json(state_path, state)
        if name_conflict:
            # Unrelated files in this segment were still offered to the API.
            # Keep all local bytes and avoid a false whole-segment ACK or a
            # busy retry loop for the platform's deterministic name conflict.
            raise ArchiveUploadError('ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT')
        if pending or any(not value.get('confirmed') for value in state['files'].values()):
            return {'state': 'pending', 'datasetId': dataset_id, 'segmentId': manifest['id']}
        ack = {'schemaVersion': 1, 'state': 'complete', 'manifestSha256': manifest_sha,
            'datasetId': dataset_id, 'folderPath': base_folder, 'segmentId': manifest['id'],
            'completedAt': now_iso(), 'files': [dict(relativePath=name, **state['files'][name]['confirmed'])
                                              for name in manifest['files']]}
        self._preserve_frame_time_index(segment, manifest)
        atomic_json(ack_path, ack)
        return ack

    @staticmethod
    def _preserve_frame_time_index(segment, manifest):
        """Index faults retain the video at cleanup without blocking its upload."""
        try:
            from .archive_frame_index_cache import preserve_frame_time_index
            result = preserve_frame_time_index(segment, manifest)
        except Exception as error:
            result = dict(state='failed', lastError=getattr(error, 'code',
                'ARCHIVE_FRAME_TIME_INDEX_CACHE_FAILED'))
        if result.get('state') != 'not_required':
            try:
                atomic_json(segment / 'frame-time-index-cache.json', result)
            except OSError:
                pass  # The destructive cleanup step verifies the cache again.
