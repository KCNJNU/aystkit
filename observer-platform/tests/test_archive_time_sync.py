"""Host timestamp matching is deterministic metadata processing only."""
import copy
import random

import pytest

from collector.archive_time_sync import SyncError, iter_pairs, pair_frames

EPOCH = 1_799_999_999_000_000_000


def frame(index, offset=0, *, camera='camera1', segment='segment-1', **extra):
    return dict(robotId='robot-1', captureSessionId='capture-1', clockDomain='jetson:boot-1',
        cameraId=camera, segmentId=segment, frameIndex=index, timestampNs=EPOCH+offset,
        videoPtsUs=index*33333, **extra)


def test_matches_nearest_original_frames_and_keeps_exact_integer_epoch():
    rgb = [frame(0, 12, sequence=17), frame(1, 44, sequence=18)]
    depth = [frame(0, 5, camera='p100r', sourceHeaderStamp=dict(sec=1799999999, nanosec=5)),
             frame(1, 40, camera='p100r', sourceHeaderStamp=dict(sec=1799999999, nanosec=40))]
    original = copy.deepcopy((rgb, depth))
    pairs = pair_frames(rgb, depth, tolerance_ns=10)
    assert [row['depthIndex'] for row in pairs] == [0, 1]
    assert [row['deltaNs'] for row in pairs] == [7, 4]
    assert [row['signedDeltaNs'] for row in pairs] == [-7, -4]
    assert pairs[0]['rgb']['timestampNs'] == EPOCH+12
    assert pairs[0]['depth']['sourceHeaderStamp'] == depth[0]['sourceHeaderStamp']
    assert all(row['state'] == 'matched' and row['syncKind'] == 'software_host_timestamp'
               and row['hardwareSynchronized'] is False and row['toleranceNs'] == 10 for row in pairs)
    pairs[0]['rgb']['sequence'] = -1
    pairs[0]['depth']['sourceHeaderStamp']['nanosec'] = -1
    assert (rgb, depth) == original


def test_equal_distance_selects_earlier_and_duplicate_timestamp_first_input():
    depth = [frame(0, 10, camera='p100r'), frame(1, 10, camera='p100r'), frame(2, 20, camera='p100r')]
    pairs = pair_frames([frame(0, 15), frame(1, 20)], depth, tolerance_ns=5)
    assert [row['depthIndex'] for row in pairs] == [0, 2]
    assert [row['signedDeltaNs'] for row in pairs] == [-5, 0]


def test_before_after_and_outside_tolerance_preserve_every_rgb():
    rgb = [frame(0, -11), frame(1, -10), frame(2, 10), frame(3, 11)]
    pairs = pair_frames(rgb, [frame(0, 0, camera='p100r')], tolerance_ns=10)
    assert [p['state'] for p in pairs] == ['unmatched', 'matched', 'matched', 'unmatched']
    for pair in (pairs[0], pairs[-1]):
        assert pair['depth'] is None and pair['depthIndex'] is None
        assert pair['deltaNs'] is None and pair['signedDeltaNs'] is None
    assert [p['rgb'] for p in pairs] == rgb


def test_empty_depth_and_zero_tolerance_are_valid_real_no_match():
    assert pair_frames([], []) == []
    rgb = [frame(0)]
    assert pair_frames(rgb, [])[0]['state'] == 'unmatched'
    assert pair_frames(rgb, [frame(0, 1, camera='p100r')], tolerance_ns=0)[0]['state'] == 'unmatched'
    assert pair_frames(rgb, [frame(0, 0, camera='p100r')], tolerance_ns=0)[0]['state'] == 'matched'


def test_all_four_grouped_cameras_can_reference_one_original_depth():
    rgb = [frame(0, 15, camera=f'camera{i}') for i in range(1, 5)]
    pairs = pair_frames(rgb, [frame(0, 10, camera='p100r')])
    assert len(pairs) == 4 and all(p['depthIndex'] == 0 for p in pairs)
    assert len({p['rgb']['cameraId'] for p in pairs}) == 4
    assert all(p['depthReusedWithinCamera'] is False for p in pairs)
    assert all(p['depthReusePolicy'] == 'nearest_reuse_allowed' for p in pairs)


def test_same_camera_repeated_depth_references_are_explicit_without_copying_frames():
    rgb = [frame(index, offset) for index, offset in enumerate((-1, 0, 1, 19, 20))]
    depth = [frame(0, camera='p100r'), frame(1, 20, camera='p100r')]
    pairs = pair_frames(rgb, depth, tolerance_ns=2)
    assert [p['depthIndex'] for p in pairs] == [0, 0, 0, 1, 1]
    assert [p['depthReusedWithinCamera'] for p in pairs] == [False, True, True, False, True]
    assert all(p['depthReusePolicy'] == 'nearest_reuse_allowed' for p in pairs)
    assert all(p['depth'] == depth[p['depthIndex']] for p in pairs)
    assert len(depth) == 2 and len(pairs) == len(rgb)


def test_reuse_is_tracked_per_camera_for_interleaved_rgb_sources():
    rgb = [frame(0, 0, camera='camera1'), frame(0, 0, camera='camera2'),
           frame(1, 1, camera='camera1'), frame(1, 1, camera='camera2')]
    pairs = pair_frames(rgb, [frame(0, camera='p100r')], tolerance_ns=2)
    assert [p['depthReusedWithinCamera'] for p in pairs] == [False, False, True, True]
    assert all(p['depthIndex'] == 0 for p in pairs)


def test_unmatched_rows_do_not_consume_depth_and_reuse_persists_across_rgb_segments():
    rgb = [frame(0, -10), frame(1, 0), frame(0, 1, segment='next-segment'), frame(1, 10, segment='next-segment')]
    pairs = pair_frames(rgb, [frame(0, camera='p100r')], tolerance_ns=2)
    assert [p['state'] for p in pairs] == ['unmatched', 'matched', 'matched', 'unmatched']
    assert [p['depthReusedWithinCamera'] for p in pairs] == [False, False, True, False]
    assert all(p['depthReusePolicy'] == 'nearest_reuse_allowed' for p in pairs)


def test_new_segment_can_reset_frame_index_but_cannot_reset_time():
    rgb = [frame(3, 10), frame(0, 11, segment='next-segment')]
    assert len(pair_frames(rgb, [frame(0, 10, camera='p100r')])) == 2


@pytest.mark.parametrize('key,value', [('robotId', 'robot-2'), ('captureSessionId', 'other-capture'),
    ('clockDomain', 'jetson:other-boot'), ('clockDomain', 'ipc:same-boot'),
    ('clockDomain', ''), ('robotId', None), ('captureSessionId', '  '), ('cameraId', ''),
    ('segmentId', ''), ('timestampNs', None), ('timestampNs', '1799999999000000000'),
    ('timestampNs', 1.8e18), ('timestampNs', True), ('timestampNs', 0),
    ('timestampNs', float('nan')), ('frameIndex', -1), ('frameIndex', True),
    ('videoPtsUs', None), ('videoPtsUs', -1), ('sequence', -1)])
def test_rejects_invalid_rows_or_cross_identity(key, value):
    rgb = frame(0)
    rgb[key] = value
    with pytest.raises(SyncError):
        pair_frames([rgb], [frame(0, camera='p100r')])


@pytest.mark.parametrize('key', ['robotId', 'captureSessionId', 'clockDomain', 'cameraId',
    'segmentId', 'frameIndex', 'timestampNs', 'videoPtsUs'])
def test_missing_required_field_is_never_invented(key):
    row = frame(0)
    del row[key]
    with pytest.raises(SyncError):
        pair_frames([row], [])


@pytest.mark.parametrize('kind', ['rgb', 'depth'])
def test_non_monotonic_time_raises_instead_of_sorting(kind):
    rows = [frame(0, 20), frame(1, 10)]
    with pytest.raises(SyncError, match='monotonic'):
        pair_frames(rows if kind == 'rgb' else [], rows if kind == 'depth' else [])


@pytest.mark.parametrize('kind', ['rgb', 'depth'])
def test_duplicate_frame_reference_is_rejected(kind):
    rows = [frame(0), frame(0)]
    with pytest.raises(SyncError, match='duplicate'):
        pair_frames(rows if kind == 'rgb' else [], rows if kind == 'depth' else [])


def test_identity_is_consistent_even_without_depth():
    first, second = frame(0), frame(1, 1)
    second['captureSessionId'] = 'different'
    with pytest.raises(SyncError, match='mismatch'):
        pair_frames([first, second], [])


def test_depth_filtering_must_select_one_camera_and_finite_json():
    with pytest.raises(SyncError, match='one selected depth'):
        pair_frames([], [frame(0, camera='p100r'), frame(1, 1, camera='other-depth')])
    with pytest.raises(SyncError, match='finite JSON'):
        pair_frames([frame(0, sourceHeaderStamp=dict(nanosec=float('inf')))], [])


@pytest.mark.parametrize('value', [-1, True, 30e6, None])
def test_tolerance_requires_explicit_nonnegative_integer(value):
    with pytest.raises(SyncError, match='tolerance'):
        pair_frames([], [], tolerance_ns=value)


def test_streaming_rows_yield_without_consuming_all_rgb_and_do_not_modify_inputs():
    consumed = []
    def source():
        for index in range(100):
            consumed.append(index)
            yield frame(index, index*10)
    rows = iter_pairs(source(), [frame(0, camera='p100r')], tolerance_ns=5)
    assert next(rows)['state'] == 'matched' and consumed == [0]
    assert next(rows)['state'] == 'unmatched' and consumed == [0, 1]


def test_binary_search_matches_integer_brute_force_for_varied_inputs():
    rng = random.Random(67)
    depth_offsets = sorted(rng.randrange(10000) for _ in range(700))
    rgb_offsets = sorted(rng.randrange(-100, 10100) for _ in range(350))
    depth = [frame(i, value, camera='p100r') for i, value in enumerate(depth_offsets)]
    rgb = [frame(i, value) for i, value in enumerate(rgb_offsets)]
    tolerance = 30
    pairs = pair_frames(rgb, depth, tolerance_ns=tolerance)
    for pair, offset in zip(pairs, rgb_offsets):
        index = min(range(len(depth)), key=lambda i: (abs(depth_offsets[i]-offset), depth_offsets[i], i))
        expected = index if abs(depth_offsets[index]-offset) <= tolerance else None
        assert pair['depthIndex'] == expected
