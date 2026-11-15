"""Foreground uploading must never turn an upload request into live recording."""
import fcntl
import base64
from contextlib import redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from collector import archive_upload_only as foreground
from collector.archive_upload import ArchiveUploadClient, ArchiveUploadConfig, ArchiveUploadError
from tests.test_archive_upload import FakeApi, PASSWORD, ROBOT


def source_descriptor_fixture(relative, manifest):
    """Use the actual SSH snapshot contract rather than a pathname-only stub."""
    day, sensor, label, identity = relative.split('/')
    value = {**manifest, 'day': day, 'sensor': sensor, 'label': label, 'id': identity,
             'metadata': {**manifest.get('metadata', {}), 'scope': 'garage'}}
    raw = json.dumps(value).encode()
    return dict(path=relative, manifest=base64.b64encode(raw).decode(),
                sha256=hashlib.sha256(raw).hexdigest()), value


class ForegroundUploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='isolated-upload-only-')
        self.root = Path(self.temporary.name)
        self.segment = self.root / 'days/2026-10-27/雷达/segment'
        self.segment.mkdir(parents=True)
        (self.segment / 'record.db3.zst').write_bytes(b'fixture-compressed-data')
        self.manifest = self.segment / 'archive.json'
        self.manifest.write_text(json.dumps(dict(schemaVersion=1, id='segment',
            day='2026-10-27', sensor='雷达', label='雷达_145000-155000', robotId=ROBOT,
            startedAt='2026-10-27T06:50:00Z', endAt='2026-10-27T07:50:00Z',
            metadata={}, files=['record.db3.zst'])))
        self.values = {'ARCHIVE_DBPROG_EMAIL': 'archive@example.invalid',
                       'ARCHIVE_DBPROG_PASSWORD': PASSWORD, 'ARCHIVE_ROBOT_ID': ROBOT,
                       'ARCHIVE_CONTROL_DIR':str(self.root/'control')}
        self.api = FakeApi()

    def tearDown(self):
        self.temporary.cleanup()

    def client(self, config):
        return ArchiveUploadClient(config, opener=self.api)

    def test_dry_run_reads_sealed_snapshot_without_credentials_network_or_writes(self):
        (self.root / '.staging/open').mkdir(parents=True)
        (self.root / '.staging/open/frames.mjpg').write_bytes(b'live-data')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'archive.json').write_text('{}')
        (self.root / 'days/linked').symlink_to(outside, target_is_directory=True)
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        with patch.object(foreground, 'upload_config', side_effect=AssertionError('credentials')):
            report, result = foreground.run_upload(self.root, {}, dry_run=True,
                client_factory=lambda _: self.fail('network client created'))
        self.assertEqual(result, 0)
        self.assertEqual(report['state'], 'dry_run')
        self.assertEqual(report['pendingSegments'], 1)
        self.assertEqual(report['remoteSync'], 'not_checked')
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')))

    def test_waits_for_warehouse_confirmation_and_never_removes_local_bytes(self):
        with patch.object(foreground.time, 'sleep', side_effect=lambda _: self.api.finish()):
            report, result = foreground.run_upload(self.root, self.values,
                timeout=2, client_factory=self.client)
        self.assertEqual((result, report['state']), (0, 'complete'))
        self.assertEqual((report['completedSegments'], report['pendingSegments']), (1, 0))
        self.assertTrue((self.segment / 'uploaded.json').is_file())
        self.assertEqual((self.segment / 'record.db3.zst').read_bytes(), b'fixture-compressed-data')
        self.assertNotIn(PASSWORD, json.dumps(report))

    def test_finalizing_timeout_returns_incomplete_and_later_resumes(self):
        started = time.monotonic()
        report, result = foreground.run_upload(self.root, self.values,
            timeout=.04, client_factory=self.client)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(result, 3)
        self.assertEqual(report['state'], 'incomplete')
        self.assertEqual(report['pendingSegments'], 1)
        self.assertEqual(report['lastError'], 'ARCHIVE_UPLOAD_TIME_BUDGET_EXPIRED')
        self.assertFalse((self.segment / 'uploaded.json').exists())
        self.assertTrue((self.segment / 'upload-state.json').exists())
        self.api.finish()
        with patch.object(foreground.time, 'sleep', side_effect=lambda _: self.api.finish()):
            resumed, result = foreground.run_upload(self.root, self.values,
                timeout=2, client_factory=self.client)
        self.assertEqual((result, resumed['state']), (0, 'complete'))
        self.assertEqual(len(self.api.uploads), 1)

    def test_per_segment_lock_prevents_a_second_client_from_touching_state(self):
        with (self.segment / '.upload.lock').open('a') as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.client(ArchiveUploadConfig('archive@example.invalid', PASSWORD)).upload_segment(self.segment)
        self.assertEqual(result, {'state': 'pending', 'reason': 'ARCHIVE_SEGMENT_UPLOAD_BUSY'})
        self.assertFalse(self.api.calls)
        self.assertFalse((self.segment / 'upload-state.json').exists())

    def test_a_second_foreground_job_exits_incomplete_without_api_calls(self):
        with foreground.upload_lock(self.root):
            report, result = foreground.run_upload(self.root, self.values,
                client_factory=lambda _: self.fail('created concurrent API client'))
        self.assertEqual(result, 3)
        self.assertEqual(report['lastError'], 'ARCHIVE_FOREGROUND_UPLOAD_BUSY')

    def test_timer_interrupts_a_single_stuck_http_request(self):
        class SlowApi(FakeApi):
            def open(self, *_args, **_kwargs):
                time.sleep(5)
                raise AssertionError('deadline did not interrupt HTTP')
        started = time.monotonic()
        report, result = foreground.run_upload(self.root, self.values, timeout=.03,
            client_factory=lambda config: ArchiveUploadClient(config, opener=SlowApi()))
        self.assertEqual(result, 3)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(report['lastError'], 'ARCHIVE_UPLOAD_TIME_BUDGET_EXPIRED')
        # The interrupted uploader released its per-segment lock.
        self.assertEqual(self.client(ArchiveUploadConfig('archive@example.invalid', PASSWORD)).upload_segment(
            self.segment)['state'], 'pending')

    def test_literal_environment_is_never_executed_or_expanded(self):
        marker = self.root / 'shell-executed'
        path = self.root / 'archive.env'
        path.write_text("ARCHIVE_ROOT='/data/with spaces'\n" +
            "ARCHIVE_DATASET_PREFIX='$(touch " + str(marker) + ")'\n")
        values = foreground.load_environment(path, {'ARCHIVE_ROOT': '/override'})
        self.assertEqual(values['ARCHIVE_ROOT'], '/override')
        self.assertEqual(values['ARCHIVE_DATASET_PREFIX'], '$(touch ' + str(marker) + ')')
        self.assertFalse(marker.exists())

    def test_forged_or_changed_ack_is_not_reported_complete(self):
        (self.segment / 'uploaded.json').write_text(json.dumps(dict(state='complete', manifestSha256='0' * 64)))
        report, result = foreground.run_upload(self.root, {}, dry_run=True)
        self.assertEqual(result, 3)
        self.assertEqual(report['lastError'], 'ARCHIVE_MANIFEST_CHANGED')

    def test_unsealed_video_or_bag_never_claims_upload_complete(self):
        for marker in ('video-pending.json', 'bag-pending.json'):
            with self.subTest(marker=marker):
                pending = self.segment / marker
                self.manifest.rename(pending)
                try:
                    report, result = foreground.run_upload(self.root, {},
                        client_factory=lambda _: self.fail('no sealed files to upload'))
                    self.assertEqual((result, report['state']), (3, 'incomplete'))
                    self.assertEqual(report['preparingSegments'], 1)
                    self.assertEqual(report['lastError'], 'ARCHIVE_LOCAL_SEALING_PENDING')
                    self.assertFalse((self.root / '.upload-only.lock').exists())
                finally:
                    pending.rename(self.manifest)

    def test_active_recording_and_incoming_transfer_are_reported_without_mutation(self):
        self.manifest.unlink()
        for name in ('.active', '.camera-active', '.incoming', '.camera-recovering'):
            (self.root / name / 'open-segment').mkdir(parents=True)
        (self.root / 'recovery/retained-original').mkdir(parents=True)
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        report, result = foreground.run_upload(self.root, {})
        self.assertEqual((result, report['state']), (3, 'incomplete'))
        self.assertEqual(report['activeSegments'], 2)
        self.assertEqual(report['stagingSegments'], 2)
        self.assertEqual(report['recoverySegments'], 1)
        self.assertEqual(report['remoteSync'], 'not_checked')
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')))

    def test_completed_derivative_does_not_count_old_pending_marker(self):
        (self.segment / 'video-pending.json').write_text('{}')
        (self.segment / 'bag-pending.json').write_text('{}')
        with patch.object(foreground.time, 'sleep', side_effect=lambda _: self.api.finish()):
            report, result = foreground.run_upload(self.root, self.values,
                timeout=2, client_factory=self.client)
        self.assertEqual((result, report['state'], report['preparingSegments']), (0, 'complete', 0))

    def test_bad_local_ack_does_not_block_uploading_another_segment(self):
        healthy = self.segment.with_name('healthy')
        shutil.copytree(self.segment, healthy)
        (self.segment / 'uploaded.json').write_text('{broken json')
        calls = []
        class CompleteClient:
            def upload_segment(self, segment):
                calls.append(segment)
                return {'state': 'complete'}
        report, result = foreground.run_upload(self.root, self.values,
            client_factory=lambda _: CompleteClient())
        self.assertEqual(calls, [healthy])
        self.assertEqual((result, report['state']), (3, 'incomplete'))
        self.assertEqual((report['completedSegments'], report['pendingSegments']), (1, 1))
        self.assertEqual(report['invalidSegments'], 1)
        self.assertEqual(report['lastError'], 'ARCHIVE_STATE_INVALID')
        self.assertEqual((self.segment / 'record.db3.zst').read_bytes(), b'fixture-compressed-data')

    def test_upload_error_does_not_block_a_later_healthy_segment(self):
        healthy = self.segment.with_name('z-healthy')
        shutil.copytree(self.segment, healthy)
        calls = []
        class MixedClient:
            def upload_segment(inner, segment):
                calls.append(segment)
                if segment == self.segment:
                    raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID')
                return {'state': 'complete'}
        report, result = foreground.run_upload(self.root, self.values,
            client_factory=lambda _: MixedClient())
        self.assertEqual(calls, [self.segment, healthy])
        self.assertEqual((result, report['completedSegments'], report['pendingSegments']), (3, 1, 1))
        self.assertEqual(report['lastError'], 'ARCHIVE_MANIFEST_INVALID')

    def test_malformed_or_deeply_nested_manifest_does_not_block_good_files(self):
        healthy = self.segment.with_name('healthy')
        shutil.copytree(self.segment, healthy)
        for malformed in ('{broken', '[]', '{"nested":' * 1500 + '0' + '}' * 1500):
            with self.subTest(malformed=malformed[:20]):
                self.manifest.write_text(malformed)
                calls = []
                class CompleteClient:
                    def upload_segment(self, segment):
                        calls.append(segment)
                        return {'state': 'complete'}
                report, result = foreground.run_upload(self.root, self.values,
                    client_factory=lambda _: CompleteClient())
                self.assertEqual(calls, [healthy])
                self.assertEqual((result, report['invalidSegments'], report['completedSegments']), (3, 1, 1))
                self.assertEqual(report['lastError'], 'ARCHIVE_MANIFEST_INVALID')

    def test_timeout_covers_inventory_and_ack_hashing(self):
        for function in ('sealed_manifests', 'acknowledged'):
            with self.subTest(function=function):
                started = time.monotonic()
                with patch.object(foreground, function, side_effect=lambda *_: time.sleep(5)):
                    report, result = foreground.run_upload(self.root, self.values, timeout=.03,
                        client_factory=lambda _: self.fail('inventory did not finish'))
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(result, 3)
                self.assertEqual(report['lastError'], 'ARCHIVE_UPLOAD_TIME_BUDGET_EXPIRED')
                self.assertFalse((self.root / '.upload-only.lock').exists())

    def test_invalid_public_timeout_cannot_disable_deadline_or_create_files(self):
        for timeout in (float('nan'), float('inf'), -float('inf'), 0, -1, 3601, True, None, '30'):
            with self.subTest(timeout=timeout):
                report, result = foreground.run_upload(self.root, self.values, timeout=timeout,
                    client_factory=lambda _: self.fail('invalid timeout accepted'))
                self.assertEqual(result, 3)
                self.assertEqual(report['lastError'], 'ARCHIVE_UPLOAD_TIMEOUT_INVALID')
                self.assertFalse((self.root / '.upload-only.lock').exists())

    def test_failed_directory_scan_cannot_claim_an_empty_completed_upload(self):
        def denied_walk(*_args, **kwargs):
            kwargs['onerror'](PermissionError('unreadable days'))
            return iter(())
        with patch.object(foreground.os, 'walk', side_effect=denied_walk):
            report, result = foreground.run_upload(self.root, {})
        self.assertEqual((result, report['state']), (3, 'incomplete'))
        self.assertEqual(report['lastError'], 'ARCHIVE_LOCAL_SCAN_FAILED')

    def test_symlinked_manifest_does_not_block_the_other_sealed_segment(self):
        healthy = self.segment.with_name('healthy')
        shutil.copytree(self.segment, healthy)
        original = self.manifest.read_bytes()
        self.manifest.unlink()
        outside = self.root / 'outside.json'
        outside.write_bytes(original)
        self.manifest.symlink_to(outside)
        calls = []
        class CompleteClient:
            def upload_segment(self, segment):
                calls.append(segment)
                return {'state': 'complete'}
        report, result = foreground.run_upload(self.root, self.values,
            client_factory=lambda _: CompleteClient())
        self.assertEqual(calls, [healthy])
        self.assertEqual((result, report['invalidSegments'], report['completedSegments']), (3, 1, 1))
        self.assertEqual(outside.read_bytes(), original)

    def test_source_snapshot_is_synced_uploaded_and_never_acknowledged_or_relisted(self):
        fixture = self.root / 'source-fixture'
        shutil.copytree(self.segment, fixture)
        relative = '2026-10-27/雷达/雷达_145000-155000/from-source'
        metadata = json.loads((fixture / 'archive.json').read_text())
        descriptor, metadata = source_descriptor_fixture(relative, metadata)
        (fixture / 'archive.json').write_text(json.dumps(metadata))
        owner = self
        class Source:
            listings = 0
            pulls = 0
            def sealed_snapshot(inner):
                inner.listings += 1
                return [descriptor]
            def _pull(inner, received):
                inner.pulls += 1
                owner.assertEqual(received, descriptor)
                shutil.copytree(fixture, owner.root / 'days' / relative)
                return True
        source = Source()
        progress = []
        with patch.object(foreground.time, 'sleep', side_effect=lambda _: self.api.finish()):
            report, result = foreground.run_upload(self.root, self.values, timeout=2,
                client_factory=self.client, sync_source=True, source_factory=lambda _: source,
                progress=progress.append)
        self.assertEqual((result, report['state'], report['remoteSync']), (0, 'complete', 'complete'))
        self.assertEqual((report['completedSegments'], report['uploadedThisRun']), (2, 2))
        self.assertEqual((report['sourceSelectedSegments'], report['sourcePendingSegments']), (1, 0))
        self.assertEqual((source.listings, source.pulls), (1, 1))
        self.assertTrue(progress)
        self.assertTrue((self.root / 'days' / relative / 'uploaded.json').is_file())
        self.assertEqual((fixture / 'record.db3.zst').read_bytes(), b'fixture-compressed-data')

    def test_busy_background_transfer_is_retried_without_overwriting_staging(self):
        manifest = json.loads(self.manifest.read_text())
        self.manifest.unlink()
        relative = '2026-10-27/雷达/雷达_145000-155000/source'
        descriptor, manifest = source_descriptor_fixture(relative, manifest)
        owner = self
        class Source:
            pulls = 0
            def sealed_snapshot(inner):return [descriptor]
            def _pull(inner, _descriptor):
                inner.pulls += 1
                if inner.pulls == 1:return False
                target = owner.root / 'days' / relative
                target.mkdir(parents=True)
                (target / 'archive.json').write_text(json.dumps(manifest))
                return True
        class CompleteClient:
            def upload_segment(inner, _segment):return dict(state='complete')
        source = Source()
        with patch.object(foreground.time, 'sleep'):
            report, result = foreground.run_upload(self.root, self.values,
                client_factory=lambda _: CompleteClient(), sync_source=True,
                source_factory=lambda _: source)
        self.assertEqual((result, source.pulls, report['uploadedThisRun']), (0, 2, 1))

    def test_unreachable_source_does_not_block_local_upload_or_claim_global_success(self):
        class OfflineSource:
            def sealed_snapshot(inner):raise ValueError('private transport details')
        with patch.object(foreground.time, 'sleep', side_effect=lambda _: self.api.finish()):
            report, result = foreground.run_upload(self.root, self.values, timeout=2,
                client_factory=self.client, sync_source=True, source_factory=lambda _: OfflineSource())
        self.assertEqual((result, report['completedSegments']), (3, 1))
        self.assertEqual(report['remoteSync'], 'unavailable')
        self.assertIsNone(report['sourcePendingSegments'])
        self.assertNotIn('均已确认入仓', foreground.readable_summary(report))
        self.assertNotIn('private transport details', json.dumps(report))

    def test_source_timeout_preserves_partial_transfer_and_never_claims_it_uploaded(self):
        descriptor, _ = source_descriptor_fixture('2026-10-27/雷达/label/source',
            json.loads(self.manifest.read_text()))
        self.manifest.unlink()
        owner = self
        class SlowSource:
            def sealed_snapshot(inner):return [descriptor]
            def _pull(inner, _descriptor):
                partial = owner.root / '.incoming/source/video.mkv.part'
                partial.parent.mkdir(parents=True)
                partial.write_bytes(b'partial video retained')
                time.sleep(5)
        report, result = foreground.run_upload(self.root, self.values, timeout=.03,
            sync_source=True, source_factory=lambda _: SlowSource())
        self.assertEqual(result, 3)
        self.assertEqual((report['pendingSegments'], report['uploadedThisRun']), (1, 0))
        self.assertEqual(report['lastError'], 'ARCHIVE_UPLOAD_TIME_BUDGET_EXPIRED')
        self.assertEqual((self.root / '.incoming/source/video.mkv.part').read_bytes(), b'partial video retained')

    def test_new_recordings_created_after_snapshot_wait_for_the_next_invocation(self):
        owner = self
        calls = []
        class EmptySource:
            def sealed_snapshot(inner):return []
        class CompleteClient:
            def upload_segment(inner, segment):
                calls.append(segment)
                late = owner.segment.with_name('new-after-snapshot')
                if not late.exists():shutil.copytree(owner.segment, late)
                return dict(state='complete')
        report, result = foreground.run_upload(self.root, self.values,
            client_factory=lambda _: CompleteClient(), sync_source=True,
            source_factory=lambda _: EmptySource())
        self.assertEqual((result, report['selectedSegments']), (0, 1))
        self.assertEqual(calls, [self.segment])
        self.assertFalse((self.segment.with_name('new-after-snapshot') / 'uploaded.json').exists())

    def test_read_only_check_never_opens_a_source_connection_even_if_requested(self):
        report, result = foreground.run_upload(self.root, {}, sync_source=True, dry_run=True,
            source_factory=lambda _: self.fail('dry run contacted source'))
        self.assertEqual((result, report['remoteSync']), (0, 'not_checked'))

    def test_real_source_protocol_reaches_warehouse_confirmation_without_source_ack(self):
        from collector.archive_pull import FilePuller, REMOTE_FILES
        source_home = self.root / 'isolated-source'
        relative = '2026-10-27/雷达/雷达_145000-155000/from-source'
        source_dir = source_home / '.local/share/robot-native-archive/days' / relative
        shutil.copytree(self.segment, source_dir)
        manifest = json.loads((source_dir / 'archive.json').read_text())
        manifest.update(id='from-source', metadata=dict(scope='garage'))
        (source_dir / 'archive.json').write_text(json.dumps(manifest))
        commands = []
        def popen(command, **kwargs):
            commands.append(command)
            return subprocess.Popen(['/usr/bin/python3', '-B', '-c', REMOTE_FILES],
                env={**os.environ, 'HOME':str(source_home)}, **kwargs)
        def source_factory(config):
            return FilePuller(['/usr/bin/ssh', '-T'], 'fixture@source', self.root, config,
                popen=popen, min_free_bytes=0, max_spool_bytes=1024**2,
                camera_only=False, training_only=False, bytes_per_second=0)
        with patch.object(foreground.time, 'sleep', side_effect=lambda _: self.api.finish()):
            report, result = foreground.run_upload(self.root, self.values, timeout=2,
                client_factory=self.client, sync_source=True, source_factory=source_factory)
        self.assertEqual((result, report['completedSegments'], report['uploadedThisRun']), (0, 2, 2))
        self.assertTrue((self.root / 'days' / relative / 'uploaded.json').is_file())
        self.assertFalse((source_dir / 'uploaded.json').exists())
        self.assertEqual((source_dir / 'record.db3.zst').read_bytes(), b'fixture-compressed-data')
        self.assertTrue(commands)

    def test_new_unconfirmed_capture_is_prioritized_across_local_source_and_timezones(self):
        owner = self
        calls = []
        acknowledged_first = []
        manifest = json.loads(self.manifest.read_text())
        manifest['startedAt'] = '2026-10-27T06:00:00+00:00'
        self.manifest.write_text(json.dumps(manifest))
        def local(name, start):
            path = self.segment.with_name(name)
            shutil.copytree(self.segment, path)
            data = {**manifest, 'id':name, 'startedAt':start}
            (path / 'archive.json').write_text(json.dumps(data))
            return path
        offset_later_text = local('offset-start', '2026-10-27T15:10:00+08:00')
        newest = local('newest', '2026-10-27T07:20:00Z')
        old_ack = local('already-stored', '2026-10-27T05:00:00Z')
        import hashlib
        (old_ack / 'uploaded.json').write_text(json.dumps(dict(state='complete',
            manifestSha256=hashlib.sha256((old_ack / 'archive.json').read_bytes()).hexdigest())))
        relative = '2026-10-27/雷达/label/source-new'
        source_manifest = {**manifest, 'id':'source-new', 'startedAt':'2026-10-27T07:15:00+00:00'}
        descriptor, source_manifest = source_descriptor_fixture(relative, source_manifest)
        class Source:
            def sealed_snapshot(inner):return [descriptor]
            def _pull(inner, _descriptor):
                target = owner.root / 'days' / relative
                target.mkdir(parents=True)
                (target / 'archive.json').write_text(json.dumps(source_manifest))
                return True
        class CompleteClient:
            def upload_segment(inner, segment):
                calls.append(segment)
                owner.assertIn(old_ack, acknowledged_first)
                return dict(state='complete')
        real_acknowledged = foreground.acknowledged
        def tracked_acknowledged(path, expected_robot_id=None):
            acknowledged_first.append(path.parent)
            return real_acknowledged(path, expected_robot_id)
        with patch.object(foreground, 'acknowledged', side_effect=tracked_acknowledged):
            report, result = foreground.run_upload(self.root, self.values, sync_source=True,
                source_factory=lambda _: Source(), client_factory=lambda _: CompleteClient())
        self.assertEqual(result, 0)
        self.assertEqual(calls, [newest, self.root / 'days' / relative, offset_later_text, self.segment])
        self.assertEqual((report['alreadyCompletedSegments'], report['uploadedThisRun']), (1, 4))

    def test_capture_priority_uses_date_fallback_and_local_only_prioritizes_new_video(self):
        newest = self.segment.with_name('z-newest')
        shutil.copytree(self.segment, newest)
        metadata = json.loads((newest / 'archive.json').read_bytes())
        metadata['startedAt'] = '2026-10-28T07:20:00+00:00'
        (newest / 'archive.json').write_text(json.dumps(metadata))
        calls = []
        class CompleteClient:
            def upload_segment(inner, segment):
                calls.append(segment)
                return dict(state='complete')
        report, result = foreground.run_upload(self.root, self.values,
            client_factory=lambda _: CompleteClient())
        self.assertEqual((result, calls), (0, [newest, self.segment]))
        metadata.update(startedAt='bad timestamp', day='2026-10-28')
        (newest / 'archive.json').write_text(json.dumps(metadata))
        from datetime import datetime
        expected = datetime.fromisoformat('2026-10-28T00:00:00+08:00').timestamp()
        self.assertEqual(foreground.recording_priority(newest / 'archive.json'), expected)
        (newest / 'archive.json').write_text('{invalid')
        self.assertEqual(foreground.recording_priority(newest / 'archive.json'), 0)

    def closure_fixture(self, *, proof=True, invalid=False):
        from collector.archive_session_end import persist_session_end
        import hashlib
        from types import SimpleNamespace
        token, segment, dataset='c'*32,'d'*32,'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        value=json.loads(self.manifest.read_text())
        value.update(id=segment,metadata=dict(captureSessionId=token))
        self.manifest.write_text(json.dumps(value))
        (self.segment/'uploaded.json').write_text(json.dumps(dict(state='complete',
            segmentId=segment,datasetId=dataset,
            manifestSha256=hashlib.sha256(self.manifest.read_bytes()).hexdigest())))
        control=Path(self.values['ARCHIVE_CONTROL_DIR'])
        if proof:
            persist_session_end(control,token,ROBOT,100,now=120,sealed=True,
                capture_incomplete=False)
        if invalid:
            (control/'session-ends'/(token+'.json')).write_text('{broken')
        owner=self
        class Client:
            config=SimpleNamespace(robot_id=ROBOT)
            def _request(self,path,method='GET',payload=None):
                owner.closure_calls.append((path,method,payload));return {}
            def upload_segment(self,_path):
                owner.fail('already ACKed segment must not be uploaded again')
        self.closure_calls=[]
        return token,Client

    def test_existing_proof_is_uploaded_even_when_all_videos_already_acknowledged(self):
        token,Client=self.closure_fixture()
        report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,report['state'],report['sessionEnds']['acknowledged']),(0,'complete',1))
        self.assertEqual(len(self.closure_calls),1)
        self.assertTrue(self.closure_calls[0][0].endswith('/'+token+'/end'))
        self.assertEqual(report['uploadedThisRun'],0)

    def test_foreground_confirms_explicit_recovered_interruption_with_evidence(self):
        token,Client=self.closure_fixture()
        path=Path(self.values['ARCHIVE_CONTROL_DIR'])/'session-ends'/(token+'.json')
        value=json.loads(path.read_text());value.update(reason='recovered_interruption',
            captureIncomplete=True,segmentIds=['d'*32],recoveryEvidenceSha256='e'*64)
        path.write_text(json.dumps(value));before=path.read_bytes()
        report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,report['state'],report['sessionEnds']['acknowledged']),(0,'complete',1))
        self.assertEqual(self.closure_calls[0][2]['reason'],'recovered_interruption')
        self.assertEqual(self.closure_calls[0][2]['recoveryEvidenceSha256'],'e'*64)
        self.assertEqual(path.read_bytes(),before)

    def test_absent_proof_preserves_originals_and_does_not_block_video_success(self):
        _,Client=self.closure_fixture(proof=False)
        report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,report['state']),(0,'complete'))
        self.assertEqual(report['sessionEnds']['missing'],1)
        self.assertIsNone(report['lastError'])
        self.assertFalse(self.closure_calls)
        self.assertFalse((Path(self.values['ARCHIVE_CONTROL_DIR'])/'session-ends').exists())
        self.assertEqual((self.segment/'record.db3.zst').read_bytes(),b'fixture-compressed-data')
        summary=foreground.readable_summary(report)
        self.assertIn('均已确认入仓',summary)
        self.assertNotIn('结束标记',summary)
        self.assertNotIn('结束证明',summary)

    def test_malformed_proof_does_not_block_video_success_or_get_rewritten(self):
        token,Client=self.closure_fixture(invalid=True)
        marker=Path(self.values['ARCHIVE_CONTROL_DIR'])/'session-ends'/(token+'.json')
        report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,report['sessionEnds']['invalid']),(0,1))
        self.assertIsNone(report['lastError'])
        self.assertEqual(marker.read_text(),'{broken')
        self.assertFalse(self.closure_calls)

    def test_source_upload_also_confirms_existing_local_end_proof(self):
        _,Client=self.closure_fixture()
        class Source:
            def sealed_snapshot(self):return []
        report,code=foreground.run_upload(self.root,self.values,sync_source=True,
            source_factory=lambda _:Source(),client_factory=lambda _:Client())
        self.assertEqual((code,report['sessionEnds']['acknowledged']),(0,1))
        self.assertEqual(len(self.closure_calls),1)

    def test_dry_run_never_uploads_or_rewrites_a_closure(self):
        token,_=self.closure_fixture()
        marker=Path(self.values['ARCHIVE_CONTROL_DIR'])/'session-ends'/(token+'.json')
        before=marker.read_bytes()
        report,code=foreground.run_upload(self.root,self.values,dry_run=True,
            client_factory=lambda _:self.fail('dry run created API client'))
        self.assertEqual((code,report['state']),(0,'dry_run'))
        self.assertEqual(marker.read_bytes(),before)
        self.assertFalse(self.closure_calls)

    def test_new_video_upload_is_followed_by_existing_end_proof_confirmation(self):
        from collector.archive_session_end import persist_session_end
        from types import SimpleNamespace
        from uuid import uuid4
        import hashlib
        token='e'*32
        value=json.loads(self.manifest.read_text())
        value.update(id='f'*32,metadata=dict(captureSessionId=token))
        self.manifest.write_text(json.dumps(value))
        persist_session_end(Path(self.values['ARCHIVE_CONTROL_DIR']),token,ROBOT,100,
            now=120,sealed=True,capture_incomplete=False)
        calls=[];owner=self
        class Client:
            config=SimpleNamespace(robot_id=ROBOT)
            def upload_segment(self,path):
                calls.append('video')
                (path/'uploaded.json').write_text(json.dumps(dict(state='complete',
                    segmentId=value['id'],datasetId=str(uuid4()),
                    manifestSha256=hashlib.sha256(owner.manifest.read_bytes()).hexdigest())))
                return dict(state='complete')
            def _request(self,_path,method='GET',payload=None):
                calls.append('closure');return {}
        report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,calls),(0,['video','closure']))
        self.assertEqual(report['sessionEnds']['acknowledged'],1)

    def test_closure_network_failure_is_optional_and_next_run_retries(self):
        token,Client=self.closure_fixture()
        class FailingClient(Client):
            def _request(self,*_args,**_kwargs):
                raise ArchiveUploadError('ARCHIVE_API_UNAVAILABLE',503)
        report,code=foreground.run_upload(self.root,self.values,
            client_factory=lambda _:FailingClient())
        self.assertEqual((code,report['sessionEnds']['pending']),(0,1))
        self.assertIsNone(report['lastError'])
        self.assertEqual(report['sessionEnds']['lastError'],'ARCHIVE_API_UNAVAILABLE')
        self.assertTrue((Path(self.values['ARCHIVE_CONTROL_DIR'])/'session-ends'/(token+'.json')).exists())
        # Advance the proof retry clock, leaving all capture gates untouched.
        with patch('time.time',return_value=time.time()+31):
            report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,report['sessionEnds']['acknowledged']),(0,1))

    def test_unsafe_session_directory_does_not_block_video_or_follow_link(self):
        _,Client=self.closure_fixture(proof=False)
        control=Path(self.values['ARCHIVE_CONTROL_DIR']);control.mkdir()
        outside=self.root/'outside-proofs';outside.mkdir()
        (control/'session-ends').symlink_to(outside,target_is_directory=True)
        report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,report['sessionEnds']['invalid']),(0,1))
        self.assertIsNone(report['lastError'])
        self.assertEqual(list(outside.iterdir()),[])

    def test_optional_proof_timeout_cannot_fail_already_confirmed_videos(self):
        _,Client=self.closure_fixture()
        class SlowProof(Client):
            def _request(self,*_args,**_kwargs):
                time.sleep(5)
                raise AssertionError('foreground deadline failed to interrupt proof')
        started=time.monotonic()
        report,code=foreground.run_upload(self.root,self.values,timeout=.03,
            client_factory=lambda _:SlowProof())
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual((code,report['state'],report['pendingSegments']),(0,'complete',0))
        self.assertIsNone(report['lastError'])
        self.assertEqual(report['sessionEnds']['lastError'],'ARCHIVE_SESSION_END_TIME_BUDGET_EXPIRED')

    def test_optional_proof_credentials_failure_cannot_fail_durable_video_ack(self):
        self.closure_fixture()
        with patch.object(foreground,'upload_config',
                side_effect=ArchiveUploadError('ARCHIVE_CONFIG_MUST_BE_PRIVATE')):
            report,code=foreground.run_upload(self.root,self.values)
        self.assertEqual((code,report['state']),(0,'complete'))
        self.assertIsNone(report['lastError'])
        self.assertEqual(report['sessionEnds']['lastError'],'ARCHIVE_CONFIG_MUST_BE_PRIVATE')

    def test_optional_proof_probe_failure_does_not_expose_exception_or_fail_video(self):
        self.closure_fixture()
        with patch.object(foreground,'_session_ends_required',
                side_effect=PermissionError('private diagnostic '+PASSWORD)):
            report,code=foreground.run_upload(self.root,self.values)
        self.assertEqual((code,report['state']),(0,'complete'))
        self.assertIsNone(report['lastError'])
        self.assertEqual(report['sessionEnds']['lastError'],'PermissionError')
        self.assertNotIn(PASSWORD,json.dumps(report))

    def test_missing_proof_does_not_block_complete_local_and_source_snapshot(self):
        _,Client=self.closure_fixture(proof=False)
        class Source:
            def sealed_snapshot(self):return []
        report,code=foreground.run_upload(self.root,self.values,sync_source=True,
            source_factory=lambda _:Source(),client_factory=lambda _:Client())
        self.assertEqual((code,report['state'],report['remoteSync']),(0,'complete','complete'))
        self.assertEqual(report['sessionEnds']['missing'],1)
        self.assertIsNone(report['lastError'])

    def test_optional_proof_failure_does_not_mask_failed_video_upload(self):
        _,Client=self.closure_fixture(invalid=True)
        failed=self.segment.with_name('unconfirmed-video')
        shutil.copytree(self.segment,failed)
        (failed/'uploaded.json').unlink()
        class FailedVideo(Client):
            def upload_segment(self,_path):
                raise ArchiveUploadError('ARCHIVE_REMOTE_FILE_MISMATCH')
        report,code=foreground.run_upload(self.root,self.values,
            client_factory=lambda _:FailedVideo())
        self.assertEqual((code,report['state'],report['pendingSegments']),(3,'incomplete',1))
        self.assertEqual(report['lastError'],'ARCHIVE_REMOTE_FILE_MISMATCH')
        self.assertEqual(report['sessionEnds']['invalid'],1)

    def test_optional_proof_failure_does_not_mask_bad_video_ack(self):
        _,Client=self.closure_fixture(proof=False)
        (self.segment/'uploaded.json').write_text('{broken')
        report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,report['invalidSegments']),(3,1))
        self.assertEqual(report['lastError'],'ARCHIVE_STATE_INVALID')

    def test_optional_proof_failure_does_not_mask_source_sync_failure(self):
        _,Client=self.closure_fixture(invalid=True)
        class Offline:
            def sealed_snapshot(self):raise ValueError('private transport details')
        report,code=foreground.run_upload(self.root,self.values,sync_source=True,
            source_factory=lambda _:Offline(),client_factory=lambda _:Client())
        self.assertEqual((code,report['state'],report['remoteSync']),(3,'incomplete','unavailable'))
        self.assertEqual(report['lastError'],'ARCHIVE_SOURCE_SYNC_UNAVAILABLE')
        self.assertEqual(report['sessionEnds']['invalid'],1)

    def test_optional_proof_failure_does_not_hide_real_unsealed_video(self):
        _,Client=self.closure_fixture(proof=False)
        partial=self.segment.with_name('still-sealing');partial.mkdir()
        (partial/'video-pending.json').write_text('{}')
        report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,report['preparingSegments']),(3,1))
        self.assertEqual(report['lastError'],'ARCHIVE_LOCAL_SEALING_PENDING')
        self.assertEqual(report['sessionEnds']['missing'],1)

    def test_bounded_closure_batches_confirm_more_than_two_sessions(self):
        from collector.archive_session_end import persist_session_end
        from types import SimpleNamespace
        import hashlib
        token,Client=self.closure_fixture()
        for number in (1,2):
            path=self.segment.with_name('capture-'+str(number))
            shutil.copytree(self.segment,path)
            value=json.loads((path/'archive.json').read_text())
            token=format(number,'032x');value.update(id=format(number+10,'032x'),
                metadata=dict(captureSessionId=token))
            (path/'archive.json').write_text(json.dumps(value))
            ack=json.loads((path/'uploaded.json').read_text());ack.update(segmentId=value['id'],
                manifestSha256=hashlib.sha256((path/'archive.json').read_bytes()).hexdigest())
            (path/'uploaded.json').write_text(json.dumps(ack))
            persist_session_end(Path(self.values['ARCHIVE_CONTROL_DIR']),token,ROBOT,100,
                now=120,sealed=True,capture_incomplete=False)
        report,code=foreground.run_upload(self.root,self.values,client_factory=lambda _:Client())
        self.assertEqual((code,report['sessionEnds']['acknowledged']),(0,3))
        self.assertEqual(len(self.closure_calls),3)


class ForegroundSummaryTests(unittest.TestCase):
    def report(self, **overrides):
        result = dict(state='incomplete', scope='local-sealed-snapshot', remoteSync='not_checked',
            selectedSegments=2403, completedSegments=2403, pendingSegments=0, invalidSegments=0,
            preparingSegments=3, activeSegments=0, stagingSegments=0, recoverySegments=1,
            lastError='ARCHIVE_LOCAL_SEALING_PENDING')
        result.update(overrides)
        return result

    def test_explains_confirmed_products_separately_from_historical_unsealed_remnants(self):
        summary = foreground.readable_summary(self.report())
        self.assertIn('2403 个本机成品均已确认入仓', summary)
        for text in ('已确认成品：2403', '待上传成品：0', '无效清单或回执：0',
                     '待封片或需处理片段：3', '录制中片段：0', '中转暂存片段：0', '恢复目录条目：1'):
            self.assertIn(text, summary)
        self.assertIn('3 个历史或待封片片段尚未形成可上传成品', summary)
        self.assertIn('未检查 Jetson', summary)
        self.assertNotIn('正在压缩', summary)

    def test_pending_or_invalid_products_cannot_claim_all_confirmed(self):
        for changed in (dict(pendingSegments=1, completedSegments=2402), dict(invalidSegments=1)):
            with self.subTest(changed=changed):
                summary = foreground.readable_summary(self.report(**changed))
                self.assertNotIn('均已确认入仓', summary)
                self.assertNotIn('可上传成品已全部确认', summary)

    def test_configuration_failure_does_not_present_unknown_counts_as_zero(self):
        summary = foreground.readable_summary(dict(state='incomplete',
            lastError='ARCHIVE_UPLOAD_CONFIG_INVALID'))
        self.assertIn('已确认成品：未核对', summary)
        self.assertIn('配置无法读取或格式有误', summary)
        self.assertNotIn('均已确认入仓', summary)

    def test_cli_summary_preserves_the_existing_exit_code(self):
        stream = io.StringIO()
        report = self.report()
        with patch.object(foreground, 'load_environment', return_value={}), \
                patch.object(foreground, 'run_upload', return_value=(report, 3)), redirect_stdout(stream):
            result = foreground.main(['--summary'])
        self.assertEqual(result, 3)
        self.assertEqual(stream.getvalue(), foreground.readable_summary(report) + '\n')

    def test_default_and_dry_run_cli_keep_machine_readable_json(self):
        for arguments, report, status in (([], self.report(), 3),
                (['--dry-run', '--timeout', '2', '--archive-root', '/fixture'],
                 self.report(state='dry_run'), 0)):
            with self.subTest(arguments=arguments):
                stream = io.StringIO()
                with patch.object(foreground, 'load_environment', return_value={}), \
                        patch.object(foreground, 'run_upload', return_value=(report, status)), \
                        redirect_stdout(stream):
                    result = foreground.main(arguments)
                self.assertEqual(result, status)
                self.assertEqual(json.loads(stream.getvalue()), report)

    def test_timeout_summary_explains_resume_without_disclosing_exception_text(self):
        summary = foreground.readable_summary(self.report(pendingSegments=1, completedSegments=2402,
            lastError='ARCHIVE_UPLOAD_TIME_BUDGET_EXPIRED'))
        self.assertIn('可稍后再次运行继续上传', summary)
        self.assertIn('ARCHIVE_UPLOAD_TIME_BUDGET_EXPIRED', summary)

    def test_video_summary_omits_optional_session_proof_diagnostics(self):
        for issue in (dict(pending=1,missing=1,invalid=0,lastError='ARCHIVE_SESSION_END_MISSING'),
                dict(pending=1,missing=0,invalid=1,lastError='JSONDecodeError'),
                dict(pending=1,missing=0,invalid=0,lastError='ARCHIVE_API_UNAVAILABLE')):
            with self.subTest(issue=issue):
                report=self.report(state='complete',preparingSegments=0,recoverySegments=0,
                    sessionEnds=issue,lastError=None)
                summary=foreground.readable_summary(report)
                self.assertIn('均已确认入仓',summary)
                self.assertNotIn('结束标记',summary)
                self.assertNotIn('结束证明',summary)
                self.assertNotIn('诊断代码',summary)


class DesktopUploadWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='isolated-desktop-upload-')
        self.root = Path(self.temporary.name)
        self.tool = self.root / 'tool'
        self.tool.mkdir()
        self.script = self.tool / 'upload.sh'
        shutil.copy2(Path.home() / '桌面/camera-section-tool/upload_data.sh', self.script)
        self.commands = self.root / 'bin'
        self.commands.mkdir()
        self.called = self.root / 'service-command-called'
        fake = self.commands / 'systemctl'
        fake.write_text('#!/bin/bash\ntouch "$SERVICE_COMMAND_MARKER"\nexit 99\n')
        fake.chmod(0o700)
        self.archive = self.root / 'archive'
        self.archive.mkdir()
        self.control = self.root / 'control'
        self.control.mkdir()
        self.gate = self.control / 'garage.json'
        self.gate.write_text('{"scope":"garage","enabled":true,"expiresAt":null}\n')
        self.env_file = self.root / 'archive.env'
        self.env_file.write_text('')
        self.env = dict(os.environ, PATH=str(self.commands) + os.pathsep + os.environ['PATH'],
            ARCHIVE_ROOT=str(self.archive), ARCHIVE_CONTROL_DIR=str(self.control),
            ARCHIVE_ENV_FILE=str(self.env_file), SERVICE_COMMAND_MARKER=str(self.called),
            LOCAL_CAMERA_DATASET_ROOT=str(self.tool / '本地采集文件'))

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, *args):
        return subprocess.run(['/bin/bash', str(self.script), *args], env=self.env,
                              capture_output=True, text=True, timeout=5)

    def test_native_dry_run_never_starts_services_or_changes_an_enabled_gate(self):
        before = self.gate.read_bytes()
        result = self.run_script('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(report['state'], 'dry_run')
        self.assertEqual(report['remoteSync'], 'not_checked')
        self.assertEqual(self.gate.read_bytes(), before)
        self.assertFalse(self.called.exists())
        self.assertFalse(list(self.archive.iterdir()))

    def test_explicit_local_session_delegates_without_native_service_side_effects(self):
        (self.tool / '本地采集文件').mkdir()
        delegate = self.root / 'delegate.sh'
        delegate.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
        self.env['LOCAL_CAMERA_UPLOAD_SCRIPT'] = str(delegate)
        result = self.run_script('--session', '20261102_120000', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['--root', str(self.tool / '本地采集文件'),
            '--session', '20261102_120000', '--dry-run'])
        self.assertFalse(self.called.exists())

    def test_completed_old_session_cannot_shadow_current_native_archive_uploads(self):
        session = self.tool / '本地采集文件/20261102_120000'
        session.mkdir(parents=True)
        (session / 'capture-session.json').write_text('{"state":"completed"}')
        delegate = self.root / 'delegate.sh'
        delegate.write_text('#!/bin/bash\nprintf "local-delegated\\n"\n')
        self.env['LOCAL_CAMERA_UPLOAD_SCRIPT'] = str(delegate)
        result = self.run_script('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(report['state'], 'dry_run')
        self.assertEqual(report['scope'], 'local-sealed-snapshot')
        self.assertNotIn('local-delegated', result.stdout)
        self.assertFalse(self.called.exists())
