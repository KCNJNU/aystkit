"""Exercise launcher shell options without starting ROS or the collector."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('workspace', [False, True])
def test_ros_setup_allows_unset_trace_variables_and_restores_nounset(tmp_path, workspace):
    ros_setup = tmp_path / 'ros_setup.bash'
    ros_setup.write_text('if [[ -n "$AMENT_TRACE_SETUP_FILES" ]]; then :; fi\nROS_SETUP_LOADED=yes\n')
    workspace_setup = tmp_path / 'workspace_setup.bash'
    workspace_setup.write_text('if [[ -n "$COLCON_TRACE" ]]; then :; fi\nWORKSPACE_SETUP_LOADED=yes\n')
    # A minimal environment guarantees both trace variables are unset and avoids
    # consuming any production configuration. Substitute setup and exec only;
    # run the actual launcher, with no ROS initialization or Python execution.
    environment = {
        'PATH': os.defpath,
        'FAKE_ROS_SETUP': str(ros_setup),
        'COLLECTOR_LAUNCHER': str(ROOT / 'deploy/scripts/run_collector.sh'),
        'EXPECTED_PYTHON': str(ROOT / '.venv/bin/python'),
    }
    if workspace:
        environment['ROBOT_ROS_SETUP'] = str(workspace_setup)
    script = r'''
source() {
    if [[ "$1" == /opt/ros/humble/setup.bash ]]; then
        builtin source "$FAKE_ROS_SETUP"
    else
        builtin source "$@"
    fi
}
exec() {
    [[ $- == *u* && $- == *e* ]] || return 91
    shopt -qo pipefail || return 92
    [[ ${ROS_SETUP_LOADED:-} == yes ]] || return 93
    if [[ -n ${ROBOT_ROS_SETUP:-} ]]; then
        [[ ${WORKSPACE_SETUP_LOADED:-} == yes ]] || return 94
    fi
    [[ $# == 3 && "$1" == "$EXPECTED_PYTHON" && "$2" == -m && "$3" == collector.main ]] || return 95
    printf 'setup completed; strict shell options restored; collector launch intercepted\n'
}
builtin source "$COLLECTOR_LAUNCHER"
'''
    result = subprocess.run(['bash', '--noprofile', '--norc', '-c', script],
                            env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'setup completed; strict shell options restored; collector launch intercepted\n'
