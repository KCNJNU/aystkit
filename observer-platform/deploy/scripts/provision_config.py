#!/usr/bin/env python3
"""Create one production configuration; never print or accept secret argv."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cloud.backend.security import Config, hash_password
from shared.protocol import IDENTIFIER


def validate_inputs(domain, robot_id, source_id, viewer_username):
    if (not isinstance(domain, str) or len(domain) > 253 or '.' not in domain
            or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                       for label in domain.split('.'))
            or domain.split('.')[-1].isdigit()):
        raise ValueError('invalid DNS domain')
    if not IDENTIFIER.fullmatch(robot_id) or not IDENTIFIER.fullmatch(source_id):
        raise ValueError('invalid robot/source identity')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', viewer_username):
        raise ValueError('invalid viewer username')
    return domain.lower()


def _directory_safe(info, *, private=False, temporary_ancestor=False):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
        raise ValueError('directory must be owned by this user or root')
    mode = stat.S_IMODE(info.st_mode)
    if private:
        if info.st_uid != os.geteuid() or mode != 0o700:
            raise ValueError('existing .runtime must be owned by this user with mode 0700')
    elif mode & 0o022:
        if not (temporary_ancestor and info.st_uid == 0 and mode & stat.S_ISVTX):
            raise ValueError('refusing a group/world-writable deployment path')


def _open_project(project_dir):
    path = Path(project_dir)
    if (not path.is_absolute() or '..' in path.parts or len(path.parts) < 2
            or any(ord(c) < 32 or ord(c) == 127 for c in str(path))):
        raise ValueError('project directory must be a safe absolute non-root path')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _directory_safe(os.fstat(descriptor))
        components = path.parts[1:]
        for index, component in enumerate(components):
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
            _directory_safe(os.fstat(descriptor), temporary_ancestor=index < len(components) - 1)
        return path, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_absent(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise FileExistsError('refusing to overwrite existing deployment target: ' + name)


def provision(project_dir, domain, robot_id, source_id, viewer_username='observer'):
    """Write private files exclusively, cleaning up only files this call created."""
    domain = validate_inputs(domain, robot_id, source_id, viewer_username)
    path, project_fd = _open_project(project_dir)
    runtime_fd = None
    created_runtime = False
    created_files = []
    try:
        _require_absent(project_fd, '.env')
        try:
            os.mkdir('.runtime', mode=0o700, dir_fd=project_fd)
            created_runtime = True
        except FileExistsError:
            pass
        runtime_fd = os.open('.runtime', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=project_fd)
        runtime_identity = (os.fstat(runtime_fd).st_dev, os.fstat(runtime_fd).st_ino)
        _directory_safe(os.fstat(runtime_fd), private=True)
        for name in ('initial-access.json', 'collector.env'):
            _require_absent(runtime_fd, name)

        password = secrets.token_urlsafe(24)
        token, session_secret = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        password_hash = hash_password(password)
        origin = 'https://' + domain
        devices = [{'token': token, 'robot_id': robot_id, 'source_id': source_id, 'role': 'primary'}]
        Config(robot_id, devices, viewer_username, password_hash, session_secret, origin,
               './data', trusted_proxy_cidrs=('127.0.0.1/32', '::1/128', '172.30.88.0/24')).validate()
        cloud = {
            'ROBOT_ID': robot_id,
            'DEVICE_CREDENTIALS_JSON': "'" + json.dumps(devices, separators=(',', ':')) + "'",
            'VIEWER_USERNAME': viewer_username, 'VIEWER_PASSWORD_HASH': "'" + password_hash + "'",
            'SESSION_SECRET': session_secret, 'PUBLIC_ORIGIN': origin,
            'ALLOW_INSECURE_LOCAL': 'false', 'DATA_DIR': './data', 'PORT': '8888',
            'HOST': '0.0.0.0', 'TRUSTED_PROXY_CIDRS': '127.0.0.1/32,::1/128,172.30.88.0/24',
            'CLOUD_SUBNET': '172.30.88.0/24', 'CLOUD_IMAGE_TAG': 'local', 'TLS_DIR': './tls',
        }
        collector = {'CLOUD_URL': origin, 'ROBOT_ID': robot_id, 'SOURCE_ID': source_id,
                     'DEVICE_TOKEN': token, 'COLLECTOR_ROLE': 'primary', 'ALLOW_INSECURE_LOCAL': 'false'}
        env_text = lambda values: ''.join(key + '=' + value + '\n' for key, value in values.items())
        contents = (
            (project_fd, '.env', env_text(cloud)),
            (runtime_fd, 'initial-access.json', json.dumps({'username': viewer_username, 'password': password}) + '\n'),
            (runtime_fd, 'collector.env', env_text(collector)),
        )
        for parent, name, content in contents:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            info = os.fstat(descriptor)
            created_files.append((parent, name, info.st_dev, info.st_ino))
            with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        return (path / '.env', path / '.runtime/initial-access.json', path / '.runtime/collector.env')
    except BaseException:
        for parent, name, device, inode in reversed(created_files):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == (device, inode):
                    os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass
        if created_runtime:
            try:
                info = os.stat('.runtime', dir_fd=project_fd, follow_symlinks=False)
                if runtime_fd is not None and (info.st_dev, info.st_ino) == runtime_identity:
                    os.rmdir('.runtime', dir_fd=project_fd)
            except OSError:
                pass
        raise
    finally:
        if runtime_fd is not None:
            os.close(runtime_fd)
        os.close(project_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, required=True)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--robot-id', required=True)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--viewer-username', default='observer')
    args = parser.parse_args(argv)
    try:
        paths = provision(args.project_dir, args.domain, args.robot_id, args.source_id, args.viewer_username)
    except (OSError, ValueError):
        parser.exit(1, 'Provisioning refused or failed; verify inputs, safe paths, permissions and existing targets. No secrets were printed.\n')
    print('Production configuration created; viewer username:', args.viewer_username)
    for path in paths:
        print(path)


if __name__ == '__main__':
    main()
