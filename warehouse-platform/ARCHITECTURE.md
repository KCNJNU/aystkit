# 数据仓库与千问助手架构

项目只有两个应用：`apps/web` 与 `apps/api`。API 和 Worker 共用 NestJS 业务模块，Worker 执行数据库任务队列。

```text
浏览器 → Web / Nginx → NestJS
                        ├─ 登录 / 权限 / 用户 / 审计
                        ├─ 数仓概览 / 搜索
                        ├─ AI 助手 → 千问 → 受当前用户权限约束的只读数仓工具
                        ├─ 数据集 / 文件夹 / 文件 / 回收站 / 上传
                        ├─ 数据版本 / 后台任务 / 存储维护
                        ├─ Prisma → PostgreSQL
                        └─ ObjectStorage → MinIO
PostgreSQL 任务队列 → Worker → 文件归档 / 数据版本发布与恢复 → Git + DVC
Redis → 登录、接口与 AI 限流及生成并发锁
```

配置统一来自根 `.env`，NestJS 按独立于当前工作目录的路径加载。基础设施必填配置使用 `ConfigService.getOrThrow`。

原始文件由 ObjectStorage 保存，数据库只存 objectKey、MIME、原始文件名、大小、校验和、目录索引、元数据和版本状态。上传按数据集与目标目录组织；普通上传不运行 DVC、不创建 Git 提交。数据版本冻结后才进入发布与 DVC 链路。

`warehouse/` 模块独立提供概览、项目、文件来源与受限数仓查询，供页面和 AI 工具共享。千问接入保留流式生成、会话、参数与来源展示；API Key 仅在后端使用，AI 不执行数据修改、任意 SQL 或系统命令。文件历史外键 `RobotFile.robotId` 继续代表文件来源；上传缺少来源时使用内部上传来源，无需设备管理。租户和项目关系用于已有权限范围。

历史 Prisma 模型、枚举及不可变迁移记录保留，以保证旧库升级及已有文件、用户、任务和版本关系兼容。没有为移除业务模块生成 DROP TABLE 迁移，也没有修改已有持久数据。应用运行代码不再注册机器人控制、地图、OTA、遥测、MQTT 或感知模块。

Compose 只运行 PostgreSQL、Redis、MinIO、API、Worker 和 Web；migration 与 storage-init 是一次性任务。历史部署拓扑已移除。部署入口为根 `docker-compose.yml`，不依赖其他 deploy 目录。
