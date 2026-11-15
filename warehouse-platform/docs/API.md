# 数据仓库 API

公共接口的完整字段与校验规则见运行中的 Swagger：`http://localhost:3000/api/docs`。除存活和就绪接口外，业务接口要求登录；写请求校验 CSRF 与角色权限。

| 入口 | 用途 |
| --- | --- |
| `/health`、`/ready` | 存活与基础设施就绪 |
| `/api/v1/auth` | 登录、会话、退出、修改密码 |
| `/api/warehouse/overview` | 当前权限范围内的数据概览 |
| `/api/warehouse/projects`、`/api/warehouse/sources` | 可访问项目与文件来源 |
| `/api/ai/status`、`/api/ai/chat`、`/api/ai/chat/stream` | 千问配置状态、对话与流式生成 |
| `/api/ai/conversations` | 当前用户的对话历史与清空消息 |
| `/api/v1/datasets` | 数据集、目录、版本、冻结、发布和恢复 |
| `/api/v1/files` | 文件列表、预览下载、元数据、引用、位置与回收站 |
| `/api/v1/uploads` | 上传默认值、会话、分片、归档与取消 |
| `/api/v1/jobs` | 后台任务、进度、重试与取消 |
| `/api/v1/storage` | 存储对象、核对与孤立对象清理 |
| `/api/v1/users` | 用户、角色、授权、启停与软删除 |
| `/api/v1/tags`、`/api/v1/saved-views` | 标签与保存的检索视图 |
| `/api/v1/activity`、`/api/v1/notifications`、`/api/v1/audit-logs` | 活动、通知和审计 |
| `/api/system/status`、`/api/v1/system` | 服务状态、仓库设置与运行配置 |
| `/metrics`、`/version` | 仓库指标与构建版本 |

`PATCH /api/v1/files/:id` 修改识别分类及对象形式的元数据，已被不可变版本引用时返回冲突。冻结文件内容通过上传新文件和建立新版本演进。

用户管理按角色和数据范围校验；软删除撤销会话并保留历史审计关联。普通用户不获得文件和数据集读取权限。详见 [用户权限](../USER_PERMISSIONS.md) 和 [文件管理](FILE_MANAGEMENT.md)。

千问工具查询遵循当前用户的数据权限，接入说明见 [AI_INTEGRATION](../AI_INTEGRATION.md)。历史设备控制、地图、OTA 和感知接口不再注册。

设备采集归档的预打包接口：

- `POST /api/v1/datasets/:id/prepared-folder-packages`：原子提交全部根设备目录，返回 `202` 和 `{ items, total, ready, active }`。任务持久化后由 API 后台按顺序处理，关闭页面或退出登录不影响处理。
- `GET /api/v1/datasets/:id/prepared-folder-packages`：恢复同一批的 `QUEUED`、`PROCESSING`、`READY`、`FAILED` 状态；就绪项含 `taskId`、`fileName`、`readyAt`，不返回内部存储路径或临时下载地址。
- 就绪后调用原 `GET /api/v1/datasets/:id/folder-package?path=...&async=true&taskId=...`，按该批快照获取下载地址。追加上传不改变已准备的快照；显式再次提交才纳入新增文件。源成员删除、移动、修改或权限撤销仍会使下载失败。

每个设备保留最新一次预打包状态与对应云端压缩包，不受临时打包任务过期和普通缓存清理影响。服务重启会恢复未完成任务；数据集删除会清除关联预打包状态，旧压缩包按数据集清理流程回收。
