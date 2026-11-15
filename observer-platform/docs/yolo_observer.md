# 网页五路 YOLO 识别

2026-10-22 补充：已增加独立 ArUco 码框与编号，YOLO 阈值恢复 0.25，见 [ArUco 识别与数据](aruco_observer.md)。

四路普通相机及 P100R 深度相机的彩色画面，在「实时画面 → 相机」叠加中文类别、置信度及检测框。深度伪彩图不做目标识别。使用 Jetson 已安装的 YOLO26n TensorRT FP16 引擎（COCO 80 类，包括人、车、椅子等）；没有新增训练，不代表能够识别所有清扫垃圾。

## 数据链路与画面参数

- 四路采集仍由原本的共享预览程序独占。YOLO 读取 `127.0.0.1:8080/camera/cameraN/inference.jpg` 的缓存，不再次打开 `/dev/video*`，不修改本地四宫格。
- 每个 inference 缓存对应一帧已经编码输出的源图。session 随编码进程重建，mediaTime 按实际输出访问单元累计；被编码队列丢弃的输入不参与输出时间线。
- P100R 彩色输入订阅原有 `/observer_camera/color/compressed`，只处理最新帧。请以 `deploy/jetson/yolo_observer.py` 中的固定 topic 为准；同一 ROS_DOMAIN_ID / DDS 图的预览编码器必须已运行。
- 一个模型轮流处理五路缓存；普通四路各最多 2 次/秒，彩色预览目标 5 次/秒。检测与视频独立；网页四路 H.264 继续最长边 640、10 FPS、每路目标 350 kbps。彩色网页沿用 480×300 JPEG 及既有上传限频。
- `~/.local/state/robot-cloud/yolo-detections.json` 仅保存最新有界检测元数据，权限 0600，包含每路最近最多 10 组、每组最多 12 个目标。没有保存图片或视频，也没有传送模型。
- 只读网关 `127.0.0.1:8081/detections` 经现有隧道到阿里云 `/native-camera/detections`。登录且获批准的账号才能获取结果；待批准与注销会话无权访问。

## 帧关联与过期

浏览器每 500 ms 获取元数据（仅此接口协商 gzip，实测传输量减少约 81%，不重新压缩视频或 JPEG），只有当前相机页面可见时轮询，最多一个在途请求。普通视频只选择同一编码 session、与当前呈现 PTS 相差不超过 0.6 秒的过去结果；彩色 JPEG 必须与实际显示图片的 SHA256 完全一致。四路是低频检测叠加，不是每个视频帧都推理；没有匹配结果时不画框。

结果年龄包含采集、推理、代理及浏览器请求耗时，最长 5 秒；彩色同时服从原图 2 秒时限。检测文件超过 3 秒未更新、来源消失、视频冻结、切页、隐藏标签或注销时清框。重复收到同一检测编号不会延长有效期；零目标也会清除上次框。录制会绘入当前有效识别框，保持原录制暂停与下载行为。

## 安装与运行

源码事实源为工控机 `/home/user/.local/share/robot-cloud`。普通相机编码关联源码在 `/home/user/code/upper_computer/src/yolo_camera`，同步到 Jetson 的同名 ROS 包后须在两机各运行 `colcon build --packages-select yolo_camera`。

Jetson 辅助脚本安装位置：

| 源文件 | Jetson 位置 |
| --- | --- |
| `deploy/jetson/yolo_observer.py` | `~/.local/share/robot-cloud/yolo_observer.py` |
| `deploy/jetson/aruco_detector.py` | `~/.local/share/robot-cloud/aruco_detector.py` |
| `deploy/jetson/run_yolo_observer.sh` | `~/.local/share/robot-cloud/run_yolo_observer.sh` |
| `deploy/jetson/robot-cloud-yolo-observer.service` | `~/.config/systemd/user/robot-cloud-yolo-observer.service` |
| `deploy/scripts/camera_status_gateway.py` | `~/.local/share/robot-cloud/camera_status_gateway.py` |
| `shared/detections.py` | `~/.local/share/robot-cloud/detections.py` |

引擎为 `/home/nvidia/yolo26_test/tensorrt_fp16/yolo26n.engine`，解释器为 `/home/nvidia/yolo26_test/venv/bin/python`，必须已有 CUDA/TensorRT/Ultralytics/ROS 依赖。运行脚本不下载模型、不自动安装依赖。采集来源由原程序拥有者管理；网页或旁路启动不打开相机。

在 Jetson 上，已有采集运行后：

```bash
systemctl --user daemon-reload
systemctl --user start robot-cloud-yolo-observer.service
systemctl --user status robot-cloud-yolo-observer.service
```

停止叠加推理：`systemctl --user stop robot-cloud-yolo-observer.service`。视频继续运行，框过期后消失。本次没有启用新的开机自启。

## 验证与回滚

`deploy/scripts/verify_yolo_live.py` 仅在 ECS 运行：凭据只读挂载 `/run/observer-initial-access.json`，同目录验证工具挂载 `/verify`。默认 20 秒 ECS 回环 TLS 验证，同时读取四路 MP4、两路 JPEG、五路检测；`--seconds 45 --proxy-port PORT` 可经明确的工控机代理检查公网传输。仅输出计数、时延、哈希匹配率和码率，不输出账号、Cookie 或图像。`verify_registration_live.py --run-account-check` 检查新账号待批准、批准、注销时的检测权限，并删除本次专用随机测试账号。

2026-10-22 备份位于工控机 `~/.local/state/robot-cloud/yolo-20261022`、Jetson 同名目录、ECS `/opt/robot-cloud/.runtime/yolo-20261022`。云端旧镜像 tag 与文件清单记录在该目录 manifest；使用旧 tag 重建 backend 并 reload nginx 可回退前端/后端，账号数据库、凭据与 TLS 不回退。Jetson 的 `sidecar-gateway-v1/rollback.py` 可校验并恢复旁路/网关，保持采集；若还需回退帧关联，恢复 `native-source-before` 中实际存在的文件，再编译原相机包。回退编码代码会需要重启对应采集，应与来源拥有者协调。

当前最终镜像为 `737dea222996327f95ed791fdfc8d3b9691adc34`，首次功能发布为 `06439d7df16baeb55751bfcd688d99215fa87787`，功能前镜像为 `ff07fbc6b15281db31672d1e5a4b4f368955ce61`。服务器 `retry-tls/` 保存首版功能前文件，`compression/` 保存压缩前文件，均在上述 ECS 备份目录。初次验收误用内部 HTTP，被原有强制 HTTPS 规则拒绝后已自动回滚；随后使用域名 TLS 重新发布通过。

本次恢复深度驱动时发现新版工作区环境只加入 `/opt/berxel/lib`，实际 SDK 为 `/opt/berxel/libs`，造成驱动 exit 127。只在该临时驱动 unit 中补齐真实库目录；没有修改全局环境。启动命令及故障日志在 Jetson `sidecar-gateway-v1/`，重启设备后应按该记录恢复来源。YOLO 不负责重启采集。

公网验收可加 `--without-native-streams`，在已有用户观看时仅检查元数据和两路 JPEG，避免再增加四路视频；此模式明确不代表新增四路公网播放验收。默认完整并发测试不变。
