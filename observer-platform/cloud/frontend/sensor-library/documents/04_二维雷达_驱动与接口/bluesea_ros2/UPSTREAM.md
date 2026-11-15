# 上游来源

- 仓库：<https://github.com/BlueSeaLidar/bluesea-ros2>
- 提交：`206ade9dbd74f83be207952383eecd5ad5037fe4`
- 提交日期：2025-06-24
- 获取日期：2026-09-20

上游仓库未提供独立 `LICENSE` 文件，两个 `package.xml` 的许可证字段均为
`TODO`。本目录因此标记为 `LicenseRef-Upstream-Undeclared`；这只是如实记录，
不代表为上游代码重新授权。

本机适配：

- 将通用包名 `base` 改为 `bluesea_interfaces`，避免与其他 ROS 包冲突。
- 补齐 `visualization_msgs`、`std_srvs`、launch 运行依赖。
- 发布队列由深度 0 改为可靠、易诊断的深度 10。
- 增加 LDS-50C-E-7 专用参数和 Python launch。
- 默认仅发布 `/scan`，不连接反光贴定位或底盘控制。
