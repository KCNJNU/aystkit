# 机器人观测平台与数仓

包含观测平台、数仓、机器人分类、数据集、上传下载、后台处理、三维传感器配置与传感器资料模块。提供独立部署入口，默认保留随包的云端页面资源。

## 获取并启动

先安装并启动 Docker Engine 或 Docker Desktop，确认具备 Docker Compose v2、Buildx 和 Python 3.10 以上版本。首次构建需要网络下载基础镜像与依赖；建议准备至少 4 核、8 GB 内存和 20 GB 空闲磁盘，业务视频另计。

```bash
git clone https://github.com/KCNJNU/aystkit.git
cd aystkit
python3 run-platform.py start
```

Windows 可将 `python3` 换成 `py -3`，并使用 Docker Desktop 的 Linux 容器模式。Linux 用户需要有 Docker daemon 的访问权限。

启动完成后：

| 内容 | 地址或文件 |
| --- | --- |
| 观测平台 | http://localhost:8080/ |
| 数仓 | http://localhost:8080/dbprog/ |
| 三维传感器配置 | http://localhost:8080/coverage |
| 传感器资料 | http://localhost:8080/sensor-library/ |
| 私密配置与初始账号 | `runtime-data/deployment.env` |

账号文件中的 `VIEWER_USERNAME` / `VIEWER_PASSWORD_PLAIN` 用于观测平台；`BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` 用于数仓。两套登录独立，数仓首次登录会要求修改密码。配置仅首次生成，后续启动保留账号、密码和数据；修改后的业务密码保存在数据库中。配置文件不会提交到 GitHub。

```bash
python3 run-platform.py status
python3 run-platform.py check
python3 run-platform.py stop
python3 run-platform.py start --no-build
```

修改初始密码后，检查时用 `python3 run-platform.py check --ask-password` 隐藏输入当前密码。更换首次安装端口可用 `start --port 8088`；已有安装修改私密配置中的 `PLATFORM_PORT` 和 `PUBLIC_ORIGIN`，两者必须对应，然后重新启动。浏览器使用打印出的 `localhost` 地址，避免混用 IP 导致 Origin 不匹配。

## 初始化与持久化

启动依次完成 PostgreSQL、Redis、MinIO 健康检查，Prisma 数据模型迁移，内部空间/项目/管理员/机器人初始化，两个存储桶及 DVC Git 工作目录初始化，再启动数仓 API、worker、观测服务和 Nginx 网关。

仅网关绑定本机回环端口。数据库、Redis、MinIO 不直接向外开放；网页的上传、下载、WebSocket 和实时事件统一经过观测网关。初始化及运行不会启动或停止实车底盘、相机、雷达、导航和 ROS 驱动。

数据使用项目独立的 Docker 命名卷：`postgres_data`、`redis_data`、`minio_data`、`dataset_data`、`observer_data`。`stop` 保留这些卷。不要执行 `docker compose down -v` 或手动清理数据卷。配置文件应与私有备份一同保留；单独复制仓库目录不会复制 Docker 数据卷。

## 云端一致性的范围

新安装具有相同的页面和业务模块，但开始时只有新管理员、默认机器人及空项目。真实机器人接入前显示离线；实时画面和传感器数据需要按 [多机器人接入说明](observer-platform/docs/multi-robot.md) 配置本次安装的设备凭据与地址。接入公网需部署 HTTPS 与域名，不能把本机 HTTP 配置直接开放到互联网。生产 Nginx 与 TLS 方案见 `observer-platform/deploy/`。

现有云端的账号、机器人历史、数据集、视频属于运行数据，未放入公开仓库。需要复现这些内容时，应私下迁移 PostgreSQL、MinIO、数仓 workspace 和观测数据，配置相应机器人绑定，并核对恢复结果。AI 问答还需要接收方自己的 `QWEN_API_KEY`；不配置时普通数仓可用，真实 AI 请求不可用。

## 修改和重建

默认使用 `observer-platform/cloud/frontend/` 随包的已部署页面，避免构建工具改变交付时的云端外观。观测后端与数仓 API/worker 从本仓库代码构建。

编辑数仓或三维配置页面后运行：

```bash
python3 run-platform.py start --rebuild-frontend
```

该选项从 React/TypeScript 源码重建两套页面，并使用 `/dbprog/` 与 `/coverage-assets/` 的正确路径。选择会保存在私密配置中，后续普通 `start` 继续使用源码构建。需要回到随包页面时，将配置中的 `OBSERVER_BUILD_TARGET` 改为 `deployed-frontend` 后重新启动。

| 目录 | 内容 |
| --- | --- |
| `deployment/`、`compose.yaml`、`run-platform.py` | 统一启动、初始化及验收脚本 |
| `observer-platform/` | 观测后端、页面、网关和维护源码 |
| `observer-platform/coverage-config/` | 三维传感器配置 React/TypeScript 源码 |
| `warehouse-platform/` | 数仓 NestJS、worker、React、Prisma 模型和迁移 |
| `deployed-snapshot/` | 2026-11-15 线上运行代码的只读对照快照 |

初始源码归档来源见 [交接说明](交接说明.txt)、[SOURCE_MANIFEST.json](SOURCE_MANIFEST.json)、[版本对照.json](版本对照.json)。这些来源文件记录的是初始归档；最新部署验收见 [STANDALONE_CHECKS.json](deployment/STANDALONE_CHECKS.json)，当前交付文件校验见 `SHA256SUMS.txt`，后续改动以 Git 提交为准。

## 验收记录

2026-11-15 在独立 Docker 项目及新数据卷中完成源码构建、全套初始化与验证：后端 859 项、前端 324 项测试全部通过；脚本测试 6 项通过，1 项 Windows 专用测试在 Linux 下跳过；Prisma 模型校验通过。默认页面 631 份文件与随包云端资产逐文件 SHA-256 一致；可选的两套前端源码重建成功。

已实际验证登录、目录访问、分块上传、后台处理、认证下载与 SHA-256、测试数据清理、整套停止再启动后的账号和文件持久化，以及观测/网关异常退出后的自动恢复。Windows 与 macOS 完整启动尚未实测。验收未连接实车，也未导入现网账号或视频。
