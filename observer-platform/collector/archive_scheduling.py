"""Lower only archival background work; capture writers keep their priority.

Linux CPU and I/O priorities belong to the calling native thread. Children
spawned by that thread inherit them, including ffmpeg, GStreamer and zstd.
No scheduler, resource quota or priority of another application is changed.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading


LOG = logging.getLogger(__name__)


def background_priority():
    """Best effort downgrade of this thread, without raising existing priority."""
    task = threading.get_native_id()
    state = {'cpu': False, 'io': False}
    try:
        current = os.getpriority(os.PRIO_PROCESS, task)
        os.setpriority(os.PRIO_PROCESS, task, max(15, current))
        state['cpu'] = os.getpriority(os.PRIO_PROCESS, task) >= 15
    except (AttributeError, OSError) as error:
        LOG.warning('Archive background CPU priority unavailable: %s', type(error).__name__)
    try:
        result = subprocess.run(['/usr/bin/ionice', '-c', '3', '-p', str(task)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=2, check=False)
        state['io'] = result.returncode == 0
        if not state['io']:
            LOG.warning('Archive background I/O priority unavailable')
    except (OSError, subprocess.SubprocessError) as error:
        LOG.warning('Archive background I/O priority unavailable: %s', type(error).__name__)
    return state


def run_background(target, *args, **options):
    background_priority()
    return target(*args, **options)
