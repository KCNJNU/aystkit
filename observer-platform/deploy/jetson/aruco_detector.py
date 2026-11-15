"""Bounded 2-D ArUco detection on existing JPEG bytes; never opens a camera.

The full JPEG is decoded independently of the reduced YOLO image so small
printed markers retain their available pixels. Outputs contain normalized
image corners only, with no pose, TF or control interpretation.
"""
from __future__ import annotations

import math


DICTIONARY = 'DICT_5X5_100'
MAX_MARKERS = 12
MAX_PIXELS = 8_000_000
MAX_EDGE = 4096


def normalized_markers(corners, identifiers, width, height):
    """Validate OpenCV outputs and retain at most twelve complete quadrilaterals."""
    if identifiers is None:
        return []
    markers = []
    for marker_corners, marker_id in zip(corners, identifiers):
        try:
            value = marker_id.item() if hasattr(marker_id, 'item') else marker_id
            marker_id = int(value)
            if marker_id != value or not 0 <= marker_id < 100:
                continue
            points = marker_corners.reshape(4, 2).tolist()
            normalized = []
            for x, y in points:
                if (not math.isfinite(x) or not math.isfinite(y)
                        or not 0 <= x < width or not 0 <= y < height):
                    raise ValueError('invalid marker corner')
                normalized.append([round(x / width, 6), round(y / height, 6)])
            # OpenCV reports a clockwise convex quadrilateral, starting at
            # the marker's top-left corner in its own encoded orientation.
            cross = []
            for index in range(4):
                a, b, c = points[index], points[(index + 1) % 4], points[(index + 2) % 4]
                cross.append((b[0] - a[0]) * (c[1] - b[1])
                             - (b[1] - a[1]) * (c[0] - b[0]))
            if not all(value > 0 for value in cross):
                continue
            markers.append(dict(dictionary=DICTIONARY, id=marker_id, corners=normalized))
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
    markers.sort(key=lambda marker: (marker['id'], marker['corners'][0]))
    return markers[:MAX_MARKERS]


class ArucoDetector:
    """One reusable CPU detector, compatible with old and new OpenCV APIs."""
    def __init__(self):
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        aruco = cv2.aruco
        self.dictionary = aruco.getPredefinedDictionary(aruco.DICT_5X5_100)
        self.parameters = (aruco.DetectorParameters() if hasattr(aruco, 'DetectorParameters')
                           else aruco.DetectorParameters_create())
        self.detector = (aruco.ArucoDetector(self.dictionary, self.parameters)
                         if hasattr(aruco, 'ArucoDetector') else None)

    def detect(self, raw, width, height):
        if (not 1 <= min(width, height) or max(width, height) > MAX_EDGE
                or width * height > MAX_PIXELS):
            raise ValueError('unsupported ArUco image dimensions')
        # The caller has validated JPEG headers before passing these bytes.
        gray = self.cv2.imdecode(self.np.frombuffer(raw, dtype=self.np.uint8),
                                 self.cv2.IMREAD_GRAYSCALE)
        if gray is None or gray.shape != (height, width):
            raise ValueError('ArUco JPEG decode failed or dimensions changed')
        if self.detector is not None:
            corners, identifiers, _ = self.detector.detectMarkers(gray)
        else:
            corners, identifiers, _ = self.cv2.aruco.detectMarkers(
                gray, self.dictionary, parameters=self.parameters)
        return normalized_markers(corners, identifiers, width, height)
