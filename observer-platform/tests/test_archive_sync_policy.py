"""Sync indexes are explicit small camera derivatives, not arbitrary source caches."""
from copy import deepcopy

import pytest

from collector.archive_policy import (allowed_archive_manifest, sync_manifest_allowed,
                                      training_manifest_allowed)


def manifest():
    return dict(sensor='同步配对', files=['sync-index.jsonl', 'sync-summary.json'],
        metadata=dict(scope='garage', source='native-time-sync', syncKind='software_host_timestamp',
            hardwareSynchronized=False, clockSource='CLOCK_REALTIME',
            clockDomain='linux-boot:11111111-2222-4333-8444-555555555555',
            captureSessionId='a' * 32))


@pytest.mark.parametrize('camera_only', [True, False])
@pytest.mark.parametrize('training_only', [True, False])
def test_software_pairing_indexes_remain_uploadable_with_video_storage_policy(camera_only, training_only):
    value = manifest()
    assert sync_manifest_allowed(value)
    assert training_manifest_allowed(value)
    assert allowed_archive_manifest(value, camera_only, training_only)


@pytest.mark.parametrize('fault', ['source', 'hardware', 'clock', 'session', 'extra_file', 'renamed_cache', 'scope'])
def test_other_payloads_cannot_bypass_training_only_selection_using_sync_label(fault):
    value = deepcopy(manifest())
    if fault == 'source': value['metadata']['source'] = 'native-camera-jpeg'
    elif fault == 'hardware': value['metadata']['hardwareSynchronized'] = True
    elif fault == 'clock': value['metadata']['clockDomain'] = ''
    elif fault == 'session': value['metadata']['captureSessionId'] = '../other'
    elif fault == 'extra_file': value['files'].append('frames.mjpg')
    elif fault == 'renamed_cache': value['files'] = ['video.mkv']
    elif fault == 'scope': value['metadata']['scope'] = 'unrelated'
    assert not sync_manifest_allowed(value)
    assert not training_manifest_allowed(value)
    assert not allowed_archive_manifest(value, True, True)
