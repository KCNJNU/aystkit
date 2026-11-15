"""Execute only private fixture setup scripts and a Python environment printer.

No ROS setup, driver, systemd, hardware or network command runs in these checks.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from deploy.scripts.device_sources import DeviceSources, Source


@pytest.mark.parametrize('key,expected', [
    ('berxel', ['ASYNCHRONOUS', '0']),
    ('mid360', ['SYNCHRONOUS', '1']),
    ('bluesea', ['SYNCHRONOUS', '1']),
    ('stl19p', ['SYNCHRONOUS', '1']),
])
def test_only_berxel_overrides_publication_after_all_setup_scripts(tmp_path, key, expected):
    workspace = tmp_path / 'workspace with spaces'
    (workspace/'install').mkdir(parents=True)
    setup = tmp_path/'fixture-ros-setup.bash'
    for index, path in enumerate((setup, workspace/'install/setup.bash', workspace/'env.sh')):
        # In particular, the last sourced env.sh attempts to restore a
        # synchronous/XML setting; the Berxel launch must override it afterward.
        path.write_text('export RMW_FASTRTPS_PUBLICATION_MODE=SYNCHRONOUS\n'
                        'export RMW_FASTRTPS_USE_QOS_FROM_XML=1\n')
    manager = DeviceSources('jetson', home=tmp_path, workspace=workspace, ros_setup=setup)
    printer = "import json,os;print(json.dumps([os.environ.get('RMW_FASTRTPS_PUBLICATION_MODE'),os.environ.get('RMW_FASTRTPS_USE_QOS_FROM_XML')]))"
    source = Source(key, 'fixture-unused', (), (sys.executable, '-c', printer), '/fixture-unused')
    before = dict(os.environ)
    completed = subprocess.run(manager.source_command(source), capture_output=True,
                               text=True, check=True, timeout=5)
    assert json.loads(completed.stdout) == expected
    assert dict(os.environ) == before


def test_berxel_wrapping_preserves_original_driver_arguments_and_other_sources(tmp_path):
    from deploy.scripts.device_sources import SOURCES
    manager = DeviceSources('jetson', home=tmp_path)
    manager.ros_command = lambda command: command
    berxel = manager.source_command(SOURCES['berxel'])
    assert berxel[:4] == ['/usr/bin/env',
        'FASTRTPS_DEFAULT_PROFILES_FILE=' + str(manager.camera_transport_profile),
        'RMW_FASTRTPS_USE_QOS_FROM_XML=0', 'RMW_FASTRTPS_PUBLICATION_MODE=ASYNCHRONOUS']
    assert berxel[4:] == list(SOURCES['berxel'].command)
    for key in ('mid360', 'bluesea', 'stl19p'):
        actual = manager.source_command(SOURCES[key])
        expected = [part.format(workspace=manager.workspace, bluesea_params=manager.params)
                    for part in SOURCES[key].command]
        assert actual == expected


@pytest.mark.parametrize('mode', ['driver', 'preview'])
def test_driver_and_preview_receive_the_same_deployed_profile_after_ros_shell_cleanup(tmp_path, mode):
    workspace = tmp_path/'fixture-workspace'
    (workspace/'install').mkdir(parents=True)
    setup = tmp_path/'fixture-ros.bash'
    for path in (setup, workspace/'install/setup.bash', workspace/'env.sh'):
        path.write_text('export FASTRTPS_DEFAULT_PROFILES_FILE=/fixture/unwanted.xml\n'
                        'export RMW_FASTRTPS_USE_QOS_FROM_XML=1\n'
                        'export RMW_FASTRTPS_PUBLICATION_MODE=SYNCHRONOUS\n')
    manager = DeviceSources('jetson', home=tmp_path, workspace=workspace, ros_setup=setup)
    printer = "import json,os;print(json.dumps([os.environ.get('FASTRTPS_DEFAULT_PROFILES_FILE'),os.environ.get('RMW_FASTRTPS_USE_QOS_FROM_XML'),os.environ.get('RMW_FASTRTPS_PUBLICATION_MODE')]))"
    if mode == 'driver':
        fixture = Source('berxel', 'unused', (), (sys.executable, '-c', printer), '/unused')
        command = manager.source_command(fixture)
    else:
        manager.preview_script.parent.mkdir(parents=True)
        manager.preview_script.write_text(printer)
        # Direct preflight uses normal shell syntax. The separate opt-in
        # systemd test covers the actual service-manager ExecStart boundary.
        command = manager.ros_command(manager.camera_transport_command(
            ['/usr/bin/python3', '-B', str(manager.preview_script)]))
    completed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
    expected_mode = 'ASYNCHRONOUS' if mode == 'driver' else 'SYNCHRONOUS'
    assert json.loads(completed.stdout) == [str(manager.camera_transport_profile), '0', expected_mode]


def test_camera_profile_template_preserves_tested_buffers_shm_and_default_message_size():
    from xml.etree import ElementTree
    template = Path(__file__).resolve().parents[1]/'deploy/jetson/berxel_preview/fastdds_camera.xml'
    namespace = {'p': 'http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles'}
    xml = ElementTree.fromstring(template.read_text())
    descriptors = xml.findall('p:transport_descriptors/p:transport_descriptor', namespace)
    by_type = {row.find('p:type', namespace).text: row for row in descriptors}
    assert set(by_type) == {'SHM', 'UDPv4'} and len(descriptors) == 2
    assert by_type['SHM'].find('p:segment_size', namespace).text == '67108864'
    udp = by_type['UDPv4']
    assert udp.find('p:sendBufferSize', namespace).text == '4194304'
    assert udp.find('p:receiveBufferSize', namespace).text == '4194304'
    assert udp.find('p:non_blocking_send', namespace).text == 'true'
    assert not xml.findall('.//p:maxMessageSize', namespace)
    participant = xml.find('p:participant', namespace)
    assert participant.get('is_default_profile') == 'true'
    assert participant.find('p:rtps/p:useBuiltinTransports', namespace).text == 'false'
    referenced = {row.text for row in participant.findall('p:rtps/p:userTransports/p:transport_id', namespace)}
    declared = {row.find('p:transport_id', namespace).text for row in descriptors}
    assert referenced == declared
