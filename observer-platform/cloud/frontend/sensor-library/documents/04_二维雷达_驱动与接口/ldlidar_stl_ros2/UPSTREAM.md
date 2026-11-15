# 上游来源

- 仓库：<https://github.com/ldrobotSensorTeam/ldlidar_stl_ros2>
- 提交：`bf668a89baf722a787dadc442860dcbf33a82f5a`
- 获取日期：2026-09-21
- 许可证：MIT（见同目录 `LICENSE`）

本机未修改上游 C++ 驱动源码。本机适配内容：

- 增加 `stl19p.launch.py`，使用 STL-19P/D500 与 LD19 相同的串口协议配置；
- 使用稳定的 CP2102 `by-id` 设备路径并发布 `/scan`；
- 根据实测安装位置发布 `drive_base_link → stl19p_link` 固定 TF；
- 补充 launch 和 `tf2_ros` 运行依赖。
