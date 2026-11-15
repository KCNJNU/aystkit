# 机器人云平台工程

观测平台、DBProg 数仓、三维传感器配置和传感器资料模块的源码与部署参考。此仓库由 2026-11-15 已检查的交接工程整理，建议放在私有 GitHub 仓库。

| 目录 | 内容 |
| --- | --- |
| `observer-platform/` | Python/aiohttp 观测后端、网页、数仓同源网关、部署与维护文件 |
| `observer-platform/coverage-config/` | 三维传感器配置页的 React/TypeScript 可编辑源码 |
| `observer-platform/cloud/frontend/sensor-library/` | 传感器资料模块及必要原文资产 |
| `warehouse-platform/` | 数仓 NestJS API、worker、React/Vite 页面和 Prisma 数据模型、迁移 |
| `deployed-snapshot/` | 提取时线上运行代码与 Compose 的只读参考副本、镜像标识及校验清单 |

完整交接与构建定位见 [交接说明](交接说明.txt)。来源、模板处理和校验记录见 [SOURCE_MANIFEST.json](SOURCE_MANIFEST.json)、[检查报告.json](检查报告.json) 和 [版本对照.json](版本对照.json)。可编辑源码与运行快照应按这些记录对照，不能将本地旧编译产物视为当前线上版本。

## 部署关系

公网访问经 Nginx 进入观测服务；观测网关连接数仓 API，数仓使用 PostgreSQL、Redis 和 MinIO，后台任务由 worker 执行。GitHub 保存这些工程文件，业务服务继续部署在云服务器。

当前部署参考在 `deployed-snapshot/compose/`，可维护模板在各工程的 `deploy/`、Dockerfile 与 Compose 文件中。数仓挂载于 `/dbprog/` 时，构建参数为 `VITE_APP_BASE_PATH=/dbprog/` 与 `VITE_API_BASE_URL=/dbprog`。

## 环境与数据

源码仓库保存数据模型、数据库迁移、备份/部署工具和配置模板。以下运行状态需要保存在对应基础设施中：

| 内容 | 保存位置 |
| --- | --- |
| PostgreSQL 业务数据 | 数据库备份与恢复体系 |
| MinIO 对象、录像和数据集 | 对象存储及其备份 |
| Docker 镜像 | 镜像仓库或镜像归档 |
| 操作系统和已安装软件 | 云服务器自定义镜像/快照 |
| 生产密码、令牌、SSH/TLS 私钥 | 私密配置或密钥管理服务 |

生产 `.env`、用户数据库、录像、对象数据和密钥未加入此仓库。示例配置使用占位值，部署前按实际环境填写；根 `.gitignore` 继续防止运行数据与密钥误入提交。Prisma 迁移 SQL、已部署前端资源和传感器原文 ZIP 则保留，避免遗漏运行所需内容。

## 当前整理状态

该目录是本地 GitHub 待迁移工程，目标为 `https://github.com/KCNJNU/aystkit.git`。实际上传状态以推送结果为准。创建本地仓库不会停止、删除或迁移现有云端服务与数据。
