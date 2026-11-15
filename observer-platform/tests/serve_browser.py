"""Opt-in loopback-only browser test service. Never uses real device credentials.

ROBOT_CLOUD_BROWSER_TEST=YES python -m tests.serve_browser
Uses an ephemeral SQLite directory; no ROS/robot/production data is read.
"""
import os
import tempfile

from aiohttp import web

from cloud.backend.app import create_app
from cloud.backend.security import Config, hash_password


def main():
    if os.environ.get('ROBOT_CLOUD_BROWSER_TEST') != 'YES':
        raise SystemExit('Explicit ROBOT_CLOUD_BROWSER_TEST=YES required; test credentials are not production credentials')
    with tempfile.TemporaryDirectory(prefix='robot-cloud-browser-backend-') as directory:
        config = Config(robot_id='ISOLATED_BROWSER_FIXTURE', device_credentials=[{
            'token': 'ISOLATED-UNUSED-DEVICE-TOKEN-' + 'x' * 32, 'robot_id': 'ISOLATED_BROWSER_FIXTURE',
            'source_id': 'TEST_ONLY', 'role': 'primary'}], viewer_username='testviewer',
            viewer_password_hash=hash_password('isolated-browser-test-only'),
            session_secret='ISOLATED-BROWSER-TEST-SECRET-' + 'x' * 32,
            public_origin='http://127.0.0.1:19088', data_dir=directory, allow_insecure_local=True)
        print('ISOLATED TEST ONLY: http://127.0.0.1:19088; user testviewer; password isolated-browser-test-only')
        web.run_app(create_app(config), host='127.0.0.1', port=19088, access_log=None)


if __name__ == '__main__':
    main()
