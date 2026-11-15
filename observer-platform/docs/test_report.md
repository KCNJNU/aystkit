# 本轮测试报告（2026-10-11）

> 下文是最初开发阶段的隔离测试基线，保留原始结果。后续已获用户授权并实际完成 ECS/HTTPS 部署；最新测试数量、线上验收和待接上游情况见 [实际部署状态](deployment_status.md)，不再以本报告末尾的历史“尚未部署”描述为当前状态。

2026-10-12 二维扫描接入更新：完整自动化 `179 passed, 11 subtests passed in 11.40s`；四种分辨率×七页、十一媒体模块、两新扫描独立录制及0/null/错误隔离通过。证据 `tests/artifacts/new-device-scans-20261012/`。正式公网HTTPS/WSS与鉴权验证PASS；实机两路扫描发布者/消息均0，画面仍等待，隔离样本不作为实机验收。仅升级独立云端与Collector，未启动设备驱动或控制。

主机：orangepi@rk3588，aarch64，Ubuntu22.04/ROS2 Humble/Python3.10。
范围：新工程独立代码、测试数据库/回环端口、真实只读ROS节点。测试fixture明确标记，只在测试进程/浏览器标签中出现，不上传生产、不发布ROS、不制造设备数据。

## 1. 自动化回归：通过

实际命令：

```bash
cd /home/orangepi/code/robot-cloud
rtk env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
rtk env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m compileall -q cloud collector shared tests deploy/scripts
```

最终结果：`67 passed, 9 subtests passed in 10.39s`；compileall退出0。
插件禁用仅对测试命令生效，避免机器人系统旧ROS pytest插件与项目新版pytest冲突。

| 范围 | 覆盖 |
| --- | --- |
| 协议/后端26项及9子测试 | 真实0/null/非有限值、JSON/深度/大小/状态/时间戳、重复/乱序/新旧session、SQLite重启检查点、角色/robot/source/media key隔离、生产Secure cookie/明文拒绝、Origin/WS过期/多标签、登录限流、JPEG/PNG结构与PNG有界解压 |
| Collector30项 | ROS纯解析、独立Page/COMMAND-ACTUAL、有界最新队列、旧数据丢弃、配置/TLS/只读AST、JPEG缩小/坏帧ERROR、真实XYZ筛选、地图origin/hash/重连补传 |
| Collector真实HTTP测试 | 503首次序号不丢、ack丢失409协商、后端停止/重启新session恢复、拒绝redirect、401/403暂停、429 Retry-After |
| Frontend4项 | 7页资产、同源/无设备凭据/单WS、JS语法、0/null/NaN/Inf/历史语义 |
| 跨模块集成1项 | 实际Uploader线程→实际HTTP后端→登录→真实WS广播→断网STALE→云健康仍可用；仅测试fixture |
| 部署安全6项 | 云/采集AST无串口控制、Compose CLI解析实际配置与端口、Nginx/systemd边界、HTTP→HTTPS实际临时目录渲染与输入拒绝、全部Bash语法/成功tag写入顺序、SQLite在线备份权限600/源库保留 |

Compose检查无需Docker daemon，只对实际YAML使用临时占位env并解析JSON；没有创建生产.env或启动容器。开发机未安装Compose/Node时对应测试会明确skip，本轮RK环境两者具备，未skip。

## 2. 实际浏览器：通过，证据复存

Chromium110，100% zoom，独立19088后端与19089 CDP，专用临时profile。
已实际完成1920×1080、2560×1440、3440×1440及390×844手机模拟，每尺寸7页，共28页面检查，另有手机登录截图。
检查无横向溢出，页面标题≥24px，切页与精简/详细仍同一WS且历史不清；真实0和缺失—；断网/STALE/恢复；9模块无真实画面时等待并禁止录制。
原生MediaRecorder实际开始/停止/生成下载文件、重新录制撤销旧Blob、画面过期自动停止保留下载均通过。退出登录后取消离页以保存视频时也停止媒体轮询/WS重连。
结果 `consoleErrors=0`、`httpErrors=[]`、`singleWebSocket=true`、`historyRetained=true`。
原临时报告目录后来消失；已重新执行全部浏览器检查并将最终证据保存在 `tests/artifacts/browser/`（Git忽略，不放生产）：results.json、console-results.json、29截图及downloads目录中的实际测试录制webm。截图上的ISOLATED_BROWSER_FIXTURE不是实机数据。

重复验证的命令（分别在专用测试终端运行；不要选生产端口/浏览器profile）：

```bash
ROBOT_CLOUD_BROWSER_TEST=YES .venv/bin/python -m tests.serve_browser
CLOUD_TEST_PROFILE=$(mktemp -d)
chromium-browser --headless --disable-gpu --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=19089 --user-data-dir="$CLOUD_TEST_PROFILE" about:blank
CLOUD_TEST_USERNAME=testviewer CLOUD_TEST_PASSWORD=isolated-browser-test-only \
  .venv/bin/python -m tests.browser_cloud tests/artifacts/browser
```

若系统环境不支持Chromium sandbox，只有隔离测试浏览器可使用--no-sandbox，不用真实账号、不浏览外网；不是生产浏览器配置建议。测试结束只停止自己启动的服务/浏览器PID，保留报告/截图，不杀未知进程。

## 3. 实机只读ROS：已执行，上游待验证

实际命令（未修改DDS配置）：

```bash
rtk bash --noprofile --norc -c 'source /opt/ros/humble/setup.bash; source /home/orangepi/code/upper_computer/install/setup.bash; exec /home/orangepi/code/robot-cloud/.venv/bin/python -m collector.main --audit-seconds 10'
rtk bash --noprofile --norc -c 'source /opt/ros/humble/setup.bash; source /home/orangepi/code/upper_computer/install/setup.bash; exec /home/orangepi/code/robot-cloud/.venv/bin/python -m tests.audit_robot'
```

重复10秒采集结果：27个已配置Topic全部publisherCount=0、messageCount=0、status=WAITING_UPSTREAM。
包括 `/chassis_host/telemetry`、Livox/FAST-LIO、地图/代价地图/路径/定位、原图/压缩相机、Berxel深度点云、蓝海雷达及视觉/对接观测。接口在源码存在不等于当前ROS图可见。
真实本机SystemCollector返回AVAILABLE；不存在上游设备数据时保持null/等待。
运行时安全证据：ownPublishers只有ROS内部 `/parameter_events`；controlPublishers=[]；serialDeviceFds=[]；自有设备FD只有 `/dev/null`、FastDDS共享内存；cloudUploadStarted=false。

后续看到原host_node只在127.0.0.1:8765监听，读取到它为Domain0/FastDDS/ROS_LOCALHOST_ONLY=0。此事实不证明它发布了遥测。无真实消息时不能宣称STM32→云真实值一致、地图/图像实机带宽或QoS匹配验证通过；本轮没有为取得数据重启host或改变全局发现设置。

## 4. 安全回归与原工程

原Observer51文件SHA256基线回归：0修改、0新增。未改变原生产代码、串口协议、8765/8888启动脚本或监听配置。
开发前8765/8888均无监听；开发期间8765出现既有host_node且PID改变，8888仍无监听。本轮没有启动/停止该host，也没有为云迁移启动旧页面，不能声称原服务始终在运行。
未生成真实生产env、未启用systemd、未Git init/commit/push、未连接ECS。安全细节见security_audit.md。

## 5. 未通过/未执行项目，原因明确

| 项目 | 分类 | 原因/后续动作 |
| --- | --- | --- |
| STM32/相机/地图/点云真实端到端 | WAITING_UPSTREAM | 当前审计无实际ROS发布/消息；由操作者确认原数据源与DDS可见后复测，不注入生产fixture |
| Docker镜像构建/容器/Nginx运行 | 尚未运行验证 | Docker daemon permission denied，sudo需密码；未改权限或系统软件，交ECS/开发机实际build及nginx -t |
| ECS首次部署及自动恢复 | 等待阿里云SSH配置 | 尚无真实公网IP/SSH用户/认证；本轮禁止自行连接生产 |
| 域名HTTPS有效证书/备案 | 等待域名/证书 | 示例域名.invalid不能上线；用户配置DNS/证书及地区要求 |
| 公网IP/域名/异网电脑手机/WSS | 等待公网实测 | 需ECS部署和HTTPS完成；本地通过不能替代公网验收 |
| 地图定位/路径叠加 | NOT_IMPLEMENTED | 原UI没有该能力，仅保留真实位姿/路径/frame摘要，不猜TF融合 |
| 长期历史/服务器视频归档 | NOT_IMPLEMENTED | 当前仅SQLite最新状态、浏览器图表历史与本地下载，不是存档平台 |

本轮交付的是独立云迁移源码、局部真实验证及可执行部署手册，不是已上线公网网站。请按acceptance.md完成余下层级。
