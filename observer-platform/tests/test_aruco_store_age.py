"""The new 2D preview's TTL must not change existing docking pose TTLs."""
import copy

import pytest

from cloud.backend.store import ARUCO_PREVIEW_TOPIC, ARUCO_TTL_MS, _age_tree


def preview(age_ms=1200):
    return {'status': 'AVAILABLE', 'topic': ARUCO_PREVIEW_TOPIC, 'ageMs': 100,
            'cameras': {'usbFront': {'status': 'AVAILABLE', 'ageMs': age_ms, 'ids': [10]}}}


def observation():
    marker = preview()
    return {'devices': {'visionDocking': {
        'arucoPreview': marker,
        'topics': {
            'arucoPreview': {'status': 'AVAILABLE', 'topic': ARUCO_PREVIEW_TOPIC,
                             'ageS': .1, 'value': copy.deepcopy(marker)},
            'boardPose': {'status': 'AVAILABLE', 'topic': '/fisheye_aruco/board_pose', 'ageS': 1.2},
            'detectionStatus': {'status': 'AVAILABLE', 'topic': '/fisheye_aruco/detection_status', 'ageS': 1.2},
            'detectedIds': {'status': 'AVAILABLE', 'topic': '/fisheye_aruco/detected_ids', 'ageS': 1.2},
            'dockingState': {'status': 'AVAILABLE', 'topic': '/aruco_docking/state', 'ageS': 1.2}}}},
        'ros2': {'topics': {ARUCO_PREVIEW_TOPIC: {
            'status': 'AVAILABLE', 'topic': ARUCO_PREVIEW_TOPIC, 'ageS': .1,
            'value': copy.deepcopy(marker)}}}}


def marker_rows(value):
    vision = value['devices']['visionDocking']
    return [vision['arucoPreview']['cameras']['usbFront'],
            vision['topics']['arucoPreview']['value']['cameras']['usbFront'],
            value['ros2']['topics'][ARUCO_PREVIEW_TOPIC]['value']['cameras']['usbFront']]


@pytest.mark.parametrize('elapsed,status', [(1.1, 'AVAILABLE'), (3.799, 'AVAILABLE'), (3.8, 'STALE'), (4, 'STALE')])
def test_only_aruco_camera_rows_inherit_the_five_second_boundary(elapsed, status):
    raw = observation()
    aged = _age_tree(raw, elapsed)
    assert ARUCO_TTL_MS == 5000
    for row in marker_rows(aged):
        assert row['ageMs'] == pytest.approx(1200 + elapsed * 1000)
        assert row['status'] == status
    assert raw['devices']['visionDocking']['arucoPreview']['cameras']['usbFront']['ageMs'] == 1200
    for key in ('boardPose', 'detectionStatus', 'detectedIds', 'dockingState'):
        assert aged['devices']['visionDocking']['topics'][key]['status'] == 'STALE'


def test_legacy_pose_stays_available_below_two_seconds_and_expires_after_two():
    raw = observation()
    assert _age_tree(raw, .799)['devices']['visionDocking']['topics']['boardPose']['status'] == 'AVAILABLE'
    assert _age_tree(raw, .801)['devices']['visionDocking']['topics']['boardPose']['status'] == 'STALE'


@pytest.mark.parametrize('path', [
    ('devices', 'visionDocking', 'arucoPreview'),
    ('devices', 'visionDocking', 'topics', 'arucoPreview'),
    ('ros2', 'topics', ARUCO_PREVIEW_TOPIC),
])
def test_fixed_preview_paths_use_five_seconds_even_without_a_redundant_topic_field(path):
    raw = preview()
    raw.pop('topic')
    assert _age_tree(raw, 1.1, path=path)['cameras']['usbFront']['status'] == 'AVAILABLE'
    assert _age_tree(raw, 3.8, path=path)['cameras']['usbFront']['status'] == 'STALE'


def test_identical_key_in_unrelated_data_gets_no_ttl_exemption():
    raw = {'robot': {'arucoPreview': {'status': 'AVAILABLE', 'ageMs': 1200}}}
    assert _age_tree(raw, 1.1)['robot']['arucoPreview']['status'] == 'STALE'


def test_stale_ancestor_or_offline_collector_still_expires_every_marker_row():
    raw = observation()
    assert all(row['status'] == 'STALE' for row in marker_rows(_age_tree(raw, .1, stale=True)))
    raw['devices']['ageS'] = 2
    assert all(row['status'] == 'STALE' for row in marker_rows(_age_tree(raw, .1))[:2])


def test_explicit_collector_stale_state_cannot_be_revived_by_the_longer_source_ttl():
    raw = observation()
    vision = raw['devices']['visionDocking']
    vision['arucoPreview']['status'] = 'STALE'
    vision['arucoPreview']['cameras']['usbFront']['status'] = 'WAITING_UPSTREAM'
    vision['arucoPreview']['cameras']['usbFront']['ids'] = []
    for topic in (vision['topics']['arucoPreview'], raw['ros2']['topics'][ARUCO_PREVIEW_TOPIC]):
        topic['status'], topic['ageS'] = 'STALE', 2.1
        topic['value'] = copy.deepcopy(vision['arucoPreview'])
    aged = _age_tree(raw, .1)
    assert aged['devices']['visionDocking']['arucoPreview']['status'] == 'STALE'
    assert aged['devices']['visionDocking']['topics']['arucoPreview']['status'] == 'STALE'
    assert aged['ros2']['topics'][ARUCO_PREVIEW_TOPIC]['status'] == 'STALE'
    assert all(row['status'] == 'WAITING_UPSTREAM' and row['ids'] == [] for row in marker_rows(aged))


def test_summary_own_age_and_exact_topic_inherit_expiry_through_lists_too():
    raw = preview(age_ms=0)
    raw['ageMs'] = 4900
    raw['extra'] = [{'status': 'AVAILABLE', 'ageMs': 0}]
    aged = _age_tree(raw, .1)
    assert aged['status'] == 'STALE'
    assert aged['cameras']['usbFront']['status'] == 'STALE'
    assert aged['extra'][0]['status'] == 'STALE'
