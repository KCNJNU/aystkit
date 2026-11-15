"""Bounded, read-only source observations outside the recording lease loop.

This monitor never creates a ROS participant, opens a sensor, or changes a
service. A late/restarted publisher is handled by the existing subscriptions.
Different DDS graphs are reported, never repaired during active recording.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import subprocess
import threading
import time

from .archive_devices import (DeviceStartupCancelled, DeviceStartupError,
                              inspect_capture_radar)
from .archive_graph import (RecordingGraphError, UNIT, _desired, _environment,
                            _same_graph)

DRIVERS = dict(mid360='livox_ros_driver2_node', stl19p='ldlidar_stl_ros2_node',
               bluesea='bluesea2_node')


def _identity(pid, proc_root, executable=None):
    if type(pid) is not int or pid <= 0:
        raise RecordingGraphError('invalid_process_identity')
    directory = Path(proc_root) / str(pid)
    if directory.stat().st_uid != os.getuid():
        raise RecordingGraphError('process_owner_changed')
    if executable is not None:
        command = (directory / 'cmdline').read_bytes()
        if len(command) > 65536 or Path(os.fsdecode(command.split(b'\0')[0])).name != executable:
            raise RecordingGraphError('driver_identity_changed')
    started = (directory / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    if not started.isdecimal():
        raise RecordingGraphError('process_start_unavailable')
    return started


def observe_recording_graph(report, *, runner=subprocess.run, proc_root='/proc'):
    """Compare verified sources with the actual receiver, not its config file.

    Only non-secret DDS keys are read. No raw environment or exception message
    is returned in reports. Process start times bound the inspection against
    a PID being reused while it is in progress.
    """
    rows = [deepcopy(row) for row in report.get('devices', []) if isinstance(row, dict)]
    for row in rows:
        row['recordingGraph'] = dict(state='unknown', reason='driver_not_running_or_unverified')
    candidates = [row for row in rows if row.get('key') in DRIVERS
                  and row.get('state') in ('started', 'reused', 'partial')]
    if not candidates:
        return rows, dict(state='waiting', reason='no_verified_driver_graph')
    try:
        result = runner(['systemctl', '--user', 'show', UNIT, '--property=MainPID', '--value'],
                        capture_output=True, text=True, timeout=2)
        value = result.stdout.strip()
        if result.returncode or not value.isdecimal():
            raise RecordingGraphError('recorder_process_unavailable')
        archive_pid = int(value)
        started = _identity(archive_pid, proc_root)
        actual = _environment(archive_pid, proc_root)
        if _identity(archive_pid, proc_root) != started:
            raise RecordingGraphError('recorder_process_changed')
    except (OSError, ValueError, IndexError, RecordingGraphError, subprocess.SubprocessError):
        for row in candidates:
            row['recordingGraph'] = dict(state='unknown', reason='recorder_process_unavailable')
        return rows, dict(state='unknown', reason='recorder_process_unavailable')
    for row in candidates:
        try:
            pids, graph = row.get('pids'), row.get('dds')
            if not isinstance(pids, list) or len(pids) != 1 or not isinstance(graph, dict):
                raise RecordingGraphError('driver_graph_unverified')
            before = _identity(pids[0], proc_root, DRIVERS[row['key']])
            expected_start = row.get('processStartTicks') or graph.get('processStartTicks')
            if before != str(expected_start):
                raise RecordingGraphError('driver_process_changed')
            desired = _desired(graph)
            if not _same_graph(_environment(pids[0], proc_root), desired):
                raise RecordingGraphError('driver_graph_changed')
            same = _same_graph(actual, desired)
            if (_identity(pids[0], proc_root, DRIVERS[row['key']]) != before
                    or _identity(archive_pid, proc_root) != started):
                raise RecordingGraphError('process_changed')
            row['recordingGraph'] = dict(state='aligned' if same else 'mismatch',
                reason='same_recording_graph' if same else 'recording_dds_graph_mismatch')
        except (OSError, ValueError, TypeError, IndexError, RecordingGraphError):
            row['recordingGraph'] = dict(state='unknown', reason='driver_graph_unverified')
    try:
        if _identity(archive_pid, proc_root) != started:
            raise RecordingGraphError('recorder_process_changed')
    except (OSError, ValueError, IndexError, RecordingGraphError):
        for row in candidates:
            row['recordingGraph'] = dict(state='unknown', reason='recorder_process_changed')
    states = {row['recordingGraph']['state'] for row in candidates}
    state = 'mismatch' if 'mismatch' in states else 'unknown' if 'unknown' in states else 'aligned'
    return rows, dict(state=state, reason='verified_source_graphs')


class RadarObserver:
    """Poll only local source status; slow checks cannot expire the capture lease."""

    def __init__(self, values, *, interval=15, clock=time.time,
                 inspector=inspect_capture_radar, graph_checker=observe_recording_graph):
        if not isinstance(interval, (int, float)) or not 1 <= interval <= 30:
            raise ValueError('invalid source observation interval')
        self.values, self.interval, self.clock = dict(values), interval, clock
        self.inspector, self.graph_checker = inspector, graph_checker
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.thread = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError('source observer already started')
        self.thread = threading.Thread(target=self._run, name='capture-source-observer', daemon=True)
        self.thread.start()

    def _run(self):
        # Startup already inspected sources. Waiting here also avoids any
        # work for a capture lasting only a few seconds.
        while not self.cancel.wait(self.interval):
            try:
                report = self.inspector(self.values, cancelled=self.cancel.is_set)
                if report.get('readOnlySources') is not True or report.get('sourceControl') is not False:
                    raise DeviceStartupError('unverified_read_only_report')
                rows, graph = self.graph_checker(report)
                snapshot = dict(checkedAt=self.clock(), readOnlySources=True, sourceControl=False,
                    hosts=dict(ipc={**report, 'devices': rows}), graph=graph)
            except DeviceStartupCancelled:
                break
            except (OSError, ValueError, TypeError, DeviceStartupError, subprocess.SubprocessError):
                snapshot = dict(checkedAt=self.clock(), readOnlySources=True, sourceControl=False,
                    hosts=dict(ipc=dict(devices=[], error='source_observation_unavailable')),
                    graph=dict(state='unknown', reason='source_observation_unavailable'))
            if not self.cancel.is_set():
                with self.lock:
                    self.latest = snapshot

    def snapshot(self):
        with self.lock:
            return deepcopy(self.latest)

    def close(self, timeout=7):
        self.cancel.set()
        if self.thread is not None:
            self.thread.join(timeout)
        return self.thread is None or not self.thread.is_alive()
