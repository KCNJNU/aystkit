"""Read-only boundaries and deployable config, without a Docker daemon or robot writes."""
import ast
from importlib.util import resolve_name
import json
import shutil
import subprocess
import tempfile
import sys
import sqlite3
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _local_import_closure(*entries):
    """Follow package imports, including lazy imports, without importing code."""
    modules = {}
    for folder in ('collector', 'cloud', 'shared'):
        for path in (ROOT / folder).rglob('*.py'):
            parts = path.relative_to(ROOT).with_suffix('').parts
            name = '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)
            modules[name] = (path, ast.parse(path.read_text()))
    pending, seen = list(entries), set()
    while pending:
        name = pending.pop()
        if name in seen or name not in modules:
            continue
        seen.add(name)
        path, tree = modules[name]
        package = name if path.name == '__init__.py' else name.rpartition('.')[0]
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                target = (resolve_name('.' * node.level + (node.module or ''), package)
                          if node.level else node.module or '')
                imported.append(target)
                imported.extend(target + '.' + alias.name for alias in node.names)
            elif isinstance(node, ast.Call) and getattr(node.func, 'attr', '') == 'import_module':
                # Observation entrypoints must not hide a local dependency in
                # an uninspectable dynamic module name.
                assert node.args and isinstance(node.args[0], ast.Constant), (path, node.lineno)
                target = node.args[0].value
                assert isinstance(target, str) and not target.startswith('.'), (path, target)
                imported.append(target)
        for target in imported:
            # Importing a child also executes its package initializers.
            parts = target.split('.')
            pending.extend('.'.join(parts[:index]) for index in range(1, len(parts) + 1))
    return seen


def test_no_control_or_serial_paths():
    forbidden_imports = {'serial', 'pymodbus', 'gpiozero'}
    forbidden_calls = {'create_publisher', 'create_client', 'ActionClient', 'send_goal_async',
                       'send_goal', 'Serial', 'Popen', 'system'}
    codec_processes = []
    admin_processes = []
    inventory_processes = []
    for folder in ('collector', 'cloud', 'shared'):
        for path in (ROOT / folder).rglob('*.py'):
            tree = ast.parse(path.read_text(), filename=str(path))
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not {x.name.split('.')[0] for x in node.names} & forbidden_imports, path
                    if folder == 'cloud':
                        assert not {x.name.split('.')[0] for x in node.names} & {'rclpy', 'serial'}, path
                if isinstance(node, ast.ImportFrom):
                    assert (node.module or '').split('.')[0] not in forbidden_imports, path
                    if folder == 'cloud':
                        assert (node.module or '').split('.')[0] != 'rclpy', path
                if isinstance(node, ast.Call):
                    name = getattr(node.func, 'attr', getattr(node.func, 'id', ''))
                    admin = {
                        ROOT / 'collector/archive_start_transport.py': (
                            'run_remote_startup', {'stdin': 'subprocess.PIPE', 'stdout': 'subprocess.PIPE',
                                                  'stderr': 'subprocess.PIPE', 'start_new_session': 'True',
                                                  'env': 'environment'}),
                        ROOT / 'collector/robot_shutdown.py': (
                            'spawn_peer', {'stdin': 'subprocess.PIPE', 'stdout': 'subprocess.PIPE',
                                           'stderr': 'stderr', 'bufsize': '0', 'close_fds': 'True'}),
                    }
                    if name == 'Popen' and path in admin:
                        owner = parents.get(node)
                        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            owner = parents.get(owner)
                        function, expected = admin[path]
                        assert owner is not None and owner.name == function, (path, name)
                        assert ast.dump(node.func) == ast.dump(ast.parse('subprocess.Popen', mode='eval').body)
                        assert len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                        assert node.args[0].id == 'command'
                        keywords = {item.arg: item.value for item in node.keywords}
                        assert len(keywords) == len(node.keywords) and set(keywords) == set(expected)
                        for key, expression in expected.items():
                            assert ast.dump(keywords[key]) == ast.dump(ast.parse(expression, mode='eval').body)
                        admin_processes.append((path.name, function))
                        continue
                    if name == 'Popen' and path == ROOT / 'collector/archive_bag_compress.py':
                        owner = parents.get(node)
                        while owner is not None and not isinstance(owner, ast.FunctionDef):
                            owner = parents.get(owner)
                        assert owner is not None and owner.name == '_verify_compressed'
                        assert isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                        assert node.func.value.id == 'subprocess'
                        assert len(node.args) == 1 and isinstance(node.args[0], ast.List)
                        argv = node.args[0].elts
                        assert isinstance(argv[0], ast.Name) and argv[0].id == 'executable'
                        assert [item.value for item in argv[1:-1]] == ['-q', '-d', '-c', '-M128MB', '--']
                        assert isinstance(argv[-1], ast.Call) and isinstance(argv[-1].func, ast.Name)
                        assert argv[-1].func.id == 'str' and len(argv[-1].args) == 1
                        assert isinstance(argv[-1].args[0], ast.Name) and argv[-1].args[0].id == 'path'
                        assert {item.arg for item in node.keywords} == {'stdout', 'stderr'}
                        continue
                    if name == 'Popen' and path == ROOT / 'collector/archive_resources.py':
                        owner = parents.get(node)
                        while owner is not None and not isinstance(owner, ast.FunctionDef):
                            owner = parents.get(owner)
                        assert owner is not None and owner.name == '__call__'
                        container = parents.get(owner)
                        assert isinstance(container, ast.ClassDef) and container.name == 'IsolatedInventoryProbe'
                        assert ast.dump(node.func) == ast.dump(ast.parse('subprocess.Popen', mode='eval').body)
                        assert len(node.args) == 1 and ast.dump(node.args[0]) == ast.dump(ast.parse('self._command(root)', mode='eval').body)
                        keywords = {item.arg: item.value for item in node.keywords}
                        assert set(keywords) == {'stdin', 'stdout', 'stderr'}
                        for key, value in {'stdin': 'subprocess.DEVNULL', 'stdout': 'subprocess.PIPE', 'stderr': 'subprocess.PIPE'}.items():
                            assert ast.dump(keywords[key]) == ast.dump(ast.parse(value, mode='eval').body)
                        command = next(item for item in container.body if isinstance(item, ast.FunctionDef) and item.name == '_command')
                        assert len(command.body) == 1 and isinstance(command.body[0], ast.Return)
                        expected = "[sys.executable, '-B', str(Path(__file__).resolve()), '--inventory-scan', str(Path(root))]"
                        assert ast.dump(command.body[0].value) == ast.dump(ast.parse(expected, mode='eval').body)
                        inventory_processes.append(node)
                        continue
                    if name == 'Popen' and path == ROOT / 'collector/archive_video.py':
                        owner = parents.get(node)
                        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            owner = parents.get(owner)
                        assert owner is not None and owner.name == '_timed_process', (path, name)
                        assert isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                        assert node.func.value.id == 'subprocess'
                        assert len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                        assert node.args[0].id == 'arguments'
                        assert {item.arg for item in node.keywords} == {'stdin', 'stdout', 'stderr'}
                        codec_processes.append(node)
                        continue
                    if name == 'Popen' and path == ROOT / 'collector/archive_sync_index.py':
                        owner = parents.get(node)
                        while owner is not None and not isinstance(owner, ast.FunctionDef):
                            owner = parents.get(owner)
                        assert owner is not None and owner.name == '_extract_attachment'
                        assert ast.dump(node.func) == ast.dump(ast.parse('subprocess.Popen', mode='eval').body)
                        assert len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                        assert node.args[0].id == 'command'
                        expected = {'stdin': 'subprocess.DEVNULL', 'stdout': 'subprocess.PIPE',
                                    'stderr': 'subprocess.PIPE', 'start_new_session': 'True'}
                        assert {item.arg for item in node.keywords} == set(expected)
                        for item in node.keywords:
                            assert ast.dump(item.value) == ast.dump(ast.parse(expected[item.arg], mode='eval').body)
                        assignment = next(item for item in owner.body if isinstance(item, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == 'command' for target in item.targets))
                        command = assignment.value
                        assert isinstance(command, ast.List) and isinstance(command.elts[0], ast.Name)
                        assert command.elts[0].id == 'ffmpeg'
                        literal = [item.value for item in command.elts if isinstance(item, ast.Constant)]
                        assert literal == ['-nostdin', '-v', 'error', 'pipe:1', '-i', '-map',
                                           '0:v:0', '-c:v', 'copy', '-frames:v', '0', '-f', 'null']
                        continue
                    assert name not in forbidden_calls, (path, name)
            if path == ROOT / 'collector/archive_video.py':
                # The one process helper can only be reached by the explicit
                # FFV1 encoder and lossless pixel-verification decoder argv.
                codec_callers = []
                for function in tree.body:
                    if not isinstance(function, ast.FunctionDef):
                        continue
                    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                             and isinstance(node.func, ast.Name) and node.func.id == '_timed_process']
                    if not calls:
                        continue
                    assert function.name in {'_ros_video', '_verify_pixels'}
                    assert len(calls) == 1 and len(calls[0].args) == 3
                    assert isinstance(calls[0].args[0], ast.Name) and calls[0].args[0].id == 'arguments'
                    argv = [node.value for node in ast.walk(function) if isinstance(node, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == 'arguments'
                                    for target in node.targets)]
                    assert len(argv) == 1 and isinstance(argv[0], ast.List)
                    assert isinstance(argv[0].elts[0], ast.Name) and argv[0].elts[0].id == 'ffmpeg'
                    literals = {node.value for node in argv[0].elts if isinstance(node, ast.Constant)}
                    assert {'-nostdin', '-i', '-f', 'rawvideo', '-pix_fmt'} <= literals
                    if function.name == '_ros_video':
                        assert {'-c:v', 'ffv1', '-an'} <= literals
                    assert all(item.arg is not None and item.arg != 'shell' for item in calls[0].keywords)
                    codec_callers.append(function.name)
                assert set(codec_callers) == {'_ros_video', '_verify_pixels'}
    assert len(codec_processes) == 1
    assert len(inventory_processes) == 1
    assert set(admin_processes) == {('archive_start_transport.py', 'run_remote_startup'),
                                    ('robot_shutdown.py', 'spawn_peer')}
    assert len(admin_processes) == 2
    # Hardware startup and coordinated poweroff are explicit desktop/admin
    # entrypoints. Neither the Observer nor its subscription-only archiver may
    # acquire them through a direct, transitive, or lazy package import.
    observation = _local_import_closure('collector.main', 'cloud.backend.app', 'collector.archive_main')
    assert not observation & {
        'collector.archive_start_transport', 'collector.archive_devices',
        'collector.archive_capture_session', 'collector.archive_desktop_entry',
        'collector.robot_shutdown', 'collector.robot_shutdown_power',
        'collector.robot_shutdown_state', 'collector.robot_shutdown_motion',
    }
    assert 'ObserverState' not in (ROOT / 'collector/main.py').read_text()


def test_local_chassis_has_fixed_passive_transport_without_observer_dependency():
    receiver = (ROOT / 'collector/local_chassis.py').read_text()
    tree = ast.parse(receiver)
    assignments = {node.targets[0].id: node.value for node in tree.body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    assert ast.literal_eval(assignments['SOURCE']) == 'ws://127.0.0.1:8765/ws'
    forbidden_calls = {'send', 'send_str', 'send_json', 'send_bytes', 'ping', 'pong',
                       'getenv', 'create_publisher', 'create_client', 'Serial'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert getattr(node.func, 'attr', getattr(node.func, 'id', '')) not in forbidden_calls
        if isinstance(node, ast.Import):
            assert not {part.name.split('.')[0] for part in node.names} & {'os', 'serial', 'rk3588_observer', 'websockets'}
    connect = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and
               getattr(node.func, 'attr', '') == 'ws_connect']
    assert len(connect) == 1 and isinstance(connect[0].args[0], ast.Name) and connect[0].args[0].id == 'SOURCE'
    keywords = {item.arg: item.value for item in connect[0].keywords}
    assert ast.literal_eval(keywords['autoping']) is True
    assert ast.literal_eval(keywords['heartbeat']) is None
    assert isinstance(keywords['max_msg_size'], ast.Name) and keywords['max_msg_size'].id == 'MAX_MESSAGE_BYTES'
    sessions = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and
                getattr(node.func, 'attr', '') == 'ClientSession']
    assert len(sessions) == 1
    assert ast.literal_eval(next(item.value for item in sessions[0].keywords if item.arg == 'trust_env')) is False


def test_compose_schema_and_only_intended_ports():
    # Compose config parses the actual file with a temporary placeholder env file.
    # No production .env is created and no daemon/container is used.
    if not shutil.which('docker'):
        pytest.skip('Docker Compose CLI not installed; runtime/container tests remain separate')
    with tempfile.TemporaryDirectory(prefix='robot-cloud-compose-') as directory:
        tmp = Path(directory)
        env = tmp / 'test.env'
        env.write_text((ROOT / '.env.example').read_text())
        compose = tmp / 'compose.yml'
        content = (ROOT / 'deploy/docker-compose.yml').read_text()
        content = content.replace('env_file: ../.env', 'env_file: ' + str(env))
        content = content.replace('context: ..', 'context: ' + str(ROOT))
        compose.write_text(content)
        prefix = ['rtk'] if shutil.which('rtk') else []
        result = subprocess.run(prefix + ['docker', 'compose', '--env-file', str(env), '-f', str(compose),
                                 'config', '--format', 'json'], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        config = json.loads(result.stdout)
        services = config['services']
        backend = services['backend']
        assert len(backend['ports']) == 1
        assert backend['ports'][0]['host_ip'] == '127.0.0.1'
        assert backend['ports'][0]['target'] == 8888
        assert backend['read_only'] is True
        assert backend['init'] is True
        assert backend['environment']['ALLOW_INSECURE_LOCAL'] == 'false'
        assert {p['target'] for p in services['nginx']['ports']} == {80, 443}
        assert not {'devices', 'privileged', 'network_mode'} & set(backend)


def test_production_nginx_and_systemd_boundaries():
    conf = (ROOT / 'deploy/nginx/https.conf.template').read_text()
    assert 'ssl_protocols TLSv1.2 TLSv1.3' in conf
    assert 'proxy_set_header Upgrade $http_upgrade' in conf
    assert 'proxy_set_header X-Forwarded-Proto https' in conf
    assert 'client_max_body_size 1m' in conf
    assert 'limit_req zone=upload_limit' in conf
    assert 'limit_req zone=login_limit' in conf
    unit = (ROOT / 'deploy/systemd/robot-cloud-collector.service').read_text()
    assert 'Restart=on-failure' in unit
    assert 'ROS_LOG_DIR=/tmp/robot-cloud-ros' in unit
    assert 'host_node' not in unit and 'run_system' not in unit
    assert 'KillMode=control-group' in unit


def test_nginx_runtime_generation_does_not_edit_tracked_template():
    template = ROOT / 'deploy/nginx/conf.d/observer.conf'
    before = template.read_bytes()
    with tempfile.TemporaryDirectory(prefix='robot-cloud-nginx-') as directory:
        command = [sys.executable, str(ROOT / 'deploy/scripts/render_nginx.py'), '--output-dir', directory]
        subprocess.run(command + ['--http'], check=True, capture_output=True)
        target = Path(directory) / 'observer.conf'
        assert target.read_bytes() == before
        subprocess.run(command + ['observer.example.invalid'], check=True, capture_output=True)
        secure = target.read_text()
        assert '__DOMAIN__' not in secure and 'server_name observer.example.invalid;' in secure
        assert secure.count('X-Forwarded-For $proxy_add_x_forwarded_for') == 9
        native = secure.split('location ^~ /native-camera/ {', 1)[1].split('}', 1)[0]
        assert 'proxy_buffering off;' in native
        assert 'proxy_request_buffering off;' in native
        assert 'proxy_pass http://backend:8888;' in native
        assert 'proxy_read_timeout 45s;' in native
        for rejected in ('--http', 'https://observer.example.invalid', '../bad', 'x;return 200;'):
            result = subprocess.run(command + [rejected], capture_output=True)
            assert result.returncode != 0
            assert target.read_text() == secure
    assert template.read_bytes() == before


def test_deployment_scripts_syntax_and_success_tag_boundary():
    for path in (ROOT / 'deploy/scripts').glob('*.sh'):
        subprocess.run(['bash', '-n', str(path)], check=True, capture_output=True)
    start = (ROOT / 'deploy/scripts/start_cloud.sh').read_text()
    rollback = (ROOT / 'deploy/scripts/rollback.sh').read_text()
    wrapper = (ROOT / 'deploy/scripts/cloud.sh').read_text()
    assert 'PYTHONPYCACHEPREFIX=/tmp/pycache' in start
    for script in (start, rollback):
        assert script.index('up -d') < script.index('nginx -t') < script.index('nginx -s reload')
        assert script.index('/healthz') < script.index('image_tag.py')
    assert '.runtime/active-image-tag' in wrapper
    assert '.runtime/' in (ROOT / '.gitignore').read_text()
    assert '.runtime' in (ROOT / '.dockerignore').read_text()


def test_sqlite_online_backup_has_private_permission_and_preserves_source():
    with tempfile.TemporaryDirectory(prefix='robot-cloud-backup-') as directory:
        temporary = Path(directory)
        database = temporary / 'fixture.sqlite'
        with sqlite3.connect(database) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE fixture (value INTEGER)')
            connection.execute('INSERT INTO fixture VALUES (0)')
            connection.commit()
            result = subprocess.run([sys.executable, str(ROOT / 'deploy/scripts/backup.py'), str(database),
                                     '--output-dir', str(temporary / 'backups')], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            backup = Path(result.stdout.strip())
            assert stat.S_IMODE(backup.stat().st_mode) == 0o600
            with sqlite3.connect(backup) as copied:
                assert copied.execute('SELECT value FROM fixture').fetchone()[0] == 0
            assert connection.execute('SELECT value FROM fixture').fetchone()[0] == 0
