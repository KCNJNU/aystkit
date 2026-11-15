"""Real Firefox registration/approval flow against an ephemeral loopback backend.

Synthetic credentials only. No robot, production account, or external data is used.
"""
import asyncio
import base64
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

import websockets

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'artifacts' / 'registration'
SERVER = 'http://127.0.0.1:19088'
DRIVER = 'http://127.0.0.1:19090'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, method='GET', data=None, timeout=15, opener=OPENER):
    body = None if data is None else json.dumps(data).encode()
    headers = {'Content-Type': 'application/json'}
    if url.startswith(SERVER + '/'):
        headers['Origin'] = SERVER
    req = urllib.request.Request(url, body, headers, method=method)
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)


def verify():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    processes, logs = [], []
    session = None
    profile = Path.home() / 'snap/firefox/common/observer-registration-test'
    profile.mkdir(parents=True, exist_ok=True)
    result = {'scope': 'Real Firefox + ephemeral real backend, synthetic credentials only', 'screenshots': []}
    try:
        for command, name in [([sys.executable, '-m', 'tests.serve_browser'], 'backend'),
                (['/snap/bin/geckodriver', '--host', '127.0.0.1', '--port', '19090', '--profile-root', str(profile)], 'geckodriver')]:
            log = (ARTIFACTS / (name + '.log')).open('w')
            logs.append(log)
            processes.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
                                               env={**os.environ, 'ROBOT_CLOUD_BROWSER_TEST': 'YES'}))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                request(SERVER + '/healthz')
                request(DRIVER + '/status')
                break
            except (OSError, ValueError):
                time.sleep(.2)
        created = request(DRIVER + '/session', 'POST', {'capabilities': {'alwaysMatch': {
            'browserName': 'firefox', 'webSocketUrl': True,
            'moz:firefoxOptions': {'binary': '/snap/firefox/current/usr/lib/firefox/firefox', 'args': ['-headless']}}}}, timeout=55)['value']
        session = created['sessionId']

        def call(endpoint, data=None, method='POST'):
            return request(DRIVER + f'/session/{session}' + endpoint, method, data)['value']

        def evaluate(script):
            return call('/execute/sync', {'script': script, 'args': []})

        def wait(expression, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = evaluate('return (' + expression + ')')
                if value:
                    return value
                time.sleep(.2)
            raise AssertionError('Timed out: ' + expression)

        async def resize(width, height):
            async with websockets.connect(created['capabilities']['webSocketUrl'], proxy=None) as ws:
                async def command(identifier, method, params):
                    await ws.send(json.dumps({'id': identifier, 'method': method, 'params': params}))
                    while True:
                        response = json.loads(await ws.recv())
                        if response.get('id') == identifier:
                            assert response.get('type') != 'error', response
                            return response['result']
                tree = await command(1, 'browsingContext.getTree', {})
                await command(2, 'browsingContext.setViewport', {'context': tree['contexts'][0]['context'],
                              'viewport': {'width': width, 'height': height}, 'devicePixelRatio': 1})

        def capture(name, width=1280, height=900):
            asyncio.run(resize(width, height))
            time.sleep(.25)
            layout = evaluate('return {width:innerWidth,scroll:document.documentElement.scrollWidth}')
            assert layout['scroll'] <= layout['width'] + 2, (name, layout)
            (ARTIFACTS / (name + '.png')).write_bytes(base64.b64decode(call('/screenshot', method='GET')))
            result['screenshots'].append(name + '.png')

        def submit(selector, fields):
            for field, value in fields.items():
                evaluate('document.querySelector(' + json.dumps(field) + ').value=' + json.dumps(value))
            evaluate('document.querySelector(' + json.dumps(selector) + ').requestSubmit()')

        def login(username, password):
            call('/url', {'url': SERVER + '/login'})
            submit('#login-form', {'#username': username, '#password': password})

        call('/url', {'url': SERVER + '/login'})
        wait("document.querySelector('a[href=\"/register\"]')")
        evaluate("document.querySelector('a[href=\"/register\"]').click()")
        wait("location.pathname==='/register'")
        capture('register-desktop')
        capture('register-mobile', 390, 844)
        # Input IDs are read from the actual form to keep label wiring exercised.
        fields = evaluate("return [...document.querySelectorAll('#register-form input')].map(e=>({id:e.id,type:e.type}))")
        assert len(fields) == 3, fields
        submit('#register-form', {'#' + field['id']: 'browser_member' if field['type'] != 'password' else 'isolated-member-password'
                                  for field in fields})
        wait("location.pathname==='/pending'&&document.body.textContent.includes('当前未获取权限')")
        assert evaluate("return !document.querySelector('canvas')&&!window.CloudObserver&&!window.RKObserverDashboard")
        capture('pending-mobile', 390, 844)
        capture('pending-desktop')
        result['pendingHasNoDashboard'] = True

        # Independent admin HTTP session approves while the browser remains pending.
        admin = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        auth = request(SERVER + '/api/login', 'POST', {'username': 'testviewer', 'password': 'isolated-browser-test-only'}, opener=admin)
        assert auth['role'] == 'admin'
        users = request(SERVER + '/api/admin/users', opener=admin)['users']
        member = next(row for row in users if row['username'] == 'browser_member')
        assert member['status'] == 'pending'
        request(SERVER + f"/api/admin/users/{member['id']}/approve", 'POST', {}, opener=admin)
        wait("location.pathname==='/'&&window.RKObserverDashboard")
        result['approvalAutoEntersDashboard'] = True
        assert evaluate("return [...document.querySelectorAll('a[href=\"/users\"]')].every(e=>e.hidden||getComputedStyle(e).display==='none')")
        result['memberHasNoAdminLink'] = True
        evaluate("document.getElementById('logout-button').click()")
        wait("location.pathname==='/login'")

        # Create another pending account through public registration for UI approval.
        request(SERVER + '/api/register', 'POST', {'username': 'browser_pending', 'password': 'isolated-pending-password'})
        login('testviewer', 'isolated-browser-test-only')
        wait("location.pathname==='/'&&document.querySelector('a[href=\"/users\"]')&&!document.querySelector('a[href=\"/users\"]').hidden")
        evaluate("document.querySelector('a[href=\"/users\"]').click()")
        wait("location.pathname==='/users'&&document.body.textContent.includes('browser_pending')")
        capture('users-desktop')
        capture('users-mobile', 390, 844)
        buttons = evaluate("return [...document.querySelectorAll('button')].filter(e=>e.textContent.includes('批准')).length")
        assert buttons == 1, buttons
        evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent.includes('批准')).click()")
        wait("![...document.querySelectorAll('button')].some(e=>e.textContent.includes('批准'))")
        users = request(SERVER + '/api/admin/users', opener=admin)['users']
        assert all(row['status'] == 'approved' for row in users), users
        result['observerUiApproval'] = True
        result['pass'] = True
    finally:
        (ARTIFACTS / 'browser-registration-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if session:
            try:
                request(DRIVER + '/session/' + session, 'DELETE')
            except OSError:
                pass
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    verify()
