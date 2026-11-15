# 停止、回滚与备份

云工程独立于机器人主控。以下命令只针对自己的云项目/Collector，不使用pkill或按端口杀未知进程。

## 停止机器人上传

若用户已按手册安装systemd：

```bash
sudo systemctl stop robot-cloud-collector.service
# 需要取消下次开机上传时：
sudo systemctl disable robot-cloud-collector.service
```

前台运行时Ctrl+C让ROS节点和uploader正常退出。停止上传后，云页面仍可登录，10秒内机器人离线，旧值标为过期。
这不停止ROS驱动/host_node/8765/原8888。恢复旧本地页面用原项目自己的启动流程，由操作者决定，不由云工程启动它。

## 回滚云端代码

```bash
cd /opt/robot-cloud
bash deploy/scripts/rollback.sh <PREVIOUS_COMMIT>
```

脚本要求独立Git仓库、无未提交tracked编辑、已有旧版本Docker镜像，保留数据卷与凭据。不删除镜像，不使用docker system prune。
回滚失败时先检查旧镜像标签、日志和SQLite协议兼容性；当前v1没有破坏性迁移。
要仅停云端：`bash deploy/scripts/cloud.sh stop`；不要加 `down -v` 删除数据。成功镜像tag存在 `.runtime/active-image-tag`，不要删除旧commit镜像或这个记录。

### direct 与 Hub 配置的回滚限制

三台直接连接时使用显式 `jetson-direct3`：front→camera2、left→camera1、usbPort21→camera3；后视和 Hub 接口不启用。四路 Hub 拓扑及独立低分辨率采集试验使用 `jetson-hub4`：front→camera2、left→camera1、Hub2.1.2→camera4、Hub2.1.3→camera3，不启用 usbRear/usbPort21。它们是不同接线与源配置，不是自动识别；当前实际运行模式和试验是否保留见 [部署状态](deployment_status.md)。先审计预览 cameraN 与真实 USB 设备/by-path，再选择匹配配置；不要为回滚直接切成默认 `legacy3`，否则原左视会被错标为后视、未知扩展接口画面会被错标为左视。

回滚到不支持usbPort21或Hub key的云版本前，先停止对应HTTP上传或整体停用HTTP适配。若回退到不认识新profile的Collector，须同时撤销HTTP URL、profile显式配置并排除HTTP媒体key，不能留下半套配置，也不能沿用旧方向映射继续取流。仅云代码/适配器回滚只重启独立Collector/relay；已获授权的采集试验恢复按下节单独处理，不改原源码、不改变接线、不启动控制设备。

### 独立低分辨率采集试验的恢复

试验源是 Jetson `~/.local/share/robot-cloud/usb_camera_preview.py`，服务为 `robot-cloud-lowres-preview.service`。只有这个独立源拥有四路视频设备；relay 与 RK3588 Collector 仍只读 HTTP，不打开视频设备。模板没有 `[Install]`、`Restart=no`，不启用开机自启。此处是恢复预案，不表示低分辨率试验已经通过或已经回滚。

切换前必须保留原相机程序的精确 argv、cwd、必要运行环境及配置 hash。原环境值只保存在受控私密位置，不输出完整环境、token、日志原文或图片。本轮私密记录在RK3588 `/home/orangepi/.local/state/robot-cloud-camera-backups/original-preview-before-lowres-20261012.json`（0600）；复制到Jetson尚未获额外许可，不能假设远端已有此记录。原 `camera_map.json` 在低清试验中不改。特别注意：原 `observer-camera-preview.service` 是 transient unit，`CollectMode=inactive`，停止后可能被回收；即使没用 `--collect`，也不能保证随后仅执行 `systemctl --user start observer-camera-preview.service` 就能恢复。

已核验的原 direct3 配置包含 `camera3 → 接口2.1`。当接线变成下级 Hub 的 `2.1.2/2.1.3` 后，原程序 `--check` 会因接口2.1缺失失败，直接重启原配置不能保证前／左继续出图。因此预先准备**独立的两路回滚配置文件**，不覆盖原 JSON：

| 源别名 | 保留方向 | 完整设备路径 |
| --- | --- | --- |
| `camera1` | 左视 | `/dev/v4l/by-path/platform-3610000.usb-usb-0:2.3:1.0-video-index0` |
| `camera2` | 前视 | `/dev/v4l/by-path/platform-3610000.usb-usb-0:2.4:1.0-video-index0` |

候选保持原 JSON 的 `cameras` 数组结构，仅保留上述已验证条目。原程序的 `--check` 明确不打开相机；本轮两路内存候选已通过该检查，但正式恢复前仍须检查部署记录中的实际候选文件、设备存在性和 hash。路径与环境应在试验前记录好，不能等旧 PID 退出后才读取 `/proc/PID/environ`。临时目录中的备份不应被当作永久灾备。

恢复顺序：

1. 在 RK3588 **先停 `robot-cloud-collector.service`**，避免采集源身份切换过程中继续上传旧 profile 的 camera3/4。
2. 仅停止 `robot-cloud-lowres-preview.service`，等待其受控子进程退出。确认 8080 及四路视频 FD 均已释放；若仍有未知持有者，停止恢复并调查，不用 `pkill python`、按端口杀进程或终止其他程序。原 relay 18081 和 SSH 隧道保持不动。
3. 核对原程序路径和已保存的运行环境。用原相机程序及原 cwd/运行环境启动，**只将 `--config` 参数替换为已核验的两路回滚配置绝对路径**，通过独立、可查的手动 user unit 管理；原 JSON 与原源码不覆盖。若原 transient unit 已消失，按记录重建，不假设旧服务名仍可启动。不要运行 `run_system`、ROS launch、串口、底盘或导航。
4. 至少观察 20 秒，确认 camera1/2 无错误、有持续新帧、年龄小于 2 秒，并核对视频 FD 和 8080 监听者。没有源数据时保持等待，不能把回滚前缓存显示成恢复成功。
5. 仅前／左恢复时，可保留支持相同 front→camera2、left→camera1 的显式 `jetson-hub4` profile；其 Hub3/Hub2 请求应因源别名缺失而保持等待／错误，不冒充已恢复。若需只上传前／左，按实际部署的既有媒体分工调整允许 key，不覆盖其他来源。不能切到 `legacy3`，也不能把 camera3 指向不明设备以凑齐画面。
6. 确认云版本认识该 profile、媒体 key 与来源后，再启动 Collector，验证前／左公网 JPEG 身份递增和持续新鲜度。记录未恢复的扩展设备与实际掉帧；恢复至少两路不等于四路试验成功。

若实际接线已经改变、原程序/配置 hash 不符，或两路回滚 `--check` 不通过，应暂停并重新核对，不反复启动不同配置试错。是否保留独立采集、是否结束试验，以及最终指标由 [部署状态](deployment_status.md) 明确记录。

### 历史三路 HTTP 相机版本的兼容回滚

仅撤销320×180网络预览时，无需停止四路低清采集或改变profile：先将Jetson `~/.config/systemd/user/robot-cloud-camera-relay.service.d/preview-size.conf` 私密备份，再仅把ExecStart中的尺寸改成 `--preview-size 640x480`，保留 `--keep-alive` 及其他既有参数，daemon-reload后只重启 `robot-cloud-camera-relay.service`（本次源实际640×360，不会放大到640×480）。不要删除该drop-in或其他drop-in；基础服务不含 `--keep-alive`，直接删除会同时撤销连接复用修复。2026-10-12 20:20已按此方式恢复，原320档配置保存在Jetson `~/.local/state/robot-cloud/preview-size.before-restore640-20261012-2020.conf`（0600）。若恢复后需要重新采用320档，先确认其他配置未再变化，再恢复该配置并仅重启relay，保持HTTP2 FPS和源采集不变。这个尺寸参数回滚不需要回滚云镜像，也不改变帧身份/年龄合同，不等于恢复试验前的硬件高清采集。

2026-10-12早先旧relay脚本保存在 `~/.local/state/robot-cloud/http_camera_relay.before-low-preview-20261012.py`；若恢复该旧脚本，必须同时撤销它不支持的尺寸／连接复用参数，否则启动失败。回退旧脚本属于另一种恢复操作，会撤销相应传输优化，不应为恢复640尺寸而执行。

18时段连接复用修复另保存 `~/.local/state/robot-cloud/http_camera_relay.before-keepalive-20261012.py` 与 `preview-size.before-keepalive-20261012.conf`。仅撤销连接复用，可将relay参数恢复为 `--preview-size 320x240` 并只重启relay，仍保留320×180预览；恢复旧脚本必须同时去掉 `--keep-alive`。Collector完整有限响应读取兼容这个旧relay。上传调度本轮RK3588源码备份在 `/home/orangepi/.local/state/robot-cloud-camera-backups/expiry-before-20261012/`（目录0700、三文件0600）；恢复对应三文件后只重启Collector，不覆盖collector.env或其他drop-in。页面最后帧标记回退使用对应旧云镜像，数据库和登录凭据不变。

旧镜像不认识 `usbFront/usbRear/usbLeft`。若回滚到增加这三项之前，先在 RK3588 停用可选 HTTP 配置：将实际安装的 `~/.config/systemd/user/robot-cloud-collector.service.d/http-camera.conf` 移到同目录外的备份位置（不要删除其他 drop-in），执行 `systemctl --user daemon-reload`，再只重启 `robot-cloud-collector.service`。若另外显式设置了 `COLLECTOR_MEDIA_KEYS`，同时恢复原有列表；默认配置会恢复旧十一项。之后再回滚云镜像，避免旧端持续拒绝未知媒体 key。

仅暂停相机传输可在 RK3588 停 `robot-cloud-camera-tunnel.service`，或在 Jetson 停 `robot-cloud-camera-relay.service`；原相机预览、底盘与导航不受这些独立服务管理。保留旧镜像、脚本和配置备份，不为回滚删除凭据或数据库。

相机稳定性修复引入 relay 私有新鲜度响应头：升级先 relay 后 Collector；新版 relay 保留旧 `/status`，可兼容旧 Collector。若需要回退 relay 到没有这些头的版本，必须先恢复匹配的旧 Collector 并仅重启该服务，否则新版会因无法验证帧身份／年龄而拒绝图片。网页相机新鲜度头与前端作为同一个云镜像一起回滚，不将新版 `media.js` 单独覆盖到旧后端。上传 schema v1、凭据和数据库无迁移。

### 20时段浏览器条件检查与HTTP频率回滚

仅恢复原HTTP最高1 FPS：将 RK3588 `/home/orangepi/.config/systemd/user/robot-cloud-collector.service.d/http-camera-rate.conf` 移到drop-in目录外的私有备份位置，执行 `systemctl --user daemon-reload` 后只重启 `robot-cloud-collector.service`。不要删除其他drop-in或覆盖collector.env。未设置 `COLLECTOR_HTTP_CAMERA_FPS` 的新Collector仍兼容原策略，ROS图像频率、Jetson采集与relay无需改变。

浏览器条件检查发布镜像为 `c4be2aaba524b56297888ce44daea0c3899b6aa5`。需回退云页面和条件GET时，使用已保留的成对旧镜像 `1e2c6da01e4abf04669fe3d1f1f3f8ec509b8a16`，不要单独覆盖新版JS或后端。设备token、登录凭据、TLS及SQLite均不回退；此版本没有数据迁移。若同时恢复不认识HTTP频率参数的旧Collector，先撤销该频率drop-in，再恢复本轮开始前的 `collector/config.py` 与 `collector/http_camera.py`，仅重启Collector。

本轮开始前的源码备份已持久保存在RK3588 `/home/orangepi/.local/state/robot-cloud-camera-backups/20261012-browser-freshness/before-source.tar.gz` 与ECS `/opt/robot-cloud/.runtime/rollback/browser-freshness-20261012/before-source.tar.gz`（目录0700、文件0600，仅源码、不含环境或凭据）。仅选择对应文件恢复；临时目录不是永久灾备。原相机程序和USB采集设置本轮未改，不为这个回滚停止相机、底盘或导航。

### 历史相机占位卡清理的回退

本次仅改变云网页显示清单：去掉 `fisheye/usbRear/usbPort21` 实时卡片和对应浏览器轮询，不删除媒体协议、API、Collector订阅或详细ROS诊断。若需恢复原9张卡，成对回退到已保留的云镜像 `c4be2aaba524b56297888ce44daea0c3899b6aa5`；不单独覆盖JS到其他版本。恢复页面不需要重启相机、relay、Collector或隧道，不回退640×360画质、HTTP2 FPS、帧身份或2秒TTL。

## 备份与恢复

### 相机占用提示回退（2026-10-13）

仅撤销占用显示时，成对回退云前端／后端到保留镜像 `800d168443dfc68918f460c9ec0385d69c1c0527`。现有8081只读网关的 `cameras` 字段兼容旧原生代理，八条转发可保持8081，无需重启相机、标定、Collector或原HTTP隧道；旧页面会恢复通用等待提示，不再显示独立占用状态。数据库、账号、设备token及TLS无需回退。

若以后需要撤销独立网关，先明确它的观看者已退出，再只停止Jetson `robot-cloud-camera-status-gateway.service`，恢复RK3588固定转发的目标到旧8080，并仅重启这八个新增模板实例。网关不打开相机，停止它不能代替停止采集，也不会把设备释放给标定；未知采集程序不得强杀。切换CLI已依赖网关，撤销时需一并恢复对应CLI版本，不能留下缺网关的半套状态接口。

本轮纯源码备份在RK3588 `~/.local/state/robot-cloud-camera-backups/camera-busy-before-20261013/before-source.tar.gz` 与ECS `.runtime/rollback/camera-busy-before-20261013/`（私有目录、0600文件）；仅按精确文件选择恢复，不覆盖 `.env`、数据或证书。

### 原生视频接入回退（2026-10-13）

本轮开始前纯源码备份在 RK3588 `~/.local/state/robot-cloud-camera-backups/native-video-before-20261013/before-source.tar.gz`（0700目录、0600文件），原云镜像 `62f84f38a63c96e909121497b27ca3039260a8d0` 保留。回退时使用整套旧云镜像，不单独混用前端与后端；不覆盖 TLS、登录凭据或 SQLite。生产环境备份仅保留在 ECS 私有目录，不复制到机器人。

恢复低清 JPEG 需先在 Jetson 停止本次 `yolo-native-direct-preview.service`，再启动保留的 `robot-cloud-lowres-preview.service` 与 `robot-cloud-camera-relay.service`，避免两套采集抢相机；不改原相机源码、USB驱动参数、原配置或底盘／导航。Collector本轮不重启，原 HTTP 隧道保留。原生视频专用隧道只停 `robot-cloud-native-source-tunnel@<四个固定key>` 与 `robot-cloud-native-cloud-tunnel@<四个固定key>`，不要停止 SSH 服务、整机网络或其他机器人程序。

仅关闭原生代理可将 ECS `NATIVE_VIDEO_SOCKET` 置空并重建后端容器，恢复兼容 JPEG 模式；前提是匹配 JPEG 上游已经运行。暂停网页或单路观看不需要停止相机采集，也不需要重新启动其他观看者的来源服务。

数据库当前为 `data/latest.sqlite3`，仅最新观测和source/session/sequence检查点；媒体在内存，图表历史在浏览器，视频由访问者下载。
使用SQLite在线backup，避免只复制运行中的主文件漏WAL：

```bash
sudo python3 deploy/scripts/backup.py data/latest.sqlite3 --output-dir backups
```

备份可能包含真实机器人状态，权限600，并仅在受控私有存储保留。建议保留最近7份，清理由操作者明确选择文件后执行；工具不自动删除。
data由容器uid10001拥有，普通SSH用户未必可读；上述sudo只运行数据库备份，产物root拥有，不改变原data权限。
`.env`及TLS私钥另行在受控密钥存储备份，不放Git。
恢复时先只停云backend（`bash deploy/scripts/cloud.sh stop backend`），将旧 `data/latest.sqlite3` 以及存在的 `data/latest.sqlite3-wal`、`data/latest.sqlite3-shm` **同组移动**到新的私有备份目录；不要只覆盖主文件而保留旧WAL。保留可恢复旧文件，不删除或用宽范围glob清理。
然后把明确选择的SQLite在线备份复制为 `data/latest.sqlite3`，确认uid10001可读写、目录权限正确，再 `bash deploy/scripts/cloud.sh up -d backend`，验证readyz。生产恢复操作须由操作者明确选定备份/目标，本轮没有执行。此操作只停云后端，不停机器人控制。
服务器重启保留最新观测并重新根据时间判断过期；机器人重新同步媒体，不把持久化旧值显示为实时。
