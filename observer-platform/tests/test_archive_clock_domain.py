"""Source clock domains survive rotations/recovery without changing bag stamps."""
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from collector import archive_camera, archive_core
from collector.archive_camera import NativeCameraStore
from collector.archive_core import ArchiveStore


ROBOT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
WALL = 1793073600.0
BOOT = '11111111-2222-4333-8444-555555555555'
DOMAIN = 'linux-boot:' + BOOT


class Writer:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir()
        self.rows = []

    def write(self, topic, type_name, raw, timestamp_ns):
        self.rows.append((topic, type_name, raw, timestamp_ns))
        with (self.path / 'bag_0.db3').open('ab') as output:
            output.write(raw)

    def close(self):
        (self.path / 'metadata.yaml').write_text('fixture bag')


def store(root, **options):
    return ArchiveStore(root, ROBOT, writer_factory=Writer,
        min_free_bytes=0, disk_usage=lambda _: SimpleNamespace(free=10**10),
        compression=False, **options)


def metadata(root):
    paths = list(Path(root).rglob('archive.json'))
    assert paths
    return [json.loads(path.read_text())['metadata'] for path in paths]


def test_same_boot_gives_usb_and_ros_the_exact_same_domain(tmp_path, monkeypatch):
    boot_file = tmp_path / 'boot-id'
    boot_file.write_text(BOOT + '\n')
    ros_read, usb_read = archive_core._linux_boot_clock_domain, archive_camera._linux_boot_clock_domain
    assert ros_read(boot_file) == usb_read(boot_file) == DOMAIN
    monkeypatch.setattr(archive_core, '_linux_boot_clock_domain', lambda: ros_read(boot_file))
    monkeypatch.setattr(archive_camera, '_linux_boot_clock_domain', lambda: usb_read(boot_file))
    ros = store(tmp_path / 'ros')
    usb = NativeCameraStore(tmp_path / 'usb', ROBOT, calibration_path=tmp_path / 'missing.json',
        min_free_bytes=0, disk_usage=lambda _: SimpleNamespace(free=10**10))
    assert ros.clock_domain == usb.clock_domain == DOMAIN
    ros.close()
    usb.close()


def test_ros_domain_is_read_once_and_segment_metadata_keeps_original_receive_stamp(tmp_path, monkeypatch):
    reads = []

    def clock_domain():
        reads.append(True)
        return DOMAIN

    monkeypatch.setattr(archive_core, '_linux_boot_clock_domain', clock_domain)
    target = store(tmp_path, segment_seconds=5)
    raw, stamp = b'\x00original-CDR-with-unaltered-header\xff', 987654321
    assert target.record_accepted('Berxel深度', '/berxel/depth/image_raw',
        'sensor_msgs/msg/Image', raw, stamp, WALL)
    first = target.active['Berxel深度']
    assert first['writer'].rows[0][-1] == stamp
    session = json.loads((first['path'] / 'session.json').read_text())
    assert session['clockDomain'] == DOMAIN
    assert session['clockSource'] == 'CLOCK_REALTIME'
    assert session['timestampSemantics'] == 'host_subscription_receive'
    target.clock_domain = 'linux-boot:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    # A newer process-level value cannot rewrite a segment already opened.
    target.close()
    pending = json.loads(next(tmp_path.rglob('video-pending.json')).read_text())
    assert pending['metadata']['clockDomain'] == DOMAIN
    assert pending['metadata']['timestampSemantics'] == 'host_subscription_receive'
    assert pending['metadata']['timestampBasis'] == 'receive-wall-clock; original ROS header retained in CDR'
    assert next(tmp_path.rglob('bag_0.db3')).read_bytes() == raw
    assert len(reads) == 1


@pytest.mark.parametrize('invalid', ['', 'not-a-uuid', '0' * 36,
    '00000000-0000-0000-0000-000000000000', BOOT + ' extra', 'x' * 100])
def test_invalid_boot_identifier_does_not_emit_a_domain(tmp_path, monkeypatch, invalid):
    boot = tmp_path / 'boot-id'
    boot.write_text(invalid)
    assert archive_core._linux_boot_clock_domain(boot) is None
    original = archive_core._linux_boot_clock_domain
    monkeypatch.setattr(archive_core, '_linux_boot_clock_domain', lambda: original(boot))
    target = store(tmp_path / 'spool')
    assert target.record_accepted('雷达', '/raw', 'fixture/type', b'raw', 1000, WALL)
    session = json.loads((target.active['雷达']['path'] / 'session.json').read_text())
    assert 'clockDomain' not in session
    target.close()
    result = metadata(tmp_path)[0]
    assert 'clockDomain' not in result
    assert result['clockSource'] == 'CLOCK_REALTIME'
    assert result['timestampSemantics'] == 'host_subscription_receive'


@pytest.mark.parametrize('original_domain', [DOMAIN, None, 'linux-boot:invalid'])
def test_recovery_keeps_old_domain_or_unknown_and_never_uses_current_boot(tmp_path, monkeypatch, original_domain):
    entry = tmp_path / '.active' / 'interrupted-fixture'
    bag = entry / 'bag'
    bag.mkdir(parents=True)
    session = dict(schemaVersion=1, scope='garage', source='native-ros-cdr', robotId=ROBOT, sensor='雷达')
    if original_domain is not None:
        session.update(clockDomain=original_domain, clockSource='CLOCK_REALTIME',
            timestampSemantics='host_subscription_receive')
    (entry / 'session.json').write_text(json.dumps(session))
    original_session = (entry / 'session.json').read_bytes()
    raw, stamp = b'original CDR', int(WALL * 1e9)
    with sqlite3.connect(bag / 'data_0.db3') as connection:
        connection.execute('CREATE TABLE topics (id INTEGER PRIMARY KEY,name TEXT,type TEXT)')
        connection.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY,topic_id INTEGER,timestamp INTEGER,data BLOB)')
        connection.execute('INSERT INTO topics VALUES (1,?,?)', ('/raw', 'fixture/Type'))
        connection.execute('INSERT INTO messages VALUES (1,1,?,?)', (stamp, raw))

    class Reindexer:
        def reindex(self, options):
            (Path(options.uri) / 'metadata.yaml').write_text('fixture reindex')

    monkeypatch.setitem(sys.modules, 'rosbag2_py', SimpleNamespace(Reindexer=Reindexer,
        StorageOptions=lambda **values: SimpleNamespace(**values)))
    new_domain = 'linux-boot:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    monkeypatch.setattr(archive_core, '_linux_boot_clock_domain', lambda: new_domain)
    recovered = store(tmp_path)
    result = metadata(tmp_path)[0]
    assert recovered.clock_domain == new_domain
    assert result.get('clockDomain') == (DOMAIN if original_domain == DOMAIN else None)
    if original_domain is None:
        assert 'clockSource' not in result and 'timestampSemantics' not in result
    else:
        assert result['timestampSemantics'] == 'host_subscription_receive'
    assert next((tmp_path / 'days').rglob('session.json')).read_bytes() == original_session
    with sqlite3.connect(next((tmp_path / 'days').rglob('data_0.db3'))) as connection:
        assert connection.execute('SELECT timestamp,data FROM messages').fetchone() == (stamp, raw)
