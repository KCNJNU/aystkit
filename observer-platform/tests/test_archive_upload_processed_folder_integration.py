"""Manual upload adds an empty destination without moving or resending media."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from collector import archive_upload_only as foreground
from collector.archive_upload import ArchiveUploadClient
from tests.test_archive_upload import FakeApi, PASSWORD, ROBOT, Reply
from tests.test_archive_upload_only import source_descriptor_fixture


FOLDER = '处理好的数据_20261112_153045'


class FolderFailureApi(FakeApi):
    """Fail only the new empty root folder, after normal media succeeds."""
    def open(self, request, timeout):
        if request.get_method() == 'POST' and request.full_url.endswith('/folders'):
            payload = json.loads(request.data)
            if payload == {'parentPath': '', 'name': FOLDER}:
                return Reply({'message': 'fixture failure'}, 500)
        return super().open(request, timeout)


class ProcessedFolderUploadIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='processed-folder-integration-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.relative = '2026-11-04/原相机前/原相机前_16:01-16:02/fixture-segment'
        self.segment = self.root / 'days' / self.relative
        self.segment.mkdir(parents=True)
        self.media = self.segment / 'video.mkv'
        self.media.write_bytes(b'fixture sealed video bytes')
        self.manifest = self.segment / 'archive.json'
        self.manifest.write_text(json.dumps(dict(schemaVersion=1, id='fixture-segment',
            day='2026-11-04', sensor='原相机前', label='原相机前_16:01-16:02',
            robotId=ROBOT, startedAt='2026-11-04T08:01:00Z',
            endAt='2026-11-04T08:02:00Z', metadata={'scope': 'garage'}, files=['video.mkv'])),
            encoding='utf-8')
        self.values = dict(ARCHIVE_DBPROG_EMAIL='archive@example.invalid',
            ARCHIVE_DBPROG_PASSWORD=PASSWORD, ARCHIVE_ROBOT_ID=ROBOT,
            ARCHIVE_CONTROL_DIR=str(self.root / 'control'), ARCHIVE_UPLOAD_BYTES_PER_SECOND='0')
        self.api = FakeApi()

    def client(self, config):
        return ArchiveUploadClient(config, opener=self.api)

    def upload(self, **options):
        with patch.object(foreground.time, 'sleep', side_effect=lambda _: self.api.finish()):
            return foreground.run_upload(self.root, self.values, timeout=3,
                client_factory=self.client, **options)

    @staticmethod
    def snapshot(root):
        return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
                for path in root.rglob('*')}

    def root_folder_posts(self):
        return [(path, json.loads(body)) for method, path, _headers, body in self.api.calls
                if method == 'POST' and path.endswith('/folders')
                and json.loads(body).get('parentPath') == '']

    def test_local_ack_still_adds_root_folder_without_reupload_or_ack_change(self):
        report, status = self.upload()
        self.assertEqual((status, report['state']), (0, 'complete'))
        before = self.snapshot(self.segment)
        uploads_before = len(self.api.uploads)
        self.api.calls.clear()

        report, status = self.upload(processed_folder=FOLDER)

        self.assertEqual((status, report['state']), (0, 'complete'))
        self.assertEqual((report['completedSegments'], report['pendingSegments'],
                          report['alreadyCompletedSegments'], report['uploadedThisRun']), (1, 0, 1, 0))
        self.assertEqual(report['processedFolder']['name'], FOLDER)
        self.assertFalse(report['processedFolder'].get('lastError'))
        self.assertIn(FOLDER, self.api.folders)
        self.assertEqual(len(self.api.uploads), uploads_before)
        self.assertFalse(any(method == 'PUT' or (method == 'POST' and path == '/v1/uploads')
                             for method, path, _headers, _body in self.api.calls))
        self.assertEqual(self.snapshot(self.segment), before)
        self.assertEqual([payload for _path, payload in self.root_folder_posts()],
                         [{'parentPath': '', 'name': FOLDER}])

    def test_source_snapshot_adds_folder_without_relocating_media_or_acknowledging_source(self):
        fixture = self.root / 'source-fixture'
        shutil.copytree(self.segment, fixture)
        descriptor, _ = source_descriptor_fixture(self.relative,
            json.loads((fixture / 'archive.json').read_text()))
        shutil.rmtree(self.segment)
        original_source = self.snapshot(fixture)
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
                shutil.copytree(fixture, owner.segment)
                return True

            def _ack(inner, *_args, **_kwargs):
                owner.fail('manual upload must not acknowledge or prune source')

        source = Source()
        report, status = self.upload(sync_source=True, source_factory=lambda _: source,
                                     processed_folder=FOLDER)

        self.assertEqual((status, report['state'], report['remoteSync']), (0, 'complete', 'complete'))
        self.assertEqual((source.listings, source.pulls), (1, 1))
        self.assertEqual((report['sourceSelectedSegments'], report['completedSegments'],
                          report['pendingSegments']), (1, 1, 0))
        self.assertIn(FOLDER, self.api.folders)
        self.assertEqual(self.snapshot(fixture), original_source)
        self.assertEqual(self.media.read_bytes(), original_source['video.mkv'])
        self.assertTrue((self.segment / 'uploaded.json').is_file())
        stored = next(iter(self.api.files.values()))
        self.assertEqual(stored['originalName'], 'video.mkv')
        self.assertEqual(stored['folderPath'], '原相机前/原相机前_16:01-16:02/fixture-segment')
        self.assertNotIn(FOLDER, stored['folderPath'])
        self.assertEqual([payload for _path, payload in self.root_folder_posts()
                          if payload['name'] == FOLDER], [{'parentPath': '', 'name': FOLDER}])

    def test_dry_run_with_requested_folder_has_no_client_source_or_filesystem_writes(self):
        for sync_source in (False, True):
            with self.subTest(sync_source=sync_source):
                before = self.snapshot(self.root)
                with patch.object(foreground, 'upload_config', side_effect=AssertionError('credentials read')):
                    report, status = foreground.run_upload(self.root, {}, dry_run=True,
                        processed_folder=FOLDER, sync_source=sync_source,
                        client_factory=lambda _: self.fail('dry-run created API client'),
                        source_factory=lambda _: self.fail('dry-run contacted source'))
                self.assertEqual((status, report['state']), (0, 'dry_run'))
                self.assertNotIn('processedFolder', report)
                self.assertEqual(self.snapshot(self.root), before)
                self.assertFalse(self.api.calls)

    def test_folder_http_failure_preserves_successful_media_ack_counts_and_exit_status(self):
        self.api = FolderFailureApi()
        manifest_before = self.manifest.read_bytes()
        media_before = self.media.read_bytes()

        report, status = self.upload(processed_folder=FOLDER)

        self.assertEqual((status, report['state']), (0, 'complete'))
        self.assertEqual((report['selectedSegments'], report['completedSegments'],
                          report['pendingSegments'], report['invalidSegments'],
                          report['uploadedThisRun']), (1, 1, 0, 0, 1))
        self.assertIsNone(report['lastError'])
        self.assertEqual(report['processedFolder']['lastError'], 'ARCHIVE_PROCESSED_FOLDER_UNAVAILABLE')
        self.assertNotIn(FOLDER, self.api.folders)
        self.assertTrue(foreground.acknowledged(self.manifest))
        ack_before = (self.segment / 'uploaded.json').read_bytes()
        self.assertEqual(self.manifest.read_bytes(), manifest_before)
        self.assertEqual(self.media.read_bytes(), media_before)

        retried, retry_status = self.upload(processed_folder=FOLDER)
        self.assertEqual((retry_status, retried['completedSegments'], retried['uploadedThisRun']), (0, 1, 0))
        self.assertEqual((self.segment / 'uploaded.json').read_bytes(), ack_before)
        self.assertEqual(len(self.api.uploads), 1)

    def test_unrequested_folder_keeps_low_level_run_upload_free_of_extra_api_calls(self):
        initial, status = self.upload()
        self.assertEqual((status, initial['state']), (0, 'complete'))
        self.api.calls.clear()
        before = self.snapshot(self.segment)
        report, status = foreground.run_upload(self.root, self.values,
            client_factory=lambda _: self.fail('unrequested folder created API client'))
        self.assertEqual((status, report['state'], report['uploadedThisRun']), (0, 'complete', 0))
        self.assertNotIn('processedFolder', report)
        self.assertFalse(self.api.calls)
        self.assertEqual(self.snapshot(self.segment), before)

    def test_cli_requests_one_timestamped_folder_only_for_non_dry_upload(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                stream = io.StringIO()
                arguments = ['--archive-root', str(self.root), '--sync-source']
                if dry_run:
                    arguments.append('--dry-run')
                with patch.object(foreground, 'load_environment', return_value=self.values), \
                        patch('collector.robot_profile.validate_current_profile'), \
                        patch.object(foreground, 'run_upload', return_value=({'state': 'fixture'}, 0)) as run, \
                        patch('collector.archive_upload_folders.processed_folder_name', return_value=FOLDER) as name, \
                        redirect_stdout(stream):
                    status = foreground.main(arguments)
                self.assertEqual(status, 0)
                self.assertEqual(run.call_args.kwargs['processed_folder'], None if dry_run else FOLDER)
                self.assertEqual(name.call_count, 0 if dry_run else 1)
                self.assertEqual(run.call_args.kwargs['sync_source'], True)
                self.assertEqual(json.loads(stream.getvalue()), {'state': 'fixture'})


if __name__ == '__main__':
    unittest.main()
