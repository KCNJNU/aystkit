# 云端 Observer 安全回归（2026-10-11）

> 下文记录最初开发阶段的审计基线。后续已获授权配置 ECS、HTTPS、独立凭据及 Collector 用户服务；部署时补充的安全验收见 [实际部署状态](deployment_status.md)。独立 Collector 可显式选择本机8765只读广播（无应用消息发送），这不是修改生产8765或访问旧8888；ROS2仍保留自身状态。

范围：独立 `/home/orangepi/code/robot-cloud` 源码、隔离本地服务、真实只读ROS节点及部署模板。本轮没有阿里云SSH或域名/证书权限，因此不是公网渗透测试或生产上线安全认证。

## 机器人控制边界与证据

| 检查项 | 结果 | 实际证据 |
| --- | --- | --- |
| RK3588_SERIAL_OWNER | NO | 实机新Collector自身文件描述符只有 `/dev/null` 与FastDDS共享内存，无STM32/USB串口；AST无serial/Serial访问 |
| COLLECTOR_CONTROL_PUBLISHER | NO | 实机自身发布者仅ROS内置 `/parameter_events`；controlPublishers=[]；AST无create_publisher/ActionClient/send_goal |
| CLOUD_ROS2_CONTROL | NO | 云端源码无rclpy/serial/control依赖，Docker仅COPY cloud/shared，无ROS安装 |
| STM32_CONTROL_MODIFIED | NO | 本轮修改均位于新项目，没有编辑STM32或upper_computer控制代码 |
| JETSON_HOST_NODE_RESTARTED | NO | 未连接Jetson、未发送重启/停止/控制命令；没有生产生命周期调用 |
| PRODUCTION_8765_MODIFIED | NO | 无端口配置/原脚本改动，无新服务绑定8765；Collector不使用本地HTTP回退 |
| EXISTING_8888_OVERWRITTEN | NO | 原Observer源码/web/scripts/launch共51文件开发前后SHA256完全相同，0新增/0修改 |

上述NO指本轮工程与操作范围，不声称现有host_node自身没有串口或控制能力。审计期间原8765曾由无监听转为host_node监听，随后PID改变；本轮没有启动/停止它，不把外部运行状态变化归为本工程行为。原8888仍无监听，本轮按要求没有启动或替换它。
未创建生产 `.env/.env.collector`，未初始化/提交/推送Git，未安装或启用systemd模板，未修改全局DDS/网络/代理，未改变Docker权限或daemon。
自建19088/19089测试服务和浏览器只使用临时目录/测试凭据，测试结束仅关闭自己的PID，不按端口杀未知进程。

## 身份、传输与访问控制

- 设备Bearer绑定robot_id/source_id/role；仅一个primary。相机source只能授权媒体key，每key一个上传者。设备凭据不能查看网页，浏览器会话不能上传。
- 访问者必须登录；scrypt密码hash与设备token、会话secret分离。Cookie为HttpOnly/SameSite=Strict，生产Secure；最多256活跃会话，服务重启失效，退出会撤销。
- 登录/退出严格同源；WS鉴权+Origin检查，不接受token查询参数，客户端发送文本/二进制即关闭，不提供控制RPC。每页面单WS，最多4标签/会话、全局64连接。
- 生产HTTPS/WSS；后端只相信显式可信反代CIDR的X-Forwarded-Proto/X-Forwarded-For。测试HTTP必须显式开关且loopback，Collector TLS验证开启，拒绝重定向，不泄露Bearer。
- Nginx HTTP初始只健康/ACME入口；HTTPS模板配置TLS1.2/1.3、HSTS、WS升级、登录与上传限流、1MiB请求上限。后端也限制上传/登录和并发scrypt（2），限流桶最多4096。
- 同源CSP、nosniff、Referrer-Policy及无缓存数据响应；前端无设备token/机器人内网URL，无第三方AI自动上传。

## 数据真实性、顺序与资源上限

协议检查版本、严格身份/字段/Topic/kind、时间戳、UUID/sequence、状态、深度和集合上限。拒绝JSON重复key及乱序/重复/退休session回放；持久化检查点防后端重启后回放。未知采样时间为null，封装/首次机器人接收/云接收时间分开。
真实0保留，null保留，NaN/Inf归null；COMMAND不替代ACTUAL；缺失STM32 Page不推断填充。心跳不能刷新旧观测，旧采样不能以新序号冒充新鲜。
机器人离线或观测过期时旧值标STALE；图片过期204，不继续展示为实时。静态地图允许事件式长期有效，但来源离线/会话变化即STALE。
上传队列容量随授权媒体数有界配置：primary 媒体数+2，cameras 媒体数+1；默认 primary 十一媒体为13项，字节上限仍为4MiB。仅保存每key最新数据并丢弃过期项；不无限缓存。最多11媒体最新帧，图片≤512000字节、点云≤4000点，默认1500点/1Hz；图片限帧和真实缩小编码。二维 LaserScan 单次输入最多100000条距离，仅输出有限有效的 XY 平面采样点。
SQLite仅最新观测及最多16source检查点，不保存视频/全量点云/无限历史；WAL每256page自动checkpoint、4MiB journal上限。Docker日志按10MiB×3轮转，限制CPU/内存/PID。

## 部署、凭据与回退

后端容器非root、只读根文件系统、drop ALL capabilities、no-new-privileges；数据库只有指定data卷。云端8888仅绑定127.0.0.1，公网只80/443（SSH由安全组限制），无DDS/8765/数据库/串口设备映射。
`.gitignore/.dockerignore`排除凭据、TLS私钥、数据库、虚拟环境与运行配置。`.runtime/nginx`与active-image-tag不会污染tracked源码；成功commit镜像用于更新与回滚，不清理未知数据/旧镜像。
数据库备份使用SQLite在线backup，私有备份文件600；未对真实生产库执行备份/恢复。systemd停止只作用于独立Collector，不绑定机器人控制生命周期。

## 仍需生产验收

Docker daemon权限不足，不能确认镜像构建、容器/资源约束或Nginx运行时；仅Compose真实解析、脚本语法、渲染及静态安全检查已通过。
尚需ECS上的TLS/证书、DNS/备案（按地区）、安全组实际暴露、跨网络鉴权与WSS、凭据轮换、真实数据断网恢复及部署回滚测试。
当前没有多用户账户管理/MFA、长期审计存档、WAF/流量攻击防护或地图位姿融合功能；不应把单查看账户称为完整企业权限系统。
任何新增控制接口、跨域访问、明文公网例外或生产节点变更都需要另行授权与安全设计。
