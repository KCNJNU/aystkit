"""Metadata contract and private-file boundary; never reads a real camera."""
import copy
import json
import math
import os

import pytest

from deploy.scripts.camera_status_gateway import DetectionFile
from shared.detections import (ARUCO_DICTIONARY, DETECTION_KEYS, DETECTION_MAX_BYTES, decode_detections,
                               sanitize_detections, video_session, waiting_detections)

SESSION = '0123456789abcdef0123456789abcdef'
STREAM = 'abcdef0123456789abcdef0123456789'


def detection_fixture():
    value = waiting_detections(SESSION)
    for counter, key in enumerate(DETECTION_KEYS, 1):
        value['cameras'][key] = {'status': 'AVAILABLE', 'results': [{
            'id': SESSION + ':' + str(counter), 'streamSession': STREAM if key != 'color' else None,
            'mediaTime': 12.3 if key != 'color' else None, 'imageId': 'a' * 64 if key == 'color' else None,
            'ageMs': 150.0, 'width': 640, 'height': 400, 'inferenceMs': 35,
            'boxes': [{'classId': 0, 'confidence': .86, 'xyxy': [.1, .2, .5, .9]}]}]}
    return value


def marker_fixture(marker_id=10):
    return {'dictionary': ARUCO_DICTIONARY, 'id': marker_id,
            'corners': [[.1, .2], [.6, .15], [.7, .8], [.2, .9]]}


def test_five_cameras_are_whitelisted_and_each_hop_only_increases_age():
    value = detection_fixture()
    value.update(generatedMonotonic=100.0, password='SECRET')
    value['cameras']['http://SECRET'] = {'url': 'SECRET'}
    value['cameras']['usbFront'].update(error='SECRET', path='/SECRET')
    item = value['cameras']['usbFront']['results'][0]
    item.update(token='SECRET', label='<script>SECRET</script>')
    item['boxes'][0]['name'] = 'SECRET'
    output = sanitize_detections(sanitize_detections(value, 100), 200)
    assert set(output['cameras']) == set(DETECTION_KEYS)
    assert all(row['results'][0]['ageMs'] == 450 for row in output['cameras'].values())
    assert 'SECRET' not in json.dumps(output) and 'generatedMonotonic' not in output
    assert value['cameras']['usbFront']['results'][0]['ageMs'] == 150


def test_no_targets_is_valid_available_with_an_empty_box_list():
    value = detection_fixture()
    value['cameras']['color']['results'][0]['boxes'] = []
    output = sanitize_detections(value)
    assert output['cameras']['color']['status'] == 'AVAILABLE'
    assert output['cameras']['color']['results'][0]['boxes'] == []


def test_old_yolo_only_results_gain_an_empty_marker_list_without_mutating_input():
    value = detection_fixture()
    output = sanitize_detections(value)
    assert all(row['results'][0]['markers'] == [] for row in output['cameras'].values())
    assert all('markers' not in row['results'][0] for row in value['cameras'].values())
    assert sanitize_detections(output) == output


@pytest.mark.parametrize('key', DETECTION_KEYS)
def test_aruco_only_and_mixed_results_preserve_corners_frame_identity_and_age(key):
    value = detection_fixture()
    item = value['cameras'][key]['results'][0]
    item['boxes'] = []
    marker = marker_fixture(99)
    marker.update(label='SECRET', confidence='SECRET', file='/SECRET')
    item['markers'] = [marker]
    output = sanitize_detections(sanitize_detections(value, 125), 250)['cameras'][key]
    assert output['status'] == 'AVAILABLE'
    result = output['results'][0]
    assert result['id'] == item['id']
    assert result['streamSession'] == item['streamSession']
    assert result['mediaTime'] == item['mediaTime']
    assert result['imageId'] == item['imageId']
    assert result['boxes'] == [] and result['markers'] == [marker_fixture(99)]
    assert result['ageMs'] == 525
    assert 'SECRET' not in json.dumps(output)
    result['markers'][0]['corners'][0][0] = .5
    assert marker['corners'][0][0] == .1


def test_marker_orientation_rotations_boundary_coordinates_and_repeated_ids_are_valid():
    value = detection_fixture()
    corners = [[0, 0], [1, 0], [1, 1], [0, 1]]
    markers = []
    for clockwise in (corners, list(reversed(corners))):
        for index in range(4):
            markers.append({'dictionary': ARUCO_DICTIONARY, 'id': 0,
                            'corners': clockwise[index:] + clockwise[:index]})
    value['cameras']['usbFront']['results'][0]['markers'] = markers
    assert sanitize_detections(value)['cameras']['usbFront']['results'][0]['markers'] == markers


@pytest.mark.parametrize('bad', [None, {}, 'SECRET', [marker_fixture()] * 13,
    [None], [{}], [dict(marker_fixture(), dictionary='DICT_4X4_100')],
    [dict(marker_fixture(), dictionary='SECRET')], [dict(marker_fixture(), id=-1)],
    [dict(marker_fixture(), id=100)], [dict(marker_fixture(), id=True)],
    [dict(marker_fixture(), id=1.0)], [dict(marker_fixture(), id='10')],
    [dict(marker_fixture(), corners=None)], [dict(marker_fixture(), corners=[])],
    [dict(marker_fixture(), corners=[[0, 0], [1, 0], [1, 1]])],
    [dict(marker_fixture(), corners=[[0, 0, 0], [1, 0], [1, 1], [0, 1]])],
    [dict(marker_fixture(), corners=[(0, 0), [1, 0], [1, 1], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [True, 0], [1, 1], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [math.nan, 0], [1, 1], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [math.inf, 0], [1, 1], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [10 ** 1000, 0], [1, 1], [0, 1]])],
    [dict(marker_fixture(), corners=[[-.01, 0], [1, 0], [1, 1], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [1.01, 0], [1, 1], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [1, 1], [1, 0], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [1, 0], [.25, .25], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [.5, 0], [1, 0], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [1, 0], [1, 0], [0, 1]])],
    [dict(marker_fixture(), corners=[[0, 0], [1e-6, 0], [1e-6, 1e-6], [0, 1e-6]])],
])
def test_invalid_markers_drop_the_entire_result_instead_of_drawing_partial_detections(bad):
    value = detection_fixture()
    value['cameras']['usbFront']['results'][0]['markers'] = bad
    assert sanitize_detections(value)['cameras']['usbFront'] == {
        'status': 'WAITING_UPSTREAM', 'results': []}


def test_marker_count_limit_and_source_expiry_apply_to_aruco_with_no_yolo_boxes():
    value = detection_fixture()
    item = value['cameras']['color']['results'][0]
    item.update(boxes=[], markers=[marker_fixture(index) for index in range(12)])
    assert len(sanitize_detections(value)['cameras']['color']['results'][0]['markers']) == 12
    assert sanitize_detections(value, 4850)['cameras']['color']['results'] == []
    item['markers'] = []
    assert sanitize_detections(value)['cameras']['color']['status'] == 'AVAILABLE'


@pytest.mark.parametrize('field,bad', [
    ('id', SESSION + ':9007199254740992'), ('id', 'f' * 32 + ':1'), ('id', SESSION + ':01'),
    ('streamSession', 'SECRET'), ('mediaTime', float('nan')), ('mediaTime', -1),
    ('imageId', 'b' * 64), ('ageMs', True), ('ageMs', -1), ('ageMs', float('inf')),
    ('width', 0), ('width', True), ('height', 8193), ('height', 1.5),
    ('inferenceMs', -1), ('inferenceMs', 10001), ('inferenceMs', 10 ** 1000),
    ('boxes', {}), ('boxes', [{}] * 13),
])
def test_malformed_results_cannot_assert_available_or_leak_boxes(field, bad):
    value = detection_fixture()
    value['cameras']['usbFront']['results'][0][field] = bad
    output = sanitize_detections(value)['cameras']['usbFront']
    assert output == {'status': 'WAITING_UPSTREAM', 'results': []}


@pytest.mark.parametrize('field,bad', [('classId', -1), ('classId', 80), ('classId', True),
    ('confidence', float('nan')), ('confidence', 1.01), ('confidence', True),
    ('xyxy', [-.1, 0, .2, .2]), ('xyxy', [.5, .2, .1, .9]),
    ('xyxy', [.1, .9, .5, .2]), ('xyxy', [.1, .2, .1, .9]),
    ('xyxy', [0, 0, 1]), ('xyxy', [0, 0, math.inf, 1])])
def test_invalid_class_confidence_or_box_is_not_sent(field, bad):
    value = detection_fixture()
    value['cameras']['usbFront']['results'][0]['boxes'][0][field] = bad
    assert sanitize_detections(value)['cameras']['usbFront']['results'] == []


@pytest.mark.parametrize('field,bad', [('imageId', 'a' * 63), ('imageId', 'SECRET'),
    ('streamSession', STREAM), ('mediaTime', 0)])
def test_color_only_uses_the_exact_jpeg_digest(field, bad):
    value = detection_fixture()
    value['cameras']['color']['results'][0][field] = bad
    assert sanitize_detections(value)['cameras']['color']['results'] == []


def test_expired_results_are_removed_and_unknown_states_do_not_claim_available():
    value = detection_fixture()
    assert all(row['results'] == [] for row in sanitize_detections(value, 4850)['cameras'].values())
    value['cameras']['usbFront']['status'] = 'ERROR'
    value['cameras']['usbLeft']['status'] = 'SECRET'
    value['cameras']['usbHub2']['results'] *= 11
    output = sanitize_detections(value)
    assert output['cameras']['usbFront'] == {'status': 'ERROR', 'results': []}
    assert output['cameras']['usbLeft']['results'] == []
    assert output['cameras']['usbHub2']['results'] == []


def test_duplicate_ids_or_invalid_root_are_rejected():
    value = detection_fixture()
    value['cameras']['usbLeft']['results'][0]['id'] = SESSION + ':1'
    with pytest.raises(ValueError):
        sanitize_detections(value)
    for bad in (None, [], {}, dict(detection_fixture(), version=True),
                dict(detection_fixture(), model='/SECRET/model.pt'),
                dict(detection_fixture(), session='SECRET')):
        with pytest.raises(ValueError):
            sanitize_detections(bad)


@pytest.mark.parametrize('raw', [b'{"version":1,"version":1}', b'{"x":NaN}', b'{"x":1e999}',
    b'{"x":' + b'1' * 50000 + b'}', b'{"x":1.' + b'1' * 50000 + b'}', b'x' * (DETECTION_MAX_BYTES + 1)])
def test_json_limits_reject_ambiguous_nonfinite_and_huge_input(raw):
    with pytest.raises(ValueError):
        decode_detections(raw)


def private_file(path, generated=100, **changes):
    value = detection_fixture()
    value.update(generatedMonotonic=generated, **changes)
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value


def test_private_file_adds_elapsed_age_without_renewing_stale_file(tmp_path):
    path = tmp_path / 'detections.json'
    private_file(path)
    clock = [100.5]
    reader = DetectionFile(path, lambda: clock[0])
    assert reader.value()['cameras']['color']['results'][0]['ageMs'] == 650
    clock[0] = 101.5
    assert reader.value()['cameras']['color']['results'][0]['ageMs'] == 1650
    clock[0] = 103
    assert reader.value() == waiting_detections()


@pytest.mark.parametrize('generated', [None, True, -1, 101, float('inf'), float('nan'), 'SECRET'])
def test_invalid_or_future_file_timestamp_is_waiting(tmp_path, generated):
    path = tmp_path / 'detections.json'
    private_file(path, generated=generated)
    assert DetectionFile(path, lambda: 100).value() == waiting_detections()


def test_missing_nonprivate_symlink_fifo_directory_and_oversized_files_are_rejected(tmp_path):
    path = tmp_path / 'detections.json'
    reader = DetectionFile(path, lambda: 100)
    assert reader.value() == waiting_detections()
    private_file(path)
    path.chmod(0o640)
    assert reader.value() == waiting_detections()
    target = tmp_path / 'target.json'
    path.rename(target)
    target.chmod(0o600)
    path.symlink_to(target)
    assert reader.value() == waiting_detections()
    path.unlink()
    os.mkfifo(path, 0o600)
    assert reader.value() == waiting_detections()
    path.unlink()
    path.mkdir()
    assert reader.value() == waiting_detections()
    path.rmdir()
    path.write_bytes(b' ' * (DETECTION_MAX_BYTES + 1))
    path.chmod(0o600)
    assert reader.value() == waiting_detections()


@pytest.mark.parametrize('value', [None, 'SECRET', 'a' * 31, 'a' * 33, 'A' * 32,
                                 'abcdef01-2345-6789-abcd-ef0123456789', 'a' * 32 + '\r\nX: bad'])
def test_video_session_header_only_allows_one_canonical_hex_token(value):
    assert video_session(value) is None
    assert video_session(STREAM) == STREAM
