# Observer 云迁移审计（2026-10-11）

## 主机与代码事实

当前为 `orangepi@rk3588`，aarch64，Ubuntu 22.04 / Orange Pi Jammy；工作目录 `/home/orangepi`。
原工程 `/home/orangepi/code/rk3588_observer` 存在，`/home/nvidia/code/upper_computer` 不存在。
新工程 `/home/orangepi/code/robot-cloud` 独立开发；不连接或操作 ECS。
上层 Git 根为 `/home/orangepi/code`，已有 `.gitignore` 修改和旧 Livox 文件删除；Observer/upper_computer 为未跟踪目录。这些既有变更全部保留，不能把父仓库当干净工作树。

审计读取了实际 node/state/telemetry/system_metrics/sensors/visuals/cameras/web_server、启动脚本和全部六个前端文件，而非仅凭文件名判断。
原工程 51 个源码、脚本、页面及 launch 文件已建立 SHA256 基线，以供回归确认未覆盖。

## 当前运行与依赖

8888 使用 stdlib ThreadingHTTPServer + websockets；ROS2 Humble Python/rclpy；psutil、OpenCV、NumPy。
`start_observer.sh` 优先 transient systemd user `rk3588-observer.service`，否则 nohup；执行独立 `rk3588_observer.node`，日志 observer.log，30秒健康检查，不应修改或重启它来完成云迁移。
ROS 主节点关闭 rosout 和参数服务，仅订阅；ObserverState 同时依赖 8765 只读回退及隔离相机子进程，不能整类直接搬到云端。
本次静态审计不能证明设备正在运行；运行图/真实收包结果另见测试报告。

## 前端、连接及页面

原生 HTML/CSS/JS，Canvas 图表；无 CDN、网络字体、构建框架。保留七页：总览、RK3588 系统、机器人状态、STM32 通信、ROS2 诊断、AI/NPU、实时画面。
精简/详细模式与图表历史由浏览器维护；页面切换不新建 WebSocket。
产品前端没有 localhost/局域网 IP 硬编码。单个同源 `/ws`，每秒 snapshot。
媒体：`/camera/{fisheye,color,depth}.jpg`、`/visuals.json`、`/visual/{map,localCostmap,globalCostmap}.png`，可保留云端兼容路由。
九模块浏览器录制已存在；只录制可见 Canvas，停止可下载，再录放弃旧段；视频不上传服务器。
当前前端主要依据 WebSocket 断线判过期；云端必须单独维护机器人状态，主动推送 STALE，不能用网页连接替代机器人连接。

## 实际数据接口

| 接口 | 类型 | 现有 QoS / 数据来源 |
| --- | --- | --- |
| /chassis_host/telemetry | std_msgs/msg/String | reliable/volatile depth10；由实际 host 发布，部署板不能仅由Topic名推断 |
| /fisheye_aruco/detection_status、/aruco_docking/state | String | reliable/volatile；Jetson识别/对接 |
| /fisheye_aruco/detected_ids | Int32MultiArray | reliable/volatile |
| /fisheye_aruco/board_pose | PoseStamped | reliable/volatile |
| /livox/points、/cloud_registered、/berxel/points | PointCloud2 | best_effort/volatile depth1；雷达RK，Berxel以实际部署为准 |
| /livox/imu | Imu | best_effort/volatile |
| /Odometry | Odometry | best_effort/volatile；RK FAST-LIO |
| /map、/local_costmap/costmap、/global_costmap/costmap | OccupancyGrid | reliable/transient_local depth1 |
| /plan | Path | best_effort/volatile；按需 |
| /scan | LaserScan | best_effort/volatile；二维雷达 |
| /localization_pose、/amcl_pose | PoseWithCovarianceStamped | best_effort/volatile；可选，现主链不使用AMCL |
| /fisheye_front/image_raw、/berxel/{color,depth}/image_raw | Image | best_effort/volatile；当前页面图像走Jetson桥 |
| /aruco_docking_preview/state、/diagnostics | String / DiagnosticArray | best_effort/volatile |
| /observer_camera/{fisheye,color,depth}/compressed | CompressedImage | Jetson已有桥，隔离DDS接收，best_effort depth1 |
| /observer_camera/depth_points、/observer_camera/status | String | Jetson既有点云抽样/健康桥 |

RK 当前导航配置已经改为 `/cloud_registered_body_xyzi`；原Observer仍使用旧 `/cloud_registered`。
新蓝海为 `/bluesea_new/scan`；原Observer未订阅它。独立Collector应允许显式Topic配置，不修改生产发布者或重映射生产控制话题。
串口真实帧只在 host 解析；Collector不直接打开串口。若唯一有值的来源是本地8765，不能冒充已经收到 ROS telemetry，必须记为 WAITING_UPSTREAM。

## 状态、数值与本机硬件

Telemetry要求 `telemetryTransportSchema=1`、`telemetrySourceNode=/chassis_host`。真实0保留；空值null；命令与实际严格分开。
Page 0x00/10/11/12/13 独立状态，未收Page不能从旧兼容页补值；频率来自真实count差分。
`robot.data.debug.fields` 包含 value/state/type/source/ageMs/unit，云端保留这些元数据才能复用已有字段显示。
系统采集读 psutil、`/sys/class/thermal`、`/sys/class/devfreq/fdab0000.npu`。模型/FPS/延迟尚无推理接口，为null。
这些采集代码留在RK；云端只展示上传的RK数据，不把ECS系统指标冒充RK。

## 媒体迁移矩阵

| 能力 | 审计状态 | 云迁移处理 |
| --- | --- | --- |
| 普通遥测/系统/诊断 | ALREADY_AVAILABLE | 只读采集、受约束版本协议、云端兼容适配 |
| 图像JPEG/深度伪彩 | ALREADY_AVAILABLE | 限尺寸/质量/频率，单帧有界缓存、HTTPS上传；实际上游未运行时WAITING_UPSTREAM |
| 点云可视化 | ALREADY_AVAILABLE | 限量XYZ采样、限帧；保留frameId，不上传整云，不坐标融合 |
| 地图PNG/分辨率/frame | ALREADY_AVAILABLE | 内容哈希去重上传，保留origin/orientation，机器人离线时过期 |
| 新FAST-LIO/蓝海Topic | ADAPTER_REQUIRED | Collector显式配置真实名称，数据验证待实机 |
| 地图上的定位/路径叠图 | NOT_IMPLEMENTED | 当前页面没有此能力；保留姿态、路径、地图原点与坐标系，不猜变换 |
| AI模型运行统计 | WAITING_UPSTREAM | 未提供实际推理Topic，不补示例 |

原点云最多4000点、地图边800像素、JPEG最多960×720/约70%质量。云上传采用更保守默认频率并允许环境配置。

## 安全与存储审计

本地页面未提供公网登录、设备身份或TLS，不可把现有端口直接公网转发。
云端新增独立设备Bearer凭据、robot/source绑定、浏览器密码登录与HttpOnly会话，WS和媒体同样鉴权；上传不能成为浏览器凭据。
原应用没有数据库历史持久化；原图表300点仅浏览器内存。云端只持久化最新状态/顺序检查点，媒体和请求缓冲有上限；长期历史独立规划。
原日志可能包含数据异常，不应上传生产日志；新日志不记录令牌、密码或完整遥测请求。
无现有AI对话触发链路需迁移，不接入第三方AI。

## 复用与职责

直接复用到新项目：六个前端文件及纯truth/telemetry/system/媒体解析代码，记录原路径来源，保留原件。
留在机器人：ROS2、硬件系统采样、图像/点云/地图编码、自动上传；原host/驱动/8765/8888继续保留。
云端：协议校验、认证、顺序去重、最新状态/过期处理、同源WebSocket及HTTP媒体、SQLite、Nginx/Docker。
必须适配：新Collector生命周期、版本化上传DTO、机器人与网页在线状态分离、媒体去重/限流、浏览器登录、TLS部署。
