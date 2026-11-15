"""Bounded ROS Image conversion. No device access and no synthetic frames."""
import cv2
import numpy as np


def image_to_jpeg(message, depth=False):
    width, height, step = int(message.width), int(message.height), int(message.step)
    if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= 8_000_000):
        raise ValueError('Image dimensions out of bounds')
    encoding = message.encoding.lower()
    formats = {'rgb8': (3, 'u1'), 'bgr8': (3, 'u1'), 'rgba8': (4, 'u1'),
               'bgra8': (4, 'u1'), 'mono8': (1, 'u1'), '8uc1': (1, 'u1'),
               '16uc1': (1, 'u2'), 'mono16': (1, 'u2'), '32fc1': (1, 'f4')}
    if encoding not in formats:
        raise ValueError('Unsupported encoding: ' + encoding)
    channels, scalar = formats[encoding]
    dtype = np.dtype(('>' if message.is_bigendian else '<') + scalar)
    row_bytes = width * channels * dtype.itemsize
    if step < row_bytes or step * height > 64_000_000 or len(message.data) < step * height:
        raise ValueError('Invalid image stride or buffer')
    raw = np.ndarray((height, width, channels), dtype=dtype, buffer=message.data,
                     strides=(step, channels * dtype.itemsize, dtype.itemsize))
    if depth:
        if channels != 1 or scalar not in ('u2', 'f4'):
            raise ValueError('Depth requires 16UC1/mono16/32FC1')
        values = raw[:, :, 0].astype(np.float32)
        valid = np.isfinite(values) & (values > 0)
        ceiling = 5.0 if scalar == 'f4' else 5000.0
        scaled = np.zeros((height, width), dtype=np.uint8)
        scaled[valid] = np.clip(values[valid] * 255 / ceiling, 0, 255).astype(np.uint8)
        image = cv2.applyColorMap(255 - scaled, cv2.COLORMAP_TURBO)
        image[~valid] = 0
    elif encoding == 'rgb8':
        image = cv2.cvtColor(raw, cv2.COLOR_RGB2BGR)
    elif encoding == 'rgba8':
        image = cv2.cvtColor(raw, cv2.COLOR_RGBA2BGR)
    elif encoding == 'bgra8':
        image = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
    elif encoding in ('mono8', '8uc1'):
        image = raw[:, :, 0]
    elif encoding == 'bgr8':
        image = raw
    else:
        raise ValueError('16-bit image requires explicit depth visualization')
    scale = min(1.0, 960 / width, 720 / height)
    if scale < 1:
        image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA)
    success, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not success:
        raise ValueError('JPEG encoder failed')
    return encoded.tobytes(), image.shape[1], image.shape[0]
