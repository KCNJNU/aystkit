"""Per-machine identity checks; never starts services or edits pending archives."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shlex
import stat
from uuid import UUID


def literal_environment(path):
    values = {}
    file = Path(path)
    if not file.exists():
        return values
    if file.is_symlink() or file.stat().st_size > 65536:
        raise ValueError('机器人环境配置无法安全读取')
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, raw = line.partition('=')
        if not sep:
            raise ValueError('机器人环境配置格式无效')
        parts = shlex.split(raw, comments=True)
        if len(parts) > 1:
            raise ValueError('机器人环境配置格式无效')
        values[key] = parts[0] if parts else ''
    return values


def validate_current_profile(*, values=None, home=None, environ=None, machine_id=None):
    """Existing installations without a profile remain compatible.

    Migrated installations explicitly bind a private profile to the host. A
    copied system disk therefore cannot silently upload as the original robot.
    """
    env = os.environ if environ is None else environ
    base = Path(home) if home is not None else Path.home()
    target = Path(env.get('ROBOT_PROFILE_FILE', str(base / '.config/robot-cloud/robot.json')))
    if not target.exists() and 'ROBOT_PROFILE_FILE' not in env:
        return None
    info = target.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > 16384 or target.is_symlink()):
        raise ValueError('机器人配置必须为当前用户的私有普通文件')
    profile = json.loads(target.read_text())
    if not isinstance(profile, dict) or not isinstance(profile.get('warehouseId'), str):
        raise ValueError('机器人身份配置无效')
    if (not isinstance(profile, dict) or profile.get('schemaVersion') != 1
            or not isinstance(profile.get('observerId'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', profile['observerId'])
            or str(UUID(profile.get('warehouseId', ''))) != profile['warehouseId']
            or type(profile.get('rosDomainId')) is not int or not 0 <= profile['rosDomainId'] <= 101):
        raise ValueError('机器人身份或 ROS 域配置无效')
    actual = machine_id if machine_id is not None else Path('/etc/machine-id').read_text().strip()
    if not profile.get('machineId') or profile['machineId'] != actual:
        raise ValueError('机器人配置与本机不匹配；迁移后请绑定新车身份，不要沿用旧车配置')
    config = base / '.config/robot-cloud'
    collector = literal_environment(config / 'collector.env')
    archive = literal_environment(config / 'archive.env')
    if values is not None:
        archive.update(values)
    observer_id = env.get('ROBOT_ID', collector.get('ROBOT_ID'))
    if observer_id != profile['observerId']:
        raise ValueError('观测身份与本车配置不一致，未启动采集或上传')
    archive_id = env.get('ARCHIVE_ROBOT_ID', archive.get('ARCHIVE_ROBOT_ID'))
    upload_path = archive.get('ARCHIVE_UPLOAD_CONFIG')
    if upload_path:
        upload = Path(upload_path)
        if upload.is_symlink() or upload.stat().st_mode & 0o077 or upload.stat().st_size > 16384:
            raise ValueError('上传配置必须为私有普通文件')
        upload_id = json.loads(upload.read_text()).get('robotId')
        if upload_id and upload_id != profile['warehouseId']:
            raise ValueError('上传配置属于其他机器人，旧队列保留，未上传')
        archive_id = archive_id or upload_id
    if archive_id != profile['warehouseId']:
        raise ValueError('归档身份与本车配置不一致，未启动采集或上传')
    domain = env.get('ROS_DOMAIN_ID', collector.get('ROS_DOMAIN_ID'))
    if domain is None or str(domain) != str(profile['rosDomainId']):
        raise ValueError('ROS 域与本车配置不一致，未启动采集或上传')
    tunnel = literal_environment(config / 'video-tunnel.env')
    if profile.get('jetsonHost'):
        host = tunnel.get('VIDEO_SOURCE_HOST')
        if host == 'auto': host = tunnel.get('VIDEO_SOURCE_NAME')
        if host != profile['jetsonHost'] or archive.get('ARCHIVE_SOURCE_HOST') != profile['jetsonHost']:
            raise ValueError('相机连接与本车 Jetson 配置不一致，未启动采集或上传')
    if profile.get('cloudSocketDir'):
        directory = profile['cloudSocketDir']
        if (not isinstance(directory, str) or not directory.endswith('/robots/' + profile['observerId'])
                or '..' in directory or tunnel.get('VIDEO_CLOUD_SOCKET_DIR') != directory):
            raise ValueError('云端视频目录未按本车隔离，未启动采集或上传')
    return profile
