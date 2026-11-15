#!/usr/bin/env python3
"""Supervise the local gateway and observer as one restartable service."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


SHUTDOWN_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.1


def signal_group(process, signum):
    """Also stop nginx workers; every child owns a separate process group."""
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def shutdown(children):
    # Stop accepting HTTP requests while aiohttp closes its streaming handlers.
    for name, process in reversed(children):
        signal_group(process, signal.SIGQUIT if name == "gateway" else signal.SIGTERM)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while any(process.poll() is None for _, process in children) and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
    # A component may already have exited while leaving its children behind.
    # Kill its whole group as well, then explicitly reap every direct child.
    for _, process in children:
        signal_group(process, signal.SIGKILL)
    for _, process in children:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("Platform component did not exit after forced shutdown.", file=sys.stderr, flush=True)


def main():
    stopping = False

    def request_shutdown(_signum, _frame):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
        signal.signal(signum, request_shutdown)

    children = []
    commands = (
        ("observer", [sys.executable, "-m", "cloud.backend.app"]),
        ("gateway", ["/usr/sbin/nginx", "-e", "stderr", "-c", "/app/nginx.conf", "-g", "daemon off;"]),
    )
    try:
        for name, command in commands:
            if stopping:
                return 0
            children.append((name, subprocess.Popen(command, start_new_session=True)))
        while not stopping:
            for name, process in children:
                status = process.poll()
                if status is not None:
                    print(f"Platform component exited: {name} (status {status}).", file=sys.stderr, flush=True)
                    # Even an unexpected successful exit must restart the pair.
                    return status if status > 0 else 1
            time.sleep(POLL_INTERVAL_SECONDS)
        return 0
    except OSError as error:
        # Do not interpolate environment or command arguments into logs.
        print(f"Platform component could not start: {type(error).__name__}.", file=sys.stderr, flush=True)
        return 1
    finally:
        shutdown(children)


if __name__ == "__main__":
    raise SystemExit(main())
