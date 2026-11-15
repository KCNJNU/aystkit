"""Explicit visual-only conversion of canonical P100R depth pixels.

This is the existing web preview's fixed inverse TURBO palette, without JPEG
encoding, overlays or frame resizing. The result cannot recover depth values.
"""
from __future__ import annotations

import cv2
import numpy as np

DEPTH_VISUALIZATION_RANGE_MM = (0, 5000)


def pseudocolor_depth16(pixels, width, height):
    if (type(width) is not int or type(height) is not int
            or not 0 < width <= 8192 or not 0 < height <= 8192
            or not isinstance(pixels, bytes) or len(pixels) != width * height * 2):
        raise ValueError('invalid canonical depth image')
    depth = np.frombuffer(pixels, dtype='<u2').reshape(height, width)
    values = depth.astype(np.float32)
    valid = values > 0
    scaled = np.zeros((height, width), dtype=np.uint8)
    scaled[valid] = np.clip(values[valid] * 255 / DEPTH_VISUALIZATION_RANGE_MM[1],
                            0, 255).astype(np.uint8)
    image = cv2.applyColorMap(255 - scaled, cv2.COLORMAP_TURBO)
    image[~valid] = 0
    return image.tobytes()
