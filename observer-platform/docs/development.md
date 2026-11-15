# Windows Remote-SSH 与 Git 更新

## Windows VS Code 连接ECS

安装VS Code Remote-SSH扩展；Windows `%USERPROFILE%\.ssh\config` 添加：

```sshconfig
Host robot-cloud-ecs
    HostName <ECS_PUBLIC_IP>
    User <ECS_SSH_USER>
    IdentityFile C:/Users/<WINDOWS_USER>/.ssh/<ECS_PRIVATE_KEY>
    IdentitiesOnly yes
```

ECS SSH安全组只允许自己的公网IP；私钥保留电脑，不加入项目/云容器。
VS Code命令面板执行 `Remote-SSH: Connect to Host`，选robot-cloud-ecs，打开 `/opt/robot-cloud`。
核对终端hostname与目录再操作；可使用Codex修改该独立工程。实际Codex账号/网络另行按客户端配置，不复用机器人设备上传凭据。

## 建立独立私有仓库

当前新项目位于 `/home/orangepi/code` 父仓库内且父仓库有既有修改。本轮没有git init/commit/push，不把整个父仓库上传。
准备好私有remote后在新项目目录执行：

```bash
cd /home/orangepi/code/robot-cloud
git init -b main
git add .
git status --short
# 确认没有.env、密钥、data、.venv、日志和机器人控制源码。
git commit -m '新增：独立只读云端Observer与采集器'
git remote add origin <PRIVATE_REPOSITORY_URL>
git push -u origin main
```

`.gitignore`与`.dockerignore`已排除凭据/数据/虚拟环境；仍应在首次提交人工核对。

## 日常变更与部署

本地修改 → 运行pytest/必要浏览器验证 → Git commit/push → ECS fetch/测试/构建 → 更新云服务。
Frontend/Backend只更新云端；Collector变更只更新机器人独立服务；不更新upper_computer或原Observer。

```bash
# 开发机/CI，先checkout并验证准备部署的新commit。
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
# ECS；只部署已经通过目标commit测试的版本，保留旧镜像。
cd /opt/robot-cloud
git rev-parse HEAD
bash deploy/scripts/update.sh <TESTED_COMMIT_HASH>
```

目标revision的完整测试由开发机/CI完成，不把更新前旧工作树的测试算作新版本验收。脚本拒绝父仓库及未提交tracked修改，fetch并切到目标commit，以commit镜像构建，容器编译检查写入/tmp，保留data/.env/.runtime配置，不执行Git清理或docker prune。
首次执行 `start_cloud.sh` 自动建立commit镜像标签；成功版本写入 `.runtime/active-image-tag`，后续 `cloud.sh` 读取同一标签。每次更新/回滚重新验证并reload Nginx，避免backend地址变更后残留旧解析。
若更新在构建前/健康检查中失败，不自动清理任何数据；检查日志后使用上一成功commit的rollback命令。成功标签只在完成健康检查后改写。
修改Nginx配置先 `nginx -t`，再reload；修改systemd配置前只停对应Collector，不能停机器人控制程序。
更新后会话因服务重启失效，用户重新登录；机器人Collector自动重连，系统不回放无限历史。
