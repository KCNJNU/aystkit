"""Read-only shutdown decisions from recorder status, without ROS or commands.

READY confirms local closure only. It never means that missing sensor data was
recovered, that a derivative succeeded, or that an upload reached the warehouse.
The caller owns process/service identity checks and must retain startup locks.
"""
from __future__ import annotations

from datetime import datetime
import math


MAX_STATUS_AGE = 8
ROS_QUEUES = ('queueMessages', 'queueBytes', 'inFlightMessages', 'subscriptionCleanupPending')
CAMERA_QUEUES = ('queueFrames', 'queueBytes', 'inFlightFrames')
# Keep in agreement with archive_main.TERMINAL_DERIVATIVE_ERRORS. Explicit
# known errors alone may be deferred; a worker error cannot masquerade as one.
_TERMINAL_DERIVATIVE_ERRORS = frozenset({
    'video_source_file_invalid', 'video_interrupted_staging_unsafe',
    'bag_source_file_invalid', 'bag_source_path_invalid', 'bag_source_link',
    'bag_manifest_invalid', 'bag_pending_manifest_invalid',
    'bag_metadata_or_message_count_invalid', 'bag_yaml_missing',
})


def _decision(state, reason, warnings=()):
    return dict(state=state, reason=reason, warnings=list(dict.fromkeys(warnings)))


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _schema_valid(report):
    # Current native status does not emit schemaVersion. Reject conflicting
    # explicitly supplied versions, including bool (a Python int subclass).
    return ('schemaVersion' not in report or
            type(report['schemaVersion']) is int and report['schemaVersion'] == 1)


def _queues(report, names):
    values = [report.get(name) for name in names]
    if any(type(value) is not int or value < 0 for value in values):
        return 'invalid'
    return 'pending' if any(values) else 'empty'


def _derivatives(report):
    """Return missing/clear/pending/terminal/error, using actual nested status."""
    video = report.get('video')
    if video is None:
        store = report.get('store', {})
        if 'videoPendingSegments' not in store and 'videoFinalizing' not in store:
            return 'missing'
        pending, running = store.get('videoPendingSegments'), store.get('videoFinalizing')
        if type(pending) is not int or pending < 0 or type(running) is not bool:
            return 'error'
        # A bare summary flag cannot prove why a source was terminal. Require
        # the detailed worker status before accepting deferred material.
        return 'clear' if pending == 0 and not running else 'pending'
    if not isinstance(video, dict):
        return 'error'
    pending, running = video.get('pendingSegments'), video.get('running')
    errors = video.get('errors', {})
    if (type(pending) is not int or pending < 0 or type(running) is not bool
            or not isinstance(errors, dict)):
        return 'error'
    if pending == 0 and not running:
        return 'error' if errors else 'clear'
    if running:
        return 'pending'
    terminal = video.get('terminalPendingSegments')
    if (type(terminal) is int and terminal == pending and pending > 0 and errors
            and all(isinstance(key, str) and key.startswith(('video:', 'bag:'))
                    and isinstance(error, str)
                    and any(code in error for code in _TERMINAL_DERIVATIVE_ERRORS)
                    for key, error in errors.items())):
        return 'terminal'
    return 'pending'


def check_archive_state(report, *, now, since, stopped=False):
    """Check fresh, locally sealed ROS data; never require network upload ACKs.

    WAIT is pending activity or an old observation; ERROR is malformed or
    explicit failure evidence. stopped=True additionally requires the native
    recorder's final ``state=stopped`` report and a confirmed exited writer.
    """
    if (not _number(now) or not _number(since) or since < 0 or since > now
            or type(stopped) is not bool):
        return _decision('ERROR', 'archive_clock_invalid')
    if report is None or report == {}:
        return _decision('WAIT', 'archive_status_missing')
    if not isinstance(report, dict) or not _schema_valid(report):
        return _decision('ERROR', 'archive_status_invalid')
    stamp = _timestamp(report.get('updatedAt'))
    if stamp is None:
        return _decision('ERROR', 'archive_timestamp_invalid')
    if stamp > now:
        return _decision('ERROR', 'archive_timestamp_future')
    if stamp < since or now - stamp > MAX_STATUS_AGE:
        return _decision('WAIT', 'archive_status_stale')
    if report.get('state') == 'stop_failed':
        return _decision('ERROR', 'archive_stop_failed')
    if report.get('state') not in (None, 'stopped'):
        return _decision('ERROR', 'archive_state_invalid')
    errors = report.get('errors', {})
    if not isinstance(errors, dict) or errors:
        return _decision('ERROR', 'archive_runtime_errors')
    if stopped and report.get('state') != 'stopped':
        return _decision('WAIT', 'archive_service_not_stopped')
    ros, store = report.get('ros'), report.get('store')
    if not isinstance(ros, dict) or not isinstance(store, dict):
        return _decision('ERROR', 'archive_raw_status_invalid')
    if type(ros.get('active')) is not bool:
        return _decision('ERROR', 'archive_active_invalid')
    if ros.get('tickError') not in (None, '') or ros.get('gateError') not in (None, ''):
        return _decision('ERROR', 'archive_ros_errors')
    queues = _queues(ros, ROS_QUEUES)
    if queues == 'invalid' or not isinstance(store.get('active'), dict):
        return _decision('ERROR', 'archive_raw_status_invalid')
    storage_errors = store.get('errors', {})
    if not isinstance(storage_errors, dict):
        return _decision('ERROR', 'archive_storage_errors_invalid')
    warnings = []
    for key in storage_errors:
        if key == 'recovery':
            warnings.append('archive_historical_recovery_preserved')
        elif key == 'sourceTransfer':
            warnings.append('archive_source_transfer_deferred')
        elif key != 'videoFinalization':
            return _decision('ERROR', 'archive_storage_errors', warnings)
    if ros['active'] or queues == 'pending' or store['active']:
        return _decision('WAIT', 'archive_raw_unsealed', warnings)
    if stopped and ros.get('workerAlive') is not False:
        return _decision('ERROR', 'archive_writer_exit_unconfirmed', warnings)
    derivatives = _derivatives(report)
    if derivatives == 'error':
        return _decision('ERROR', 'archive_derivative_status_invalid', warnings)
    if derivatives == 'terminal':
        warnings.append('archive_terminal_derivatives_preserved')
    elif derivatives == 'pending':
        return _decision('WAIT', 'archive_derivative_pending', warnings)
    elif 'videoFinalization' in storage_errors:
        return _decision('ERROR', 'archive_derivative_error_unconfirmed', warnings)
    final_warnings = report.get('warnings', {})
    if isinstance(final_warnings, dict):
        if final_warnings.get('recovery'):
            warnings.append('archive_historical_recovery_preserved')
        if final_warnings.get('videoFinalize') == 'DeferredTerminalSource':
            if derivatives != 'terminal':
                return _decision('ERROR', 'archive_terminal_derivatives_unconfirmed', warnings)
            warnings.append('archive_terminal_derivatives_preserved')
    upload = report.get('upload')
    if isinstance(upload, dict) and (upload.get('pendingSegments') or upload.get('lastError')):
        warnings.append('archive_upload_deferred')
    return _decision('READY', 'archive_locally_sealed', warnings)


def check_camera_state(report, *, now, boot_time, service_active, process_count):
    """Check inactive camera writers, preserving narrow pre-boot failure evidence.

    Successfully closed camera status is not periodically refreshed, so a
    timestamp within this boot is accepted after independent process/service
    closure checks. The old ``stop_failed/store=None`` exception confirms only
    that there is no current writer; it explicitly preserves unknown old data.
    """
    if (not _number(now) or not _number(boot_time) or boot_time < 0 or boot_time > now
            or type(service_active) is not bool
            or type(process_count) is not int or process_count < 0):
        return _decision('ERROR', 'camera_probe_invalid')
    if service_active or process_count:
        return _decision('WAIT', 'camera_processes_running')
    if report is None or report == {}:
        return _decision('WAIT', 'camera_status_missing')
    if not isinstance(report, dict) or not _schema_valid(report):
        return _decision('ERROR', 'camera_status_invalid')
    stamp = _timestamp(report.get('updatedAt'))
    if stamp is None:
        return _decision('ERROR', 'camera_timestamp_invalid')
    if stamp > now:
        return _decision('ERROR', 'camera_timestamp_future')
    sink = report.get('sink')
    if not isinstance(sink, dict):
        return _decision('ERROR', 'camera_sink_invalid')
    queues = _queues(sink, CAMERA_QUEUES)
    if (queues == 'invalid' or type(sink.get('enabled')) is not bool
            or type(sink.get('writerRunning')) is not bool
            or not isinstance(sink.get('errors'), dict)):
        return _decision('ERROR', 'camera_sink_invalid')
    if sink['errors']:
        return _decision('ERROR', 'camera_writer_errors')
    if queues == 'pending' or sink['enabled'] or sink['writerRunning']:
        return _decision('WAIT', 'camera_writer_unsealed')
    if stamp < boot_time:
        if (report.get('state') == 'stop_failed' and 'store' in report and report['store'] is None
                and report.get('errors', {}) == {}):
            return _decision('READY', 'camera_previous_boot_writer_absent',
                             ['camera_previous_boot_capture_unconfirmed'])
        return _decision('ERROR', 'camera_previous_boot_status_unconfirmed')
    if report.get('state') == 'stop_failed':
        return _decision('ERROR', 'camera_stop_failed')
    if report.get('state') not in (None, 'stopped'):
        return _decision('ERROR', 'camera_state_invalid')
    if report.get('errors', {}) != {}:
        return _decision('ERROR', 'camera_runtime_errors')
    store = report.get('store')
    if not isinstance(store, dict) or not isinstance(store.get('active'), dict):
        return _decision('ERROR', 'camera_store_invalid')
    errors = store.get('errors', {})
    if not isinstance(errors, dict):
        return _decision('ERROR', 'camera_store_errors_invalid')
    warnings = []
    for key in errors:
        if key == 'recovery':
            warnings.append('camera_historical_recovery_preserved')
        else:
            return _decision('ERROR', 'camera_storage_errors', warnings)
    if store['active']:
        return _decision('WAIT', 'camera_segments_unsealed', warnings)
    if report.get('captureComplete') is False or report.get('captureWarnings'):
        warnings.append('camera_capture_had_warnings')
    return _decision('READY', 'camera_locally_closed', warnings)
