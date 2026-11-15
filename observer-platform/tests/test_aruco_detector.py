"""Synthetic marker coverage; never reads cameras, real frames or image files."""
import importlib.util
from pathlib import Path
import unittest

import cv2
import numpy as np


SPEC = importlib.util.spec_from_file_location('observer_aruco_detector',
    Path(__file__).parents[1] / 'deploy/jetson/aruco_detector.py')
aruco = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aruco)


class ArucoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cv2.setNumThreads(1)
        cls.detector = aruco.ArucoDetector()
        cls.dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_100)

    def marker(self, marker_id, side=84):
        if hasattr(cv2.aruco, 'generateImageMarker'):
            return cv2.aruco.generateImageMarker(self.dictionary, marker_id, side)
        return cv2.aruco.drawMarker(self.dictionary, marker_id, side)

    def encoded(self, frame):
        ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        self.assertTrue(ok)
        return encoded.tobytes()

    def detect(self, frame):
        return self.detector.detect(self.encoded(frame), frame.shape[1], frame.shape[0])

    def test_all_one_hundred_dictionary_ids_including_previous_filtered_ids(self):
        for marker_id in range(100):
            with self.subTest(marker_id=marker_id):
                frame = np.full((140, 160), 255, np.uint8)
                frame[28:112, 38:122] = self.marker(marker_id)
                rows = self.detect(frame)
                self.assertEqual([row['id'] for row in rows], [marker_id])
                self.assertEqual(rows[0]['dictionary'], 'DICT_5X5_100')
                for actual, expected in zip(rows[0]['corners'],
                        [[38/160, 28/140], [121/160, 28/140],
                         [121/160, 111/140], [38/160, 111/140]]):
                    self.assertAlmostEqual(actual[0], expected[0], places=5)
                    self.assertAlmostEqual(actual[1], expected[1], places=5)

    def test_small_source_marker_keeps_full_resolution_detail(self):
        frame = np.full((1944, 2592), 255, np.uint8)
        frame[950:978, 1250:1278] = self.marker(37, 28)
        raw = self.encoded(frame)
        self.assertEqual([row['id'] for row in self.detector.detect(raw, 2592, 1944)], [37])
        reduced = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_4)
        rows = self.detector.detect(self.encoded(reduced), reduced.shape[1], reduced.shape[0])
        self.assertEqual(rows, [])

    def test_rotated_code_preserves_corner_winding_and_id(self):
        frame = np.full((140, 160), 255, np.uint8)
        frame[28:112, 38:122] = np.rot90(self.marker(99))
        rows = self.detect(frame)
        self.assertEqual([row['id'] for row in rows], [99])
        self.assertEqual(len(rows[0]['corners']), 4)

    def test_multiple_codes_same_id_and_output_limit(self):
        frame = np.full((420, 780), 255, np.uint8)
        for index in range(13):
            x, y = 12 + (index % 7) * 108, 12 + (index // 7) * 180
            frame[y:y + 84, x:x + 84] = self.marker(index)
        rows = self.detect(frame)
        self.assertEqual([row['id'] for row in rows], list(range(12)))
        duplicate = np.full((160, 280), 255, np.uint8)
        duplicate[38:122, 16:100] = self.marker(10)
        duplicate[38:122, 172:256] = self.marker(10)
        self.assertEqual([row['id'] for row in self.detect(duplicate)], [10, 10])

    def test_blank_and_other_dictionary_do_not_create_false_markers(self):
        self.assertEqual(self.detect(np.full((300, 480), 255, np.uint8)), [])
        other = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        marker = (cv2.aruco.generateImageMarker(other, 10, 84)
                  if hasattr(cv2.aruco, 'generateImageMarker') else cv2.aruco.drawMarker(other, 10, 84))
        frame = np.full((140, 160), 255, np.uint8)
        frame[28:112, 38:122] = marker
        self.assertEqual(self.detect(frame), [])

    def test_invalid_dimensions_decode_and_corner_outputs_are_rejected(self):
        raw = self.encoded(np.full((48, 64), 255, np.uint8))
        for width, height in ((0, 1), (5000, 5000), (64, 49)):
            with self.subTest(width=width, height=height), self.assertRaises(ValueError):
                self.detector.detect(raw, width, height)
        with self.assertRaises(ValueError):
            self.detector.detect(b'not a JPEG', 64, 48)
        good = np.array([[[0, 0], [20, 0], [20, 20], [0, 20]]], np.float32)
        for points, marker_id in ((good, 100), (good, -1), (good[:, ::-1], 10),
                (np.full((1, 4, 2), np.nan), 10), (np.zeros((1, 4, 2)), 10)):
            self.assertEqual(aruco.normalized_markers([points], np.array([[marker_id]]), 64, 48), [])


if __name__ == '__main__':
    unittest.main()
