# 环境配置

唯一开发配置文件是仓库根 `.env`，NestJS 使用项目绝对路径加载。示例见 [.env.example](.env.example)。已有 `.env` 中的值应保留，按示例补充所需配置。

| 分组 | 主要变量 |
| --- | --- |
| PostgreSQL | DATABASE_URL、POSTGRES_DB、POSTGRES_USER、POSTGRES_PASSWORD |
| 登录 | BOOTSTRAP_ADMIN_EMAIL、BOOTSTRAP_ADMIN_PASSWORD、AUTH_SESSION_HOURS、AUTH_COOKIE_SECURE |
| Redis 与限流 | REDIS_HOST、REDIS_PORT、RATE_LIMIT_KEY_SALT |
| 文件存储 | OBJECT_STORAGE_ENDPOINT/PORT/USE_SSL/BUCKET/ACCESS_KEY/SECRET_KEY |
| 浏览器下载地址 | OBJECT_STORAGE_PUBLIC_ENDPOINT、OBJECT_STORAGE_PUBLIC_PORT |
| 版本发布与恢复 | DVC_COMMAND、DVC_REPOSITORY_ROOT、DVC_REMOTE_*、DATASET_PUBLISH_GIT_AUTHOR_* |
| 千问（仅后端） | QWEN_API_KEY、QWEN_BASE_URL、QWEN_MODEL、QWEN_TIMEOUT、QWEN_MAX_TOKENS |
| Web | WEB_HOST、WEB_PORT、WEB_HMR_HOST、CORS_ORIGIN、VITE_API_BASE_URL、VITE_APP_VERSION |
| 运行 | API_PORT、WORKER_CONCURRENCY、APP_VERSION、DEPLOYMENT_ENVIRONMENT |

本地 Web 默认 5173，NestJS 默认 3000。Docker 静态 Web 使用同源 `/api`，Nginx 上游为 `backend:3000`。

Docker 自动覆盖容器内 PostgreSQL、Redis 和 MinIO 的地址。浏览器签名下载地址必须可由访问设备连接；默认 localhost:9000 只适合本机使用，局域网可通过 `enable-lan.cmd` 更新地址。

HTTP 本机及局域网使用 `AUTH_COOKIE_SECURE=false`；HTTPS 使用 `true`，并匹配文件存储 HTTPS 地址。保留非空 `RATE_LIMIT_KEY_SALT`，缺失会使限流相关接口返回 503。

服务就绪接口 `/ready` 检查 PostgreSQL、Redis 和文件存储，登录后的 `/api/system/status` 提供详细状态及千问状态。千问故障或缺少 Key 不阻断数仓；QWEN 配置不能使用 VITE 前缀。项目不再需要 Java、MQTT 或感知采集配置；已有私有 `.env` 不自动清空。
