# 多机器人接入与代码迁移

适用：同一平台管理 2～10 台车。观测页面按车查看实时状态、画面、测试和故障记录；数仓按车筛选数据集、版本、文件和搜索。账号、项目权限、任务审计、存储容量仍属于平台管理，不是每车独立的一套平台。

## 身份和目录

| 配置 | 用途 | 新车要求 |
| --- | --- | --- |
| `ROBOT_ID` / profile.observerId | 观测端身份，例 `car-02` | 唯一；生成工具限 32 字符 |
| `ARCHIVE_ROBOT_ID` / profile.warehouseId | 数仓 Robot UUID | 每车独立，先注册到正确项目 |
| `SOURCE_ID` | 同一车内的工控机/Jetson 数据源 | 每车恰好一个 primary；附加来源 media_keys 不重叠 |
| `DEVICE_TOKEN` | 采集端向云端上报 | 独立随机生成，不复制旧车 token |
| `ROS_DOMAIN_ID` | 本车 ROS 通信域 | 同一车所有 ROS 进程一致；同网段不同车使用不同域 |
| `VIDEO_CLOUD_SOCKET_DIR` | 云端视频隧道 | `/var/lib/robot-video-tunnel/preview/robots/car-02` |
| `robot.json` | 两套 ID、ROS 域、本机 machine-id 绑定 | 到新工控机后重新绑定；不复制旧 machine-id |

现有 01 号车保留 `rk3588-robot` / `365c77c6-0029-4406-a504-739014d0a672`，保留旧视频与数仓路径。新增车使用独立目录，不能沿用现车的状态文件、SSH 私钥、相机标定文件或待上传队列。

## 接入步骤

1. 在数仓已有的有效项目内注册新车。由平台管理员调用 `POST /api/warehouse/robots`（公网为 `/dbprog/api/warehouse/robots`），请求 `{ "id": "新生成的UUID", "projectId": "目标项目UUID", "name": "02号车", "externalId": "car-02" }`。`externalId` 与新车 observerId 一致。重复的同一绑定允许重试，冲突绑定拒绝。账号仍按原有租户/项目权限授权；为新车创建专用、仅目标项目上传的采集账号。
2. 在新工控机用户下生成新配置，先准备好全新的 UUID：

   ```bash
   cd ~/.local/share/robot-cloud
   python3 deploy/scripts/fleet_config.py generate --observer-id car-02 --warehouse-id 新车UUID --name 02号车 --domain 2 --jetson-host car-02-jetson.local --output ~/.config/robot-cloud/enrollment-car-02
   ```

   输出目录含凭据，权限为 700/600，命令不打印凭据。`--jetson-host` 必填，填写新车 Jetson 的独立主机名或 IPv4，先确认它确实解析/连接到新车，不能继续使用旧车的 Jetson 地址或通用重名主机名。生成工具无法代替现场核验各车的实际主机。`archive-upload.json` 的账号密码、Jetson/USB/网络配置需要现场补齐。不要用生成文件整体覆盖现车配置。
3. 从模板安装新车自己的 `collector.env`、`archive.env`、`archive-upload.json`、`robot-runtime.env`、`warehouse-cloud.json` 到 `~/.config/robot-cloud/`，文件权限均为 600。必须在身份绑定和首次启动前安装 `warehouse-cloud.json`：它让一键启动使用统一云数仓 API 隧道，云端断线时也不会启动旧本地数仓；缺少该标记会选择旧的本地数仓模式。填写 `archive.env` 的 `ARCHIVE_SOURCE_USER`、`ARCHIVE_SOURCE_KEY`、`ARCHIVE_SOURCE_KNOWN_HOSTS`，后两项故意留空，防止自动沿用旧车默认 SSH 文件。填写 `video-tunnel.env.template` 中本车的 Jetson 用户、私钥路径、已核验的 known_hosts、独立云端 SSH 账号，保存为 `video-tunnel.env`。两个源端 SSH 配置都应连接刚核验的新车 Jetson；生成的主机密钥别名也使用新车主机名/IP，不沿用旧地址别名。不得关闭 SSH 主机密钥检查。生成的 `robot.json.template` 已包含 `jetsonHost`，身份绑定会核对 `VIDEO_SOURCE_HOST` 和 `ARCHIVE_SOURCE_HOST` 一致，同时核对独立 `cloudSocketDir`；旧车未设置此字段仍兼容。
4. 检查当前目录内的相机设备路径、USB 序列号、分辨率/帧率、相机顺序、topic、ROS 工作区、雷达消息类型、标定内参/外参。生成的工控机配置明确使用 `COLLECTOR_BERXEL_TRANSPORT=bridge`；归档仅订阅三路雷达与定位/TF，`ARCHIVE_TRAINING_ONLY=1` 保持成品视频与压缩训练数据策略，避免工控机重复录制相机原始图像、深度点云和诊断流。相机视频由本车 Jetson 源端录制后拉取；生成的工控机配置不能替代 Jetson 相机安装。Jetson 的本地归档配置须设置同一个新车数仓 UUID，并单独核对视频编码、10 FPS 采样、分段容量和原始缓存清理策略；工控机拒绝拉取其他 robotId 的片段。不要把旧车 archive 根目录或控制目录当作新车队列/开关。生成的工控机策略保留 128 MiB 相机临时分段上限、10 GiB 磁盘余量和 20 GiB 缓存上限，交付前按新车容量核对。
5. 本车采集、导航、建图、相机及相关遥测服务使用同一个 ROS 域。在新车的相应 user unit drop-in 中加入：

   ```ini
   [Service]
   EnvironmentFile=%h/.config/robot-cloud/robot-runtime.env
   ```

   同时核对启动算法/导航的终端环境，只有采集程序设置域不能隔离全部算法。此步骤在新车交付窗口配置；接入检查工具不会启动或重启设备。
6. 将填写完成的配置设为当前用户私有后，在新工控机绑定并检查：

   ```bash
   python3 deploy/scripts/robot_profile.py bind --template ~/.config/robot-cloud/enrollment-car-02/robot.json.template
   python3 deploy/scripts/robot_profile.py check
   ```

   bind 只创建身份文件，不覆盖已有身份，不启动设备，不清理缓存。克隆系统盘时必须先重新生成 Linux machine-id，再使用新车配置绑定，不能复用旧车 machine-id。
7. 仅将新车 `cloud-entry.json` 通过安全方式交给云端管理员。在云端部署相同版本的工具后执行 `fleet_config.py cloud --env-file /opt/robot-cloud/.env --entry 新车cloud-entry.json` 校验；通过后加 `--apply` 原子写入并保留私有备份。工具不会轮换现车 token，也不会重启服务。再按正常云端发布流程使配置生效。
8. 为新车创建独立、受限的云端 SSH 账号/密钥授权，仅允许该车目录内的反向 socket，禁止交互 shell、代理转发和越权目录；让 Observer 的只读挂载包含该目录。不要让各车共享能覆盖其他车 socket 的授权。当前四路 USB 转发的模板沿用，新车还可启动 `robot-cloud-native-source-tunnel@status` 与 `robot-cloud-native-cloud-tunnel@status`，提供自己的 `jetson.sock`（本地 loopback 18086 -> Jetson 8081），四路画面使用 usbFront/usbLeft/usbHub2/usbHub3。这些端口仅在本车 loopback，不新增公网端口。
9. 数据集入仓统一指向平台数仓。每车本地上传 API 隧道可使用相同的本地 loopback 端口，但连接与账号属于各自车。填写 `warehouse-upload-tunnel.env.template` 并安装为 `~/.config/robot-cloud/warehouse-upload-tunnel.env`，由 `robot-warehouse-cloud-api-tunnel.service` 读取；`WAREHOUSE_CLOUD_USER` 使用本车授权的上传隧道账号，仅允许连接云端 loopback 13000。新车默认关闭占用旧本地 3000 端口的兼容转发。`dbprog_tunnel.sh` 中独立目录的旧本地数仓代理仅供确实部署了本地数仓的车，不能把多台车的本地库当作一个共享库。公网数仓保持统一数据库、对象存储和权限体系。
10. 地库开关按车绑定控制目录：API 配置 `GARAGE_ARCHIVE_DEFAULT_ROBOT_ID` 指向现车，新增车使用 `GARAGE_ARCHIVE_ROBOTS_JSON={"新车UUID":"/garage-controls/car-02"}`。每个目录由对应车的控制/状态同步链路维护并挂载，不能共享目录。多车映射存在时，“全部机器人”视图不能操作默认车开关。当前代码提供隔离和拒绝未配置车辆的能力；第二台车的端到端同步/断网收敛须在接入时验证，不能只填写 JSON 就认为远程采集已连通。

## 上传与旧数据保护

- 桌面采集入口、上传入口、collector 和 recorder 都会核对已绑定的本车身份。身份不匹配时不启动采集/上传；停止和状态命令仍可用。
- `archive.json` 中 robotId 与上传配置不一致时，在登录/网络请求前拒绝上传，原视频、原 manifest 与队列保留。不要改写旧 manifest 去“适配”新车。
- 同日数据集的旧车原名保持；其他车遇到旧式同名日数据集会使用带完整 UUID 的独立名称。新生成配置默认使用 `地库_car-02_YYYY-MM-DD`，减少冲突并方便识别。
- 无 identity profile 的旧安装保留兼容；正式迁移应使用 profile 绑定，不能把“不报错”视作完成接入。已有缺少 robotId 的历史 manifest 仍按原兼容策略处理，迁移时不要复制旧队列。

## 上线核对

软件隔离测试覆盖双车相同传感器键、不同 token、独立实时/持久状态、测试删除、WebSocket、视频代理、数据集筛选、同日上传冲突以及旧车队列保护。运行构建、单元测试和 Prisma validate 不代表双车实测已完成。

第二台车接入后由现场人员启动设备，至少验证：

- 同时打开两台车页面/两个标签页，画面、地图、测试和故障不串车；切换离线车不显示旧车画面。
- 两台车同日各采集、结束、上传，按车筛选后可下载正确视频；旧车历史记录和视频链接仍可用。
- 算法原有雷达驱动先运行，采集只订阅现有数据，不重复抢占设备；导航、建图和手柄控制并行验收。
- 弱网/断网继续本地采集、联网重试上传，关闭采集后封片完成；验证各车的地库开关只影响本车。
- ROS 域、USB/网口/串口、CPU/IO/带宽配额、磁盘余量、时钟同步和 SSH 授权逐车检查。

相同软件协议和设备接口的新增车主要改配置，不需要复制并修改一套业务代码。更换相机 SDK、雷达消息、底盘协议或传感器布局时，需要对应驱动适配和重新标定；不保证任意硬件免改代码。
