#!/usr/bin/env python3
"""Isolated standalone deployment; no production hosts or robot hardware used."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent


def password_hash(password):
    salt = secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return f"scrypt$16384$8$1${encode(salt)}${encode(result)}"


def private_write(path, text):
    # Exclusive creation prevents accidental rotation of a working installation.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def initialize(path, port, project):
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    observer_password, admin_password = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    postgres_password, storage_password = secrets.token_urlsafe(30), secrets.token_urlsafe(30)
    robot_id = "robot-001"
    warehouse_robot_id = str(uuid.uuid4())
    config = {
        "COMPOSE_PROJECT_NAME": project,
        "PLATFORM_IMAGE_PREFIX": project,
        "PLATFORM_PORT": str(port),
        "OBSERVER_BUILD_TARGET": "deployed-frontend",
        "PUBLIC_ORIGIN": f"http://localhost:{port}",
        "ROBOT_ID": robot_id,
        "BOOTSTRAP_ROBOT_ID": warehouse_robot_id,
        "BOOTSTRAP_ROBOT_EXTERNAL_ID": robot_id,
        "ROBOT_FLEET_JSON": json.dumps({robot_id: {"name": "01号车", "warehouseId": warehouse_robot_id}}, ensure_ascii=False, separators=(",", ":")),
        "DEVICE_CREDENTIALS_JSON": json.dumps([{
            "robot_id": robot_id, "source_id": "primary", "role": "primary",
            "token": secrets.token_urlsafe(36),
        }], separators=(",", ":")),
        "VIEWER_USERNAME": "observer",
        "VIEWER_PASSWORD_HASH": password_hash(observer_password),
        "VIEWER_PASSWORD_PLAIN": observer_password,
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "POSTGRES_PASSWORD": postgres_password,
        "DATABASE_URL": f"postgresql://robot_platform:{postgres_password}@postgres:5432/robot_platform?schema=public",
        "MINIO_ROOT_USER": "platformstorage",
        "MINIO_ROOT_PASSWORD": storage_password,
        "BOOTSTRAP_ADMIN_EMAIL": "admin@robot-platform.local",
        "BOOTSTRAP_ADMIN_PASSWORD": admin_password,
        "RATE_LIMIT_KEY_SALT": secrets.token_urlsafe(48),
        "QWEN_API_KEY": "",
    }
    content = "# PRIVATE: generated once. Keep this file with your private backups.\n"
    # Compose single quotes prevent interpolation of scrypt's dollar signs.
    content += "".join(f"{key}='{value}'\n" for key, value in config.items())
    private_write(path, content)
    return True


def read_config(path):
    # Use the same literal-only dotenv reader as the acceptance test.
    import importlib.util
    spec = importlib.util.spec_from_file_location("platform_smoke", ROOT / "deployment/smoke-test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_env(path)


def select_frontend(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line for line in lines if not line.startswith("OBSERVER_BUILD_TARGET=")]
    lines.append("OBSERVER_BUILD_TARGET='rebuilt-frontend'")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write("\n".join(lines) + "\n")
    try:
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run(command, *, extra_env=None):
    environment = os.environ.copy()
    # Prevent an unrelated working directory's .env from selecting a project.
    if extra_env:
        environment.update(extra_env)
    subprocess.run(command, cwd=ROOT, env=environment, check=True)


def main():
    parser = argparse.ArgumentParser(description="观测平台与数仓独立部署（Docker Compose）")
    parser.add_argument("command", choices=("start", "stop", "status", "check", "logs", "init"), nargs="?", default="start")
    parser.add_argument("--env-file", type=Path, default=ROOT / "runtime-data/deployment.env")
    parser.add_argument("--port", type=int, default=8080, help="首次初始化的本机端口")
    parser.add_argument("--project", default="aystkit", help="首次初始化的独立 Docker 项目名")
    parser.add_argument("--rebuild-frontend", action="store_true", help="从 React 源码重建页面；默认使用与云端一致的随包页面")
    parser.add_argument("--no-build", action="store_true", help="使用已构建或导入的镜像")
    parser.add_argument("--ask-password", action="store_true", help="检查时输入当前密码，适用于修改初始密码后")
    args = parser.parse_args()
    if args.no_build and args.rebuild_frontend:
        parser.error("--no-build 和 --rebuild-frontend 不能同时使用")
    if not 1 <= args.port <= 65535 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", args.project):
        parser.error("端口或项目名无效")
    path = args.env_file.expanduser().resolve()
    if args.command in ("start", "init"):
        if initialize(path, args.port, args.project):
            print(f"已生成私密配置与初始账号：{path}", flush=True)
    if not path.is_file():
        parser.error("部署配置不存在，请先运行 start 或 init")
    config = read_config(path)
    origin = config.get("PUBLIC_ORIGIN", "")
    if origin != f"http://localhost:{config.get('PLATFORM_PORT')}":
        parser.error("本地部署 PUBLIC_ORIGIN 应为 http://localhost:端口；公网 HTTPS 请使用部署文档中的生产方案")
    project = config.get("COMPOSE_PROJECT_NAME", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", project):
        parser.error("配置中的 COMPOSE_PROJECT_NAME 无效")
    if args.command == "init":
        return
    if args.command == "start" and args.rebuild_frontend:
        select_frontend(path)
        config["OBSERVER_BUILD_TARGET"] = "rebuilt-frontend"
    if args.command == "check":
        run([sys.executable, str(ROOT / "deployment/smoke-test.py"), "--base-url", origin, "--env-file", str(path)] + (["--ask-password"] if args.ask_password else []))
        return
    if not shutil.which("docker"):
        parser.error("请先安装并启动 Docker Engine/ Docker Desktop、Docker Compose v2 和 Buildx")
    run(["docker", "compose", "version"])
    # -p and --env-file keep this installation separate from existing platforms.
    compose = ["docker", "compose", "--project-name", project, "--env-file", str(path), "-f", str(ROOT / "compose.yaml")]
    # Explicit installation values outrank unrelated shell exports in Compose.
    target = {**config, "OBSERVER_BUILD_TARGET": config.get("OBSERVER_BUILD_TARGET", "deployed-frontend")}
    if args.command == "start":
        run(["docker", "info", "--format", "Docker {{.ServerVersion}}"])
        if not args.no_build:
            run(["docker", "buildx", "version"])
            run(compose + ["build"], extra_env=target)
        run(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "240"], extra_env=target)
        print(f"\n观测页面：{origin}/\n数仓：{origin}/dbprog/\n初始账号：{path}\n首次登录数仓后按页面要求修改密码。", flush=True)
    elif args.command == "stop":
        run(compose + ["stop", "--timeout", "60"], extra_env=target)
        print("平台已停止，数据库和文件数据卷保留。")
    elif args.command == "status":
        run(compose + ["ps", "--all"], extra_env=target)
    elif args.command == "logs":
        run(compose + ["logs", "--tail", "100", "observer", "warehouse-api", "worker", "migration"], extra_env=target)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print("命令未成功完成，请运行 python3 run-platform.py logs 核对。不要公开私密配置。", file=sys.stderr)
        raise SystemExit(error.returncode)
    except (OSError, ValueError) as error:
        print(f"部署失败：{type(error).__name__}；请检查目录权限和配置。", file=sys.stderr)
        raise SystemExit(1)
