# 阿里云 ECS 首次部署

本手册操作由服务器拥有者在ECS执行。当前目标为 Ubuntu ECS `8.140.19.17`，域名 `aystkit.cn`；具体上线结果见 [部署状态](deployment_status.md)。
目标Ubuntu 22.04/24.04，先执行 `hostname; whoami; uname -m; cat /etc/os-release` 确认真实主机；不要把机器人终端当ECS。
Docker镜像基于Python3.12，支持amd64/arm64基础镜像；当前机器人为arm64，不能把其ROS install/build复制给ECS。

## 准备

1. ECS分配公网IP；域名A记录指向真实IP。全程以自己的值替换示例，占位 `observer.example.invalid` 不能用于上线。
2. 按 [Docker Ubuntu安装文档](https://docs.docker.com/engine/install/ubuntu/) 安装Engine与Compose插件。在ECS运行 `docker version` 和 `docker compose version`；不要在机器人修改Docker权限或重启daemon。
3. 安全组允许公网80/443；SSH22仅允许自己的来源IP。8888/8765/DDS UDP/数据库不向公网开放。
4. 如果ECS在中国内地，域名提供网站服务需核对并完成ICP备案；阿里云说明见 [ICP备案要求](https://www.alibabacloud.com/help/en/icp-filing/basic-icp-service/product-overview/what-is-an-icp-filing)。实际区域和备案状态由用户确认。
5. Git克隆独立私有项目，例如 `/opt/robot-cloud`；切勿在已有未知应用目录直接覆盖。

```bash
git clone <PRIVATE_REPOSITORY_URL> /opt/robot-cloud
cd /opt/robot-cloud
cp .env.example .env
chmod 600 .env
python3 deploy/scripts/hash_password.py
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

交互生成的密码hash写入VIEWER_PASSWORD_HASH，两个随机值分别作设备token和SESSION_SECRET；不用同一个值。
hash含 `$`，在 `.env` 中用单引号包住，防止Compose变量插值改变内容（[Compose环境变量文档](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)）。
编辑 `.env` 真实robot/source身份、VIEWER_USERNAME、PUBLIC_ORIGIN=https://自己的域名、DEVICE_CREDENTIALS_JSON。
设备凭据数组必须与机器人端ROBOT_ID/SOURCE_ID/token一致。不给机器人浏览器密码或session secret。
`CLOUD_SUBNET` 默认172.30.88.0/24，若ECS已有同网段Docker网络，选一个不冲突的私网子网，并同步修改TRUSTED_PROXY_CIDRS；后端只采信此可信Nginx来源的HTTPS头。

```bash
sudo install -d -m 0750 -o 10001 -g 10001 data
mkdir -p deploy/tls deploy/acme
bash deploy/scripts/start_cloud.sh
curl -fsS http://127.0.0.1:8888/readyz
curl -fsS http://<ECS_PUBLIC_IP>/
```

初始HTTP配置只展示云服务入口/健康，无机器人数据或凭据登录。完整Observer登录在HTTPS配置完成后开放。
首次脚本以当前Git commit作为镜像tag，成功后保存到Git忽略的 `.runtime/active-image-tag`。HTTP配置生成到 `.runtime/nginx/observer.conf`，不会改动tracked模板。后续Compose命令统一用 `bash deploy/scripts/cloud.sh ...`，避免新终端误用local镜像。
上线前先在开发机/CI测试该commit；日常升级使用已测试的固定commit hash，而不是尚未测试或可能移动的分支头。
当前Compose发布的后端8888只绑定宿主127.0.0.1，Nginx对外80/443；后端内部无ROS/控制端口。
不要在public HTTP上调用登录/上传；本地loopback测试开关不能用于生产。

### 官方镜像来源备用配置

若 ECS 无法连接 Docker Hub，可在 `.env` 中设置 `PYTHON_BASE_IMAGE` 和 `NGINX_IMAGE`，使用 [Docker 官方在 Amazon ECR Public 发布的镜像](https://gallery.ecr.aws/docker/)，不要配置未知第三方镜像站。实际部署固定已拉取镜像的 `sha256` 摘要，以便复现；修改部署配置不会修改机器人网络或 Docker 配置。

```dotenv
PYTHON_BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm
NGINX_IMAGE=public.ecr.aws/docker/library/nginx:1.28-alpine
```

## 证书与HTTPS

已有证书时，将完整链与私钥放入 `deploy/tls/fullchain.pem` / `deploy/tls/privkey.pem`，私钥由ECS操作者限制权限；不提交Git。
也可以在ECS安装官方Certbot，通过webroot `/opt/robot-cloud/deploy/acme` 的HTTP-01挑战取得证书；80与DNS必须正确。

```bash
# 在ECS执行；域名、邮箱由自己提供。
sudo certbot certonly --webroot -w /opt/robot-cloud/deploy/acme \
  -d <YOUR_DOMAIN> --email <YOUR_EMAIL> --agree-tos
sudo cp /etc/letsencrypt/live/<YOUR_DOMAIN>/fullchain.pem deploy/tls/fullchain.pem
sudo cp /etc/letsencrypt/live/<YOUR_DOMAIN>/privkey.pem deploy/tls/privkey.pem
sudo chmod 600 deploy/tls/privkey.pem
python3 deploy/scripts/render_nginx.py <YOUR_DOMAIN>
bash deploy/scripts/cloud.sh exec -T nginx nginx -t
bash deploy/scripts/cloud.sh exec -T nginx nginx -s reload
curl -fsS https://<YOUR_DOMAIN>/healthz
```

Certbot证书续期后需要重新同步证书并 `nginx -t` /reload。由运维配置续期hook，不在机器人安装。
HTTPS配置使用TLS1.2/1.3，WS显式Upgrade/Connection头和读超时（[Nginx WS说明](https://nginx.org/en/docs/http/websocket.html)）。上传1MiB限制与限流，登录限流，80跳转HTTPS。
第一次浏览器访问 `https://<YOUR_DOMAIN>` 会要求登录；没有机器人时依然可看页面并显示等待数据。

## 运维与验收

### 四路原生 H.264 视频接入

**使用权交接**：RK3588 `deploy/scripts/switch_camera_owner.sh` 的显式 `web/peer` 操作按用户授权管理固定的相机预览服务；网站自身没有任何采集启动／停止接口。网站及独立只读状态网关先恢复，不依赖相机交接成功。`peer` 不关闭整站或状态转发，标定／其他采集需由操作者启动，未知进程不会被强制终止。安装与使用见项目README。

网站自动恢复使用独立受限密钥：将 `deploy/scripts/web_receiver_gate.sh` 安装为ECS root拥有、0555的 `/usr/local/libexec/robot-web-receiver`，公钥项必须为 `command="/usr/local/libexec/robot-web-receiver",restrict <专用公钥>`。已有管理员公钥不能覆盖。入口仅接受固定 `receiver-status/receiver-start`，拒绝附加参数与shell命令；启动固定 backend/nginx、使用已成功部署的镜像、不build、不pull，不改变凭据或数据库。RK3588私钥保存在0700状态目录、0600文件中，不提交Git。此权限仅在用户明确批准后安装，安装不自动恢复网站。

只读接入已运行的原生预览，不嵌入原网页、不复制它的界面代码；仅允许 `/status` 与固定四路 `/camera/camera1..4/video.mp4`。原生预览由来源拥有者独立启动／停止，网页及其隧道不管理相机采集，不在云端启动 YOLO、ROS、底盘或导航。当前配置前后 2592×1944 / 25 FPS，左右 1920×1080 / 30 FPS，编码目标约 2 Mbps/路。采集、编码和浏览器实际呈现帧率分别验证，不能互相替代。

单人四路约 8 Mbps，需至少 10 Mbps 的 ECS 公网出口；两人同时四路建议 20 Mbps 或更高，并结合实际播放验证。3 Mbps 不足以同时保持四路原分辨率与码率，不能靠播放器缓冲解决持续带宽不足。

独立Jetson网关 `camera_status_gateway.py` 固定监听 `127.0.0.1:8081`，只接收 `/status` 和四路 `/camera/camera1..4/video.mp4` 的GET，固定代理本机8080；没有采集管理接口、不转发Cookie、不跟随重定向、不打开设备。`/status` 即使原预览停止也保留独立后台实际占用检查，返回白名单 `cameraUsage`，不暴露进程、账号或设备路径。相机占用判断和原帧新鲜度互相独立；检查年龄加上网络耗时，超过6秒不能继续声明占用。服务模板 `deploy/systemd/robot-cloud-camera-status-gateway.jetson.user.service` 安装为Jetson用户 `robot-cloud-camera-status-gateway.service`，需将网关与已有独立 `camera_owner.py` 放在同一私有目录；切换CLI会启动只读网关，普通网页不会启动相机。

RK3588 每路使用独立 SSH TCP 连接，把8081只读网关接到本机回环 18082–18085，再转到 ECS 专用 Unix socket。前视、后视、右视、左视分别保留既有 `usbFront/usbLeft/usbHub2/usbHub3` 窗口位置；此映射按本次原生状态确认，不按旧低清程序的方向标签猜测。模板为 `deploy/systemd/robot-cloud-native-*-tunnel@.user.service`，脚本中的主机地址、已核对的主机密钥文件与部署路径需随实际环境核对。模板不自动开机启用。

ECS 用专用 `robot-video-tunnel` 非登录账号及专用密钥，配置见 `deploy/ssh/native-video-tunnel.conf`；不要使用管理员的长期部署密钥作无人值守视频凭据。该账号禁止交互命令、代理与 X11，仅提供受控远端转发。OpenSSH 的 Unix 转发使用共享监听权限，因此 TCP 白名单限定为该非 root 账号不能绑定的回环端口 1，实际拒绝已验证；不能为此开放任意 TCP 或公网监听。公钥选项为 `restrict,port-forwarding,permitlisten="127.0.0.1:1"`。

socket 目录 `/var/lib/robot-video-tunnel/preview` 由专用账号拥有、组为容器 GID10001，模式 2710；受控 socket 0666，但目录禁止其他用户进入。`.runtime/native-video` 指向此目录，只读挂给后端 `/run/native-preview`。`jetson.sock` 为状态入口，各路为 `jetson-usbFront.sock` 等；生产不向公网发布这些 socket 或本机端口。ECS `.env` 可显式设置：

```dotenv
NATIVE_VIDEO_SOCKET=/run/native-preview/jetson.sock
NATIVE_VIDEO_MAX_PLAYING=4
NATIVE_VIDEO_SPLIT_SOCKETS=true
```

未配置 socket 时保持原 JPEG 兼容模式。已配置但视频断流时显示等待，不把旧 JPEG 冒充新的 H.264 视频。云端 `/native-camera/status` 与 `/native-camera/{key}/video.mp4` 沿用网页登录鉴权；不把浏览器 Cookie 或上传凭据转给机器人，注销即中断，最多每会话四路、全局十六路。Nginx 对这些路径关闭缓冲，CSP 仅增加同源和 blob 视频权限；无新增遥测 WebSocket。

播放器目标缓冲 1.2 秒，不是实测端到端延迟。窗口只显示真实解码画面、实际播放 FPS，不标来源；画面停顿、采集状态过期或无法核实会停止录制。录制保留原按钮与本地下载行为，解码停止不生成带新时间的假帧。原 JPEG 2 秒期限、Store、上传协议、数据库与登录凭据不变。

```bash
bash deploy/scripts/cloud.sh ps
bash deploy/scripts/cloud.sh logs --tail 100 backend nginx
sudo python3 deploy/scripts/backup.py data/latest.sqlite3 --output-dir backups
```

进程健康 `/healthz`、存储就绪 `/readyz`、经登录的 `/api/robots` 机器人lastSeen/真实数据年龄分别检查。
用另一网络手机/电脑验收登录、WS、媒体、机器人断网后的离线状态；未登录的snapshot/WS/媒体必须拒绝。
故障时保留backend/nginx日志与版本号，不贴设备token或密码hash；不要为云问题重启机器人host。
现有机器人开机ROS数据源由原启动流程管理；Collector自启动模板由用户另外安装，见robot_collector.md。
