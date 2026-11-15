# 架构与实现约定

机器人 ROS2 → 独立只读 Collector → HTTPS POST `/api/v1/ingest` → 云后端最新状态 → 同源 `/ws` / HTTP 媒体 → 已有RK中文页面。
原8888不参与云端数据路径；云端不安装ROS、不加入DDS，不提供控制接口。

## 协议 v1

统一 envelope：`schema_version=1, robot_id, source_id, session_id(UUID), sequence(正整数), kind, topic, message_timestamp(真实采样UTC秒或null), collector_timestamp(此次封装UTC秒), payload`。媒体payload的received_timestamp保持首次机器人接收UTC，后端另记云端接收UTC，缓存重发不改真实采样/接收时间。
`kind=observation`，topic `/observer/observation`，payload严格根键：`system,robot,devices,ros2,pages,errors`。这是有版本、大小、深度、字段和数值校验的传输DTO；后端将system映射为旧页面的rk3588。
`kind=heartbeat`，topic `/observer/heartbeat`，payload `uptime_s`。心跳不刷新真实观测采样时间。
`kind=media`，topic是真实ROS媒体话题，payload `{key,kind,status,ageS,hz,frameId,frameSeq,message_timestamp,...}`；key为fisheye/color/depth/lidar/registered/depthPoints/bluesea/scan/map/localCostmap/globalCostmap。image/map采用base64编码JPEG/PNG；cloud采用最多4000个有限XYZ，不接受任意文件或URL。bluesea/scan 为 LaserScan 传感器本地二维投影，Z=0，不做 TF 融合。
观测DTO最大256 KiB、单媒体JPEG/PNG解码后最大500 KiB、总请求最大1 MiB，嵌套深度和集合长度受限。非有限值归null，布尔不冒充数字。字段状态沿用英文枚举，UI显示中文。
每source只保留最新有界状态；服务器校验robot/source/kind权限，主source只能一个；可选相机source只授权指定媒体key。云端拒绝重复/乱序sequence及旧session回放，返回明确错误。
队列按topic/媒体key合并最新；容量为已启用媒体数加角色的非媒体项（primary 加2，cameras 加1），默认 primary 十一媒体合计13项，字节上限仍为4 MiB；发送时赋sequence。断网丢弃已过时媒体/状态，恢复发送最新，不回放无界历史。

## Cloud Backend约定

Python aiohttp、SQLite最新状态；云容器无ROS依赖。创建入口 `cloud/backend/app.py`，共享协议 `shared/protocol.py`。
环境：`ROBOT_ID, DEVICE_CREDENTIALS_JSON`（数组，token/robot_id/source_id/role，可选media_keys）、`VIEWER_USERNAME, VIEWER_PASSWORD_HASH`（scrypt格式，由脚本生成）、`SESSION_SECRET, PUBLIC_ORIGIN, DATA_DIR, ALLOW_INSECURE_LOCAL=false, PORT=8888`。
登录POST `/api/login`，会话cookie `observer_session`，HttpOnly/SameSite=Strict/生产Secure；`/api/logout`退出，`/api/session`会话状态。
保护index、snapshot、WS和所有媒体；静态login页可访问。WS校验Origin，保持心跳和定期过期更新，单浏览器单连接。上传Bearer不能访问查看接口，会话不能上传。
`/healthz`仅进程活性；`/readyz`服务/存储就绪；受保护 `/api/robots` 返回robot在线、last_seen、last_observation、age，网页在线不等于机器人在线。
兼容路由 `/snapshot.json,/ws,/visuals.json,/camera/{key}.jpg,/visual/{key}.png`。对离线或采样过期递归更新AVAILABLE→STALE，旧实际值可保留但带状态；媒体过期不返回图片。
心跳离线阈值10秒；观测过期阈值5秒；上游Age累加云经过时间，各自Page/字段过期仍按原2秒。静态map在连接健康时可长期有效，但断网即STALE。

## Collector约定

`collector/main.py`入口；直接独立ROS订阅、psutil/sys硬件采样，复用已审计纯解析模块到 `collector/legacy/`；不导入原ObserverState/LocalChassis/WebServer，不依赖8888。
机器人环境：`CLOUD_URL(HTTPS), DEVICE_TOKEN, ROBOT_ID, SOURCE_ID, COLLECTOR_ROLE=primary|cameras`，可覆盖实际ROS Topic；测试只允许显式loopback HTTP。
`collector/transport.py`负责有界队列、限速、HTTPS认证、指数退避抖动和正常退出；TLS验证开启，禁止重定向泄露Authorization。
默认RK主source采集全部真实可见topic；另台Jetson如必要仅用camera role，不能抢占主观测和RK系统身份。
单次测试创建只读采集节点，不启动驱动、不发布控制、不打开串口、不重启现有host。

## 前端与部署约定

六个页面资产独立复用到 `cloud/frontend/`，保留7页/图表/录制。新增登录页与云/机器人状态横条，数据源标明远程RK；同源端点保持，不引入浏览器到局域网路径。
Docker Compose提供后端+Nginx；80只健康/ACME/跳转，443对登录、WS、upload服务；HTTP验收只允许IP健康页，不在明文互联网发送凭据。
Cloud内部8888最多绑定宿主127.0.0.1，数据库只卷挂载；无8765、DDS、串口或控制端口映射。
systemd模板由用户按文档安装，开发期间不启用生产自启动。ECS IP/域名/SSH配置全用占位值。
