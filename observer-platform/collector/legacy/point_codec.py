"""Small, dependency-light XYZ sampler shared by local and remote previews."""
import math
import numpy as np


def cloud_points(msg, limit=4000):
    fields = {field.name: field for field in msg.fields}
    if not all(name in fields and fields[name].datatype == 7 for name in ('x', 'y', 'z')):
        raise ValueError('XYZ float32 fields required')
    width, height = msg.width, msg.height
    if not width or not height:
        return []
    if width * height > 5_000_000 or len(msg.data) < msg.row_step * height:
        raise ValueError('Invalid point cloud size')
    if msg.row_step < width * msg.point_step:
        raise ValueError('Invalid row stride')
    stride = max(1, math.ceil(width * height / limit))
    indices = np.arange(0, width * height, stride)
    columns = []
    for name in ('x', 'y', 'z'):
        offset = fields[name].offset
        if offset < 0 or offset + 4 > msg.point_step:
            raise ValueError('Invalid field offset')
        array = np.ndarray((height, width), dtype='>f4' if msg.is_bigendian else '<f4',
                           buffer=msg.data, offset=offset, strides=(msg.row_step, msg.point_step))
        columns.append(array[indices // width, indices % width])
    points = np.stack(columns, axis=1)
    points = points[np.isfinite(points).all(axis=1)]
    return np.round(points, 3).tolist()
