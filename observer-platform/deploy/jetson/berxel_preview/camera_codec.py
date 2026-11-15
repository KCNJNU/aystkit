"""Bounded ROS Image conversion. No device access and no synthetic frames."""
import cv2
import numpy as np


def center_depth_cm(values, scalar):
    """Robust Z depth from this raw frame's central 5x5 pixels, never JPEG color.

    Berxel publishes mono16/16UC1 millimetres after unpacking SDK fixed-point
    depth. Float32 depth is metres. A majority of the patch must agree; zero,
    missing, saturated and nonfinite samples never become a numeric distance.
    """
    height, width = values.shape
    cx, cy = width // 2, height // 2
    roi = (max(0, cx - 2), max(0, cy - 2), min(width, cx + 3), min(height, cy + 3))
    x1, y1, x2, y2 = roi
    patch = values[y1:y2, x1:x2].astype(np.float64)
    millimetres = patch * (1000.0 if scalar == 'f4' else 1.0)
    # Driver point-cloud code excludes the SDK's 4.095/8.191 m sentinels.
    # Image encoding omits the active packing mode, so exclude either sentinel.
    valid = np.isfinite(millimetres) & (millimetres > 0) & (millimetres < 8191.0)
    if scalar == 'u2':
        valid &= millimetres != 4095.0
    samples = millimetres[valid]
    if samples.size < 13:
        return None, roi
    # Lower median always selects a measured sample, including an even count.
    middle = (samples.size - 1) // 2
    median = float(np.partition(samples, middle)[middle])
    # Do not report a midpoint across two surfaces or isolated valid speckles.
    if np.count_nonzero(np.abs(samples - median) <= max(20.0, median * .05)) < 13:
        return None, roi
    return median / 10.0, roi


def annotate_center_depth(image, distance_cm, roi, source_shape):
    """Draw after resize so the same-frame measurement remains legible."""
    height, width = image.shape[:2]
    if width < 128 or height < 48:
        return
    source_height, source_width = source_shape
    x1, y1, x2, y2 = roi
    sx, sy = width / source_width, height / source_height
    cx = round((source_width // 2 + .5) * sx - .5)
    cy = round((source_height // 2 + .5) * sy - .5)
    left, top = max(0, round(x1 * sx)), max(0, round(y1 * sy))
    right = max(left, min(width - 1, round(x2 * sx) - 1))
    bottom = max(top, min(height - 1, round(y2 * sy) - 1))
    accent = (186, 245, 104) if distance_cm is not None else (230, 230, 230)
    for color, thickness in (((8, 18, 24), 3), (accent, 1)):
        cv2.rectangle(image, (left, top), (right, bottom), color, thickness, cv2.LINE_AA)
        for start, end in (((cx - 13, cy), (cx - 5, cy)), ((cx + 5, cy), (cx + 13, cy)),
                           ((cx, cy - 13), (cx, cy - 5)), ((cx, cy + 5), (cx, cy + 13))):
            cv2.line(image, start, end, color, thickness, cv2.LINE_AA)
    label = 'Z: ' + (f'{distance_cm:.1f}' if distance_cm is not None else '--') + ' cm'
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(.45, min(.7, width / 800.0))
    (text_width, text_height), baseline = cv2.getTextSize(label, font, scale, 1)
    box_width, box_height = text_width + 16, text_height + baseline + 12
    x = max(0, min(width - box_width, cx - box_width // 2))
    y = max(0, min(height - box_height, cy + 20))
    patch = image[y:y + box_height, x:x + box_width]
    shade = np.full_like(patch, (18, 23, 28))
    cv2.addWeighted(patch, .2, shade, .8, 0, dst=patch)
    cv2.rectangle(image, (x, y), (x + box_width - 1, y + box_height - 1), accent, 1)
    cv2.putText(image, label, (x + 8, y + 6 + text_height), font, scale,
                (245, 250, 252), 1, cv2.LINE_AA)


def append_depth_scale(image):
    """Append a display legend without cropping or painting over sensor pixels.

    Colours intentionally match the established 0--5 metre mapping. The final
    tick says >=5 m because this is a display scale, not the camera's range.
    """
    height, width = image.shape[:2]
    output_width, legend_height = max(160, width), 86
    output = np.full((height + legend_height, output_width, 3), (18, 23, 28), dtype=np.uint8)
    # Very narrow input images receive side padding instead of losing labels.
    offset = (output_width - width) // 2
    output[:height, offset:offset + width] = image
    panel = output[height:]
    cv2.line(panel, (0, 0), (output_width - 1, 0), (65, 77, 86), 1)
    font = cv2.FONT_HERSHEY_SIMPLEX
    heading_scale = min(.5, max(.35, output_width / 960.0))
    tick_scale = min(.42, max(.32, output_width / 1140.0))
    note_scale = min(.36, max(.28, output_width / 1340.0))
    foreground = (235, 244, 248)
    cv2.putText(panel, 'Depth color (m)', (12, 16), font, heading_scale,
                foreground, 1, cv2.LINE_AA)
    left, right, top, bar_height = 14, output_width - 15, 24, 10
    length = right - left + 1
    values = np.linspace(0, 255, length).astype(np.uint8)
    gradient = cv2.applyColorMap((255 - values)[None, :], cv2.COLORMAP_TURBO)
    panel[top:top + bar_height, left:right + 1] = gradient
    for index, label in enumerate(('0', '1', '2', '3', '4', '>=5m')):
        x = round(left + index * (right - left) / 5)
        cv2.line(panel, (x, top + bar_height), (x, top + bar_height + 4), foreground, 1)
        (text_width, _), _ = cv2.getTextSize(label, font, tick_scale, 1)
        text_x = max(8, min(output_width - text_width - 8, x - text_width // 2))
        cv2.putText(panel, label, (text_x, 49), font, tick_scale,
                    foreground, 1, cv2.LINE_AA)
    cv2.rectangle(panel, (14, 60), (25, 69), (0, 0, 0), -1)
    cv2.rectangle(panel, (14, 60), (25, 69), (126, 146, 156), 1)
    cv2.putText(panel, 'black: invalid', (32, 69), font, tick_scale,
                foreground, 1, cv2.LINE_AA)
    cv2.putText(panel, 'Display scale, not sensor range', (12, 81), font,
                note_scale, (161, 183, 195), 1, cv2.LINE_AA)
    return output


def image_to_jpeg(message, depth=False, *, max_edge=None, jpeg_quality=70, annotate_depth=False):
    if max_edge is not None and (type(max_edge) is not int or not 160 <= max_edge <= 960):
        raise ValueError('Preview max_edge must be an integer from 160 to 960')
    if type(jpeg_quality) is not int or not 20 <= jpeg_quality <= 85:
        raise ValueError('JPEG quality must be an integer from 20 to 85')
    if type(annotate_depth) is not bool:
        raise ValueError('annotate_depth must be a boolean')
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
        measurement = center_depth_cm(raw[:, :, 0], scalar) if annotate_depth else None
        valid = np.isfinite(values) & (values > 0)
        if annotate_depth:
            # Match the centre readout's invalid/saturated-sample policy; an
            # SDK sentinel must not look like a measured point beyond 5 m.
            valid &= values < (8.191 if scalar == 'f4' else 8191.0)
            if scalar == 'u2':
                valid &= values != 4095.0
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
    # Omitted max_edge preserves the original 960 x 720 bounding rectangle.
    scale = (min(1.0, 960 / width, 720 / height) if max_edge is None
             else min(1.0, max_edge / width, max_edge / height))
    if scale < 1:
        image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA)
    if depth and annotate_depth:
        annotate_center_depth(image, *measurement, (height, width))
        image = append_depth_scale(image)
    success, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not success:
        raise ValueError('JPEG encoder failed')
    return encoded.tobytes(), image.shape[1], image.shape[0]
