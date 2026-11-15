# 机器人只读 Collector 部署

本工程在机器人端读取已运行 ROS2 数据源及本机 psutil、`/sys` 硬件指标，然后主动 HTTPS 上传。还支持显式选择本机底盘服务的只读广播；原 Observer 8888、底盘 8765、host_node 与串口均不受 Collector 生命周期控制。当前部署结果见 [部署状态](deployment_status.md)。

## 2026-11-04 相机预览调度

工控机 `collector.env` 的 `COLLECTOR_IMAGE_FPS=5` 与 Jetson 已有的 5 Hz 彩色／深度预览对齐。图像接收按固定采样时钟限帧，避免按上次回调时间累计误差而额外丢帧；长时间断流后只接收最新帧，不补发历史帧。普通相机 H.264 仍为 640 像素最长边、10 FPS、每路 350 kbps。

观测页面只读取当前实际可见的相机画面，排除固定顶栏及手机底部导航遮挡的部分。离屏取消该路媒体请求，滚回后重新验证并解码；页面切换及退出仍停止全部媒体。JPEG 预览每路最多 4 次条件请求／秒；检测接口失败后从请求结束时退避 1、2、4 秒，成功后恢复原 500 ms 刷新周期。视频、深度值和检测结果的原有效期保持原值。

相机端的新视频连接从最新可独立解码的关键帧开始，避免启动时突发回放整个环形缓存；运行中缺片则结束该连接供播放器重连。以上均为网页预览调整，归档的采集参数由归档服务单独管理。隔离浏览器验证入口为 `tests/browser_camera_viewport.py`，覆盖桌面、手机、滚动恢复和离页停流。

## 安装及手动启动

先确认当前是应运行主采集的 RK3588；不要在云服务器启动 Collector，也不要把 Jetson 系统指标标成 RK3588。

```bash
cd /home/orangepi/code/robot-cloud
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements-collector.txt
source /opt/ros/humble/setup.bash
cp .env.collector.example .env.collector
chmod 600 .env.collector
# 编辑 .env.collector：设备身份/凭据/真实HTTPS域名，并核对已有DDS配置。
set -a
source .env.collector
set +a
.venv/bin/python -m collector.main
```

`--system-site-packages` 使本机现有 ROS Python、OpenCV、NumPy 可用。若源环境缺少 OpenCV/NumPy，请在独立虚拟环境安装兼容机器人架构的包，先评估资源；不要替换机器人系统级依赖。保持原本 DDS 域、RMW 与发现配置；上述启动不修改全局配置，不启动任何驱动。`.env.collector` 只放受控机器人设备凭据，权限设为 `600`，不进入 Git。
已有配置时不要再次cp覆盖它。模板中的ROS_DOMAIN_ID=0注释只是示例，必须与真实发布者匹配；若需要工作区消息定义，可在当前进程source已有install/setup.bash。

当前RK系统缺ensurepip，若标准venv命令失败，可只为本项目建立无pip虚拟环境，使用已存在的系统pip安装到虚拟环境目录（不修改系统site-packages）：

```bash
python3 -m venv --without-pip --system-site-packages .venv
COLLECTOR_SITE_DIR=$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
python3 -m pip install --target "$COLLECTOR_SITE_DIR" -r requirements-collector.txt
.venv/bin/python -c 'import aiohttp, psutil, cv2, numpy; print("Collector dependencies ready")'
```

不需要升级系统Python或为本轮开发安装系统级venv包。

主采集必填示例（替换占位值）：

```dotenv
CLOUD_URL=https://YOUR_OBSERVER_DOMAIN
DEVICE_TOKEN=YOUR_DEVICE_TOKEN_AT_LEAST_32_CHARACTERS
ROBOT_ID=YOUR_ROBOT_ID
SOURCE_ID=rk3588-primary
COLLECTOR_ROLE=primary
COLLECTOR_IMAGE_FPS=1
COLLECTOR_CLOUD_FPS=1
COLLECTOR_CLOUD_POINTS=1500
REGISTERED_TOPIC=/cloud_registered_body_xyzi
BLUESEA_TOPIC=/bluesea_new/scan
```

不要将占位 token 当生产凭据；设备 token 在云端 `DEVICE_CREDENTIALS_JSON` 中必须绑定相同 robot_id、source_id 和 primary 权限。token 与浏览器登录密码相互独立。

测试模式只允许明确设置 `ALLOW_INSECURE_LOCAL=true` 后上传到 `http://127.0.0.1:PORT`、`http://[::1]:PORT` 或 localhost。生产必须 HTTPS；SSL 验证始终开启，不跟随 HTTP 重定向。现有代理环境变量由标准 HTTP 库读取，不自动修改代理配置。

## 只读实机审计

```bash
cd /home/orangepi/code/robot-cloud
source /opt/ros/humble/setup.bash
.venv/bin/python -m collector.main --audit-seconds 10
```

无需云地址、token 或部署权限。此模式仅创建只读订阅节点、检查 publisher graph/QoS 和统计实际接收消息，10 秒后输出 JSON 并正常退出；不会运行上传线程、连接 HTTP 相机预览、启动驱动、读取串口或绑定 8888/8765。输出每个 Topic 的 type、publisher 数、实际 messageCount、age、Hz 和发布者 QoS。`WAITING_UPSTREAM` 是未发现上游；publisher 可见但未收到为 `NO_DATA`；类型不符合或消息损坏为 `ERROR`。不要把接口存在与实际数据传入混为一谈。

## 实际 Topic 与 QoS

### 工控机独立串口只读遥测（当前接法）

2026-10-22 用户选择将底盘 CH340 USB 串口保留在工控机。上位机工作区 `/home/user/code/upper_computer` 新增独立 `run_chassis_telemetry_rx.sh` / `chassis_telemetry_rx`，以 115200、`O_RDONLY` 接收真实帧并发布既有 `/chassis_host/telemetry` String 契约（schema 1、来源 `/chassis_host`）。Collector 保持 `COLLECTOR_CHASSIS_SOURCE=ros2`，自身仍不打开串口、不启动或停止该接收器。

```bash
systemctl --user start robot-cloud-chassis-telemetry.service
systemctl --user status robot-cloud-chassis-telemetry.service
systemctl --user stop robot-cloud-chassis-telemetry.service
```

该 unit 未开机自启；维护模板为 `deploy/systemd/robot-cloud-chassis-telemetry.user.service`。前台运行须先停止该 unit，再执行工作区内 `./run_chassis_telemetry_rx.sh`。接收器独占串口，不能与普通 `host_node` 或第二接收器共用；没有行驶、刷子、举升、手柄、`/cmd_vel` 控制及 8765 控制页面，输出明确标记 `RX_ONLY`、`controlEnabled=false`。Jetson 原运动控制没有迁移，当前仅恢复观测数据。

当前实际只收到旧 Page `0x00` 的电压；速度、刷子、举升及故障详细反馈所需 V2 Page 未上报，保留 `WAITING_UPSTREAM` 和空值。失效页保持 `STALE`，不会由接收器心跳、旧累计计数或默认零值刷新。两机各 8 项协议/真实伪终端测试及 SIGTERM 完整参数恢复证据在 `/home/user/.local/state/robot-cloud/chassis-rx-20261022/acceptance.json`；Collector 的 inactive-page 修正经 `tests/test_collector.py`、`tests/test_local_chassis.py`、`tests/test_chassis_selection.py` 共 106 项回归。

### 本机底盘只读广播（显式选择）

`COLLECTOR_CHASSIS_SOURCE=ros2` 为默认值。当实际底盘数据来自已有本机服务时，可在 primary Collector 配置 `COLLECTOR_CHASSIS_SOURCE=local_ws`，接收固定 `ws://127.0.0.1:8765/ws` 的 `status/rx` 广播。不向该连接发送应用消息，不读取/打开串口，不修改或重启 host_node，不依赖 8888。

两种来源只选一个作为机器人底盘反馈，不自动回退、不混合字段/Page。ROS2 订阅及诊断仍保留原始状态：本机广播正常不能使 ROS2 显示正常。相机角色不能选择 local_ws，`--audit-seconds` 也不连接8765。只解释真实接收帧；`tx/drive/nav` 等指令广播忽略。独立 Page 年龄、缺失字段及 ACTUAL 标记沿用已审计协议，上游串口坏帧计数仍为 null。

### ROS2 接口

| 类别 | 默认 Topic | 类型 | 订阅 QoS |
|---|---|---|---|
| STM32 遥测 | `/chassis_host/telemetry` | `std_msgs/msg/String` | RELIABLE / VOLATILE / depth 10 |
| 视觉识别 | `/fisheye_aruco/detection_status`、`/fisheye_aruco/detected_ids`、`/fisheye_aruco/board_pose` | String、Int32MultiArray、PoseStamped | RELIABLE / VOLATILE / depth 10 |
| 对接观测 | `/aruco_docking/state` | String | RELIABLE / VOLATILE / depth 10 |
| MID360 | `/livox/points`、`/livox/imu` | PointCloud2、Imu | BEST_EFFORT / VOLATILE / depth 1 |
| FAST-LIO | `/Odometry`、`/cloud_registered_body_xyzi` | Odometry、PointCloud2 | BEST_EFFORT / VOLATILE / depth 1 |
| 地图 | `/map`、`/local_costmap/costmap`、`/global_costmap/costmap` | OccupancyGrid | RELIABLE / TRANSIENT_LOCAL / depth 1 |
| 路径 | `/plan` | Path | BEST_EFFORT / VOLATILE / depth 1 |
| 二维雷达 | `/scan`、`/bluesea_new/scan` | LaserScan | BEST_EFFORT / VOLATILE / depth 1 |
| 定位 | `/localization_pose`、`/amcl_pose` | PoseWithCovarianceStamped | BEST_EFFORT / VOLATILE / depth 1 |
| 相机原图 | `/fisheye_front/image_raw`、`/berxel/color/image_raw`、`/berxel/depth/image_raw` | Image | BEST_EFFORT / VOLATILE / depth 1 |
| 深度点云 | `/berxel/points` | PointCloud2 | BEST_EFFORT / VOLATILE / depth 1 |
| 预览状态／诊断 | `/aruco_docking_preview/state`、`/diagnostics` | String、DiagnosticArray | BEST_EFFORT / VOLATILE / depth 1 |
| 已有压缩桥 | `/observer_camera/{fisheye,color,depth}/compressed` | CompressedImage | BEST_EFFORT / VOLATILE / depth 1 |
| 已有点云桥 | `/observer_camera/depth_points` | String（已审计 XYZ JSON） | BEST_EFFORT / VOLATILE / depth 1 |

这些是源码中已确认的接口；当前 publisher 和收到数据以只读审计结果为准。可使用 `COLLECTOR_TOPIC_` + 内部 key 大写覆盖，如 `COLLECTOR_TOPIC_REGISTERED`、`COLLECTOR_TOPIC_BLUESEA`、`COLLECTOR_TOPIC_LOCALCOSTMAP`。`REGISTERED_TOPIC`、`BLUESEA_TOPIC` 是两个常用别名，通用形式优先。类型绑定保持明确，不以任意控制 Topic 推断实际反馈。

## 主源与可选 Jetson 相机源

默认只运行 RK3588 的一个 primary Collector；它采集完整观测和当前真正可见的媒体。同一个媒体 key 若同时收到原图和现有压缩桥，在压缩桥正常时只采用压缩桥。

跨机接收 Berxel 时，可显式设置 `COLLECTOR_BERXEL_TRANSPORT=bridge`。该模式不创建 `color`、`depth`、`depthPoints` 的三个原始订阅，避免原始大图与完整点云同时占用网络；继续接收已有 `/observer_camera/` 压缩图和精简点云。默认 `raw` 保持原有订阅行为，其他取值拒绝启动。两个 Collector 角色均支持此设置，其他传感器、HTTP 视频、媒体权限及话题覆盖配置不变。必须由操作者先启动 Jetson 上的相机和现有编码器；Collector 只有收到真实压缩帧后才显示可用，桥离线不会悄悄切回高带宽原始订阅。

Berxel 网页预览编码器的受维护源码在 `deploy/jetson/berxel_preview/`，部署到 Jetson 的 `~/.local/share/rk3588-observer-camera/`。`stable.env.example` 为稳定优先参数：最长边480、JPEG质量45、桥图像5Hz、点云最多900点/2Hz；工控机另设 `COLLECTOR_IMAGE_FPS=2`，保留 `COLLECTOR_CLOUD_FPS=1` 和 `COLLECTOR_BERXEL_TRANSPORT=bridge`。640×400原图生成480×300预览；原始采集、鱼眼及四路H.264参数不变。点云仅保留真实抽样坐标，以三位小数紧凑JSON传输，保留原始总点数和坐标系。无新消息不重发旧帧，源图像时间戳和2秒实时有效期保持有效。新增环境变量未设置时保留旧960×720包围框、Q70、5Hz、3000点/2Hz行为。

如果相机只能在 Jetson 的另一 DDS 网络可见，可以在 Jetson 的独立工程副本运行 `COLLECTOR_ROLE=cameras`。此角色只订阅相机／深度点云及已有压缩桥，上传心跳和被授权媒体，不采集系统、不订阅主遥测、不上传主 observation。

每个媒体 key 必须只有一个权威上传者。用 `COLLECTOR_MEDIA_KEYS` 显式划分，比如 RK3588 主源 `lidar,registered,bluesea,scan,map,localCostmap,globalCostmap`；Jetson 相机源 `fisheye,color,depth,depthPoints`。云端设备凭据 `media_keys` 必须与分工一致。仅在两台机器各自配置启动环境，不修改全局 DDS，也不让云端加入 DDS。

未拆分来源时 primary 默认十一媒体项。新增两路二维扫描使用原有只读 LaserScan 订阅：`COLLECTOR_TOPIC_BLUESEA` 默认 `/bluesea_new/scan`，`COLLECTOR_TOPIC_SCAN` 默认 `/scan`。网页绘制本地 XY 平面并保留真实 Topic、frameId、上传来源和频率；不推测 `/scan` 的具体型号，也不把上传来源误作实体相机的连接主机。无消息或未运行上游时保持等待，Collector 不会替用户启动雷达/相机驱动。

### 现有 Jetson HTTP 相机预览（可选，只读）

若相机已由 Jetson 的 `camera_preview` 提供本机 HTTP 预览，可复用 RK3588 的 primary Collector 和设备凭据接收。Jetson 只需新增独立的只读单帧 relay，不另启 Collector 或相机驱动、不修改原相机程序。该功能默认关闭；显式配置如下：

```dotenv
COLLECTOR_HTTP_CAMERA_URL=http://127.0.0.1:18080
# 三台直接连接的示例；必须先核对实际拓扑，未指定时仍为 legacy3。
# 四路 Hub 低分辨率试验使用 jetson-hub4，不能沿用此示例。
COLLECTOR_HTTP_CAMERA_PROFILE=jetson-direct3
# 可选，仅HTTP预览；未设置时兼容原每路最高1 FPS，不影响ROS图像频率。
# COLLECTOR_HTTP_CAMERA_FPS=2
```

只接受这个固定回环地址，不接受局域网 URL、附加路径、查询参数或任意远程地址。部署顺序如下，不要把持续 MJPEG 流直接转发进 SSH：

1. 在 Jetson 核对现有 `127.0.0.1:8080` 预览服务，再单独部署 `collector/http_camera_relay.py`。脚本可独立运行，但除 Python 标准库外还需 OpenCV 和 NumPy 用于预览压缩；本次 Jetson 已核对现有 OpenCV 4.10、NumPy 1.23，无需另装或替换系统依赖。用户级服务模板为 `deploy/systemd/robot-cloud-camera-relay.user.service`，其默认脚本位置为 `~/.local/share/robot-cloud/http_camera_relay.py`。relay 只绑定 Jetson `127.0.0.1:18081`，就地 GET 原 8080 的状态或首个 JPEG part；取帧后先关闭原 stream，再等比缩小至不超过 640×480、JPEG quality 60，返回有界、有限长度的响应。不裁剪、不改变原相机采集设置、不持有相机设备。
2. 确认 relay 运行及 SSH 主机身份、认证后，在 RK3588 建立 `127.0.0.1:18080` → Jetson `127.0.0.1:18081` 隧道；模板为 `deploy/systemd/robot-cloud-camera-tunnel.user.service`。隧道目标是单帧 relay，不是原 8080。只取首帧后关连接仍可能让直接隧道预先缓存额外 MJPEG 帧，造成带宽与延迟积累；本地 relay 使 SSH 只传有限响应。
3. 核对云端版本、媒体授权和下面的 Collector 配置后，才启用 RK3588 HTTP 适配器。relay、隧道与原相机程序生命周期独立；停止新服务不应停止原相机程序。

不将 8080、18080 或 18081 暴露到公网，不向 Jetson relay 配置或转发云端设备 token。上述为部署流程，不代表这些服务已经部署成功；实际状态另行验证。

relay 服务模板将 OpenBLAS/OMP/MKL 线程数设为 1，并限制内存 256 MiB、任务数 32。它是 Jetson 用户级服务：当前该用户 `Linger=no`，启用服务仅保证随用户管理器／会话启动，不等于已实现或已验收整机重启后无人登录自启。不要把 RK3588 的 linger 配置或启动验收结论套用到 Jetson。

三台直接连接的 profile `jetson-direct3`（不是对当前接线的自动判断）：

| 媒体 key | HTTP 路径 / 逻辑接口 | 页面标签 |
| --- | --- | --- |
| `usbFront` | `/stream/camera2` / `/http_camera/camera2` | 前视相机 |
| `usbLeft` | `/stream/camera1` / `/http_camera/camera1` | 左视相机 |
| `usbPort21` | `/stream/camera3` / `/http_camera/camera3` | 扩展相机 · 接口 2.1 |

扩展接口方向未知，不标为后视或深度图；本profile不上传 `usbRear/usbHub2/usbHub3`，不挪用其旧身份。协议仍兼容这些key，但当前四路Hub页面已移除旧后视与旧接口2.1卡；切回direct3时还须核对页面显示清单，不能只换采集profile。启用前必须审计预览别名与实际设备/by-path一致。

默认 `legacy3` 的历史映射如下，只适用于原三路接线，不适用于上述 direct 或下面的 Hub 配置：

| 媒体 key | 已有 HTTP 路径 | 显示方向 | 上传逻辑接口 |
| --- | --- | --- | --- |
| `usbRear` | `/stream/camera1` | 后视相机 | `/http_camera/camera1` |
| `usbFront` | `/stream/camera2` | 前视相机 | `/http_camera/camera2` |
| `usbLeft` | `/stream/camera3` | 左视相机 | `/http_camera/camera3` |

四路 Hub 拓扑的 profile `jetson-hub4` 如下；独立低分辨率采集试验也使用这组映射：

| 媒体 key | HTTP 路径 / 逻辑接口 | 页面标签 |
| --- | --- | --- |
| `usbFront` | `/stream/camera2` / `/http_camera/camera2` | 前视相机 |
| `usbLeft` | `/stream/camera1` / `/http_camera/camera1` | 左视相机 |
| `usbHub2` | `/stream/camera4` / `/http_camera/camera4` | 扩展相机 · 接口 2.1.2 |
| `usbHub3` | `/stream/camera3` / `/http_camera/camera3` | 扩展相机 · 接口 2.1.3 |

`jetson-hub4` 不启用 `usbRear/usbPort21`，当前页面已移除两张历史卡，不挪用其身份；两路 Hub 接口方向未知。旧鱼眼ROS入口目前无发布者，也已从实时画面移除占位卡，不能据此断言它与四路USB属于同一实体设备。后端/API/协议及详细ROS诊断保留兼容。cameraN 只是预览程序别名，更改 profile、接线或上游配置前必须核对 cameraN 与真实 USB 设备/by-path 的对应，不能按编号猜方向或把 legacy 映射直接用于回滚。历史 Hub 源端曾仅两路扩展相机可用，前视/左视受 USB2 带宽不足影响；direct3 也曾完成三路短时验收，之后接线再次改变。历史出图不代表当前状态，拔插停流也不等于网页故障。实际运行 profile、是否保留低分辨率试验及持续监测结果只以 [部署状态](deployment_status.md) 的实测记录为准。

以上均为普通相机预览，不是深度图或测距数据，不冒充原有鱼眼、Berxel 彩色或深度流。接口标识不是 ROS2 Topic，不生成 ROS 发布者计数或采样时间。HTTP 元数据中的 `width/height` 是 RK3588 收到的 relay JPEG 尺寸，不是未经提供的相机 native／原始采集分辨率；不能据此声称传输了原生原图。只执行 `GET /status` 和白名单流的 GET，每次消费一个有界单帧响应；成功的有限HTTP/1.1响应可在relay规定的连接寿命内复用，不读取持续视频流。不发送 POST、不打开相机设备、不启动底盘或导航。

全局协议支持17媒体项（9相机），不代表Collector全部启用；当前页面显示14个媒体卡（6相机），显示清单与协议兼容清单分开。开启URL且未显式指定 `COLLECTOR_MEDIA_KEYS` 时，`jetson-direct3` 为十一项加front/left/usbPort21，共14项；兼容 `legacy3` 为14项，`jetson-hub4` 为15项。三个profile不混用HTTP key；未开启HTTP仍是十一项。若已有显式媒体分工，须按所选profile核对允许key，不覆盖其他来源的分工或混用profile；错误配置拒绝启动。云端 primary 凭据未列 `media_keys` 时仍默认授权全部协议媒体key；显式授权列表须按需追加。每个key保持唯一上传者，不因启用HTTP而更换身份或生成token。`cameras` Collector角色不启用此HTTP适配器。

未设置 `COLLECTOR_HTTP_CAMERA_FPS` 时兼容原每路最高1 FPS，仍受 `COLLECTOR_IMAGE_FPS` 的更低值约束；显式HTTP频率允许0.1–2 FPS，仅在HTTP适配器已启用时生效，不改变ROS的 `COLLECTOR_IMAGE_FPS`。2026-10-12当前部署显式为2 FPS，模板 `deploy/systemd/robot-cloud-collector-http-camera-rate.conf` 安装为 RK3588 `/home/orangepi/.config/systemd/user/robot-cloud-collector.service.d/http-camera-rate.conf`；daemon-reload后仅重启Collector。各路独立循环并错峰，不因一条慢请求同时阻塞其他相机。状态检查移至 Jetson relay 本机，RK3588 每帧只发一次 GET。跨 SSH 前已由 relay 等比压缩到不超过 640×480、quality 60；RK3588 对已满足 960×720 上限的 JPEG 解码并协议校验后直接上传，避免二次有损编码增大体积。更大输入仍按后续上限缩小、quality 70 编码。请求总超时 1.5 秒，状态体 32 KiB、part 头 4 KiB、单 JPEG 500000 字节；不使用环境代理，不跟随重定向。HTTP 网络处理在独立线程，不阻塞 ROS 订阅；`--audit-seconds` 不启动此线程或发出相机 HTTP 请求。

上游仅提供 `age_s` 与 JPEG，没有可信采样 UTC。relay 携带契约版本、原始 JPEG hash 和源年龄响应头（详见 [协议](protocol.md)），源年龄包含本机检查及处理时间；RK3588 再计入跨机请求和解码耗时。必须同时满足新鲜状态和原始 hash 变化才计为新帧，不能将压缩后预览 hash 当作原帧身份。重复原始 hash 不刷新 `frameSeq` 或 `received_timestamp`，超过 2 秒保守显示 `STALE`；原始字节完全相同的画面仍可能因此过期。临时 timeout/502/503/504 仅保留原帧剩余有效期，不上传旧图或刷新年龄；坏源元数据、坏 JPEG、真实到期立即失效。`message_timestamp` 为 null，`received_timestamp` 是 Collector 首次验证该新帧的时间，不是相机曝光时间。仅升级 relay/Collector 时先更新独立 relay，再更新 Collector，不需要重启原相机程序；替换采集源属于下节单独授权的操作。

### 四路 USB 低分辨率独立采集试验（显式授权）

`collector/usb_camera_preview.py` 是本工程独立实现的可选采集入口，不读取或复制 Jetson 原相机源码。它与原预览互斥，只有用户明确同意切换相机采集后才能启用。原 `camera_map.json` 不改；原预览程序和服务信息保留用于恢复。此节描述操作与验收要求，不代表试验已经成功或决定长期保留。

低分辨率发生在两个不同位置，不能混为一谈：

- 独立采集入口请求相机输出 **640×360 MJPG**，降低进入 USB 总线的采集负荷；必须用协商结果和实际图像尺寸验证，不能只看目标值。
- relay 在采集之后缩小 JPEG，只减轻跨机／公网传输与后续处理，不能修复不存在的设备路径，也不能降低已经发生的 USB 采集带宽占用。

独立入口固定 `127.0.0.1:8080`，只提供 `GET /status` 和固定 `/stream/camera1` 至 `/stream/camera4`，兼容已有单帧 relay。没有任意设备路径、控制操作或远程地址的 HTTP/CLI 参数。四条 by-path 固定如下，开机或拔插后仍须核验真实 VID/PID 和接线：

| 源别名 | USB by-path 接口段 | 已审计设备 VID:PID | 请求采集格式 | Collector 媒体 key |
| --- | --- | --- | --- | --- |
| `camera1` | `2.3` | `0bda:5187` | 640×360 MJPG，10 FPS | `usbLeft` |
| `camera2` | `2.4` | `0bda:5187` | 640×360 MJPG，10 FPS | `usbFront` |
| `camera3` | `2.1.3` | `0bda:d584` | 640×360 MJPG，30 FPS | `usbHub3` |
| `camera4` | `2.1.2` | `0bda:d584` | 640×360 MJPG，30 FPS | `usbHub2` |

完整路径前缀为 `/dev/v4l/by-path/platform-3610000.usb-usb-0:`，后缀为 `:1.0-video-index0`。这里的 FPS 是设备采集请求，不是公网刷新率。编码预览最高每路 5 FPS，Collector HTTP预览未配置时兼容原每路最高1 FPS、当前显式为2 FPS，ROS图像频率独立不变。新 USB 相机枚举的 MJPG 模式只有 30 FPS，不声称它支持未验证的 10 FPS。

只有该独立入口允许打开上述四个视频设备；relay 与 Collector 仍不打开 `/dev/video*`。每路读取在独立子进程中，阻塞和恢复只管理自己的采集子进程，不能终止外部程序。单路异常不能用旧帧伪装正常；2 秒新鲜度要求不放宽，`/status` 的 `age_basis=conservative_read_observation` 表示保守的读取观测年龄，不是硬件曝光时间。`requested`、`negotiated`、`frames`、`capture_hz` 用于区分请求值、驱动协商值和实际接收情况；没有真实帧时仍报告错误或等待。

部署位置为 Jetson `~/.local/share/robot-cloud/usb_camera_preview.py`，手动服务模板为 `deploy/systemd/robot-cloud-lowres-preview.user.service`，安装后的服务名为 `robot-cloud-lowres-preview.service`。模板 `Restart=no` 且没有 `[Install]`，不得通过 `enable` 宣称开机自动采集；单次 `systemd-run --user` 试验也是临时服务。使用 Jetson 已有 Python/OpenCV/NumPy，不替换系统依赖，不加载模型，不运行 ROS、串口、底盘、导航或 `run_system`。

切换顺序必须保持源身份与上传 profile 一致：

1. 只读记录原预览的精确 PID/命令/cwd、允许的运行环境、配置 hash 和视频 FD；环境或凭据不得输出到日志。核对四个 by-path、640×360 MJPG 的对应帧率支持，以及 8080/18081 的监听者。先准备已通过原程序 `--check` 的独立“仅前视＋左视”回滚配置，详见 [恢复步骤](rollback.md#独立低分辨率采集试验的恢复)。停止成功的 transient unit 可能被回收，必须有可重建的启动记录，不能只记服务名。
2. **先在 RK3588 停止 `robot-cloud-collector.service`**，避免切换期间将新的 camera3 内容按旧 `usbPort21` 身份上传。
3. 仅停止已经确认的 Jetson 原相机预览服务／PID，等待退出；确认 8080 空闲、四路视频设备无其他采集者，再手动启动 `robot-cloud-lowres-preview.service`。不得并行启动两个采集源，不按端口或 Python 名称宽泛杀进程。保留原 relay、SSH 隧道，不需要改它们的生命周期。
4. 核对 `/status` 四路真实帧、请求与协商格式，并用只读 `v4l2-ctl --get-fmt-video --get-parm` 复核。先观察至少 20 秒；若设备缺失、协商不符、仍有 USB 带宽错误或失去原本正常的前／左画面，执行已准备的回滚，不反复盲目重启。
5. 确认源已按上表出图后，将停止中的 Collector 显式设为 `COLLECTOR_HTTP_CAMERA_PROFILE=jetson-hub4`；若有显式 `COLLECTOR_MEDIA_KEYS`，也必须匹配该 profile。然后启动 Collector，核对单一上传者、云端媒体授权与公网 JPEG 帧递增。不要同时保留 `usbPort21`／`usbRear` 的错误来源。
6. 用持续监测区分源和公网结果：记录每路源错误／年龄／协商格式、公网 200/204/超时比例、帧递增、响应耗时与图像字节数。ECS 回环 HTTPS 验证不代表用户 Wi-Fi／公网客户端已经稳定；出现真实掉帧应如实记录，不能只凭一张截图宣布全部恢复。

脚本和服务模板部署后，可只读检查服务和状态；启动／停止仍按上述互斥顺序执行：

```bash
# Jetson：不打开额外采集，不包含图片或云端凭据。
systemctl --user status robot-cloud-lowres-preview.service --no-pager
curl --noproxy '*' --fail --max-time 2 http://127.0.0.1:8080/status
```

最终是否保留试验、实际 PID/profile 和监测指标记录在 [部署状态](deployment_status.md)，不要把 `jetson-direct3`、历史 Hub 出图或本文请求参数直接当作当前部署状态。

### 低带宽网络预览

独立 `http_camera_relay.py` 支持受限参数 `--preview-size 320x240`，将640×360源同比缩至320×180；默认不加参数仍是640×480上限。该参数只减少Jetson→RK3588→云端的JPEG字节数，**不能降低USB原始采集带宽**。quality60、原始JPEG hash、保守源年龄及2秒TTL不变；同一原始帧换压缩尺寸不续帧或续期。

2026-10-12 20:20按用户要求恢复网络画质：当前参数为 `--preview-size 640x480 --keep-alive`，本次640×360源不再缩小，JPEG quality60保持。只备份并更新Jetson已有 `preview-size.conf`，daemon-reload后仅重启relay；不重启源、隧道、Collector或云容器，不改变四路USB采集参数、HTTP2 FPS及真实2秒TTL。此操作恢复的是网络预览，不是切回原相机程序或恢复试验前未经重新验证的硬件高清参数。画面更大可能增加网络传输时间，实际连续性需以[部署状态](deployment_status.md#2026-10-12-网络预览画质恢复2020)记录的恢复后监测为准。

可选模板 `deploy/systemd/robot-cloud-camera-relay-low-preview.conf` 安装为 Jetson `~/.config/systemd/user/robot-cloud-camera-relay.service.d/preview-size.conf`，需先备份旧relay并安装支持该参数的版本，再daemon-reload、仅重启relay。源采集、隧道、Collector、云容器均无需为这个参数重启。四路USB采集与网络预览是两层不同设置，部署记录分别列出协商尺寸、实际源FPS和公网出帧率。

需要恢复640预览时，仅将该drop-in中的尺寸改成 `640x480`，保留 `--keep-alive` 和其他既有参数，不直接删除整个drop-in；基础服务模板不含连接复用参数，直接删除会同时撤销此前的延迟修复。该可选模板仍作为320档配置示例保留，不表示当前实机仍使用320档。

该模板同时显式开启 `--keep-alive`：成功HTTP/1.1单帧GET可复用客户端到relay的SSH转发连接，默认不开启仍为逐帧关闭。每连接空闲最多2秒、总寿命120秒、最多512个请求，仍仅5个worker；状态和错误响应关闭连接。每次重新从源读取帧和真实年龄，无视频缓存或连续流；源端连接始终单帧关闭。Collector严格校验有限Content-Length及固定multipart结尾并完全消费响应，防止未读结尾导致连接无法复用。帧身份和2秒TTL不变；HTTP取帧频率由上节独立配置，不由keep-alive参数改变。

## 数据真实性与时间

遥测必须是 schema 1、`telemetrySourceNode=/chassis_host` 的 JSON。真实 0 保留为 0，缺失／非有限数值为 null；遇到 NaN/Infinity 归 null并累计 `telemetryNonFiniteCount`，结构错误拒绝。COMMAND 字段与 ACTUAL 字段分别保留，绝不从指令推导实际反馈。Page 0x00/0x10/0x11/0x12/0x13 的内容、counter 和 age 来自上游；缺失或零次接收为 WAITING_UPSTREAM，不从其他 Page 补齐。

实时遥测和常规传感器默认 2 秒过期，代价地图 5 秒，诊断 3 秒。静态地图／按需路径以 publisher 存续判断，断开为 STALE。相机／点云没有新帧时不继续发送旧图假装实时。云端另按连接心跳判断离线，收到心跳不会刷新实际观测采样时间。

envelope 的 `message_timestamp` 只填写真实 ROS header UTC epoch 时间；String 没有可信采样时间时为 null，0 或仿真相对时间为 null。完整 observation 的 message_timestamp 为 null，collector_timestamp 是组装并采样系统指标的 UTC 时间。相机／地图／点云保留 frameId，地图保留分辨率与 origin position/quaternion，路径限 200 个实际 pose 并保留各自 frame；不会把未经 TF 转换的定位或路径叠到错误坐标系。

媒体的 `received_timestamp` 是 Collector 真正收到被编码消息的 UTC 时间；`collector_timestamp` 是本次封装状态的 UTC 时间；`message_timestamp` 是原 ROS 采样时间。静态地图重传保留原 received_timestamp 和采样时间，不把缓存地图冒充新采样；云端另记录实际接收时间。

## 带宽、缓存与重连

默认每秒一个 heartbeat、一个主 observation；图像 1 FPS，点云 1 Hz／1500 点，可配置图像 0.1–5 FPS、点云 0.1–2 Hz／最多 4000 个有限 XYZ。原 Image 转 JPEG，预览受 960×720 限制、quality 70；已有压缩桥的较大帧也缩小到相同预览上限，并在标记 AVAILABLE 前按共享协议检查完整 JPEG 格式与像素范围。深度图用现有伪彩转换，不当测距数据。单 JPEG/PNG 最大 500000 字节。地图 PNG 长边 800、内容／origin／frame hash 变化时上传；相同静态图默认 30 秒完整同步一次，且连接恢复立即补传，覆盖云服务重启导致的缓存缺失。

上述图像频率为ROS路径；HTTP预览当前单独设为2 FPS。网页在已解码相机且有强ETag时采用最短250ms条件GET、每路单个在途请求，未变化返回304空体，避免重复下载同帧。ETag同时绑定媒体key、帧身份与JPEG字节；304只确认该帧仍在原2秒期限内，当前帧龄继续增长，不续期、不恢复已过期录制。无有效ETag时仍按原1秒策略取图，地图PNG读取不改变。请求仍受云端现有读取限流，多个同IP全开画面的标签页可能触发429，不宜无限开页。

队列按观测类／媒体 key 合并最新，容量为已启用媒体数加非媒体项（primary +2、cameras +1；未开启HTTP时13项，`jetson-direct3` 与兼容 `legacy3` 均为14媒体/16队列项，`jetson-hub4` 为15媒体/17队列项），字节上限4 MiB不变；断网期间后来的值覆盖旧值，丢弃过时流数据，不无限保存历史。发送时才赋序号，成功 ack 后推进；首次请求 503、429、timeout 不会消耗序号。服务器已接收但 ack 丢失时，通过鉴权后的 409 duplicate/expected_sequence 协商；云状态丢失时建立新 session UUID 并重新同步。

同一待发送key更新时保留排队位置，旧版本在途确认后才将替换版本让到队尾，避免高频更新长期等待。AVAILABLE实时图像/点云按源年龄、已知时间戳和排队耗时合计2秒预算剔除；状态和静态地图保留既有策略。同一序号丢失应答后若载荷已被新帧替换，409 duplicate只推进已提交检查点，不误删尚未上传的新帧。

未变化的无数据/过期媒体元数据每30秒重发，状态、原因变化和重连立即同步；系统主观测及心跳频率不变。每60秒记录有界聚合：`Camera fetch diagnostics`含取帧耗时、源年龄和结果计数，`Cloud upload timings`含排队/POST/结束时帧龄。字段只使用固定媒体key和数值，不含图片、Topic、URL或凭据。若用户单元过滤未返回日志，可按实际Collector PID查询：

```bash
collector_pid=$(systemctl --user show robot-cloud-collector.service -p MainPID --value)
journalctl _PID="$collector_pid" --since '5 minutes ago' --no-pager -o cat
```

429 遵循有界 Retry-After（最长 60 秒）；网络／5xx 指数退避并加入抖动，最长 3 秒。401/403 暂停上传直到修正凭据并重启 Collector，日志不输出 token、服务器响应原文或数据。其他 3xx/4xx 丢弃被拒项目，不跟随 redirect。上传在线程中独立运行，网络故障不会停止 ROS 主循环；正常退出等待上传任务有限时间。

## 自动启动、停止与升级

systemd 模板位于 `deploy/systemd/robot-cloud-collector.service`，由用户按部署手册核对环境与路径后安装。模板只运行 Collector；上游驱动／host_node 的启动与生命周期保持独立。未收到上游数据时，Collector 保持运行并诚实显示等待状态。

仅在 HTTPS 验证成功后，由操作者安装自启动。下列为系统级服务安装方式（与用户级服务二选一，不能重复启动）：

```bash
# RK3588；先核对模板User/Group/WorkingDirectory/ExecStart及.env.collector。
sudo install -d -m 0700 /etc/robot-cloud
sudo install -m 0600 .env.collector /etc/robot-cloud/collector.env
sudo install -m 0644 deploy/systemd/robot-cloud-collector.service /etc/systemd/system/robot-cloud-collector.service
sudo systemctl daemon-reload
sudo systemctl enable --now robot-cloud-collector.service
sudo systemctl status robot-cloud-collector.service --no-pager
```

当前实机使用独立用户级服务，模板为 `deploy/systemd/robot-cloud-collector.user.service`。设备配置保存在 `~/.config/robot-cloud/collector.env`（目录0700、文件0600）；不要放入浏览器口令或云端会话密钥。需要 `sudo loginctl enable-linger orangepi` 才能在无人登录时启动并继续运行。模板 DDS 参数仅针对本次已核对的 domain0/FastDDS 环境，其他部署应核对真实发布者。

```bash
systemctl --user status robot-cloud-collector.service --no-pager
systemctl --user restart robot-cloud-collector.service
systemctl --user stop robot-cloud-collector.service
```

停止/重启仅作用于该新 Collector，不影响原8888、8765、驱动或主控。新部署、源选择和实测数据情况以 [部署状态](deployment_status.md) 为准。

可在collector.env设置 `ROBOT_ROS_SETUP=/home/orangepi/code/upper_computer/install/setup.bash` 以读取已有消息定义；这只source环境，不运行run_system或host_node。Jetson相机服务需要单独核对用户/工程路径及cameras角色，不复制RK系统身份。

手动运行时 Ctrl+C；如果用户已自行安装专用服务：

```bash
sudo systemctl stop robot-cloud-collector
sudo journalctl -u robot-cloud-collector -n 100 --no-pager
```

只停止这个精确服务，不使用 `pkill python`、不停止 host_node 或原 Observer。升级仅更新 robot-cloud 工程及对应虚拟环境，重新启动 Collector；云端停止／回滚不会改变底盘控制。回退只需停止新 Collector，原 8888 仍保留。

## 开发测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_collector.py -q
```

关闭插件自动加载避免本机旧 ROS launch_testing 与新版 pytest 冲突。测试消息只在测试进程和随机回环 HTTP 端口出现；测试覆盖真值/Page、类型、相机角色隔离、队列容量／失效、JPEG/PNG/点云、地图去重／补传、无控制输出 AST 检查以及真实 HTTP 503、ack丢失、重启恢复与禁止 redirect。

### 弱网恢复（2026-10-28）

观测上传保持单连接和同一来源的顺序确认，心跳、主观测、其他待发数据轮流取得发送机会；媒体、故障事件和测试指标之间也轮转，避免一个慢请求长期占用队列。心跳请求最多 2 秒、观测及事件/指标最多 3 秒、地图最多 4 秒；实时图片和点云受剩余的 2 秒真实有效期约束。极低带宽下地图或视频仍可能过期，不延长数据有效期或更改原始采集。

浏览器观测连接 10 秒没有有效快照（包括握手卡住、没有首包或只收到无效消息）会主动重连，网络恢复事件会尝试及时恢复。每条新连接独立计时，旧连接事件和重复网络事件不会重复建连；页面隐藏或认证失效时暂停。页面原有 3.5 秒无消息过期显示、服务器 10 秒机器人离线及各数据源的新鲜度规则保留。

这些修改已用隔离 HTTP/WebSocket 和真实浏览器模拟断流验证，不代表地库实车测试已完成。原始地库归档使用独立服务，开关已开启且传感器/供电/磁盘正常时，本地落盘和工控机入库不依赖公网；应以真实写入计数、归档文件和数仓记录进行现场验收。


### 一键启动后的深度画面接收检查

一键开启相机在 Jetson 的彩色、深度真实帧检查通过后，还会读取工控机当前 Collector 的接收状态。只有两路新帧计数持续增长、帧龄不超过 2 秒，才报告“工控机已连续收到深度相机彩色与深度画面”。状态文件为 `~/.local/state/robot-cloud/preview-receiver.json`，仅包含当前进程号、单调时钟、两路帧计数与帧龄，不保存图像。

如果接收服务先于相机启动，旧 DDS 订阅未恢复，一键开启会在有界等待后仅重启一次 `robot-cloud-collector.service`，重新确认接收。相机驱动、预览编码器、原始归档、底盘和导航不随此恢复动作重启。`--status` 仍为只读检查；接收未确认时明确报错，不把驱动进程存在当成网页已出图。
