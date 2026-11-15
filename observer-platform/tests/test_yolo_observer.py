"""Read-only sidecar contracts; uses synthetic pixels, no CUDA or ROS required."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import threading
import time
import unittest

import cv2
import numpy as np


SPEC = importlib.util.spec_from_file_location('yolo_observer',
    Path(__file__).parents[1] / 'deploy/jetson/yolo_observer.py')
yolo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(yolo)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        ok, jpeg = cv2.imencode('.jpg', np.zeros((48, 64, 3), np.uint8))
        self.assertTrue(ok)
        self.jpeg = jpeg.tobytes()

    def native(self, sequence='1', session='a' * 32):
        return yolo.native_packet({'X-Video-Session': session,
            'X-Frame-Sequence': sequence, 'X-Video-Media-Time': '123.1',
            'X-Frame-Age-Ms': '100'}, self.jpeg, 20.0, 20.05)

    def test_native_identity_and_capture_age_are_preserved(self):
        packet = self.native()
        self.assertEqual((packet['width'], packet['height']), (64, 48))
        self.assertEqual(packet['token'], ('a' * 32, '1'))
        self.assertEqual(packet['mediaTime'], 123.1)
        self.assertEqual(packet['captured'], 19.9)
        self.assertIsNone(packet['imageId'])
        with self.assertRaises(ValueError):
            self.native(session='untrusted\r\nheader')

    def test_color_is_exact_hash_of_transmitted_bytes_and_deduplicated(self):
        import hashlib
        packet = yolo.color_packet(self.jpeg, 20)
        self.assertEqual(packet['imageId'], hashlib.sha256(self.jpeg).hexdigest())
        frames = yolo.LatestFrames()
        frames.put('color', packet)
        self.assertEqual(frames.take(20.1)[0], 'color')
        frames.put('color', packet)
        self.assertIsNone(frames.take(20.2))
        # A new source frame can legitimately have the exact same JPEG bytes.
        # It must refresh result age even though its browser matching hash is unchanged.
        frames.put('color', yolo.color_packet(self.jpeg, 20.3))
        self.assertEqual(frames.take(20.4)[0], 'color')

    def test_latest_only_fair_scheduling_and_stale_clear(self):
        frames = yolo.LatestFrames()
        for key in yolo.NATIVE:
            frames.put(key, self.native('1'))
            frames.put(key, self.native('2'))
        frames.put('color', yolo.color_packet(self.jpeg, 20))
        seen = [frames.take(20.1)[0] for _ in range(5)]
        self.assertEqual(set(seen), set(yolo.KEYS))
        self.assertEqual(seen[0], 'color')
        self.assertIsNone(frames.take(20.2))
        frames.put('usbFront', self.native('3'))
        self.assertIsNone(frames.take(22))
        self.assertEqual(frames.status('usbFront', 22), 'WAITING_UPSTREAM')

    def test_normalized_boxes_validate_class_bounds_and_nonfinite_values(self):
        boxes = yolo.bounded_boxes([
            [-2, 0, 32, 48, .8, 0], [0, 0, 1, 1, .5, 80],
            [0, 0, float('nan'), 2, .6, 3], [3, 3, 1, 1, .8, 2]], 64, 48)
        self.assertEqual(boxes, [dict(classId=0, confidence=.8, xyxy=[0., 0., .5, 1.])])
        many = yolo.bounded_boxes([[0, 0, 32, 48, .25, 0]] * 24, 64, 48)
        self.assertEqual(len(many), 12)
        self.assertTrue(all(box['confidence'] == .25 for box in many))

    def test_results_empty_boxes_available_and_history_is_bounded_and_ages(self):
        frames = yolo.LatestFrames()
        results = yolo.Results(frames)
        for index in range(15):
            packet = self.native(str(index))
            frames.put('usbFront', packet)
            results.add('usbFront', packet, [], 20)
        snapshot = results.snapshot(20.3)
        row = snapshot['cameras']['usbFront']
        self.assertEqual(row['status'], 'AVAILABLE')
        self.assertEqual(len(row['results']), 10)
        self.assertEqual(row['results'][-1]['boxes'], [])
        self.assertEqual(row['results'][-1]['ageMs'], 400)
        self.assertNotIn('captured', row['results'][-1])
        self.assertNotIn('jpeg', json.dumps(snapshot))
        later = results.snapshot(22)
        self.assertEqual(later['cameras']['usbFront'], {'status': 'WAITING_UPSTREAM', 'results': []})

    def test_new_video_session_drops_old_associations(self):
        frames = yolo.LatestFrames()
        results = yolo.Results(frames)
        for session in ('a' * 32, 'b' * 32):
            packet = self.native(session=session)
            frames.put('usbFront', packet)
            results.add('usbFront', packet, [], 20)
        history = results.snapshot(20.3)['cameras']['usbFront']['results']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['streamSession'], 'b' * 32)

    def test_marker_results_and_latest_only_read_only_ros_snapshot_expire(self):
        frames = yolo.LatestFrames()
        results = yolo.Results(frames)
        packet = self.native()
        frames.put('usbFront', packet)
        markers = [dict(dictionary='DICT_5X5_100', id=37,
                        corners=[[.1, .2], [.3, .2], [.3, .4], [.1, .4]])] * 2
        results.add('usbFront', packet, [], 80, markers)
        private = results.snapshot(20.3)['cameras']['usbFront']['results'][-1]
        self.assertEqual(private['markers'], markers)
        public = results.marker_snapshot(20.3, 1700000000.)
        self.assertEqual(public['timestamp'], 1700000000.)
        row = public['cameras']['usbFront']
        self.assertEqual(row, dict(status='AVAILABLE', ageMs=400.,
                                  dictionary='DICT_5X5_100', ids=[37]))
        self.assertNotIn('corners', json.dumps(public))
        self.assertNotIn('pose', json.dumps(public))
        self.assertNotIn('streamSession', json.dumps(public))
        self.assertLess(len(json.dumps(public).encode()), 8192)
        frames.unavailable('usbFront')
        row = results.marker_snapshot(20.4, 1700000000.)['cameras']['usbFront']
        self.assertEqual(row, dict(status='WAITING_UPSTREAM', ageMs=None,
                                  dictionary='DICT_5X5_100', ids=[]))

    def test_dense_history_stays_within_bound_without_losing_latest_detections(self):
        frames = yolo.LatestFrames()
        results = yolo.Results(frames)
        markers = [dict(dictionary='DICT_5X5_100', id=99,
                        corners=[[.123456, .123456], [.987654, .123456],
                                 [.987654, .987654], [.123456, .987654]])] * 12
        boxes = [dict(classId=79, confidence=.98765,
                      xyxy=[.123456, .123456, .987654, .987654])] * 12
        for key in yolo.KEYS:
            for index in range(10):
                packet = self.native(str(index)) if key != 'color' else yolo.color_packet(self.jpeg, 19.9)
                packet['captured'] = 19.9 + index / 100
                frames.put(key, packet)
                results.add(key, packet, boxes, 123.456, markers)
        snapshot = results.snapshot(20.3)
        raw = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertLessEqual(len(raw), 124_000)
        self.assertLess(sum(len(row['results']) for row in snapshot['cameras'].values()), 50)
        for row in snapshot['cameras'].values():
            self.assertGreaterEqual(len(row['results']), 1)
            self.assertEqual(row['results'][-1]['markers'], markers)
            self.assertEqual(row['results'][-1]['boxes'], boxes)

    def test_marker_workers_bound_five_sources_and_one_pending_per_source(self):
        frames, stop = yolo.LatestFrames(), threading.Event()
        workers = yolo.MarkerWorkers(frames, yolo.Results(frames), stop, lambda: None)
        for key in yolo.KEYS:
            self.assertTrue(workers.submit(key, self.native(), [], 20))
            self.assertFalse(workers.submit(key, self.native('2'), [], 20))
        self.assertEqual(workers.tasks.qsize(), 5)
        self.assertEqual(len(workers.inflight), 5)
        with self.assertRaises(ValueError):
            workers.submit('untrusted_source', self.native(), [], 20)
        stop.set()
        workers.join()
        self.assertEqual(workers.tasks.qsize(), 0)
        self.assertEqual(workers.inflight, set())
        self.assertFalse(workers.submit('usbFront', self.native(), [], 20))

    def test_three_independent_marker_workers_publish_complete_original_frame_results(self):
        frames, stop = yolo.LatestFrames(), threading.Event()
        barrier, finished = threading.Barrier(3), threading.Event()
        lock, calls, detectors = threading.Lock(), [], []
        results = yolo.Results(frames)
        original_add = results.add

        def record_add(*args):
            original_add(*args)
            with lock:
                calls.append(args)
                if len(calls) == 3:
                    finished.set()
        results.add = record_add

        class Detector:
            def __init__(self):
                with lock:
                    detectors.append(self)

            def detect(self, raw, width, height):
                self.asserted = (raw, width, height)
                barrier.wait(timeout=2)
                return [dict(dictionary='DICT_5X5_100', id=37,
                             corners=[[.1, .2], [.3, .2], [.3, .4], [.1, .4]])]

        workers = yolo.MarkerWorkers(frames, results, stop, Detector, clock=lambda: 20.2)
        workers.start()
        try:
            for key in ('usbFront', 'usbLeft', 'usbHub3'):
                packet = self.native()
                frames.put(key, packet)
                self.assertTrue(workers.submit(key, packet, [{'classId': 0}], 20))
            self.assertTrue(finished.wait(3), 'all three bounded workers must finish')
            workers.tasks.join()
            self.assertEqual(len(detectors), 3)
            self.assertEqual(len({id(detector) for detector in detectors}), 3)
            for detector in detectors:
                self.assertEqual(detector.asserted, (self.jpeg, 64, 48))
            snapshot = results.snapshot(20.3)
            for key in ('usbFront', 'usbLeft', 'usbHub3'):
                row = snapshot['cameras'][key]['results'][-1]
                self.assertEqual(row['streamSession'], 'a' * 32)
                self.assertEqual(row['mediaTime'], 123.1)
                self.assertEqual(row['boxes'], [{'classId': 0}])
                self.assertEqual(row['markers'][0]['id'], 37)
                self.assertEqual(row['inferenceMs'], 200.)
        finally:
            stop.set()
            workers.join()

    def test_marker_workers_drop_expired_frames_and_clear_errors(self):
        for expired, error in ((True, False), (False, True)):
            with self.subTest(expired=expired, error=error):
                entered, stop = threading.Event(), threading.Event()
                frames = yolo.LatestFrames()
                results = yolo.Results(frames)

                class Detector:
                    def detect(self, *args):
                        entered.set()
                        if error:
                            raise ValueError('synthetic decode error')
                        return []

                workers = yolo.MarkerWorkers(frames, results, stop, Detector,
                                             clock=lambda: 22. if expired else 20.2)
                workers.start()
                try:
                    packet = self.native()
                    frames.put('usbFront', packet)
                    workers.submit('usbFront', packet, [], 20)
                    workers.tasks.join()
                    self.assertFalse(workers.busy('usbFront'))
                    self.assertEqual(len(results.history['usbFront']), 0)
                    self.assertEqual(entered.is_set(), not expired)
                    if error:
                        self.assertEqual(frames.status('usbFront', 20.2), 'ERROR')
                finally:
                    stop.set()
                    workers.join()

    def test_atomic_private_json_and_model_metadata_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'detections.json'
            yolo.write_atomic(path, {'version': 1, 'cameras': {}})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text())['version'], 1)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            engine = Path(directory) / 'test.engine'
            metadata = json.dumps(dict(task='detect', batch=1, imgsz=[640, 640],
                                       names={str(i): 'label' for i in range(80)})).encode()
            engine.write_bytes(struct.pack('<I', len(metadata)) + metadata + b'engine')
            yolo.validate_engine(engine)
            with self.assertRaises(ValueError):
                yolo.validate_engine(Path(directory) / 'missing.engine')

    def test_invalid_jpeg_cannot_allocate_arbitrary_pixels(self):
        for invalid in (b'bad', b'\xff\xd8' + b'a' * 40 + b'\xff\xd9'):
            with self.assertRaises(ValueError):
                yolo.jpeg_dimensions(invalid)


if __name__ == '__main__':
    unittest.main()
