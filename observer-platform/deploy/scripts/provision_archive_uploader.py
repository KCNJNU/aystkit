#!/usr/bin/env python3
"""One-time scoped operator creation. Does not provision tenant/project tables.

The operator must select a previously provisioned dedicated archive project and
source Robot. The bootstrap administrator is used only by this explicit setup
command; its credentials are never written into the uploader configuration.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from collector.archive_upload import (ArchiveUploadClient, ArchiveUploadConfig,
    ArchiveUploadError, LocalWarehouseApi)


def bootstrap_credentials(path):
    values = {}
    with Path(path).open(encoding='utf-8-sig') as source:
        for line in source:
            key, separator, value = line.strip().partition('=')
            if separator and key in ('BOOTSTRAP_ADMIN_EMAIL', 'BOOTSTRAP_ADMIN_PASSWORD'):
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('\"', "'"):
                    value = value[1:-1]
                values[key] = value
    if not all(values.get(key) for key in ('BOOTSTRAP_ADMIN_EMAIL', 'BOOTSTRAP_ADMIN_PASSWORD')):
        raise ArchiveUploadError('ARCHIVE_BOOTSTRAP_CREDENTIALS_MISSING')
    return values['BOOTSTRAP_ADMIN_EMAIL'], values['BOOTSTRAP_ADMIN_PASSWORD']


def provision(args, *, opener=None):
    tenant_id, project_id, robot_id = (str(UUID(value)) for value in
        (args.tenant_id, args.project_id, args.robot_id))
    destination = Path(args.config).expanduser()
    if destination.exists() or destination.is_symlink():
        raise ArchiveUploadError('ARCHIVE_CONFIG_ALREADY_EXISTS')
    email, password = bootstrap_credentials(args.env_file)
    admin = LocalWarehouseApi(args.api_url, opener=opener)
    created_id = None
    installed = False
    try:
        administrator = admin.login(email, password)
        if administrator.get('role') not in ('ADMIN', 'SUPER_ADMIN'):
            raise ArchiveUploadError('ARCHIVE_PROVISION_ADMIN_REQUIRED')
        scope = admin.request('/v1/users/scope-options')
        if not any(item.get('id') == project_id and item.get('tenantId') == tenant_id
                   for item in scope.get('projects', [])):
            raise ArchiveUploadError('ARCHIVE_DEDICATED_PROJECT_REQUIRED')
        sources = admin.request('/warehouse/sources')
        if not any(item.get('id') == robot_id for item in sources):
            raise ArchiveUploadError('ARCHIVE_SOURCE_ROBOT_REQUIRED')
        operator_email = args.email or ('robot.archive.' + uuid4().hex + '@local.invalid')
        temporary = secrets.token_urlsafe(36)
        final_password = secrets.token_urlsafe(48)
        user = admin.request('/v1/users', 'POST', {'email': operator_email,
            'displayName': '地库原始数据归档服务', 'role': 'OPERATOR', 'tenantId': tenant_id,
            'projectIds': [project_id], 'temporaryPassword': temporary})
        created_id = user['id']
        first_login = LocalWarehouseApi(args.api_url, opener=opener)
        try:
            initial = first_login.login(operator_email, temporary)
            if initial.get('role') != 'OPERATOR' or initial.get('projectIds') != [project_id]:
                raise ArchiveUploadError('ARCHIVE_OPERATOR_ASSIGNMENT_INVALID')
            first_login.request('/v1/auth/change-password', 'POST', {
                'currentPassword': temporary, 'newPassword': final_password})
        finally:
            if first_login.token:
                first_login.request('/v1/auth/logout', 'POST')
        uploader = ArchiveUploadClient(ArchiveUploadConfig(operator_email, final_password,
            args.api_url, args.dataset_prefix, robot_id), opener=opener)
        try:
            uploader.login()
            if uploader.user.get('tenantId') != tenant_id or uploader.user['projectIds'] != [project_id]:
                raise ArchiveUploadError('ARCHIVE_OPERATOR_ASSIGNMENT_INVALID')
            # The non-administrator query applies the actual project scope.
            # The public source DTO does not include projectId, so checking the
            # administrator's unscoped source list alone cannot prove ownership.
            scoped_sources = uploader._request('/warehouse/sources')
            if not any(item.get('id') == robot_id for item in scoped_sources):
                raise ArchiveUploadError('ARCHIVE_SOURCE_ROBOT_SCOPE_INVALID')
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                json.dump({'apiUrl': args.api_url, 'email': operator_email, 'password': final_password,
                    'tenantId': tenant_id, 'projectId': project_id, 'robotId': robot_id,
                    'datasetPrefix': args.dataset_prefix}, target, ensure_ascii=False, indent=2)
                target.write('\n'); target.flush(); os.fsync(target.fileno())
            installed = True
        finally:
            if uploader.api.token:
                uploader.api.request('/v1/auth/logout', 'POST')
        return {'configured': True, 'configPath': str(destination)}
    finally:
        if created_id and not installed:
            try:
                admin.request('/v1/users/' + str(UUID(created_id)), 'DELETE')
            except ArchiveUploadError:
                pass
        if admin.token:
            try:
                admin.request('/v1/auth/logout', 'POST')
            except ArchiveUploadError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default='/home/user/DBProg/RobotHygienism/.env')
    parser.add_argument('--api-url', default='http://127.0.0.1:3000/api')
    parser.add_argument('--tenant-id', required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--robot-id', required=True)
    parser.add_argument('--email')
    parser.add_argument('--dataset-prefix', default='地库_')
    parser.add_argument('--config', default=str(Path.home() / '.config/robot-cloud/archive-uploader.json'))
    args = parser.parse_args()
    try:
        print(json.dumps(provision(args)))
    except (ArchiveUploadError, ValueError, OSError) as error:
        print(json.dumps({'configured': False, 'error': error.code if isinstance(error, ArchiveUploadError)
                          else 'ARCHIVE_PROVISION_FAILED'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
