# 公网观测页连接 DBProg

DBProg 运行在工控机，公网观测平台运行在 ECS。工控机 `8888` 的本地调试页与 `https://aystkit.cn` 是两个独立服务；修改本地调试页不会更新公网平台。

公网后端通过独立 SSH 连接访问现有 DBProg HTTP API 和对象存储，沿用专用受限账号 `robot-video-tunnel` 的密钥与已核对的主机密钥。新增连接不启动 DBProg，不管理相机、遥测、导航或其他视频隧道。PostgreSQL 和数据库密码不传给 ECS，不新增公网监听端口。

## 完整数仓界面

入口 `/dbprog/` 挂载 DBProg 原有的白色 React 应用，包含数据集与目录、文件、上传中心、回收站、版本与对比、AI 助手、系统状态，以及原账号有权访问的用户、存储和系统设置。侧栏下方“任务与审计”合并数据处理任务与审计日志，按原权限显示标签。首页和独立 API 状态页已移除；默认进入数据集，基础用户进入 AI 助手，旧地址分别跳转默认入口和系统状态。原 `BASIC`、只读和管理角色的访问限制继续由 DBProg 校验，不通过公共管理员账号代替用户登录。

白色应用以 `VITE_APP_BASE_PATH=/dbprog/`、`VITE_API_BASE_URL=/dbprog` 构建，静态产物安装到云工程 `cloud/frontend/warehouse/`，无需代理工控机的 5173 页面服务。浏览器路由、懒加载资源和原生跳转均保留此前缀；原数仓在根路径下的本地部署仍兼容。

`/dbprog/api/` 原样转发已有 REST 接口的方法、CSRF、二进制分片及 AI 流式响应。数仓接口专用上传上限为 16 MiB，允许原 8 MiB 分片，其他观测接口保持原 1 MiB 上限。上传校验使用 WebAssembly，浏览器策略仅在数仓路径允许 WASM 编译。文件签名地址改为 `/dbprog/objects/` 同源预览和下载，不把内网 MinIO 地址传给浏览器；主动脚本类文件强制下载，防止上传内容在公网页面执行。

## 通道与权限

| ECS 宿主机 Unix socket | 工控机固定目标 | 后端容器路径 |
| --- | --- | --- |
| `/var/lib/robot-video-tunnel/preview/dbprog-api.sock` | `127.0.0.1:3000` | `/run/native-preview/dbprog-api.sock` |
| `/var/lib/robot-video-tunnel/preview/dbprog-objects.sock` | `127.0.0.1:9000` | `/run/native-preview/dbprog-objects.sock` |

两个 socket 使用一条独立 SSH 连接；现有 `.runtime/native-video` 目录只读挂载已让后端可访问该目录。后端通过 `aiohttp.UnixConnector` 访问固定上游，不需要把 ECS 的 `127.0.0.1` 指向工控机，也不需要开放 API、MinIO 或 PostgreSQL 的公网端口。

专用账号现有 SSH 配置允许远端 Unix socket 转发，拒绝交互命令和普通 TCP 转发。宿主机目录由专用账号拥有、组为容器 GID 10001，模式 `2710`；socket 受目录访问权限保护。新增文件名不覆盖现有视频 socket，不改 SSH 配置或密钥权限。

云端已有的观测账号仍需通过平台鉴权。DBProg 登录与数据权限由 DBProg 校验；本机 `:5173` 的登录 Cookie 不会自动跨域传到公网。文件下载须由后端通过对象 socket 读取签名 URL，再返回给当前已授权用户；不能把工控机局域网的 MinIO 签名地址直接作为公网下载地址。对象请求保留签名 URL 的 Host、路径及查询参数，不转发平台 Cookie，不跟随重定向。

## 安装独立隧道

先完成并验证公网后端与页面版本，再由操作者安装和启动此独立 unit：

```bash
cd ~/.local/share/robot-cloud
install -m 644 deploy/systemd/robot-cloud-dbprog-tunnel.user.service \
  ~/.config/systemd/user/robot-cloud-dbprog-tunnel.service
systemctl --user daemon-reload
systemctl --user start robot-cloud-dbprog-tunnel.service
systemctl --user status robot-cloud-dbprog-tunnel.service
```

unit 读取现有 `~/.config/robot-cloud/video-tunnel.env`，只使用 `VIDEO_CLOUD_KEY`、`VIDEO_CLOUD_KNOWN_HOSTS` 及可选的 `VIDEO_CLOUD_BIND_INTERFACE`。不复制私钥、不输出环境变量。若现有视频隧道通过专用 drop-in 绑定无线接口避开代理路由，应给新 unit 单独配置同一 `VIDEO_CLOUD_BIND_INTERFACE`；其他 unit 的 drop-in 不会自动继承。接口名以当前实际网络为准。

2026-10-27 用户已明确授权现有数仓隧道开机启动，robot-cloud-dbprog-tunnel.service 现已 enabled；上面的 start 命令也可用于手动恢复。数仓数据库、对象存储及现有后端容器已设为 unless-stopped，Docker 随主机启动。隧道 socket 绑定成功仍需配合上游健康检查；无需重启视频、Collector 或机器人服务。

## 验证与回退

先在工控机确认 `http://127.0.0.1:3000/health` 和 `http://127.0.0.1:9000/minio/health/live` 返回正常，再从 ECS 后端容器内以 UID 10001 检查两个 socket 可用。API 健康可以使用不带 Cookie 的 `/health`；对象健康使用 `/minio/health/live`。这些只读检查不登录账号、不修改数仓数据。

公网验收应分别确认：未登录平台无法读取 DBProg 接口；已批准用户可见数仓入口；DBProg 原账号可登录并读取其权限内数据；文件通过同源下载成功；退出后原会话失效。断开隧道或停止 DBProg 时，数仓显示连接不可用，其他观测功能继续工作。文档和模板存在不表示部署或这些检查已经完成。

回退隧道只停止精确 unit `robot-cloud-dbprog-tunnel.service`，保留原有四路视频、Collector、数据库及密钥。页面和后端按本次部署记录的上一镜像回退，不覆盖 `.env`、平台账号数据库、证书或数仓数据。

## 长期部署连接

2026-10-28 用户明确要求并授权保留专用 ECS 部署密钥。工控机 SSH 别名为 `robot-cloud-deploy`，固定连接 `root@8.140.19.17` 并校验已有主机指纹；私钥位于 `~/.ssh/robot_cloud_ecs_deploy_ed25519`，权限为 0600。服务端使用 `restrict` 禁止转发、代理、X11 和 PTY，保留发布所需的 root 命令执行能力。本次临时密钥已撤销，长期密钥应按用户要求保留，后续发布无需重复索取临时公钥授权。

只读连接验证：`ssh -T robot-cloud-deploy 'hostname'`。发布前仍须完成测试、核对生产基线并准备回滚；不得把私钥打包进镜像或提交仓库。部署连接验证记录在 `~/.local/state/robot-cloud/system-audit-20261028/permanent-deploy-completion.json`。
