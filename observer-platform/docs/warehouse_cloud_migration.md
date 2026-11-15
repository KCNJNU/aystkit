# 数仓独立于小车网络：迁移已完成

**2026-11-02 16:03:13（Asia/Shanghai；08:03:13 UTC），数仓已正式切换至云端，迁移完成。** 原 IPC 数仓六个容器已停止且禁止自动重启，四个原数据卷保留；旧数仓反向 socket 隧道已禁用。云端账号、数据库、对象与工作区一致性，以及禁用旧 socket 后的真实 TLS 登录、视频下载和云端压缩包均已验证。汇总见 [migration-complete.json](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/migration-complete.json)。

15:53 用户经已认证 API 删除旧大数据集后，最终迁移采用删除后的源清单，未复活已删除数据集。**10 分钟真实录制、离线重连补传及性能测试尚未执行；当前无雷达样本，本次最终雷达结果为 `unavailable_in_current_source`，不声称雷达通过。**

迁移前，`https://aystkit.cn/dbprog/` 的静态页面和观测账号在 ECS，数仓 Nest API、PostgreSQL、MinIO 在小车 IPC；云 `DbprogGateway` 经 `dbprog-api.sock`、`dbprog-objects.sock` 访问小车反向隧道，小车断网会导致数仓登录失败。现在原数仓账号、授权、对象和工作区均在云端运行，小车仅负责采集与补传，入口地址保持不变。

## 当前基线与已执行事项

- 最终源清单现为 **6 个用户、1 个数据集、12 个 MKV / RobotFile，共 52,437,563 字节**。原账号、密码哈希、角色、项目授权和会话随最终源库迁移，不运行 seed、不创建空白替代账号。
- [source-deletion-events-readonly.json](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/source-deletion-events-readonly.json) 记录了 15:53 经已认证 DELETE 发起的旧数据集删除请求及成功完成的 `DATASET_DELETE`。删除后重新生成最终计划并核对：**33 个逻辑对象、52,439,655 字节**（派生压缩包缓存生成前）；最终物理清单为 **65 个文件、52,479,473 字节**。此前预传清单为 64 个物理文件，源 MinIO 关闭时新增 3 字节 MRF 元数据文件，已纳入最终哈希。必要对象和所选非终态上传分片缺失均为 0。
- 原 2 个数据集 / 1,922 文件 / 32,082,085,978 字节的源基线，以及 1,364 文件 / 23,008,761,650 字节的筛选计划，已归档在 `selection-before-source-dataset-deletion/`，**仅供历史审计，绝不恢复或复活用户已删除的数据集**。
- ECS 虚拟磁盘已扩为 **100 GiB**，根文件系统约 **98 GiB**；根分区已扩展，沿用原 UUID 持久挂载。数仓数据目录为 **`/srv/robot-warehouse`**，位于该根文件系统，并非另一个未挂载的数据盘。
- 云主机内存约 **1.7 GiB**，已配置 **2 GiB swap**。最终检查磁盘使用约 **11 GiB**、可用 **84 GiB**，available RAM 约 **582 MiB**、swap 使用 **0**；云 PostgreSQL、Redis、MinIO、API、worker 五个服务及网关健康，无 OOM。资源限制继续保留。
- 云端已恢复写入冻结后的完整最终 dump 并执行云专用筛选事务，**69 张表计数、身份授权指纹及所选文件/对象证据与源一致**。DRAFT 版本保留原 8 条文件引用，共 **26,082,332 字节**；未将其他文件自动加入版本。
- 新云网关与 50 文件 UI 已部署，已切换到固定云 HTTP 上游，ready/healthy 和 nginx 配置重载核对通过。旧数仓 socket 隧道禁用后，真实 TLS 观测登录、原 OP 数仓账号登录及视频 Range / 完整 SHA256 均通过；当前无雷达，最终验收明确报告其不可用。独立运行状态见 [independent-service-acceptance.json](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/independent-service-acceptance.json)。
- 原约 **23 GB** 预拷贝曾以 **2 MiB/s**、CPU 配额 **0.25**、低 CPU / 空闲 I/O 优先级及只读源卷运行。用户删除源数据集后，该范围失效，wrapper 重新生成清单、冻结源写入并完成最终复制和校验；旧预传进度仅供审计。
- 迁移不删除 IPC 仍合法保留的数据、原始采集缓存及备份；最终恢复必须以用户删除后的源库为准，历史备份不是恢复已删除数据集的授权。机器人导航、建图及传感器不属于数仓维护停机范围。

## 历史筛选规则与原件保留

- 通用规则保留 MKV，以及 MID360 和定位/TF 的压缩 ROS 包及其必要 YAML 元数据；小型相机标定包仅在明确标记 `cameraMetadataOnly=true`、且全部 topic 为 CameraInfo 时保留。**当前源仅余四路相机各 3 个 MKV，共 12 个；没有雷达或深度文件可供此次最终迁移与验收。**
- 历史 Berxel 点云、诊断及覆盖任务不属于训练数据迁移选择。历史深度视频中明确标记的内容为伪彩可视化，`losslessPixelValues=false`，不能称为保留真实深度像素的训练原件；不会通过重写元数据改变其含义，也不会从历史备份补回用户已删除的数据集。
- 对象选择同时保留所选文件对应的目录标记、数据集标记、必要版本引用及实际存在的所选上传分片。MinIO 持久配置和桶元数据保留，**排除 `.minio.sys/tmp/` 临时文件**，避免消失的回收站叶子使预传和哈希失败。
- `prepare_training_inventory.py` 和 `filter-training-migration.py` 默认只读导出及生成计划，不执行数据库裁剪。本次已在最终冻结后重新生成 inventory、物理路径清单、选择和 SQL；旧预哈希清单未跨源主机重启冒充最终校验缓存。
- 本次先在云端恢复完整最终 dump，再仅在云库执行带源 PostgreSQL system ID 拒绝护栏的事务筛选，按 FK 顺序处理版本文件、上传及分片、文件和目录，并重算 DRAFT 统计。6 个账号、授权、原有 ID 和所选版本引用保持不变；源库未执行筛选 SQL。

## 部署与镜像维护

云数仓配置使用 `deploy/warehouse/docker-compose.yml`，`WAREHOUSE_DATA_DIR=/srv/robot-warehouse`。私密配置仅通过已校验的 SSH 加密传输，文件权限为 0600；本文件不记录凭据。

不能仅用旧镜像重建后宣称保留了原部署：IPC 容器包含热更新代码。迁移镜像应在对应原镜像基础上，叠加已核对的当前 API/worker 运行时文件，保留原 PostgreSQL、Redis、MinIO 的兼容版本和镜像标识。工作区与 MinIO 数据挂载到上述持久目录，不打包进镜像。

采集脚本生命周期、API 与 Web 的修复已部署；云端是唯一数仓写端，原 IPC 数仓写端已停用。关闭手动采集会话时应关闭全部相机，导航和雷达不属于相机停机范围。Web 将缺少结束标记的历史分段显示为“等待结束标记”，不能据此声称仍在采集或文件已完整；未闭合录像不能下载或组装连续成品。删除遇到待完成上传时显示阻塞摘要，符合服务端条件的空上传只能经用户确认取消，随后手动重试删除，不自动删除数据集。

数仓 UI 初始备份为 85 个文件，曾完成与 IPC 部署文件的 SHA256 核对和 `/dbprog` 路径适配。当前修复后的源码已另行构建到迁移暂存目录的 `cloud-ui-current/`，包含 **50 个文件**，并随新云网关部署；不能用旧云端静态文件或早期 overlay 覆盖新功能。该构建的静态资源、JavaScript 解析、路径、认证跳转和新 UI 行为已验证；API base 为 `/dbprog`，请求层会追加 `/api`。

压缩包生成的流式写入、大小校验和资源限制修复已有隔离测试。真实 tar 报错后，视频与雷达打包均已将输入子目录和输出压缩包隔离，消除输入目录 mtime 变化；真实 tar/zstd 各重复 3 次验证通过。完整新镜像传输任务已停止，改为向现有云镜像叠加经过审查的编译文件和必要公共工具/库后重建，私密环境文件独立保留，不进入镜像。当前云 API 已启用 **`robot-warehouse-api:training-isolated-20261102`**，ID 缩写为 `07a8fb…993`；此前 `training-fixed-20261102` 仅为历史构建记录。

当前已部署云网关 tag 为 **`cf60adbc6e90819c2bca0598be8b74be77d4ce2c`**，镜像 ID 为 `sha256:f652cca16c3c5dec2329fa66c1f75036a6183c0bd22b869333009ea763dac407`。其普通对象与压缩包代理均使用 GET 预签名方法访问存储，HEAD 客户端只接收响应头，不读取或转发正文，避免 S3 签名方法不匹配。

切换后的真实云 API 已为 **930,855 字节**视频生成 **912,692 字节**压缩包，原 OP 授权、时间戳文件名、同源地址、HEAD、Range、完整下载长度均通过；tar/zstd 解包仅含视频与 manifest，原视频 SHA256 和片段 manifest 完全匹配。报告为 [cloud-proxy-result.json](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/cloud-proxy-result.json) 与 [cloud-content-result.json](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/cloud-content-result.json)。此前源链路 912,717 字节封包是切换前记录，不能与云端重新生成的压缩字节要求完全相同。

部署执行记录、镜像标识、变更前配置校验和与备份保存在本次迁移暂存目录。新镜像启动后的文件与健康状态已核对；云临时审核数据库、验证凭据及验收 tar 副本已清理，`transfer-partials` 目录不存在。原 IPC 四卷及迁移审计记录继续保留。

## 当前连接方式与运行约束

- 云 API 只绑定宿主机 **`127.0.0.1:13000`**，不开放公网数据库、API 或 MinIO 端口。
- 云观测后端已使用 `DBPROG_API_URL=http://warehouse-api:3000`、`DBPROG_OBJECT_URLS=http://warehouse-minio:9000`，通过现有私有 Docker 网络访问云数仓。旧 Unix socket 数仓隧道已停止并禁用，旧配置保留用于受控回退。
- 观测授权、原数仓账号登录、原角色权限和 CSRF 继续生效。对象下载只允许明确配置的存储 origin，保留签名 Host、编码路径和 Range，不向存储转发平台 Cookie。
- 连续录像压缩包下载入口为 `/dbprog/packages/<dataset>/<recording>`；每次下载重新校验观测授权和绑定的数仓会话，并向原 package API 重新授权。切换后真实云端 Range/HEAD、文件名和解包内容已验证，构包超时及资源限制有隔离回归测试；不直接暴露内网 MinIO 地址或接受调用方指定的上游 URL。
- 本次最终验收无雷达样本时，仅可显式传入 **`--allow-video-only`**，严格验证一个视频样本并报告 `radar=unavailable_in_current_source`；默认仍要求视频和雷达各一个，不能伪造雷达样本或把缺失标记写成雷达通过。
- 小车上传使用独立受限 SSH 身份与专用密钥，转发本机 **`127.0.0.1:13000`** 至云 **`127.0.0.1:13000`**。服务端只允许该固定 `PermitOpen`，常驻上传不使用 root 部署身份。
- 兼容入口已将本机 **`127.0.0.1:3000`** 也转发到同一个云 API，供原有断点上传客户端继续使用。两个本机端口的原 OP 登录、会话和视频元数据检查均通过；两者指向同一个云写端。
- 云 API/worker 的 `GARAGE_ARCHIVE_CONTROL_DIR` 均为空，不打开小车采集开关，不启动机器人驱动，不接管 IPC 本地采集会话。

## 本次切换执行记录

1. 在用户删除旧大数据集后重新生成当前选择，完成云暂存区所选缓存核对；原 23 GB 范围封存作历史审计。
2. 冻结源 API/worker 写入，导出完整最终 dump，停止源 MinIO 后刷新物理清单并执行最终差量；导航、建图不在数仓停机范围。
3. 云端恢复最终 dump，执行仅云库可用的筛选 SQL，核对 69 表计数、身份指纹、所选文件与 DRAFT 引用；物理文件、Redis 与 workspace 对比均零差异，已删除数据集未复活。
4. 启动云 API/worker，与 PostgreSQL、Redis、MinIO 一同达到健康状态，启用固定云 HTTP 网关和本机 13000 / 3000 上传转发。
5. 停止原 IPC 六个数仓容器并设 `restart=no`，保留原四卷；停止并禁用旧数仓反向 socket 隧道。两端口登录/会话/视频元数据检查，以及旧 socket 不可用时真实 TLS 登录和视频下载均通过。
6. 切换后复测云端时间压缩包，验证授权、HEAD、Range、完整下载及原视频 SHA256；清理临时审核数据库、验证凭据和验收 tar 副本。
7. 记录正式完成时间 **16:03:13**。10 分钟真实录制、断网重连补传与性能仍为未执行项；无当前雷达样本的事实单独记录，不纳入已通过项目。

## 最终结果记录

| 项目 | 最终结果 |
| --- | --- |
| 正式切换、最终 dump 与恢复记录 | 2026-11-02 16:03:13 CST 完成；[切换状态](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/cutover-state.json)、[完成报告](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/migration-complete.json) |
| 最终用户 / 数据集 / 文件与各表核对 | 6 / 1 / 12 MKV，52,437,563 字节；69 张表计数及身份授权指纹一致；[源证据](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/verification-source-final.json)、[云证据](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/verification-cloud-final.json) |
| 云筛选、DRAFT、源卷与上传分片 | DRAFT 8 文件、26,082,332 字节；必要分片无缺失；原六容器 stopped / restart=no，四原卷保留，删除数据集未复活 |
| 最终对象与 MinIO 物理哈希 | 缓存生成前 33 逻辑对象 / 52,439,655 字节；最终 65 物理文件 / 52,479,473 字节，missing / extra / changed 均 0；[最终清单](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/minio-cloud-final.manifest.json) |
| Redis 与 workspace | Redis 4 文件 / 984,520 字节，workspace 17 文件 / 23,941 字节，均零差异 |
| 云服务、镜像与 UI | PostgreSQL / Redis / MinIO / API / worker 五服务及网关 healthy；API training-isolated，网关 cf60adbc…，UI 50 文件；[独立服务检查](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/independent-service-acceptance.json) |
| 原账号会话、角色授权、下载与视频 | 原 OP 授权、真实 TLS 登录、视频 Range / 完整 SHA256 通过；[网关验收](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/gateway-acceptance.log) |
| 雷达最终样本状态 | 当前源无雷达；显式 video-only 报告 unavailable_in_current_source，不声称雷达通过 |
| 云压缩包代理与内容 | 原 OP 授权、时间戳文件名、同源、HEAD / Range / 完整下载 912,692 字节通过；解包仅视频+manifest，原视频 930,855 字节及 SHA256 一致；[代理结果](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/cloud-proxy-result.json)、[内容结果](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/cloud-content-result.json)。超时与资源限制通过隔离回归 |
| 网关与本机双端口上传通道 | 16:03:13 正式切换；13000 / 3000 原 OP 登录、会话和视频元数据通过；[IPC 上传入口检查](/home/user/.local/state/robot-cloud/warehouse-migration-20261102/ipc-upload-acceptance.log) |
| 旧反向隧道隔离后的独立性 | 旧数仓 socket 隧道 inactive / disabled；真实 TLS 登录、视频下载及云 API 构包通过。只读与会话验收不代替新增真实录制补传测试 |
| 10 分钟真实录制、离线补传与性能 | **未执行**；需要后续实录、断网重连、索引与性能验证 |
| 云资源与清理 | 可用磁盘 84 GiB、available RAM 582 MiB、swap 使用 0、无 OOM；临时审核 DB、验证凭据、验收 tar 副本已清，transfer-partials 不存在 |

## 回退边界

新云库尚未接收新写入时，可恢复原网关配置、停止云数仓写端，重新启用原 IPC API/worker 与原上传入口。恢复前核对两个本机端口没有同时指向不同写端。

**当前云端已成为主写端，并已生成派生压缩包缓存及验收会话，回退应按已有新写入处理。** 必须先暂停云写入，回拷并校验新增数据库、对象和工作区，再切回；禁止直接回到旧快照而丢失新增文件，也禁止恢复用户已明确删除的数据集。原 IPC 数据卷、迁移备份和执行记录继续保留；旧数仓隧道已禁用，受控回退时才可恢复，不能与云主写端并行启用。
