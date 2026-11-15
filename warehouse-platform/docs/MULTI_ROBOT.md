# 多机器人分类

观测与采集侧接入流程：`/home/user/.local/share/robot-cloud/docs/multi-robot.md`。

## API

- `GET /api/warehouse/robots`：当前用户有权访问的已注册真实机器人，保留租户/项目约束。
- `POST /api/warehouse/robots`：全局管理员注册 `{id, projectId, name, externalId}`；不允许重新绑定已占用的 ID。
- `GET /api/v1/datasets/page?robotId=<UUID>`、`GET /api/v1/datasets/versions/page?robotId=<UUID>`：与项目权限及关键词一起筛选。
- `GET /api/v1/files?robotId=<UUID>`、`GET /api/v1/search?q=...&robotId=<UUID>`：按同一上下文筛选。
- `GET /api/v1/archive/status?robotId=<UUID>`、`POST /api/v1/archive/garage` 的可选 robotId：独立控制目录；不配置的车辆拒绝控制，不回退到现车。

公网请求以上路径前加 `/dbprog`；原有身份认证和角色检查仍生效。机器人选择是分类条件，不替代账号权限；单个数据集/文件仍由原有项目 ACL 控制。

## 配置

- 新注册记录 metadata.type=`fleet-robot`。`WAREHOUSE_ROBOT_IDS` 可逗号分隔现有真实车的 UUID，使旧记录进入选择器；种子/测试/虚拟上传来源不自动成为车队。
- `GARAGE_ARCHIVE_DEFAULT_ROBOT_ID`：旧车 UUID，复用已有控制目录。
- `GARAGE_ARCHIVE_ROBOTS_JSON`：新增 UUID 到独立控制目录的 JSON。必须先部署每车状态/开关同步并挂载目录；无 robotId 的全部视图在多车映射存在时不能触碰默认车。
- 机器人上下文使用每个标签页的 sessionStorage，观测入口可通过 `?robotId=<UUID>` 交接。切换会清理旧查询和页面状态。
- 数据集筛选兼容 metadata.robotId、当前文件归属和版本内历史文件归属。原有项目 ACL 同时生效。混合数据集可能出现在多台车的筛选结果中；数据集详情保留其完整内容，不把分类当作数据权限边界。
- 用户、任务审计、存储和系统状态是平台级功能。当前版本没有按车分库，也没有迁移或重新命名旧数据。

无 Prisma schema 改动，文件继续存对象存储，数据库保存元数据及索引。新车使用独立 Robot UUID、上传账号、观测 token、相机/ROS/隧道配置；不可复制现车私钥和待上传队列。

## 数据集机器人分类入口

数据集页面直接提供“全部机器人 / 各台机器人”的分类按钮，切换会保留筛选条件、回到第一页，并同步顶部选车、文件/版本/搜索与浏览器历史。明确绑定的卡片显示对应车名；未明确绑定的数据集不按当前选车猜测归属。

创建数据集可指定 `robotId`，界面默认采用当前所选机器人并匹配项目。服务端校验机器人、项目和租户权限，写入 metadata.robotId；所属项目不能与机器人冲突。已绑定数据集的手动上传默认使用真实机器人来源，并在去重前拒绝其他来源。旧的未绑定数据集和现有归档创建接口保持兼容；不会批量改写历史数据归属。

新增机器人完成注册并具有当前账号可访问的项目后，会自动出现在分类入口。页面分类不取代项目权限，不会把无权限的数据集加入列表。
