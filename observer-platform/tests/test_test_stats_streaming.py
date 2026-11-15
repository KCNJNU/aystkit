"""Large manual-test histories remain readable with bounded temporary storage."""
from datetime import datetime
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest

from cloud.backend.test_stats import BEIJING, TestStats as Stats


ROOT = Path(__file__).resolve().parents[1]


def history(count=60000):
    base = datetime(2026, 10, 28, 23, 59, 30, tzinfo=BEIJING).timestamp()
    now = [base]
    db = sqlite3.connect(':memory:')
    stats = Stats(db, clock=lambda: now[0])

    def control(actor, action):
        state = stats.control_state(actor=actor)
        return stats.control(action, state['revision'], str(uuid.uuid4()), actor=actor,
                             **({'tester_name': actor} if action == 'start' else {}))

    control('alice', 'start')
    capture = stats.collector_control_state()
    now[0] = base + .5
    control('bob', 'start')
    now[0] = base + 20.25
    control('alice', 'pause')
    now[0] = base + 30.75
    control('alice', 'resume')
    now[0] = base + count - .25
    control('bob', 'finish')
    now[0] = base + count + .5
    control('alice', 'finish')
    with db:
        db.executemany('INSERT INTO test_samples VALUES (?,?,?,?,?,?,?,?,?,?)', (
            ('robot', 'primary', f'00000000-0000-4000-8000-{index:012x}',
             capture['run_id'], capture['revision'], base + index, base + index + 1,
             1., .5, now[0]) for index in range(count)))
    return stats


def test_fractional_sample_across_beijing_midnight_keeps_both_days():
    boundary = datetime(2026, 10, 29, tzinfo=BEIJING).timestamp()
    before = math.nextafter(boundary - .25, -math.inf)
    after = math.nextafter(boundary + .25, math.inf)
    now = [before]
    db = sqlite3.connect(':memory:')
    stats = Stats(db, clock=lambda: now[0])
    try:
        stats.control('start', 0, str(uuid.uuid4()), actor='alice', tester_name='alice')
        capture = stats.collector_control_state()
        now[0] = after
        stats.control('finish', 1, str(uuid.uuid4()), actor='alice')
        with db:
            db.execute('INSERT INTO test_samples VALUES (?,?,?,?,?,?,?,?,?,?)',
                       ('robot', 'primary', str(uuid.uuid4()), capture['run_id'], capture['revision'],
                        before, after, 1., after - before, after))
        value = stats.snapshot(days=2, actor='alice')
        assert [row['date'] for row in value['days']] == ['2026-10-28', '2026-10-29']
        assert [row['distance_m'] for row in value['days']] == pytest.approx([.5, .5])
        assert value['totals']['distance_m'] == pytest.approx(1.)
        assert sum(row['valid_seconds'] for row in value['days']) == after - before
    finally:
        db.close()


def test_large_history_streams_contributions_without_sqlite_materialization():
    stats = history()
    plans = []
    consumed = [0]

    class Rows:
        def __init__(self, cursor):
            self.cursor = cursor

        def __iter__(self):
            for row in self.cursor:
                consumed[0] += 1
                yield row

        def fetchall(self):
            raise AssertionError('Sample history must stay in a cursor')

    class ReadProbe:
        def execute(self, sql, args=()):
            cursor = stats.db.execute(sql, args)
            if 'FROM test_samples s' in sql:
                plans.extend(row[3] for row in stats.db.execute('EXPLAIN QUERY PLAN ' + sql, args))
                return Rows(cursor)
            return cursor

    try:
        before = stats.db.total_changes
        stats.db.execute('PRAGMA query_only=ON')
        value = stats.snapshot(days=2, actor='observer', read_all=True, connection=ReadProbe())
        assert consumed[0] >= 60000
        assert plans and not any('MATERIALIZE' in plan or 'TEMP B-TREE' in plan for plan in plans)
        assert stats.db.total_changes == before
        assert value['active'] is None
        assert value['totals']['distance_m'] == pytest.approx(120000 - 11.25)
        assert value['totals']['valid_seconds'] == pytest.approx((120000 - 11.25) / 2)
        assert value['totals']['duration_s'] == pytest.approx(120000 - 10.75)
        assert value['days'][0]['date'] == '2026-10-28'
        assert value['days'][0]['distance_m'] == pytest.approx(20.25 + 29.5)
        assert value['days'][1]['distance_m'] == pytest.approx((60000 - 30.75) + (60000 - 30.25))
        assert {row['username']: row['total']['distance_m'] for row in value['users']} == {
            'alice': pytest.approx(60000 - 10.5), 'bob': pytest.approx(60000 - .75), 'observer': None}
    finally:
        stats.db.close()


def test_large_history_reads_with_tiny_temp_file_limit_in_isolated_process():
    if sys.platform != 'linux':
        pytest.skip('Linux resource limits required')
    code = r'''
import resource, signal
from tests.test_test_stats_streaming import history
stats = history()
try:
    stats.db.execute('PRAGMA temp_store=FILE')
    stats.db.execute('PRAGMA query_only=ON')
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    value = stats.snapshot(days=2, actor='observer', read_all=True)
    assert abs(value['totals']['distance_m'] - (120000 - 11.25)) < 1e-8
    own = stats.snapshot(days=1, actor='alice')
    assert {row['created_by'] for row in own['runs']} == {'alice'}
    assert abs(own['totals']['distance_m'] - (60000 - 10.5)) < 1e-8
    assert abs(own['summary']['distance_m'] - (60000 - 30.75)) < 1e-8
    print('large history reads without large temporary files')
finally:
    stats.db.close()
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                            env={**os.environ, 'PYTHONPATH': str(ROOT)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    assert 'large history reads without large temporary files' in result.stdout
