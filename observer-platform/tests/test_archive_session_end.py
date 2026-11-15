"""Closure proof uses temporary metadata and a fake API, never real IO services."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from collector.archive_core import atomic_json
from collector.archive_session_end import drain_confirmed, persist_session_end, upload_session_ends, iso
from collector.archive_upload import ArchiveUploadError

ROBOT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
DATASET = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
SESSION = 'c'*32
SEGMENT = 'd'*32


class Client:
    config = SimpleNamespace(robot_id=ROBOT)
    def __init__(self, failure=None, folders_exist=True):
        self.calls, self.failure, self.folders_exist = [], failure, folders_exist
    def _request(self, path, method='GET', payload=None):
        self.calls.append((path,method,payload))
        if method == 'POST' and self.failure:
            raise ArchiveUploadError('FIXTURE_FAILURE', self.failure)
        if method == 'GET' and not self.folders_exist:
            raise ArchiveUploadError('FIXTURE_LOOKUP',404)
        return {}


@pytest.fixture
def recorded(tmp_path):
    control, root = tmp_path/'control', tmp_path/'spool'
    path = root/'days'/'2026-11-02'/'camera'/'label'/SEGMENT
    path.mkdir(parents=True)
    manifest = dict(id=SEGMENT, robotId=ROBOT, metadata=dict(
        captureSessionId=SESSION, closeReason='rotation'))
    atomic_json(path/'archive.json',manifest)
    digest = hashlib.sha256((path/'archive.json').read_bytes()).hexdigest()
    atomic_json(path/'uploaded.json',dict(state='complete',segmentId=SEGMENT,
        datasetId=DATASET,manifestSha256=digest))
    return control, root, path


def mark(control, *, sealed=True, session=SESSION):
    return persist_session_end(control,session,ROBOT,100,now=120,
        stop_requested_at=110,sealed=sealed,capture_incomplete=True)


def test_sealed_proof_uses_drain_confirmation_time_not_stop_request(recorded):
    control, _, _ = recorded
    value = mark(control)
    assert value['endedAt'] == iso(120) and value['stopRequestedAt'] == 110
    assert value['captureIncomplete'] is True
    assert mark(control) == value
    assert persist_session_end(control,SESSION,ROBOT,100,now=140,sealed=True,
        capture_incomplete=False) == value


def test_uploaded_rotation_manifest_and_video_ack_never_change(recorded):
    control,root,path = recorded
    before = {name:(path/name).read_bytes() for name in ('archive.json','uploaded.json')}
    mark(control);client=Client()
    result=upload_session_ends(control,root,client,clock=lambda:125)
    assert result['acknowledged']==1 and result['pending']==0
    assert client.calls==[(f'/v1/datasets/{DATASET}/recording-sessions/{SESSION}/end','POST',dict(
        robotId=ROBOT,startedAt=iso(100),endedAt=iso(120),reason='desktop_stopped',captureIncomplete=True))]
    assert before == {name:(path/name).read_bytes() for name in before}
    upload_session_ends(control,root,client,clock=lambda:130)
    assert len(client.calls)==1


def test_network_failure_retains_marker_and_retries_independently(recorded):
    control,root,_=recorded;mark(control);client=Client(failure=503)
    assert upload_session_ends(control,root,client,clock=lambda:125)['pending']==1
    assert (control/'session-ends'/(SESSION+'.json')).is_file()
    client.failure=None
    assert upload_session_ends(control,root,client,clock=lambda:130)['pending']==1
    assert upload_session_ends(control,root,client,clock=lambda:160)['acknowledged']==1


@pytest.mark.parametrize('folders_exist',[False,True])
def test_post_404_checks_dataset_before_suppressing_retry(recorded,folders_exist):
    control,root,_=recorded;mark(control);client=Client(failure=404,folders_exist=folders_exist)
    result=upload_session_ends(control,root,client,clock=lambda:125)
    assert client.calls[-1][:2]==(f'/v1/datasets/{DATASET}/folders','GET')
    assert result['acknowledged']==(0 if folders_exist else 1)
    assert result['pending']==(1 if folders_exist else 0)
    client.failure=None
    upload_session_ends(control,root,client,clock=lambda:160)
    assert len(client.calls)==(3 if folders_exist else 2)


def test_no_acked_segment_never_creates_or_revives_dataset(recorded):
    control,root,path=recorded;mark(control);(path/'uploaded.json').unlink()
    client=Client();result=upload_session_ends(control,root,client)
    assert not client.calls and result['pending']==1


def test_ack_manifest_mismatch_never_selects_a_target(recorded):
    control,root,path=recorded;mark(control)
    value=json.loads((path/'archive.json').read_text());value['metadata']['closeReason']='shutdown'
    atomic_json(path/'archive.json',value)
    client=Client();assert upload_session_ends(control,root,client)['pending']==1
    assert not client.calls


def test_acked_session_with_absent_proof_is_not_reported_closed(recorded):
    control,root,_=recorded
    client=Client();result=upload_session_ends(control,root,client)
    assert result['missing']==1 and result['pending']==1
    assert result['lastError']=='ARCHIVE_SESSION_END_MISSING'
    assert not client.calls and not control.exists()


@pytest.mark.parametrize('payload',['{broken','[]','{}'])
def test_unreadable_or_invalid_proof_remains_pending_without_replacement(recorded,payload):
    control,root,_=recorded
    path=control/'session-ends'/(SESSION+'.json')
    path.parent.mkdir(parents=True);path.write_text(payload)
    before=path.read_bytes();client=Client()
    result=upload_session_ends(control,root,client)
    assert result['pending']==1 and result['invalid']==1 and result['missing']==0
    assert result['lastError'] and not client.calls
    assert path.read_bytes()==before


def test_legacy_upload_without_capture_token_does_not_imply_missing_proof(recorded):
    control,root,path=recorded
    value=json.loads((path/'archive.json').read_text());value['metadata'].pop('captureSessionId')
    atomic_json(path/'archive.json',value)
    ack=json.loads((path/'uploaded.json').read_text())
    ack['manifestSha256']=hashlib.sha256((path/'archive.json').read_bytes()).hexdigest()
    atomic_json(path/'uploaded.json',ack)
    result=upload_session_ends(control,root,Client())
    assert result['pending']==result['missing']==result['invalid']==0


def test_invalid_legacy_proof_is_not_rewritten_during_robot_migration(recorded):
    control,root,_=recorded;mark(control)
    path=control/'session-ends'/(SESSION+'.json')
    value=json.loads(path.read_text());value.update(robotId=None,segmentIds=[])
    atomic_json(path,value);before=path.read_bytes()
    result=upload_session_ends(control,root,Client())
    assert result['pending']==result['invalid']==1
    assert path.read_bytes()==before


def test_unconfirmed_stop_stays_pending_until_fresh_queues_are_empty(recorded):
    control,root,_=recorded;mark(control,sealed=False);client=Client()
    assert upload_session_ends(control,root,client,clock=lambda:125)['pending']==1
    assert not client.calls
    empty=dict(updatedAt=iso(126),ros=dict(active=False,queueMessages=0,queueBytes=0,
        inFlightMessages=0,subscriptionCleanupPending=0),store=dict(active={}))
    atomic_json(control/'status.json',empty)
    atomic_json(control/'source.json',dict(updatedAt=iso(126),connected=True,
        gateAcknowledged=True,gateEnabled=False,recorder=empty))
    assert upload_session_ends(control,root,client,clock=lambda:127)['acknowledged']==1
    proof=json.loads((control/'session-ends'/(SESSION+'.json')).read_text())
    assert proof['endedAt']==iso(127)


@pytest.mark.parametrize('enabled', [True, None, 0, 'false'])
def test_pending_usb_closure_requires_admission_closed_even_when_queues_are_empty(
        tmp_path, enabled):
    empty = dict(updatedAt=iso(126), ros=dict(active=False, queueMessages=0,
        queueBytes=0, inFlightMessages=0, subscriptionCleanupPending=0), store=dict(active={}))
    atomic_json(tmp_path/'status.json', empty)
    source = dict(updatedAt=iso(126), connected=True, gateAcknowledged=True,
        gateEnabled=False, recorder=empty, camera=dict(updatedAt=iso(126), state='running',
            store=dict(active={}), sink=dict(enabled=enabled, writerRunning=True,
                queueFrames=0, queueBytes=0, inFlightFrames=0)))
    atomic_json(tmp_path/'source.json', source)
    assert not drain_confirmed(tmp_path, 110, 127, requires_camera=True)
    source['camera']['sink']['enabled'] = False
    atomic_json(tmp_path/'source.json', source)
    assert drain_confirmed(tmp_path, 110, 127, requires_camera=True)


def test_pending_proof_never_promotes_with_stale_or_nonempty_status(recorded):
    control,root,_=recorded;mark(control,sealed=False);client=Client()
    empty=dict(updatedAt=iso(120),ros=dict(active=False,queueMessages=0,queueBytes=0,
        inFlightMessages=0,subscriptionCleanupPending=0),store=dict(active={}))
    atomic_json(control/'status.json',empty)
    atomic_json(control/'source.json',dict(updatedAt=iso(120),connected=True,
        gateAcknowledged=True,gateEnabled=False,recorder=empty))
    assert upload_session_ends(control,root,client,clock=lambda:150)['pending']==1
    assert not client.calls


def test_legacy_explicit_segment_proof_does_not_need_manifest_rewrite(recorded):
    control,root,path=recorded
    manifest=json.loads((path/'archive.json').read_text());manifest['metadata'].pop('captureSessionId')
    atomic_json(path/'archive.json',manifest)
    ack=json.loads((path/'uploaded.json').read_text());ack['manifestSha256']=hashlib.sha256((path/'archive.json').read_bytes()).hexdigest()
    atomic_json(path/'uploaded.json',ack);mark(control)
    marker=control/'session-ends'/(SESSION+'.json');value=json.loads(marker.read_text())
    value['segmentIds']=[SEGMENT];atomic_json(marker,value)
    client=Client();assert upload_session_ends(control,root,client)['acknowledged']==1
    assert client.calls[0][2]['segmentIds']==[SEGMENT]


def recovered_marker(control, **updates):
    mark(control)
    path=control/'session-ends'/(SESSION+'.json')
    marker=json.loads(path.read_text())
    marker.update(reason='recovered_interruption',segmentIds=[SEGMENT],
        recoveryEvidenceSha256='e'*64)
    marker.update(updates);atomic_json(path,marker)
    return path


def test_explicit_recovered_interruption_uploads_evidence_and_retains_source_boundaries(recorded):
    control,root,path=recorded;marker=recovered_marker(control)
    before=marker.read_bytes()
    retained={name:(path/name).read_bytes() for name in ('archive.json','uploaded.json')}
    client=Client();result=upload_session_ends(control,root,client,clock=lambda:999)
    assert result['acknowledged']==1 and result['pending']==0
    assert client.calls[0]==(f'/v1/datasets/{DATASET}/recording-sessions/{SESSION}/end','POST',dict(
        robotId=ROBOT,startedAt=iso(100),endedAt=iso(120),reason='recovered_interruption',
        captureIncomplete=True,segmentIds=[SEGMENT],recoveryEvidenceSha256='e'*64))
    assert marker.read_bytes()==before
    assert retained=={name:(path/name).read_bytes() for name in retained}
    upload_session_ends(control,root,client,clock=lambda:1100)
    assert len(client.calls)==1


@pytest.mark.parametrize('updates',[
    dict(state='pending'),dict(state=None),dict(captureIncomplete=False),
    dict(captureIncomplete=1),dict(captureIncomplete=None),
    dict(segmentIds=None),dict(segmentIds=[]),dict(segmentIds=[SEGMENT,SEGMENT]),
    dict(segmentIds=['invalid']),dict(segmentIds=[SEGMENT.upper()]),
    dict(recoveryEvidenceSha256=None),dict(recoveryEvidenceSha256='e'*63),
    dict(recoveryEvidenceSha256='g'*64),dict(recoveryEvidenceSha256='E'*64),
    dict(robotId=None),dict(robotId=DATASET),
    dict(startedAt=iso(130)),dict(startedAt='2026-11-04T16:00:00'),
    dict(endedAt='invalid'),
])
def test_invalid_recovered_proof_cannot_upload_promote_or_rewrite(recorded,monkeypatch,updates):
    from collector import archive_session_end
    control,root,_=recorded;marker=recovered_marker(control,**updates)
    before=marker.read_bytes()
    monkeypatch.setattr(archive_session_end,'drain_confirmed',
        lambda *_a,**_k:pytest.fail('recovered proof must never use current live drain status'))
    client=Client();result=upload_session_ends(control,root,client,clock=lambda:999)
    assert result['invalid']==1 and result['pending']==1
    assert not client.calls and marker.read_bytes()==before


def test_recovered_proof_evidence_participates_in_ack_digest(recorded):
    control,root,_=recorded;marker=recovered_marker(control)
    client=Client();upload_session_ends(control,root,client,clock=lambda:125)
    first=json.loads((control/'session-ends/.upload-acks'/SESSION/(DATASET+'.json')).read_text())
    value=json.loads(marker.read_text());value['recoveryEvidenceSha256']='f'*64
    atomic_json(marker,value)
    result=upload_session_ends(control,root,client,clock=lambda:130)
    second=json.loads((control/'session-ends/.upload-acks'/SESSION/(DATASET+'.json')).read_text())
    assert result['acknowledged']==1 and len(client.calls)==2
    assert first['proofSha256'] != second['proofSha256']
    assert client.calls[1][2]['recoveryEvidenceSha256']=='f'*64


def test_normal_desktop_persistence_cannot_generate_or_promote_recovered_proof(recorded):
    control,_,_=recorded;marker=recovered_marker(control,state='pending')
    before=marker.read_bytes()
    with pytest.raises(ValueError,match='explicit sealed proof'):
        mark(control)
    assert marker.read_bytes()==before


def test_desktop_legacy_segments_keep_existing_duplicate_id_compatibility(recorded):
    control,root,_=recorded;mark(control)
    marker=control/'session-ends'/(SESSION+'.json');value=json.loads(marker.read_text())
    value['segmentIds']=[SEGMENT,SEGMENT];atomic_json(marker,value)
    client=Client();result=upload_session_ends(control,root,client)
    assert result['acknowledged']==1 and result['invalid']==0
    assert client.calls[0][2]['segmentIds']==[SEGMENT,SEGMENT]


def test_capture_session_marks_loss_incomplete_and_uses_confirmed_time(tmp_path):
    from collector.archive_capture_session import CaptureSession
    session=CaptureSession(tmp_path,clock=lambda:150)
    session.capture_started_at=100
    atomic_json(session.marker,dict(token=session.token,stoppedAt=110))
    atomic_json(tmp_path/'source.json',dict(camera=dict(sink=dict(counters=dict(writerDropped=5)))))
    value=session.persist_session_end(dict(ARCHIVE_ROBOT_ID=ROBOT),sealed=True,incomplete=False)
    assert value['endedAt']==iso(150) and value['captureIncomplete'] is True


def test_slow_closure_api_does_not_block_the_video_upload_lane(recorded, monkeypatch):
    import threading
    from collector import archive_main, archive_retention, archive_session_end
    control,root,path=recorded
    (path/'uploaded.json').unlink()
    entered,release=threading.Event(),threading.Event()
    clients=[]
    class UploadClient:
        def __init__(self, _config):
            self.api=SimpleNamespace(timeout=120)
            clients.append(self)
        def upload_segment(self, segment):
            assert entered.wait(2), 'closure worker did not start'
            assert not release.is_set(), 'closure was not blocked during upload'
            return dict(state='complete')
    def slow_proof(*_args,**_kwargs):
        entered.set()
        assert release.wait(3)
        return dict(pending=0,acknowledged=1,lastError=None)
    class OnePass:
        done=False
        def is_set(self):return self.done
        def wait(self,_seconds):self.done=True;return True
    monkeypatch.setattr(archive_main,'ArchiveUploadClient',UploadClient)
    monkeypatch.setattr(archive_session_end,'upload_session_ends',slow_proof)
    monkeypatch.setattr(archive_retention,'prune_confirmed_copy',lambda *_a,**_k:False)
    worker=archive_main.UploadWorker(root,None,OnePass(),control=control)
    try:
        worker.run()
        assert worker.snapshot()['completedSegments']==1
        assert len(clients)==2 and clients[0] is not clients[1]
        assert clients[1].api.timeout==10
    finally:
        release.set()
