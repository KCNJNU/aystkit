# 本地启动

Windows 配置根 `.env`，启动 Docker Desktop 后，双击根目录 **start-all.cmd**，访问 http://localhost:5173。双击 **stop-all.cmd** 停止，数据卷保留。

Linux 配置根 `.env` 并准备 Docker Engine、Compose 和 `curl`，运行 `bash scripts/start-linux.sh`；停止使用 `bash scripts/stop-linux.sh`。已有数据迁移后加 `--skip-seed`，仅在已导入或构建当前镜像时加 `--no-build`。远端访问 `http://<服务器IP>:5173`。

局域网访问在服务器以管理员身份运行 **enable-lan.cmd**，其他设备打开输出的局域网地址。

已有基础设施时，`npm run dev` 只启动本机 NestJS、Worker 和 Web。完整步骤、参数与排错见 [DEPLOYMENT](../DEPLOYMENT.md)。
