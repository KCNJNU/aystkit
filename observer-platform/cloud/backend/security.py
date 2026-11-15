"""Independent device/viewer credentials, password hashing and bounded sessions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from shared.protocol import CAMERA_KEYS, IDENTIFIER, MEDIA_KEYS


def _b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def _unb64(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def hash_password(password, salt=None):
    if not isinstance(password, str) or not password:
        raise ValueError('password must not be empty')
    salt = secrets.token_bytes(16) if salt is None else salt
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f'scrypt$16384$8$1${_b64(salt)}${_b64(derived)}'


def verify_password(password, encoded):
    try:
        algorithm, n, r, p, salt, expected = encoded.split('$')
        if (algorithm != 'scrypt' or (n, r, p) != ('16384', '8', '1')
                or not isinstance(password, str) or not password):
            return False
        salt, expected = _unb64(salt), _unb64(expected)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


@dataclass(frozen=True)
class DeviceCredential:
    token: str
    robot_id: str
    source_id: str
    role: str
    media_keys: frozenset = field(default_factory=lambda: MEDIA_KEYS)


@dataclass
class Config:
    robot_id: str
    device_credentials: list
    viewer_username: str
    viewer_password_hash: str
    session_secret: str
    public_origin: str
    data_dir: str
    allow_insecure_local: bool = False
    port: int = 8888
    session_ttl_s: int = 8 * 3600
    heartbeat_timeout_s: float = 10
    observation_timeout_s: float = 5
    upstream_timeout_s: float = 2
    trusted_proxy_cidrs: tuple = ('127.0.0.1/32', '::1/128')
    frontend_dir: str = str(Path(__file__).resolve().parents[1] / 'frontend')
    fleet: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls):
        required = ('ROBOT_ID', 'DEVICE_CREDENTIALS_JSON', 'VIEWER_USERNAME',
                    'VIEWER_PASSWORD_HASH', 'SESSION_SECRET', 'PUBLIC_ORIGIN', 'DATA_DIR')
        if any(not os.environ.get(key) for key in required):
            raise ValueError('missing required cloud environment configuration')
        return cls(robot_id=os.environ['ROBOT_ID'],
            device_credentials=json.loads(os.environ['DEVICE_CREDENTIALS_JSON']),
            viewer_username=os.environ['VIEWER_USERNAME'],
            viewer_password_hash=os.environ['VIEWER_PASSWORD_HASH'],
            session_secret=os.environ['SESSION_SECRET'], public_origin=os.environ['PUBLIC_ORIGIN'],
            data_dir=os.environ['DATA_DIR'], allow_insecure_local=os.environ.get('ALLOW_INSECURE_LOCAL', 'false').lower() == 'true',
            port=int(os.environ.get('PORT', '8888')),
            fleet=json.loads(os.environ.get('ROBOT_FLEET_JSON', '{}')),
            trusted_proxy_cidrs=tuple(os.environ.get('TRUSTED_PROXY_CIDRS', '127.0.0.1/32,::1/128').split(',')))

    def validate(self):
        if not IDENTIFIER.fullmatch(self.robot_id) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', self.viewer_username):
            raise ValueError('invalid robot or viewer identity')
        if len(self.session_secret) < 32:
            raise ValueError('SESSION_SECRET must be at least 32 characters')
        parts = urlsplit(self.public_origin)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.path or parts.query or parts.fragment:
            raise ValueError('PUBLIC_ORIGIN must be an exact origin without path')
        if parts.scheme != 'https' and not (self.allow_insecure_local and parts.hostname in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('production PUBLIC_ORIGIN requires HTTPS; test HTTP must be loopback')
        if self.allow_insecure_local and parts.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('insecure local option requires loopback origin')
        for network in self.trusted_proxy_cidrs:
            ipaddress.ip_network(network.strip(), strict=False)
        # Validate encoded hash without accepting plaintext configuration.
        fields = self.viewer_password_hash.split('$')
        if len(fields) != 6 or fields[:4] != ['scrypt', '16384', '8', '1'] or len(_unb64(fields[4])) != 16 or len(_unb64(fields[5])) != 32:
            raise ValueError('VIEWER_PASSWORD_HASH must use supported scrypt format')
        credentials, pairs, tokens = [], set(), set()
        if not isinstance(self.device_credentials, list) or not 1 <= len(self.device_credentials) <= 64:
            raise ValueError('expected 1 to 64 device credentials')
        for item in self.device_credentials:
            if isinstance(item, DeviceCredential):
                device = item
            else:
                if not isinstance(item, dict) or set(item) - {'token', 'robot_id', 'source_id', 'role', 'media_keys'}:
                    raise ValueError('invalid device credential fields')
                device = DeviceCredential(item['token'], item['robot_id'], item['source_id'], item['role'],
                    frozenset(item.get('media_keys', MEDIA_KEYS if item['role'] == 'primary' else ())))
            if (not isinstance(device.robot_id, str) or not IDENTIFIER.fullmatch(device.robot_id)
                    or not IDENTIFIER.fullmatch(device.source_id) or device.role not in ('primary', 'cameras')):
                raise ValueError('device identity or role mismatch')
            if not isinstance(device.token, str) or not 32 <= len(device.token) <= 512 or any(c.isspace() for c in device.token):
                raise ValueError('device token must have at least 32 characters without whitespace')
            if not device.media_keys <= MEDIA_KEYS or (device.role == 'cameras' and not device.media_keys):
                raise ValueError('camera role needs an explicit media key allowlist')
            if device.role == 'cameras' and not device.media_keys <= (CAMERA_KEYS | {'depthPoints'}):
                raise ValueError('camera source may only upload camera frames and depth points')
            pair = (device.robot_id, device.source_id)
            if pair in pairs or device.token in tokens:
                raise ValueError('duplicate device source or token')
            pairs.add(pair)
            tokens.add(device.token)
            credentials.append(device)
        robots = {d.robot_id for d in credentials}
        if self.robot_id not in robots or len(robots) > 10:
            raise ValueError('expected default robot and at most 10 registered robots')
        if any(sum(d.role == 'primary' and d.robot_id == robot for d in credentials) != 1 for robot in robots):
            raise ValueError('exactly one primary source is required')
        # Keys must have one owner, preventing camera/primary races.
        owners = {}
        for device in credentials:
            for key in device.media_keys:
                identity = (device.robot_id, key)
                if identity in owners:
                    raise ValueError('media keys must have exactly one authorized source')
                owners[identity] = device.source_id
        if not isinstance(self.fleet, dict) or set(self.fleet) - robots:
            raise ValueError('fleet metadata must reference configured robot identities')
        warehouse_ids = set()
        sockets = set()
        for robot, profile in self.fleet.items():
            if not isinstance(profile, dict) or set(profile) - {'name', 'warehouseId', 'nativeVideoSocket'}:
                raise ValueError('invalid robot profile')
            if 'name' in profile and (not isinstance(profile['name'], str) or not 1 <= len(profile['name']) <= 60
                    or any(ord(c) < 32 for c in profile['name'])):
                raise ValueError('invalid robot display name')
            if 'warehouseId' in profile:
                from uuid import UUID
                value = profile['warehouseId']
                if not isinstance(value, str) or str(UUID(value)) != value or value in warehouse_ids:
                    raise ValueError('warehouse robot IDs must be distinct canonical UUIDs')
                warehouse_ids.add(value)
            if profile.get('nativeVideoSocket'):
                from .native_video import socket_path
                value = socket_path(profile['nativeVideoSocket'])
                if value in sockets:
                    raise ValueError('robots must use distinct native video sockets')
                sockets.add(value)
        self.device_credentials = credentials
        return self

    def is_secure(self, request):
        if request.secure:
            return True
        try:
            address = ipaddress.ip_address(request.remote or '')
            if self.allow_insecure_local and address.is_loopback:
                return True
            return (request.headers.get('X-Forwarded-Proto') == 'https'
                    and any(address in ipaddress.ip_network(n.strip(), strict=False) for n in self.trusted_proxy_cidrs))
        except ValueError:
            return False

    def client_ip(self, request):
        """Only a configured reverse proxy may supply its verified client IP."""
        peer = request.remote or 'unknown'
        try:
            address = ipaddress.ip_address(peer)
            if any(address in ipaddress.ip_network(n.strip(), strict=False) for n in self.trusted_proxy_cidrs):
                forwarded = request.headers.get('X-Forwarded-For', '').split(',')[-1].strip()
                if forwarded:
                    return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
        return peer

    def device_for_token(self, token):
        found = None
        # Constant-time comparison even when token is unknown.
        for credential in self.device_credentials:
            if hmac.compare_digest(token.encode(), credential.token.encode()):
                found = credential
        return found


class Sessions:
    """Signed cookie plus bounded revocable in-memory sessions, invalid after restart."""
    def __init__(self, secret, ttl=28800, clock=time.time, maximum=256):
        self.secret, self.ttl, self.clock, self.maximum = secret.encode(), ttl, clock, maximum
        self.active = OrderedDict()
        self.usernames = {}

    def issue(self, username, account_id=None):
        now = self.clock()
        self._expire(now)
        nonce = secrets.token_urlsafe(24)
        identity = {'u': username, 'e': now + self.ttl, 'n': nonce}
        if account_id is not None:
            if type(account_id) is not int or account_id < 1:
                raise ValueError('invalid session account identity')
            identity['a'] = account_id
        payload = _b64(json.dumps(identity, separators=(',', ':')).encode())
        signature = _b64(hmac.digest(self.secret, payload.encode(), 'sha256'))
        self.active[nonce] = now + self.ttl
        self.usernames[nonce] = username
        while len(self.active) > self.maximum:
            removed, _ = self.active.popitem(last=False)
            self.usernames.pop(removed, None)
        return payload + '.' + signature

    def _expire(self, now):
        for nonce, expiry in list(self.active.items()):
            if expiry <= now:
                del self.active[nonce]
                self.usernames.pop(nonce, None)

    def read(self, cookie):
        try:
            if not isinstance(cookie, str) or len(cookie) > 1024:
                return None
            payload, signature = cookie.split('.')
            expected = _b64(hmac.digest(self.secret, payload.encode(), 'sha256'))
            if not hmac.compare_digest(signature, expected):
                return None
            value = json.loads(_unb64(payload))
            now = self.clock()
            self._expire(now)
            if value['e'] <= now or self.active.get(value['n']) != value['e']:
                return None
            return value
        except (ValueError, KeyError, TypeError):
            return None

    def revoke(self, cookie):
        value = self.read(cookie)
        if value:
            self.active.pop(value['n'], None)
            self.usernames.pop(value['n'], None)

    def revoke_user(self, username):
        """Return revoked nonces so existing media connections can be closed."""
        removed = [nonce for nonce, owner in self.usernames.items() if owner == username]
        for nonce in removed:
            self.active.pop(nonce, None)
            self.usernames.pop(nonce, None)
        return removed


class RateLimiter:
    def __init__(self, maximum=4096, clock=time.monotonic):
        self.buckets, self.maximum, self.clock = OrderedDict(), maximum, clock

    def allow(self, key, rate, burst):
        now = self.clock()
        tokens, last = self.buckets.pop(key, (burst, now))
        tokens = min(burst, tokens + max(0, now - last) * rate)
        allowed = tokens >= 1
        self.buckets[key] = (tokens - 1 if allowed else tokens, now)
        while len(self.buckets) > self.maximum:
            self.buckets.popitem(last=False)
        return allowed
