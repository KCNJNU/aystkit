# 已审计纯模块来源

来源：`/home/orangepi/code/rk3588_observer/src/rk3588_observer/rk3588_observer/`，2026-10-11 静态审计后独立复制。

复制 truth.py、telemetry.py、system_metrics.py、devices.py、sensors.py、camera_codec.py、point_codec.py。它们不导入控制节点、不启动网页或串口。原目录未修改；云工程运行时完全不依赖原目录、8888 或旧 ObserverState。

部署实测发现本机已有底盘反馈来自 8765 的只读广播，而 ROS2 遥测仍无发布者。新增独立 `collector/local_chassis.py`，复用原 `local_chassis.py` 的 17 字节/BCC/Page 解码和年龄语义，传输使用项目已有 aiohttp；只在显式 `COLLECTOR_CHASSIS_SOURCE=local_ws` 时启用固定 loopback 8765 的接收连接。它不读取原 Observer 代码、不依赖8888、不发送控制或应用消息、不打开串口、不修改8765服务。默认仍为ROS2，选源不自动切换或混合，ROS2诊断保留真实状态。

云端适配记录：遥测允许非有限 JSON 数值归 null并记录计数；结构错误拒绝。订阅、媒体限流和上传由新的 collector 模块负责，默认配准点云 Topic 更新为 /cloud_registered_body_xyzi；/cloud_registered 可通过配置显式覆盖。
