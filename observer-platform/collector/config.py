"""Explicit collector configuration; production credentials only use HTTPS."""
from __future__ import annotations

import ipaddress
import math
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from shared.protocol import HTTP_CAMERA_KEYS

HTTP_CAMERA_ORIGIN = 'http://127.0.0.1:18080'
HTTP_CAMERA_PROFILES = {
    'legacy3': ('usbFront', 'usbRear', 'usbLeft'),
    'jetson-hub4': ('usbFront', 'usbLeft', 'usbHub2', 'usbHub3'),
    'jetson-direct3': ('usbFront', 'usbLeft', 'usbPort21'),
}


def ingest_url(value: str, allow_insecure_local: bool = False) -> str:
    parts = urlsplit(value)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('CLOUD_URL must be an origin without credentials, query or fragment')
    if parts.path not in ('', '/', '/api/v1/ingest'):
        raise ValueError('CLOUD_URL must be an origin or /api/v1/ingest URL')
    local = parts.hostname == 'localhost'
    try:
        local = local or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if parts.scheme != 'https' and not (parts.scheme == 'http' and allow_insecure_local and local):
        raise ValueError('HTTPS is required; explicit test HTTP is limited to loopback')
    return value.rstrip('/') if parts.path == '/api/v1/ingest' else value.rstrip('/') + '/api/v1/ingest'


@dataclass(frozen=True)
class Config:
    robot_id: str
    source_id: str
    role: str = 'primary'
    cloud_url: str = ''
    token: str = ''
    image_fps: float = 1.0
    cloud_fps: float = 1.0
    cloud_points: int = 1500
    map_resend_s: float = 30.0
    allow_insecure_local: bool = False
    media_keys: tuple[str, ...] = ('fisheye', 'color', 'depth', 'lidar', 'registered', 'depthPoints', 'map', 'localCostmap', 'globalCostmap', 'bluesea', 'scan')
    chassis_source: str = 'ros2'
    http_camera_url: str = ''
    http_camera_profile: str = 'legacy3'
    http_camera_fps: float | None = None
    berxel_transport: str = 'raw'

    def __post_init__(self):
        if self.berxel_transport not in ('raw', 'bridge'):
            raise ValueError('COLLECTOR_BERXEL_TRANSPORT must be raw or bridge')
        if self.http_camera_fps is not None:
            if (isinstance(self.http_camera_fps, bool)
                    or not isinstance(self.http_camera_fps, (int, float))
                    or not math.isfinite(self.http_camera_fps)
                    or not 0.1 <= self.http_camera_fps <= 2):
                raise ValueError('COLLECTOR_HTTP_CAMERA_FPS must be 0.1..2')
            if not self.http_camera_url:
                raise ValueError('HTTP camera FPS requires an explicitly enabled HTTP receiver')
        if self.chassis_source not in ('ros2', 'local_ws'):
            raise ValueError('COLLECTOR_CHASSIS_SOURCE must be ros2 or local_ws')
        if self.chassis_source == 'local_ws' and self.role != 'primary':
            raise ValueError('Only a primary collector can use local_ws chassis feedback')
        if self.http_camera_url and self.http_camera_url != HTTP_CAMERA_ORIGIN:
            raise ValueError('HTTP camera input must use the fixed loopback SSH tunnel')
        if self.http_camera_profile not in HTTP_CAMERA_PROFILES:
            raise ValueError('COLLECTOR_HTTP_CAMERA_PROFILE must be legacy3, jetson-hub4 or jetson-direct3')
        if self.http_camera_profile != 'legacy3' and (not self.http_camera_url or self.role != 'primary'):
            raise ValueError('HTTP camera profile requires an explicitly enabled primary HTTP receiver')
        http_keys = HTTP_CAMERA_KEYS.intersection(self.media_keys)
        if not http_keys.issubset(HTTP_CAMERA_PROFILES[self.http_camera_profile]):
            raise ValueError('HTTP camera keys do not belong to the selected profile')
        if http_keys and (not self.http_camera_url or self.role != 'primary'):
            raise ValueError('HTTP camera keys require an explicitly enabled primary HTTP receiver')
        if self.http_camera_url and (self.role != 'primary' or not http_keys):
            raise ValueError('HTTP camera receiver requires primary ownership of HTTP camera keys')

    @classmethod
    def from_env(cls, audit: bool = False) -> 'Config':
        from .robot_profile import validate_current_profile
        validate_current_profile()
        role = os.environ.get('COLLECTOR_ROLE', 'primary')
        if role not in ('primary', 'cameras'):
            raise ValueError('COLLECTOR_ROLE must be primary or cameras')
        image_fps = float(os.environ.get('COLLECTOR_IMAGE_FPS', '1'))
        cloud_fps = float(os.environ.get('COLLECTOR_CLOUD_FPS', '1'))
        points = int(os.environ.get('COLLECTOR_CLOUD_POINTS', '1500'))
        if not 0.1 <= image_fps <= 5 or not 0.1 <= cloud_fps <= 2 or not 1 <= points <= 4000:
            raise ValueError('Image FPS 0.1..5, cloud FPS 0.1..2, points 1..4000 required')
        http_url = os.environ.get('COLLECTOR_HTTP_CAMERA_URL', '')
        http_profile = os.environ.get('COLLECTOR_HTTP_CAMERA_PROFILE', 'legacy3')
        http_fps = os.environ.get('COLLECTOR_HTTP_CAMERA_FPS')
        if http_profile not in HTTP_CAMERA_PROFILES:
            raise ValueError('COLLECTOR_HTTP_CAMERA_PROFILE must be legacy3, jetson-hub4 or jetson-direct3')
        allowed = (('fisheye', 'color', 'depth', 'depthPoints') if role == 'cameras'
                   else cls.media_keys + tuple(sorted(HTTP_CAMERA_PROFILES[http_profile])))
        defaults = allowed if role == 'cameras' or http_url else cls.media_keys
        configured = os.environ.get('COLLECTOR_MEDIA_KEYS')
        keys = tuple(item.strip() for item in configured.split(',') if item.strip()) if configured is not None else defaults
        if len(set(keys)) != len(keys) or any(key not in allowed for key in keys):
            raise ValueError('COLLECTOR_MEDIA_KEYS contains duplicate or unauthorized key')
        token = os.environ.get('DEVICE_TOKEN', '')
        url = os.environ.get('CLOUD_URL', '')
        if not audit:
            url = ingest_url(url, os.environ.get('ALLOW_INSECURE_LOCAL', '').lower() == 'true')
            if len(token) < 32 or any(ch.isspace() for ch in token):
                raise ValueError('DEVICE_TOKEN must contain at least 32 non-whitespace characters')
            if not os.environ.get('ROBOT_ID') or not os.environ.get('SOURCE_ID'):
                raise ValueError('ROBOT_ID and SOURCE_ID are required')
        return cls(os.environ.get('ROBOT_ID', 'audit-robot'), os.environ.get('SOURCE_ID', 'audit-rk3588'), role,
                   url, token, image_fps, cloud_fps, points, media_keys=keys,
                   chassis_source=os.environ.get('COLLECTOR_CHASSIS_SOURCE', 'ros2'),
                   http_camera_url=http_url,
                   http_camera_profile=http_profile,
                   http_camera_fps=None if http_fps is None else float(http_fps),
                   berxel_transport=os.environ.get('COLLECTOR_BERXEL_TRANSPORT', 'raw'),
                   allow_insecure_local=os.environ.get('ALLOW_INSECURE_LOCAL', '').lower() == 'true')
