"""A broken local ACK must not block unrelated source-copy progress."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from collector.archive_core import atomic_json
from collector.archive_pull import FilePuller
from collector.archive_upload import ArchiveUploadConfig


def local_segment(puller, identity):
    relative = '2026-11-02/雷达/雷达_10:00-10:01/' + identity
    directory = puller.root/'days'/relative
    directory.mkdir(parents=True)
    content = b'verified lidar packet'
    (directory/'radar.db3').write_bytes(content)
    manifest = dict(schemaVersion=1, id=identity, day='2026-11-02', sensor='雷达',
        label='雷达_10:00-10:01', robotId=puller.config.robot_id,
        metadata=dict(scope='garage'), files=['radar.db3'])
    atomic_json(directory/'archive.json', manifest)
    digest = hashlib.sha256((directory/'archive.json').read_bytes()).hexdigest()
    atomic_json(directory/'.source-pull.json', dict(path=relative, manifestSha256=digest))
    ack = dict(state='complete', manifestSha256=digest, segmentId=identity,
        completedAt=datetime.now(timezone.utc).isoformat(), folderPath='雷达/label/'+identity,
        datasetId=str(uuid4()), files=[dict(relativePath='radar.db3', robotFileId=str(uuid4()),
            sha256=hashlib.sha256(content).hexdigest(), size=len(content))])
    atomic_json(directory/'uploaded.json', ack)
    return directory, relative, ack


@pytest.fixture
def puller(tmp_path):
    config = ArchiveUploadConfig('fixture@example.invalid', 'fixture-password', robot_id=str(uuid4()))
    value = FilePuller([], 'unused', tmp_path/'ipc', config, retain_seconds=0)
    value.client._confirmed_file = lambda *args: None
    return value


@pytest.mark.parametrize('fault', ['source-json', 'ack-json', 'done-json', 'manifest-json',
                                  'manifest-mismatch', 'missing-field', 'bad-file-row', 'naive-date',
                                  'read-error', 'missing-checksum', 'source-not-object', 'ack-not-object',
                                  'done-not-object', 'empty-files', 'duplicate-files', 'unknown-file',
                                  'unsafe-file', 'source-path-mismatch', 'segment-id-mismatch'])
def test_bad_local_ack_metadata_does_not_block_other_acks_or_new_pulls(puller, monkeypatch, fault):
    bad, bad_relative, ack = local_segment(puller, 'bad')
    good, good_relative, _ = local_segment(puller, 'good')
    if fault in ('source-json', 'ack-json', 'done-json', 'manifest-json'):
        filename = {'source-json': '.source-pull.json', 'ack-json': 'uploaded.json',
                    'done-json': 'source-ack.json', 'manifest-json': 'archive.json'}[fault]
        (bad/filename).write_text('{broken local metadata')
    elif fault == 'manifest-mismatch':
        ack['manifestSha256'] = '0'*64
        atomic_json(bad/'uploaded.json', ack)
    elif fault == 'missing-field':
        del ack['completedAt']
        atomic_json(bad/'uploaded.json', ack)
    elif fault == 'bad-file-row':
        ack['files'] = [dict(relativePath=['radar.db3'])]
        atomic_json(bad/'uploaded.json', ack)
    elif fault == 'naive-date':
        ack['completedAt'] = '2026-11-02T10:00:00'
        atomic_json(bad/'uploaded.json', ack)
    elif fault == 'missing-checksum':
        del ack['files'][0]['sha256']
        atomic_json(bad/'uploaded.json', ack)
    elif fault in ('source-not-object', 'ack-not-object', 'done-not-object'):
        filename = {'source-not-object': '.source-pull.json', 'ack-not-object': 'uploaded.json',
                    'done-not-object': 'source-ack.json'}[fault]
        (bad/filename).write_text('[]')
    elif fault in ('empty-files', 'duplicate-files', 'unknown-file', 'unsafe-file', 'segment-id-mismatch'):
        if fault == 'empty-files':
            ack['files'] = []
        elif fault == 'duplicate-files':
            ack['files'] = [ack['files'][0], dict(ack['files'][0])]
        elif fault == 'segment-id-mismatch':
            ack['segmentId'] = 'another-segment'
        else:
            ack['files'][0]['relativePath'] = '../radar.db3' if fault == 'unsafe-file' else 'other.db3'
        atomic_json(bad/'uploaded.json', ack)
    elif fault == 'source-path-mismatch':
        descriptor = json.loads((bad/'.source-pull.json').read_text())
        descriptor['path'] = '2026-11-02/雷达/雷达_10:00-10:01/another-segment'
        atomic_json(bad/'.source-pull.json', descriptor)
    else:
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == bad/'.source-pull.json':
                raise PermissionError('fixture private local file')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', read)
    requests, pulled = [], []
    def remote(request):
        requests.append(request)
        if request['op'] == 'ack':
            return dict(acknowledged=True, pruned=request['prune'])
        return [dict(path='next-valid-source-segment')]
    monkeypatch.setattr(puller, '_json', remote)
    monkeypatch.setattr(puller, '_pull', lambda descriptor: pulled.append(descriptor))
    # Deterministic order proves that a failure does not terminate the scan.
    original_rglob = Path.rglob
    monkeypatch.setattr(Path, 'rglob', lambda path, pattern:
        iter([bad/'.source-pull.json', good/'.source-pull.json'])
        if path == puller.root/'days' and pattern == '.source-pull.json'
        else original_rglob(path, pattern))
    puller.once()
    assert [request['segment'] for request in requests if request['op'] == 'ack'] == [good_relative]
    assert requests[-1] == dict(op='list')
    assert pulled == [dict(path='next-valid-source-segment')]
    assert not (bad/'source-ack.json').exists() or fault in ('done-json', 'done-not-object')
    assert (bad/'radar.db3').read_bytes() == b'verified lidar packet'
    assert json.loads((good/'source-ack.json').read_text()) == dict(acknowledged=True, pruned=True)
    assert puller.snapshot()['localAckMetadataErrors'] == 1
    assert puller.snapshot()['lastError'] == 'local_ack_metadata_invalid'
    assert bad_relative not in json.dumps(puller.snapshot())


def test_source_ack_transport_or_verification_failure_still_stops_the_cycle(puller, monkeypatch):
    segment, _, _ = local_segment(puller, 'good')
    requests = []
    def remote(request):
        requests.append(request)
        raise ValueError('source transfer failed')
    monkeypatch.setattr(puller, '_json', remote)
    with pytest.raises(ValueError, match='source transfer failed'):
        puller.once()
    assert [request['op'] for request in requests] == ['ack']
    assert not (segment/'source-ack.json').exists()
    assert (segment/'radar.db3').exists()


def test_run_preserves_fixed_local_ack_error_and_clears_it_after_repair(puller, monkeypatch):
    segment, _, ack = local_segment(puller, 'bad')
    (segment/'uploaded.json').write_text('{broken local metadata')
    class OneCycle:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            self.stopped = True
    puller.stopping = OneCycle()
    monkeypatch.setattr(puller, '_json', lambda request:
        dict(acknowledged=True, pruned=request['prune']) if request['op'] == 'ack' else [])
    puller.run()
    assert puller.snapshot()['localAckMetadataErrors'] == 1
    assert puller.snapshot()['lastError'] == 'local_ack_metadata_invalid'
    atomic_json(segment/'uploaded.json', ack)
    puller.stopping = OneCycle()
    puller.run()
    assert puller.snapshot()['localAckMetadataErrors'] == 0
    assert puller.snapshot()['lastError'] is None


@pytest.mark.parametrize('identity', ['foreign', 'missing', 'invalid', 'noncanonical'])
def test_other_robot_ack_never_reaches_api_or_source_and_later_healthy_ack_continues(puller, monkeypatch, identity):
    bad, bad_relative, bad_ack = local_segment(puller, 'foreign-robot')
    good, good_relative, good_ack = local_segment(puller, 'current-robot')
    manifest = json.loads((bad/'archive.json').read_bytes())
    if identity == 'foreign':
        manifest['robotId'] = str(uuid4())
    elif identity == 'missing':
        manifest.pop('robotId')
    elif identity == 'invalid':
        manifest['robotId'] = 'not-a-robot-uuid'
    else:
        manifest['robotId'] = 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'
    atomic_json(bad/'archive.json', manifest)
    digest = hashlib.sha256((bad/'archive.json').read_bytes()).hexdigest()
    bad_ack['manifestSha256'] = digest
    atomic_json(bad/'uploaded.json', bad_ack)
    atomic_json(bad/'.source-pull.json', dict(path=bad_relative, manifestSha256=digest))
    original_ack = (bad/'uploaded.json').read_bytes()
    original_source = (bad/'.source-pull.json').read_bytes()
    checked, requests = [], []
    monkeypatch.setattr(puller.client, '_confirmed_file', lambda *args: checked.append(args))

    def remote(request):
        requests.append(request)
        return dict(acknowledged=True, pruned=True) if request['op'] == 'ack' else []

    monkeypatch.setattr(puller, '_json', remote)
    original_rglob = Path.rglob
    monkeypatch.setattr(Path, 'rglob', lambda path, pattern:
        iter([bad/'.source-pull.json', good/'.source-pull.json'])
        if path == puller.root/'days' and pattern == '.source-pull.json'
        else original_rglob(path, pattern))
    puller.once()

    assert len(checked) == 1 and checked[0][0] == good_ack['files'][0]['robotFileId']
    assert [request['segment'] for request in requests if request['op'] == 'ack'] == [good_relative]
    assert requests[-1] == dict(op='list')
    assert not (bad/'source-ack.json').exists()
    assert (bad/'uploaded.json').read_bytes() == original_ack
    assert (bad/'.source-pull.json').read_bytes() == original_source
    assert (bad/'radar.db3').read_bytes() == b'verified lidar packet'
    assert json.loads((good/'source-ack.json').read_bytes()) == dict(acknowledged=True, pruned=True)
    assert puller.snapshot()['localAckMetadataErrors'] == 1
    assert puller.snapshot()['lastError'] == 'local_ack_metadata_invalid'
