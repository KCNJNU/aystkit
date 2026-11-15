"""Pair existing frames by one host clock; this never synchronizes exposure.

The caller must select P100R depth rows and enforce input size limits.  Rows
must identify the same robot, capture session and host/boot clock domain.
RGB timestamps may be grouped by camera, but must never decrease within a
camera.  Depth rows must be in timestamp order.  No input is sorted or altered.
"""
from bisect import bisect_left
import copy
import json

DEFAULT_TOLERANCE_NS = 30_000_000  # Engineering default, not measured accuracy.


class SyncError(ValueError):
    """Frames cannot be compared without inventing or crossing time bases."""


def _integer(value, *, positive=False):
    return type(value) is int and (value > 0 if positive else value >= 0)


def _row(row, expected, label):
    if not isinstance(row, dict):
        raise SyncError(f'{label}: frame must be an object')
    for key in ('robotId', 'captureSessionId', 'clockDomain', 'cameraId', 'segmentId'):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise SyncError(f'{label}: missing or invalid {key}')
    identity = tuple(row[key] for key in ('robotId', 'captureSessionId', 'clockDomain'))
    if expected is not None and identity != expected:
        raise SyncError(f'{label}: robot, capture session or clock domain mismatch')
    for key in ('frameIndex', 'timestampNs', 'videoPtsUs', 'sequence'):
        if key == 'sequence' and key not in row:
            continue
        if not _integer(row.get(key), positive=key == 'timestampNs'):
            raise SyncError(f'{label}: missing or invalid {key}')
    try:
        json.dumps(row, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise SyncError(f'{label}: frame must contain finite JSON data') from error
    return identity


def iter_pairs(rgb_rows, depth_rows, *, tolerance_ns=DEFAULT_TOLERANCE_NS):
    """Yield one record per RGB frame, retaining unmatched frames and all stamps.

    Nearest-neighbour matching uses integer nanoseconds.  Ties choose the earlier
    depth timestamp, then its first input index.  Depth references may be reused;
    depthReusedWithinCamera identifies repeat references by the same USB camera.
    Matching never creates a video frame or claims simultaneous exposure.
    Validation of streaming RGB input may fail after earlier records were yielded;
    callers publishing a manifest must therefore write and commit it atomically.
    """
    if not _integer(tolerance_ns):
        raise SyncError('tolerance_ns must be a nonnegative integer')
    depths, timestamps, indices = [], [], []
    identity, depth_camera, previous = None, None, None
    depth_refs = set()
    for index, row in enumerate(depth_rows):
        identity = _row(row, identity, f'depth[{index}]')
        if depth_camera is not None and row['cameraId'] != depth_camera:
            raise SyncError('depth rows must belong to one selected depth camera')
        depth_camera = row['cameraId']
        timestamp = row['timestampNs']
        if previous is not None and timestamp < previous:
            raise SyncError('depth timestamps are not monotonic')
        reference = (row['cameraId'], row['segmentId'], row['frameIndex'])
        if reference in depth_refs:
            raise SyncError('duplicate depth frame reference')
        depth_refs.add(reference)
        depths.append(row)
        if previous != timestamp:
            timestamps.append(timestamp)
            indices.append(index)
        previous = timestamp
    previous_rgb, rgb_refs, previous_depth = {}, set(), {}
    for index, row in enumerate(rgb_rows):
        identity = _row(row, identity, f'rgb[{index}]')
        camera_id, timestamp = row['cameraId'], row['timestampNs']
        if camera_id in previous_rgb and timestamp < previous_rgb[camera_id]:
            raise SyncError(f'{camera_id}: RGB timestamps are not monotonic')
        previous_rgb[camera_id] = timestamp
        reference = (camera_id, row['segmentId'], row['frameIndex'])
        if reference in rgb_refs:
            raise SyncError('duplicate RGB frame reference')
        rgb_refs.add(reference)
        position = bisect_left(timestamps, timestamp)
        candidates = [p for p in (position-1, position) if 0 <= p < len(timestamps)]
        nearest = min(candidates, key=lambda p: (abs(timestamps[p]-timestamp), timestamps[p]), default=None)
        depth_index = indices[nearest] if nearest is not None else None
        delta = timestamps[nearest]-timestamp if nearest is not None else None
        matched = delta is not None and abs(delta) <= tolerance_ns
        # Each camera's nondecreasing RGB timestamps imply nondecreasing nearest
        # depth indices.  Keeping its last match therefore detects every reuse
        # without an unbounded additional history; unmatched rows do not consume.
        reused = matched and previous_depth.get(camera_id) == depth_index
        if matched:
            previous_depth[camera_id] = depth_index
        yield dict(rgbIndex=index, rgb=copy.deepcopy(row),
                   depthIndex=depth_index if matched else None,
                   depth=copy.deepcopy(depths[depth_index]) if matched else None,
                   deltaNs=abs(delta) if matched else None, signedDeltaNs=delta if matched else None,
                   toleranceNs=tolerance_ns, state='matched' if matched else 'unmatched',
                   depthReusedWithinCamera=reused, depthReusePolicy='nearest_reuse_allowed',
                   syncKind='software_host_timestamp', hardwareSynchronized=False)


def pair_frames(rgb_rows, depth_rows, *, tolerance_ns=DEFAULT_TOLERANCE_NS):
    """Return all pair records; use iter_pairs for bounded-memory output writing."""
    return list(iter_pairs(rgb_rows, depth_rows, tolerance_ns=tolerance_ns))
