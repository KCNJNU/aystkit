# 启动数据仓库

当前部署入口是根 `docker-compose.yml`，持久数据存储在 PostgreSQL、MinIO、Redis 和 dataset_data 卷中。

## Windows 一键启动

1. 安装并启动 Docker Desktop，选择 Linux 容器。
2. 根 `.env` 设置 `BOOTSTRAP_ADMIN_EMAIL` 和至少 12 位的 `BOOTSTRAP_ADMIN_PASSWORD`。首次没有配置文件时，启动器复制示例并提示填写，已有文件不会覆盖。千问使用已有 `QWEN_*` 配置，见 [AI_INTEGRATION](AI_INTEGRATION.md)。
3. 双击 **start-all.cmd**。启动器构建并启动基础设施、执行 Prisma 迁移、初始化管理员和独立数据版本 Git 目录，再检查 API 与 Web 是否就绪。
4. 出现 `Warehouse is ready.` 后访问 **http://localhost:5173**。
5. 双击 **stop-all.cmd** 停止服务，持久数据保留。

已有账号和密码不会被初始化覆盖；管理员首次登录需修改临时密码。所需端口：3000、5173、5432、6379、9000、9001。启动器不会终止其他占用端口的进程。

可选参数：

```powershell
.\start-all.cmd -CheckOnly
.\start-all.cmd -SkipSeed
.\start-all.cmd -NoBuild
.\start-all.cmd -ReadyTimeoutSeconds 600
```

更新源码后应重建镜像，不使用 `-NoBuild`。启动失败后可查看：

```powershell
docker compose ps
docker compose logs --tail 100 backend worker migration storage-init
```

首次构建会下载 Node、Nginx、系统依赖和 DVC。基础镜像下载失败时检查 Docker Desktop 的可用网络、代理及 registry-mirrors 配置后重试。

## Linux 启动

服务器安装 Docker Engine、Docker Compose 插件和 `curl`，确保当前 SSH 用户可以访问 Docker。项目放在 `~/DBProg/RobotHygienism`，根 `.env` 保留已有管理员、存储和 `QWEN_*` 配置。局域网使用服务器 IP 配置 `OBJECT_STORAGE_PUBLIC_ENDPOINT` 和 `CORS_ORIGIN`；通过 HTTP 访问时设置 `AUTH_COOKIE_SECURE=false`，开放 5173 和 9000 端口。

```bash
cd ~/DBProg/RobotHygienism
bash scripts/start-linux.sh --check-only
bash scripts/start-linux.sh
```

脚本从自身位置定位项目，构建并启动 Compose 服务，执行数据库迁移、管理员初始化和数据版本 Git 目录初始化，再检查服务就绪。已有数据迁移恢复后使用 `--skip-seed`；只有已经导入或构建过当前版本镜像时才使用 `--no-build`：

```bash
bash scripts/start-linux.sh --skip-seed --no-build
bash scripts/stop-linux.sh
```

停止脚本保留所有数据卷。源码更新后去掉 `--no-build` 重建；启动较慢时可追加 `--ready-timeout 600`。启动完成后访问 `http://<服务器IP>:5173`。

## 局域网访问

服务器电脑右键 **enable-lan.cmd → 以管理员身份运行**。脚本选择有默认网关的物理网卡，更新根 `.env` 的公开下载地址和 Cookie 配置，添加限定所选网卡、本机 IP 与本地子网的 5173、9000 入站规则，并构建、启动、检查服务。

其他设备访问输出的 `http://<WINDOWS_IP>:5173`，使用独立账号登录。网页和上传经过 5173，带签名的文件下载经过 9000；数据库、Redis、后端和 MinIO 控制台仅绑定本机。IP 改变后重新运行脚本。

```powershell
.\enable-lan.ps1 -IPAddress 192.168.1.100
.\enable-lan.ps1 -NoBuild
```

HTTP 局域网配置使用 `AUTH_COOKIE_SECURE=false`；HTTPS 使用 `true`。服务器和 Docker 需持续运行。远端无法访问时检查访客网络、客户端隔离及防火墙配置。

## 本机开发

本机准备 Node.js、PostgreSQL、Redis 和 MinIO，根 `.env` 配置完成后运行：

```powershell
npm.cmd ci
npx.cmd prisma generate --schema apps/api/prisma/schema.prisma
npx.cmd prisma migrate deploy --schema apps/api/prisma/schema.prisma
npm.cmd run prisma:seed --workspace @cleaning-robot/api
npm.cmd run dev
```

开发启动同时运行 NestJS、Worker 和 Vite Web，Ctrl+C 由监督器停止子服务。Docker 启停脚本只负责 Compose 服务。不要同时运行占用相同端口的开发与 Docker 应用。

## 文件和数据版本

创建数据集后，可在文件管理器逐级创建目录、上传文件或整个文件夹。文件按 `<数据集名称>/<目标文件夹>/<文件名>` 保存，目录使用标记对象保持可见；文件夹选择上传其文件，空目录不会由浏览器自动上传。上传文件自动加入最新可用草稿，冻结版本保持不变。

文件删除先进入回收站；数据集删除需输入完整名称，经引用和任务检查后由后台任务清理。细节见 [文件管理](docs/FILE_MANAGEMENT.md)。

普通上传不创建 Git 提交或执行 DVC。冻结后发布生成清单、Git tag 与 DVC 快照；API 与 Worker 的 Git/DVC 目录共享 dataset_data 卷。恢复说明见 [DATASET_RECOVERY](docs/DATASET_RECOVERY.md)。

## 验收与数据兼容

- 未登录访问 Web 跳转登录页；用户权限见 [USER_PERMISSIONS](USER_PERMISSIONS.md)。
- `/health` 检查存活；`/ready` 检查基础设施；`/api/system/status` 提供详细状态。
- 核验文件上传、下载、多级目录、回收站、版本冻结/发布及 Worker 任务。
- 核验白色界面的首页与 AI 助手，千问流式对话、历史、生成参数及当前权限内的数据来源保持可用。未配置千问 Key 时明确显示未配置，不生成模拟回答。
- 新安装与旧库升级均使用 Prisma 历史迁移。代码精简没有执行删表、卷删除或数据库重置。
