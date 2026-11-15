"""Offline/restart upload contracts, without production access or credentials."""
from datetime import datetime, timedelta, timezone
from email.message import Message
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from collector.archive_upload import (ArchiveUploadClient, ArchiveUploadConfig, ArchiveUploadError,
    CHUNK_BYTES, LocalWarehouseApi, MAX_RESPONSE_BYTES)

TENANT, PROJECT, ROBOT = (str(uuid4()) for _ in range(3))
SESSION, CSRF = 's' * 43, 'c' * 43
PASSWORD = 'isolated-uploader-password-not-real'


class Reply:
    def __init__(self, data, status=200, cookies=(), raw=None):
        self.data = json.dumps(data).encode() if raw is None else raw
        self.status = status
        self.headers = Message()
        for cookie in cookies:
            self.headers.add_header('Set-Cookie', cookie)

    def read(self, limit):
        return self.data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class FakeApi:
    def __init__(self):
        self.calls, self.datasets, self.uploads, self.files, self.jobs = [], {}, {}, {}, {}
        self.folders = set()
        self.role = 'OPERATOR'
        self.csrf = CSRF
        self.disconnect_part_once = False
        self.conflict_on_create = False
        self.wrong_duplicate = False
        self.expire_auth_once = False
        self.csrf_once = False

    def user(self):
        return {'id': str(uuid4()), 'role': self.role, 'tenantId': TENANT,
                'projectIds': [PROJECT], 'mustChangePassword': False}

    def uploaded_file(self, upload):
        identity = str(uuid4())
        file = {key: upload[key] for key in ('datasetId', 'folderPath', 'robotId', 'originalName', 'checksum', 'fileSize')}
        file.update(id=identity, objectKey='fixture/' + identity)
        if self.wrong_duplicate:
            file['checksum'] = '0' * 64
        self.files[identity] = file
        upload.update(status='COMPLETED', robotFileId=identity)
        return file

    def finish(self):
        for upload in self.uploads.values():
            if upload['status'] == 'FINALIZING':
                actual = b''.join(upload['_bytes'][i] for i in sorted(upload['_bytes']))
                assert hashlib.sha256(actual).hexdigest() == upload['checksum']
                file = self.uploaded_file(upload)
                self.jobs[upload['_job']]['status'] = 'SUCCEEDED'
                self.jobs[upload['_job']]['result'] = {'robotFileId': file['id']}

    def public_upload(self, upload):
        return {key: value for key, value in upload.items() if not key.startswith('_')}

    def open(self, request, timeout):
        parsed = urlsplit(request.full_url)
        path = parsed.path.removeprefix('/api')
        method = request.get_method()
        headers = dict(request.header_items())
        body = request.data or b''
        if not isinstance(body, bytes):
            assert request.get_header('Content-length') == str(len(body))
            assert request.get_header('Transfer-encoding') is None
            body = b''.join(body)
        payload = json.loads(body) if body and request.get_header('Content-type') == 'application/json' else {}
        self.calls.append((method, path, headers, body))
        if path == '/v1/auth/login':
            assert payload['password'] == PASSWORD
            return Reply({'user': self.user(), 'csrfToken': self.csrf},
                cookies=(f'cr_session={SESSION}; HttpOnly; Path=/',))
        if self.expire_auth_once:
            self.expire_auth_once = False
            return Reply({'message': 'expired'}, 401)
        assert request.get_header('Cookie') == 'cr_session=' + SESSION
        if path == '/v1/auth/session':
            self.csrf = 'n' * 43
            return Reply({'user': self.user(), 'csrfToken': self.csrf})
        if method != 'GET':
            if self.csrf_once:
                self.csrf_once = False
                return Reply({'message': 'Invalid CSRF token'}, 403)
            assert request.get_header('X-csrf-token') == self.csrf
        if path == '/v1/auth/logout':
            return Reply({'success': True}, cookies=('cr_session=; Path=/',))
        if path == '/v1/datasets/page':
            return Reply({'items': list(self.datasets.values()), 'total': len(self.datasets)})
        if path == '/v1/datasets' and method == 'POST':
            identity = str(uuid4())
            self.datasets[identity] = {'id': identity, **payload}
            return Reply(self.datasets[identity], 201)
        if path.endswith('/folders'):
            if method == 'GET':
                return Reply([{'path': p} for p in self.folders])
            folder = '/'.join(filter(None, (payload['parentPath'], payload['name'])))
            if folder in self.folders:
                return Reply({'message': 'exists'}, 409)
            self.folders.add(folder)
            return Reply({'path': folder}, 201)
        if path == '/v1/files':
            query = parse_qs(parsed.query, keep_blank_values=True)
            matches = [file for file in self.files.values() if all(
                str(file.get(key)) == value[0] for key, value in query.items()
                if key in ('checksum', 'datasetId', 'folderPath'))]
            return Reply({'items': matches})
        if path == '/v1/uploads' and method == 'POST':
            identity = str(uuid4())
            relative = Path(payload['originalName'])
            upload = {'id': identity, **payload, 'originalName': relative.name,
                'folderPath': '/'.join((payload['folderPath'], *relative.parts[:-1])),
                'status': 'CREATED', 'chunkSize': CHUNK_BYTES,
                'totalParts': (payload['fileSize'] + CHUNK_BYTES - 1) // CHUNK_BYTES,
                'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                'parts': [], '_bytes': {}}
            self.uploads[identity] = upload
            if self.conflict_on_create:
                file = self.uploaded_file(upload)
                return Reply({'message': 'duplicate', 'robotFileId': file['id']}, 409)
            return Reply(self.public_upload(upload), 201)
        if path.startswith('/v1/uploads/'):
            identity = path.split('/')[3]
            if identity not in self.uploads:
                return Reply({'message': 'missing'}, 404)
            upload = self.uploads[identity]
            if method == 'GET':
                return Reply(self.public_upload(upload))
            if method == 'DELETE':
                upload['status'], upload['parts'], upload['_bytes'] = 'ABORTED', [], {}
                return Reply({}, 204)
            if '/parts/' in path:
                number = int(path.rsplit('/', 1)[1])
                checksum = hashlib.sha256(body).hexdigest()
                assert request.get_header('X-part-sha256') == checksum
                start = (number - 1) * CHUNK_BYTES
                assert request.get_header('Content-range') == f'bytes {start}-{start + len(body) - 1}/{upload["fileSize"]}'
                upload['_bytes'][number] = body
                part = {'partNumber': number, 'checksum': checksum, 'size': len(body)}
                upload['parts'] = [item for item in upload['parts'] if item['partNumber'] != number] + [part]
                upload['status'] = 'UPLOADING'
                if self.disconnect_part_once:
                    self.disconnect_part_once = False
                    raise URLError('PRIVATE_NETWORK_DETAIL')
                return Reply(part)
            if path.endswith('/complete'):
                job = str(uuid4())
                upload.update(status='FINALIZING', _job=job)
                self.jobs[job] = {'id': job, 'status': 'QUEUED'}
                return Reply(self.jobs[job], 202)
        if path.startswith('/v1/jobs/'):
            return Reply(self.jobs[path.rsplit('/', 1)[1]])
        if path.startswith('/v1/files/'):
            identity = path.split('/')[3]
            if path.endswith('/download'):
                return Reply({'url': 'http://127.0.0.1:9000/isolated-object'})
            return Reply(self.files[identity])
        raise AssertionError('Unexpected fixture path ' + path)


class ArchiveUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-archive-upload-')
        self.segment = Path(self.temp.name) / 'segment'
        (self.segment / 'bag').mkdir(parents=True)
        (self.segment / 'bag/metadata.yaml').write_text('rosbag2_bagfile_information: {}\n')
        (self.segment / 'bag/data_0.db3').write_bytes(b'ISOLATED_NATIVE_BYTES' * 70)
        self.manifest = {'schemaVersion': 1, 'id': 'isolated-segment', 'day': '2026-10-27',
            'sensor': '雷达', 'label': '雷达_14:50-15:50', 'startedAt': '2026-10-27T06:50:00Z',
            'endAt': '2026-10-27T07:50:00Z', 'robotId': ROBOT,
            'metadata': {'source': 'native-ros-cdr'}, 'files': ['bag/metadata.yaml', 'bag/data_0.db3']}
        self.write_manifest()
        self.api = FakeApi()
        self.config = ArchiveUploadConfig('archive@example.invalid', PASSWORD)
        self.client = ArchiveUploadClient(self.config, opener=self.api)

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self):
        (self.segment / 'archive.json').write_text(json.dumps(self.manifest, ensure_ascii=False))

    def complete(self):
        first = self.client.upload_segment(self.segment)
        self.assertEqual(first['state'], 'pending')
        self.api.finish()
        return self.client.upload_segment(self.segment)

    def test_sealed_native_bag_names_and_only_verified_complete_get_acknowledged(self):
        first = self.client.upload_segment(self.segment)
        self.assertEqual(first['state'], 'pending')
        self.assertFalse((self.segment / 'uploaded.json').exists())
        state = json.loads((self.segment / 'upload-state.json').read_text())
        self.assertTrue(all(item['uploadId'] and item['jobId'] for item in state['files'].values()))
        self.api.finish()
        ack = self.client.upload_segment(self.segment)
        self.assertEqual(ack['state'], 'complete')
        self.assertEqual(ack['folderPath'], '雷达/雷达_14:50-15:50/isolated-segment')
        self.assertEqual(next(iter(self.api.datasets.values()))['name'], '地库_2026-10-27')
        self.assertEqual({file['folderPath'] for file in self.api.files.values()}, {ack['folderPath'] + '/bag'})
        for file in ack['files']:
            local = self.segment / file['relativePath']
            self.assertTrue(local.exists())
            self.assertEqual(hashlib.sha256(local.read_bytes()).hexdigest(), file['sha256'])
        self.assertEqual({file['originalName'] for file in self.api.files.values()}, {'metadata.yaml', 'data_0.db3'})
        self.assertEqual((self.segment / 'uploaded.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(PASSWORD, (self.segment / 'upload-state.json').read_text())
        self.assertNotIn(SESSION, (self.segment / 'upload-state.json').read_text())

    def test_day_dataset_is_visible_before_first_segment_and_does_not_invent_files(self):
        client = ArchiveUploadClient(ArchiveUploadConfig('archive@example.invalid', PASSWORD, robot_id=ROBOT), opener=self.api)
        first = client.ensure_day('2026-10-27', ('雷达', '原始相机'))
        second = client.ensure_day('2026-10-27', ('雷达', '原始相机'))
        self.assertEqual(first, second)
        self.assertEqual(len(self.api.datasets), 1)
        self.assertEqual(len(self.api.folders), 2)
        self.assertFalse(self.api.files)
        self.assertFalse(self.api.uploads)

    def test_process_restart_resumes_server_parts_after_lost_part_acknowledgement(self):
        self.manifest['files'] = ['bag/data_0.db3']; self.write_manifest()
        (self.segment / 'bag/data_0.db3').write_bytes(b'\x01' * (CHUNK_BYTES + 19))
        self.api.disconnect_part_once = True
        with self.assertRaises(ArchiveUploadError) as failure:
            self.client.upload_segment(self.segment)
        self.assertEqual(failure.exception.code, 'ARCHIVE_API_UNAVAILABLE')
        state = json.loads((self.segment / 'upload-state.json').read_text())
        upload_id = state['files']['bag/data_0.db3']['uploadId']
        self.client = ArchiveUploadClient(self.config, opener=self.api)
        self.assertEqual(self.client.upload_segment(self.segment)['state'], 'pending')
        self.assertEqual(len(self.api.uploads), 1)
        parts = [path for method, path, _, _ in self.api.calls if method == 'PUT']
        self.assertEqual(parts.count(f'/v1/uploads/{upload_id}/parts/1'), 1)
        self.assertEqual(parts.count(f'/v1/uploads/{upload_id}/parts/2'), 1)
        self.api.finish()
        self.assertEqual(self.client.upload_segment(self.segment)['state'], 'complete')

    def test_expired_upload_rebuilds_only_owned_session_and_keeps_native_original(self):
        self.manifest['files'] = ['bag/data_0.db3']; self.write_manifest()
        self.api.disconnect_part_once = True
        with self.assertRaises(ArchiveUploadError):
            self.client.upload_segment(self.segment)
        old = next(iter(self.api.uploads.values()))
        old['expiresAt'] = '2020-01-01T00:00:00Z'
        self.assertEqual(self.client.upload_segment(self.segment)['state'], 'pending')
        self.assertEqual(old['status'], 'ABORTED')
        self.assertEqual(len(self.api.uploads), 2)
        self.api.finish()
        self.assertEqual(self.client.upload_segment(self.segment)['state'], 'complete')
        self.assertTrue((self.segment / 'bag/data_0.db3').is_file())

    def test_finalizing_is_polled_and_no_duplicate_complete_job_is_enqueued(self):
        self.assertEqual(self.client.upload_segment(self.segment)['state'], 'pending')
        jobs = len(self.api.jobs)
        for _ in range(2):
            self.assertEqual(self.client.upload_segment(self.segment)['state'], 'pending')
        self.assertEqual(len(self.api.jobs), jobs)
        self.api.finish()
        self.assertEqual(self.client.upload_segment(self.segment)['state'], 'complete')
        calls = len(self.api.calls)
        self.assertEqual(self.client.upload_segment(self.segment)['state'], 'complete')
        self.assertEqual(len(self.api.calls), calls)

    def test_checksum_conflict_only_acknowledges_matching_verified_server_file(self):
        self.api.conflict_on_create = True
        ack = self.client.upload_segment(self.segment)
        self.assertEqual(ack['state'], 'complete')
        self.assertEqual(len(ack['files']), 2)
        self.assertFalse(any(method == 'PUT' for method, *_ in self.api.calls))
        self.assertTrue((self.segment / 'bag/data_0.db3').exists())

    def test_bad_checksum_conflict_and_foreign_daily_dataset_fail_closed(self):
        self.api.conflict_on_create = self.api.wrong_duplicate = True
        with self.assertRaises(ArchiveUploadError) as failure:
            self.client.upload_segment(self.segment)
        self.assertEqual(failure.exception.code, 'ARCHIVE_REMOTE_FILE_MISMATCH')
        self.assertFalse((self.segment / 'uploaded.json').exists())
        self.assertTrue((self.segment / 'bag/data_0.db3').exists())

    def test_existing_daily_dataset_requires_matching_archive_metadata(self):
        identity = str(uuid4())
        self.api.datasets[identity] = {'id': identity, 'name': '地库_2026-10-27', 'projectId': PROJECT, 'metadata': {}}
        with self.assertRaises(ArchiveUploadError) as failure:
            self.client.upload_segment(self.segment)
        self.assertEqual(failure.exception.code, 'ARCHIVE_DATASET_OWNERSHIP_CONFLICT')
        self.assertEqual(self.api.uploads, {})

    def test_parent_path_and_symlink_files_are_rejected_before_any_network(self):
        for relative in ('../secret', '/tmp/secret', 'bag//data_0.db3', 'bag/../data_0.db3'):
            with self.subTest(relative=relative):
                self.manifest['files'] = [relative]; self.write_manifest()
                with self.assertRaises(ArchiveUploadError):
                    self.client.upload_segment(self.segment)
        (self.segment / 'bag/link.db3').symlink_to(self.segment / 'bag/data_0.db3')
        self.manifest['files'] = ['bag/link.db3']; self.write_manifest()
        with self.assertRaises(ArchiveUploadError):
            self.client.upload_segment(self.segment)
        self.assertEqual(self.api.calls, [])

    def test_administrator_credentials_are_not_used_for_unattended_business_calls(self):
        with self.assertRaises(ArchiveUploadError):
            ArchiveUploadClient(ArchiveUploadConfig('admin', PASSWORD))
        self.api.role = 'SUPER_ADMIN'
        with self.assertRaises(ArchiveUploadError) as failure:
            self.client.upload_segment(self.segment)
        self.assertEqual(failure.exception.code, 'ARCHIVE_SCOPED_OPERATOR_REQUIRED')
        self.assertEqual([path for _, path, _, _ in self.api.calls], ['/v1/auth/login', '/v1/auth/logout'])
        self.assertNotIn(PASSWORD, repr(self.config))

    def test_loopback_only_no_redirects_response_bounds_and_safe_error_text(self):
        for value in ('http://example.invalid/api', 'http://127.0.0.1:3000/not-api',
                      'http://user:private@127.0.0.1/api', 'http://localhost:3000/api'):
            with self.subTest(value=value), self.assertRaises(ArchiveUploadError):
                LocalWarehouseApi(value)
        class Responses:
            def open(self, *_args, **_kwargs):
                return Reply({}, 302)
        with self.assertRaises(ArchiveUploadError) as failure:
            LocalWarehouseApi(opener=Responses()).request('/health')
        self.assertEqual(failure.exception.status, 302)
        class Large:
            def open(self, *_args, **_kwargs):
                return Reply({}, raw=b'x' * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(ArchiveUploadError) as failure:
            LocalWarehouseApi(opener=Large()).request('/health')
        self.assertEqual(failure.exception.code, 'ARCHIVE_API_RESPONSE_INVALID')

    def test_csrf_refresh_and_expired_login_reauthenticate_without_shared_browser_session(self):
        self.api.csrf_once = True
        self.assertEqual(self.client.upload_segment(self.segment)['state'], 'pending')
        self.assertEqual(sum(path == '/v1/auth/session' for _, path, _, _ in self.api.calls), 1)
        self.api.finish()
        self.api.expire_auth_once = True
        self.assertEqual(self.client.upload_segment(self.segment)['state'], 'complete')
        self.assertGreaterEqual(sum(path == '/v1/auth/login' for _, path, _, _ in self.api.calls), 2)

    def test_manifest_or_already_started_file_changes_do_not_get_false_ack(self):
        self.client.upload_segment(self.segment)
        (self.segment / 'bag/data_0.db3').write_bytes(b'CHANGED_NATIVE_FILE')
        with self.assertRaises(ArchiveUploadError) as failure:
            self.client.upload_segment(self.segment)
        self.assertEqual(failure.exception.code, 'ARCHIVE_FILE_CHANGED')
        self.assertFalse((self.segment / 'uploaded.json').exists())

    def test_private_configuration_file_is_required(self):
        path = Path(self.temp.name) / 'uploader.json'
        path.write_text(json.dumps({'email': self.config.email, 'password': PASSWORD}))
        path.chmod(0o644)
        with self.assertRaises(ArchiveUploadError):
            ArchiveUploadConfig.from_environment(path)
        path.chmod(0o600)
        self.assertEqual(ArchiveUploadConfig.from_environment(path).email, self.config.email)


class ProvisionFixture:
    def __init__(self, scoped_robot=True):
        self.sessions = {}
        self.created = None
        self.scoped_robot = scoped_robot
        self.removed = []

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path.removeprefix('/api')
        method = request.get_method()
        payload = json.loads(request.data) if request.data else {}
        if path == '/v1/auth/login':
            administrator = payload['email'] == 'bootstrap@example.invalid'
            if administrator:
                assert payload['password'] == 'isolated-bootstrap-password'
                user = {'id': str(uuid4()), 'role': 'SUPER_ADMIN', 'tenantId': None, 'projectIds': []}
            else:
                assert self.created and payload['email'] == self.created['email']
                assert payload['password'] == self.created['_password']
                user = {key: value for key, value in self.created.items() if not key.startswith('_')}
            token = uuid4().hex + uuid4().hex[:11]
            self.sessions[token] = user
            return Reply({'user': user, 'csrfToken': CSRF}, cookies=(f'cr_session={token}; Path=/',))
        cookie = request.get_header('Cookie', '').removeprefix('cr_session=')
        assert cookie in self.sessions
        user = self.sessions[cookie]
        if method != 'GET':
            assert request.get_header('X-csrf-token') == CSRF
        if path == '/v1/users/scope-options':
            assert user['role'] == 'SUPER_ADMIN'
            return Reply({'projects': [{'id': PROJECT, 'tenantId': TENANT}]})
        if path == '/warehouse/sources':
            return Reply([{'id': ROBOT}] if user['role'] == 'SUPER_ADMIN' or self.scoped_robot else [])
        if path == '/v1/users' and method == 'POST':
            assert user['role'] == 'SUPER_ADMIN'
            assert payload['role'] == 'OPERATOR' and payload['projectIds'] == [PROJECT]
            self.created = {'id': str(uuid4()), 'email': payload['email'], 'role': 'OPERATOR',
                'tenantId': payload['tenantId'], 'projectIds': payload['projectIds'],
                'mustChangePassword': True, '_password': payload['temporaryPassword']}
            return Reply({key: value for key, value in self.created.items() if not key.startswith('_')}, 201)
        if path == '/v1/auth/change-password':
            assert user['role'] == 'OPERATOR'
            assert payload['currentPassword'] == self.created['_password']
            self.created['_password'] = payload['newPassword']
            self.created['mustChangePassword'] = False
            self.sessions.pop(cookie)
            return Reply({'success': True}, cookies=('cr_session=; Path=/',))
        if path.startswith('/v1/users/') and method == 'DELETE':
            assert user['role'] == 'SUPER_ADMIN'
            self.removed.append(path.rsplit('/', 1)[1])
            return Reply({}, 204)
        if path == '/v1/auth/logout':
            self.sessions.pop(cookie)
            return Reply({'success': True}, cookies=('cr_session=; Path=/',))
        raise AssertionError('Unexpected provision fixture path')


class ArchiveProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-archive-provision-')
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('isolated_archive_provision',
            root / 'deploy/scripts/provision_archive_uploader.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.env = Path(self.temp.name) / '.env'
        self.env.write_text('BOOTSTRAP_ADMIN_EMAIL=bootstrap@example.invalid\n'
                            'BOOTSTRAP_ADMIN_PASSWORD=isolated-bootstrap-password\n')
        self.destination = Path(self.temp.name) / 'private/uploader.json'
        self.args = SimpleNamespace(tenant_id=TENANT, project_id=PROJECT, robot_id=ROBOT,
            config=str(self.destination), env_file=str(self.env), api_url='http://127.0.0.1:3000/api',
            email=None, dataset_prefix='地库_')

    def tearDown(self):
        self.temp.cleanup()

    def test_only_scoped_operator_secret_is_written_and_bootstrap_session_is_closed(self):
        fixture = ProvisionFixture()
        result = self.module.provision(self.args, opener=fixture)
        self.assertTrue(result['configured'])
        data = json.loads(self.destination.read_text())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(data['projectId'], PROJECT)
        self.assertEqual(data['robotId'], ROBOT)
        self.assertNotEqual(data['password'], 'isolated-bootstrap-password')
        self.assertNotEqual(data['email'], 'bootstrap@example.invalid')
        self.assertEqual(fixture.created['role'], 'OPERATOR')
        self.assertFalse(fixture.created['mustChangePassword'])
        self.assertEqual(fixture.sessions, {})
        self.assertNotIn(data['password'], json.dumps(result))

    def test_robot_outside_operator_scope_does_not_install_or_keep_new_account(self):
        fixture = ProvisionFixture(scoped_robot=False)
        with self.assertRaises(ArchiveUploadError) as error:
            self.module.provision(self.args, opener=fixture)
        self.assertEqual(error.exception.code, 'ARCHIVE_SOURCE_ROBOT_SCOPE_INVALID')
        self.assertFalse(self.destination.exists())
        self.assertEqual(fixture.removed, [fixture.created['id']])
        self.assertEqual(fixture.sessions, {})
