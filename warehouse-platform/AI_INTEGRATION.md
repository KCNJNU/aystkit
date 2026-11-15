# Qwen 与内部数仓

在仓库根 `.env` 配置后端变量：

```dotenv
QWEN_API_KEY=
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen-plus
QWEN_TIMEOUT=30000
QWEN_MAX_TOKENS=2048
```

Key、地域和模型必须匹配自己的阿里云账号。Key 不进入 Vite、不返回浏览器、不写日志或提交 Git。实现使用 [阿里云兼容 Chat Completions 协议](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions)和 [Function Calling](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-function-calling)，不依赖特定模型 SDK。`enable_thinking=false`；选择支持此模式及工具调用的 Qwen 模型。

API（除公开存活探针以外均要求现有 Session；写请求还要求 CSRF）：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | /api/ai/status | 配置、模型、状态、检测时间、耗时；不返回 Key |
| POST | /api/ai/chat | `{message, conversation_id?}` → `{answer, conversation_id, sources, warehouse_queries}` |
| POST | /api/ai/chat/stream | 相同请求；SSE `conversation`、`tool`、`delta`、`done`、`error` |
| GET | /api/ai/conversations | 当前用户最近 50 个会话 |
| GET | /api/ai/conversations/:id | 当前用户可访问的最近 100 条消息 |
| DELETE | /api/ai/conversations/:id/messages | 清空自己的会话；生成过程中拒绝 |
| GET | /api/warehouse/sources | 当前权限内的文件来源选择（不返回设备控制信息） |
| GET | /api/warehouse/projects | 当前权限内的数据项目选择 |
| GET | /api/warehouse/overview | 按当前权限过滤的首页统计与最近数据 |

工具白名单：

1. `warehouse_list_datasets`
2. `warehouse_search_datasets`
3. `warehouse_get_dataset`
4. `warehouse_list_versions`
5. `warehouse_list_recent_uploads`
6. `warehouse_search_files`
7. `warehouse_get_file_metadata`
8. `warehouse_get_storage_summary`
9. `warehouse_get_dataset_statistics`
10. `system_get_api_status`
11. `system_get_system_status`

工具名采用协议兼容的下划线格式。后端校验 JSON、必填字段、额外字段、UUID、关键词长度及条数。每个列表默认 10 条、最多 20 条；最多 4 轮、8 次工具调用；本轮工具结果总字符数上限 48,000。模型无法提供 SQL、函数路径、Shell 或系统命令让后端执行。只有 WarehouseQueryService 能为 AI 读取数仓。

文件只返回选定元信息；不返回 objectKey、下载链接或原文件正文。数据集描述限长；工具内容视为不可信数据。系统提示要求内部事实以本轮工具结果为准、空结果明确说明。明显的内部数据问题若模型未调用工具，后端返回无法确认，不采用该未验证回答。一般知识回答在界面标记未引用数仓数据；模型结果仍需要人员核对。

会话属于用户；每次检索重新校验角色、租户、项目和引用来源。上下文仅保留最近 8 条用户问题（每条最多 4,000 字符），不重放过期的内部回答。清空历史保留审计用量；权限变化后需新建会话。

对话使用真实 Qwen 流式增量。工具选择阶段使用普通请求；有工具结果后的最终回答使用上游 SSE。停止按钮中断浏览器连接并传播 AbortSignal，取消后不保存未完成回答。接口总处理预算 90 秒；Redis 并发锁 120 秒并在结束时释放，每用户每分钟最多 10 次请求。

只有网络错误和 500/502/503/504 最多重试一次；401/403/429/400 不自动重试。返回可读错误，不转发上游原始错误体。健康检测用只读 `/models`，缓存 60 秒；该探针不产生聊天 Token，也不等同于已验证指定模型可以生成回答。真实模型联调必须另做。

Prisma migration `20260922090000_warehouse_ai` 新增 `ai_conversation`、`ai_message`、`ai_usage`。用量包含用户、会话、模型、官方返回的 Token、请求次数、耗时和成功/失败状态；未知 Token 为 null/缺省，不推算价格。失败调用也记录；日志只保留身份、工具名、耗时、状态和 Token，不记录 Key、问题全文或文件内容。

测试使用 `MockQwenProvider`，生产模块始终绑定 `QwenProvider`，不会因缺少 Key 自动伪造回答。Mock 测试不能代替真实 Qwen + PostgreSQL 联调。
