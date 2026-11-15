"""Priority isolation in owned test processes, never production services."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from collector import archive_scheduling


def test_background_thread_and_encoder_child_yield_without_lowering_writer():
    if not sys.platform.startswith('linux') or not Path('/usr/bin/ionice').exists():
        pytest.skip('Linux per-thread scheduling and ionice required')
    code = r'''
import json, os, subprocess, sys, threading
from collector.archive_scheduling import run_background
def current():
    return dict(nice=os.getpriority(os.PRIO_PROCESS, threading.get_native_id()),
        io=subprocess.check_output(['/usr/bin/ionice','-p',str(threading.get_native_id())],text=True).strip())
before=current(); result={}
def background():
    result['background']=current()
    result['encoder']=json.loads(subprocess.check_output([sys.executable,'-c',
        "import json,os,subprocess;print(json.dumps(dict(nice=os.getpriority(os.PRIO_PROCESS,0),io=subprocess.check_output(['/usr/bin/ionice','-p',str(os.getpid())],text=True).strip())))"],text=True))
worker=threading.Thread(target=run_background,args=(background,))
worker.start();worker.join(timeout=5)
assert not worker.is_alive()
print(json.dumps(dict(before=before,after=current(),**result)))
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=True)
    state = json.loads(result.stdout)
    assert state['after'] == state['before']
    assert state['background']['nice'] >= 15
    assert state['encoder']['nice'] == state['background']['nice']
    assert state['background']['io'] == state['encoder']['io'] == 'idle'


def test_priority_failure_keeps_archival_work_and_logs_no_command_details(monkeypatch, caplog):
    def forbidden(*_args, **_options):
        raise PermissionError('PRIVATE_PROCESS_DETAILS')
    monkeypatch.setattr(archive_scheduling.os, 'getpriority', forbidden)
    monkeypatch.setattr(archive_scheduling.subprocess, 'run', forbidden)
    assert archive_scheduling.run_background(lambda: 'raw data retained') == 'raw data retained'
    assert 'priority unavailable' in caplog.text
    assert 'PRIVATE_PROCESS_DETAILS' not in caplog.text


def test_background_priority_never_promotes_an_already_nice19_thread(monkeypatch):
    calls = []
    monkeypatch.setattr(archive_scheduling.os, 'getpriority', lambda *_: 19)
    monkeypatch.setattr(archive_scheduling.os, 'setpriority', lambda *args: calls.append(args))
    monkeypatch.setattr(archive_scheduling.subprocess, 'run', lambda *_args, **_kw: SimpleNamespace(returncode=0))
    assert archive_scheduling.background_priority() == {'cpu': True, 'io': True}
    assert calls == [(os.PRIO_PROCESS, archive_scheduling.threading.get_native_id(), 19)]
