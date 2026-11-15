"""Legacy batches keep healthy sessions moving and never hide a partial result."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from collector.archive_upload import ArchiveUploadError


ROBOT = '365c77c6-0029-4406-a504-739014d0a672'


@pytest.fixture
def uploader(tmp_path, monkeypatch):
    path = Path.home() / 'code/upper_computer/tools/upload_local_camera_dataset.py'
    spec = importlib.util.spec_from_file_location('isolated_legacy_camera_upload', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('ARCHIVE_TRAINING_ONLY', '0')
    root = tmp_path / 'camera dataset'
    root.mkdir()
    (root / 'capture-session.json').write_text('{"state":"completed"}')
    entries = []
    for identity in ('a-first', 'z-second'):
        segment = root / 'days/2026-11-04/原相机前/time-slot' / identity
        segment.mkdir(parents=True)
        (segment / 'video.mkv').write_bytes(identity.encode())
        (segment / 'archive.json').write_text(json.dumps(dict(schemaVersion=1,
            robotId=ROBOT, day='2026-11-04', id=identity, sensor='原相机前',
            label='time-slot', files=['video.mkv'], metadata={})))
        entries.append(segment)
    config = SimpleNamespace(robot_id=ROBOT)
    monkeypatch.setattr(module.ArchiveUploadConfig, 'from_environment', lambda _: config)
    return module, root, entries, config


def invoke(module, root):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = module.main(['--root', str(root)])
    report = json.loads(stdout.getvalue().splitlines()[-1])
    return code, report, stderr.getvalue()


@pytest.mark.parametrize('first_result', ['exception', {}, {'state': 'unexpected'}])
def test_bad_segment_does_not_block_healthy_later_media(uploader, monkeypatch, first_result):
    module, root, entries, config = uploader
    calls = []

    class Client:
        def __init__(self):
            self.config = config

        def upload_segment(self, segment):
            calls.append(segment)
            if segment == entries[0]:
                if first_result == 'exception':
                    raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID')
                return first_result
            return dict(state='complete', datasetId='fixture-dataset')

        def ensure_day(self, day, sensors=()):
            return 'fixture-dataset'

    monkeypatch.setattr(module, 'ArchiveUploadClient', lambda _: Client())
    before = [path.read_bytes() for path in (entries[0] / 'video.mkv', entries[1] / 'video.mkv')]
    code, report, diagnostic = invoke(module, root)
    assert calls == entries
    assert code == 3
    assert report['states'] == dict(complete=1, pending=0, failed=1)
    assert len(report['errors']) == 1
    assert '继续处理其余片段' in diagnostic
    assert before == [path.read_bytes() for path in (entries[0] / 'video.mkv', entries[1] / 'video.mkv')]


def test_pending_and_completed_results_remain_distinct(uploader, monkeypatch):
    module, root, entries, config = uploader
    calls = []

    class Client:
        def __init__(self):
            self.config = config

        def upload_segment(self, segment):
            calls.append(segment)
            return dict(state='pending' if segment == entries[0] else 'complete')

        def ensure_day(self, day, sensors=()):
            return 'fixture-dataset'

    monkeypatch.setattr(module, 'ArchiveUploadClient', lambda _: Client())
    code, report, _ = invoke(module, root)
    assert calls == entries
    assert code == 3
    assert report['states'] == dict(complete=1, pending=1, failed=0)


def test_malformed_session_metadata_is_rejected_without_traceback(uploader):
    module, root, _entries, _config = uploader
    (root / 'capture-session.json').write_text('[]')
    with pytest.raises(SystemExit, match='尚未正常结束'):
        module.main(['--root', str(root), '--dry-run'])


@pytest.fixture
def batch(tmp_path):
    work = tmp_path / 'workspace'
    tools = work / 'tools'
    tools.mkdir(parents=True)
    script = work / 'upload.sh'
    shutil.copy2(Path.home() / 'code/upper_computer/upload_local_camera_dataset.sh', script)
    root = tmp_path / 'sessions with spaces'
    root.mkdir()
    for name in ('20261104_160000', '20261104_170000'):
        session = root / name
        session.mkdir()
        (session / 'capture-session.json').write_text('{"state":"completed"}')
    marker = tmp_path / 'attempted.jsonl'
    (tools / 'upload_local_camera_dataset.py').write_text(
        'import json, os, sys\nfrom pathlib import Path\n'
        'with Path(os.environ["TEST_UPLOAD_ATTEMPTS"]).open("a") as stream:\n'
        ' stream.write(json.dumps(dict(root=sys.argv[2], '
        'folder=os.environ.get("ARCHIVE_PROCESSED_FOLDER_NAME"), '
        'bytecode=sys.dont_write_bytecode))+"\\n")\n'
        'raise SystemExit(int(os.environ.get("TEST_FIRST_UPLOAD_EXIT","3")) '
        'if "160000" in sys.argv[2] else 0)\n')
    env = dict(os.environ, TEST_UPLOAD_ATTEMPTS=str(marker), TEST_FIRST_UPLOAD_EXIT='3')
    return script, root, marker, env


@pytest.mark.parametrize('failure', ['2', '3'])
def test_batch_continues_after_failed_or_pending_session(batch, failure):
    script, root, marker, env = batch
    env['TEST_FIRST_UPLOAD_EXIT'] = failure
    result = subprocess.run(['/bin/bash', str(script), '--root', str(root)],
        env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 3
    attempted = [json.loads(line) for line in marker.read_text().splitlines()]
    assert [Path(item['root']).name for item in attempted] == ['20261104_160000', '20261104_170000']
    assert len({item['folder'] for item in attempted}) == 1
    assert all(item['bytecode'] for item in attempted)
    assert '1 个会话尚未完成' in result.stderr
    assert '上传结束；' not in result.stdout


def test_operator_interrupt_stops_batch_instead_of_starting_another_session(batch):
    script, root, marker, env = batch
    env['TEST_FIRST_UPLOAD_EXIT'] = '130'
    result = subprocess.run(['/bin/bash', str(script), '--root', str(root)],
        env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 130
    assert len(marker.read_text().splitlines()) == 1


@pytest.mark.parametrize('args', [
    ['--latest', '--session', '20261104_160000'], ['--latest', '--latest'],
    ['--session', '20261104_160000', '--latest'], ['--root'],
    ['--root', '--dry-run'], ['--session', '--dry-run'],
])
def test_invalid_or_ambiguous_scope_never_starts_upload(batch, args):
    script, _root, marker, env = batch
    result = subprocess.run(['/bin/bash', str(script), *args], env=env,
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert not marker.exists()
