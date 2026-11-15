# 网页与 Jetson ArUco 识别

2026-10-22 已部署四路普通相机及深度相机彩色画面的 ArUco 四边形和编号，支持 `DICT_5X5_100` 的全部 ID 0–99，每帧最多 12 个码。与 YOLO 类别框同屏显示，录制包含当前有效框；机器人 → 视觉对接另有五路 ArUco 状态和编号卡。

## 输入与检测

Jetson 继续使用原共享采集，检测旁路从四路 `/camera/cameraN/inference.jpg` 缓存读取对应已编码帧，彩色订阅 `/observer_camera/color/compressed`。ArUco 独立解码完整源 JPEG 的灰度图，保留小码像素；YOLO 保留原降采样路径。原采集进程与网页 640 最长边、10 FPS 没有改变。

YOLO 单线程使用原 TensorRT 引擎，置信度恢复旧程序默认 `0.25`，最多 12 个目标。三个独立 CPU 工作线程处理完整 ArUco，每路最多一个在途任务，合计最多五个，不积压过时图像。每项结果的 YOLO 框与 ArUco 四角来自同一源图，完成后只发布一次；超过原 1.5 秒处理期限直接丢弃。

源码：

- `deploy/jetson/aruco_detector.py`：字典检测和规范化四角。
- `deploy/jetson/yolo_observer.py`：缓存、GPU 调度、CPU 工作线程、结果与 ROS 摘要。
- `shared/detections.py`：公开元数据的 `markers` 白名单和凸四边形校验。
- `collector/aruco.py`：只读 ROS 摘要验证与年龄累计。
- `cloud/frontend/detection-overlay.js`：四角、编号与原帧关联；复用录制画布。

Jetson 安装时，新 `aruco_detector.py` 必须与 `yolo_observer.py` 同在 `~/.local/share/robot-cloud/`；`shared/detections.py` 还需复制为该目录的 `detections.py`，供独立网关使用。继续运行 `robot-cloud-yolo-observer.service` 与只读网关，不需要另开摄像头或运行对桩启动器。

## 两条输出

网页仍通过批准权限保护的 `/native-camera/detections` 获取数据。每个结果增加 `markers: [{dictionary,id,corners}]`，码框没有虚构置信度。原 session / PTS / JPEG SHA256、最多 10 组历史、私有文件与身份验证规则保持。密集结果会按最旧完整历史帧裁剪到 124 KB，保留每路最新完整结果。

浏览器每 500 ms 取元数据，普通视频仅选同 session 且在当前视频帧过去 0.6 秒内的结果，彩色必须精确匹配实际 JPEG。未匹配、无目标、过期、冻结、离页或注销就清框，不让旧码框续期。短暂不显示框属于匹配保护，不代表码板已经定位成功。

Jetson 新增只读 ROS 话题 `/observer_camera/aruco_detections`，可靠 `std_msgs/msg/String`、2 Hz，仅包含五路状态、帧龄、字典、编号，最大 8192 字节；无图像、位置、TF 或速度。IPC 采集器只订阅该话题，将结果存放在 `devices.visionDocking.arucoPreview`。摘要、服务端该子树和状态卡统一最多 5 秒；采集器明确失效或离线仍清空，旧位姿/对桩保持原 2 秒时限。

原 `fisheye_aruco` 双码位姿程序仍需要匹配的相机、内参与安装几何。旧配置的设备序列号链接当前不存在，因此本次没有将现有普通相机套入旧标定，也没有把二维识别发布成 `/fisheye_aruco/board_pose` 或启动对桩运动。新话题恢复的是码检测观测，自动对桩与目标板位姿需要另行核实硬件和标定。

## 验证与恢复

实物后视画面已检出 ID 10、11，用户确认网页显示码框。正式 Jetson 20 秒五路持续可用，ROS 约 2 Hz；四路编码约 9.96–10.01 FPS，采集进程 PID 保持，服务无重启循环。侧车约使用两核、cgroup 内存约 1.25 GB；板上 16 GB 内存有余量。

真实后视 Firefox 读流验证，在额外模拟 150 ms 元数据延迟、原 0.6 秒窗口和 500 ms 轮询下，稳态 129 帧中 120 帧显示有效框（93%），约 10.04 FPS，无重连或 JavaScript 错误；未保存真实影像。合成测试覆盖全部 100 个 ID、旋转、同 ID 多位置、小码、无码和非法数据；2592×1944 中 28 像素小码完整图可识别，而 1/4 缩图未识别。

整体暂存回归 1125 项通过、1 项因本机没有 Docker Compose 跳过；随后工作线程相关 19 项与摘要有效期相关 17 项检查通过。桌面和手机的码框、录制、独立状态卡、空结果、过期及权限分别验证通过。阿里云完整回环视频/元数据检查及公网元数据/JPEG检查通过；公网检查保留用户观看，没有再增加一组四路视频，不等同于多用户带宽验收。

维护源码在工控机 `~/.local/share/robot-cloud`。备份和验证报告在 `~/.local/state/robot-cloud/aruco-20261022`；Jetson 同名目录记录原三个文件和采集 PID；ECS `/opt/robot-cloud/.runtime/aruco-20261022` 记录原镜像、文件及状态卡小补丁。回退云镜像只替换 backend 并 reload nginx，保留账号、凭据和 TLS；回退 Jetson 只需恢复上述三个文件并重启旁路/网关，采集服务不参与。回退 IPC 采集器时恢复备份的三个 collector 文件再重启只读采集器。

`deploy/scripts/verify_yolo_live.py --require-aruco` 在 ECS 内使用只读凭据挂载，检查 marker 合同和 ROS 来源状态；可结合 `--without-native-streams --proxy-port PORT` 验证公网已有观看下的数据。凭据不得复制到机器人或浏览器。没有新增开机自启；来源采集应由原程序拥有者管理。

最终云镜像：`f52ef1f083001d71e36c1433d36edf6d0be20c3a`。状态卡补丁前镜像：`4b779abfcd39757261f6e414edc45e8bdee0b689`；功能前镜像：`737dea222996327f95ed791fdfc8d3b9691adc34`。最终公网 20 次快照中五路状态全部 AVAILABLE。
