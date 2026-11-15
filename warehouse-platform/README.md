# 内部数据仓库与千问助手

项目保留白色界面的数据仓库、已接入千问的 AI 助手及运行所需的管理功能。

- 前端：`apps/web`，React、TypeScript、Vite、Ant Design。
- 后端：`apps/api`，NestJS、TypeScript。
- 数据库：PostgreSQL，由 Prisma 管理模型与迁移。
- 文件存储：通过 ObjectStorage 接口访问 MinIO；原始文件不写入数据库。
- 功能：数据集与多级目录、文件上传/下载/预览/回收站、数据版本、冻结、Git + DVC 发布与恢复。
- AI 助手：千问流式对话、会话历史、生成参数、当前权限内的数仓只读查询与来源展示。
- 支撑：登录、分级权限、用户管理、搜索、后台任务、通知、审计、存储维护和服务状态。

Windows 安装 Docker Desktop 并配置根 `.env` 后，双击 **start-all.cmd**，访问 **http://localhost:5173**。停止使用 **stop-all.cmd**，持久数据卷会保留。局域网使用管理员身份运行 **enable-lan.cmd**。

已有 PostgreSQL、Redis 和 MinIO 基础设施时，可运行：

```powershell
npm.cmd ci
npx.cmd prisma generate --schema apps/api/prisma/schema.prisma
npm.cmd run dev
```

默认启动 API、Worker 和 Web。验证命令：

```powershell
npm.cmd run build
npm.cmd run lint
npm.cmd test
npx.cmd prisma validate --schema apps/api/prisma/schema.prisma
```

说明：[部署与启动](DEPLOYMENT.md) · [架构](ARCHITECTURE.md) · [环境配置](ENVIRONMENT.md) · [千问接入](AI_INTEGRATION.md) · [用户权限](USER_PERMISSIONS.md) · [文件管理](docs/FILE_MANAGEMENT.md) · [API](docs/API.md)。

历史数据库模型与迁移保留用于已有数据和升级兼容；本次精简没有执行删表、重置数据库或清理文件存储。设备控制、Java 后端、移动端、营销站和感知采集均已从项目源码、构建和启动配置移除。
