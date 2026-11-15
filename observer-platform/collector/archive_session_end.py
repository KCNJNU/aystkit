"""Durable session closure proof, separate from immutable video manifests."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID

from .archive_core import GarageGate, atomic_json


def read_json(path, limit=262144):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        return {}
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp must include timezone')
    return parsed.timestamp()


def persist_session_end(control, session_id, robot_id, started_at, *, now,
                        sealed, capture_incomplete, requires_camera=False,
                        stop_requested_at=None):
    if re.fullmatch(r'[a-f0-9]{32}', session_id) is None:
        raise ValueError('invalid capture session')
    robot_id = str(UUID(robot_id)) if robot_id else None
    value = dict(schemaVersion=1, sessionId=session_id, robotId=robot_id,
        startedAt=iso(started_at), reason='desktop_stopped',
        captureIncomplete=bool(capture_incomplete), state='sealed' if sealed else 'pending',
        requiresCamera=bool(requires_camera),
        stopRequestedAt=stop_requested_at if stop_requested_at is not None else now)
    if sealed:
        value['endedAt'] = iso(max(now, started_at))
    path = Path(control)/'session-ends'/(session_id+'.json')
    old = read_json(path)
    # Repeated cleanup must not move an already confirmed end forward.
    if old.get('state') == 'sealed':
        return old
    if old.get('reason') == 'recovered_interruption':
        raise ValueError('recovered interruption requires an explicit sealed proof')
    atomic_json(path, value)
    return value


def drain_confirmed(control, after, now, requires_camera):
    """Promote an offline pending proof only with fresh, empty source queues."""
    control = Path(control)
    if GarageGate(control/'garage.json').enabled():
        return False
    local = read_json(control/'status.json')
    source = read_json(control/'source.json')
    remote = source.get('recorder') or {}
    def fresh(report):
        try:
            stamp = _timestamp(report.get('updatedAt', ''))
        except (ValueError, TypeError, AttributeError):
            return False
        return after <= stamp <= now and now-stamp <= 8
    def empty(report):
        ros, store = report.get('ros') or {}, report.get('store') or {}
        return (store.get('active') == {} and ros.get('active') is False
            and all(type(ros.get(key)) is int and ros[key] == 0 for key in
                ('queueMessages','queueBytes','inFlightMessages','subscriptionCleanupPending')))
    if not (fresh(local) and fresh(source) and fresh(remote) and empty(local) and empty(remote)
            and source.get('connected') is True and source.get('gateAcknowledged') is True
            and source.get('gateEnabled') is False):
        return False
    if requires_camera:
        camera = source.get('camera') or {}
        sink, store = camera.get('sink') or {}, camera.get('store') or {}
        if not (fresh(camera) and camera.get('state') != 'stop_failed'
                and sink.get('enabled') is False and store.get('active') == {}
                and all(type(sink.get(key)) is int and sink[key] == 0 for key in
                    ('queueFrames','queueBytes','inFlightFrames'))):
            return False
    return True


def upload_session_ends(control, root, client, *, manifests=None, clock=None, max_requests=2):
    """Only existing upload ACKs select targets; never create/revive datasets."""
    import time
    from .archive_upload import ArchiveUploadError
    clock = clock or time.time
    control, root = Path(control), Path(root)
    directory = control/'session-ends'
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError('unsafe session end directory')
    targets, legacy_targets = {}, {}
    for path in manifests if manifests is not None else (root/'days').rglob('archive.json'):
        try:
            manifest = read_json(path)
            ack = read_json(Path(path).parent/'uploaded.json')
            if (ack.get('state') != 'complete' or manifest.get('robotId') != client.config.robot_id
                    or ack.get('segmentId') != manifest.get('id')
                    or ack.get('manifestSha256') != hashlib.sha256(Path(path).read_bytes()).hexdigest()):
                continue
            dataset = str(UUID(ack['datasetId']))
            legacy_targets[manifest['id']] = dataset
            token = (manifest.get('metadata') or {}).get('captureSessionId')
            if isinstance(token, str) and re.fullmatch(r'[a-f0-9]{32}', token):
                targets.setdefault(token, set()).add(dataset)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    result = dict(pending=0, acknowledged=0, missing=0, invalid=0, lastError=None)
    # A successfully uploaded rotation segment is not proof of a closed
    # recording. Report absent proofs rather than silently treating them as
    # completed; upload-only callers must never invent a stop time.
    for token in targets:
        marker = directory/(token+'.json')
        if not marker.exists() and not marker.is_symlink():
            result['missing'] += 1
            result['pending'] += 1
    if result['missing']:
        result['lastError'] = 'ARCHIVE_SESSION_END_MISSING'
    if not directory.is_dir():
        return result
    requests = 0
    for path in sorted(directory.glob('*.json')):
        pending_before = result['pending']
        try:
            marker = read_json(path)
            token = marker.get('sessionId')
            if (re.fullmatch(r'[a-f0-9]{32}', token or '') is None or path.stem != token
                    or marker.get('schemaVersion') != 1 or marker.get('robotId') not in (None, client.config.robot_id)
                    or marker.get('reason') not in ('desktop_stopped', 'recovered_interruption')
                    or type(marker.get('captureIncomplete')) is not bool):
                raise ValueError('invalid session end marker')
            segment_ids = marker.get('segmentIds')
            if segment_ids is not None:
                if not isinstance(segment_ids, list) or not segment_ids or not all(
                        isinstance(item, str) and re.fullmatch(r'[a-f0-9]{32}', item) for item in segment_ids):
                    raise ValueError('invalid legacy closure segments')
            recovered = marker['reason'] == 'recovered_interruption'
            if recovered:
                evidence = marker.get('recoveryEvidenceSha256')
                if (marker.get('state') != 'sealed' or marker.get('captureIncomplete') is not True
                        or marker.get('robotId') != client.config.robot_id
                        or segment_ids is None or len(set(segment_ids)) != len(segment_ids)
                        or not isinstance(evidence, str) or re.fullmatch(r'[a-f0-9]{64}', evidence) is None):
                    raise ValueError('invalid recovered interruption proof')
            # Recovered boundaries are backed by retained source evidence; a
            # later empty live queue cannot establish or move those boundaries.
            started = _timestamp(marker['startedAt'])
            if marker.get('state') == 'pending':
                if not drain_confirmed(control, marker['stopRequestedAt'], clock(), marker.get('requiresCamera', False)):
                    result['pending'] += 1
                    continue
                marker.update(state='sealed', endedAt=iso(max(clock(), started)))
                atomic_json(path, marker)
            if marker.get('state') != 'sealed' or _timestamp(marker['endedAt']) < started:
                raise ValueError('unconfirmed session end')
            if marker.get('robotId') is None:
                marker['robotId'] = str(UUID(client.config.robot_id))
                atomic_json(path, marker)
            datasets = set(targets.get(token, ()))
            if segment_ids is not None:
                datasets.update(legacy_targets[item] for item in segment_ids if item in legacy_targets)
            if not datasets:
                result['pending'] += 1
                continue
            body = {key: marker[key] for key in ('robotId','startedAt','endedAt','reason','captureIncomplete')}
            if segment_ids is not None:
                body['segmentIds'] = segment_ids
            if recovered:
                body['recoveryEvidenceSha256'] = marker['recoveryEvidenceSha256']
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            for dataset in sorted(datasets):
                ack_path = directory/'.upload-acks'/token/(dataset+'.json')
                if any(parent.is_symlink() for parent in (directory/'.upload-acks', ack_path.parent)):
                    raise ValueError('unsafe session acknowledgement directory')
                old = read_json(ack_path)
                if old.get('proofSha256') == digest and old.get('state') in ('complete', 'dataset_missing'):
                    result['acknowledged'] += 1
                    continue
                result['pending'] += 1
                if (old.get('proofSha256') == digest and old.get('state') == 'retry'
                        and old.get('retryAt', 0) > clock()):
                    continue
                if requests >= max_requests:
                    continue
                requests += 1
                def retry(code):
                    result['lastError'] = code
                    atomic_json(ack_path, dict(schemaVersion=1, state='retry', proofSha256=digest,
                        retryAt=clock()+30, lastError=code))
                try:
                    client._request(f'/v1/datasets/{dataset}/recording-sessions/{token}/end', 'POST', body)
                    state = 'complete'
                except ArchiveUploadError as error:
                    result['lastError'] = error.code
                    if error.status != 404:
                        retry(error.code)
                        continue
                    # During rolling upgrades a route can be absent while the
                    # dataset still exists. Only a second scoped 404 confirms
                    # deletion; permission/network errors remain retryable.
                    try:
                        client._request(f'/v1/datasets/{dataset}/folders')
                    except ArchiveUploadError as lookup:
                        if lookup.status != 404:
                            retry(lookup.code)
                            continue
                    else:
                        retry('ARCHIVE_SESSION_END_ROUTE_UNAVAILABLE')
                        continue
                    state = 'dataset_missing'
                atomic_json(ack_path, dict(schemaVersion=1, state=state, sessionId=token,
                    datasetId=dataset, proofSha256=digest, acknowledgedAt=iso(clock())))
                result['pending'] -= 1
                result['acknowledged'] += 1
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            result['invalid'] += 1
            if result['pending'] == pending_before:
                result['pending'] += 1
            result['lastError'] = type(error).__name__
    return result
