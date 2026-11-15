# 2D 雷达驱动

本目录保存 x86 主机使用的独立 2D 雷达驱动。当前包含蓝海光电
LDS-50C-E-7 UDP 雷达和 LDROBOT STL-19P USB 雷达；两者均只发布 ROS 2
`sensor_msgs/msg/LaserScan`，不启动反光贴定位、PID 或底盘控制。

## 当前接口

- x86 主机：`192.168.158.15/24`
- 雷达：`192.168.158.98/24`
- 雷达命令端口：UDP `6543`
- 本机监听端口：UDP `6668`
- 输出话题：`/scan`
- 坐标系：`lds_50c_link`

运行入口位于工作区根目录：

```bash
./run_bluesea_lidar.sh check
./run_bluesea_lidar.sh
```

驱动尚未与 `src/docking/reflector/` 建立话题重映射；`/scan` 不会自动送入
`/picoScan_process`。

## 实机验证

2026-09-20 在旧平台与 LDS-50C-E-7 实机验证：UDP 端口 `6668` 正常接收，
`/scan` 发布频率约 `14.95 Hz`，`frame_id` 为 `lds_50c_link`，`ranges` 与
`intensities` 均包含有效数据。启动时出现一次
`subpacket merge timestamp error`，随后驱动进入 `lidar start work` 并持续稳定发布，
不影响本次验证结果。

上游来源和本机改动见 [`bluesea_ros2/UPSTREAM.md`](bluesea_ros2/UPSTREAM.md)。

## LDROBOT STL-19P

- 接口：USB CP2102，`230400` 波特率
- 稳定串口：`/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_LD0001-if00-port0`
- 输出话题：`/scan`
- 坐标系：`stl19p_link`
- 协议配置：`LDLiDAR_LD19`（STL-19P/D500 使用 LD19 数据协议）
- 固定 TF：`drive_base_link → stl19p_link`，平移 `(-0.26, 0, 0.54) m`
- 安装姿态：整机上下翻转，并在原坐标基础上依次做左右、前后翻转；两次
  翻转合成为绕 Z 轴旋转 `π`，最终 RPY 为 `(π, 0, π/2)`，零度射线指向车辆左侧

运行入口位于工作区根目录：

```bash
./run_ldrobot_stl19p.sh check
MID360_NAV2_DDS_SERVER=off ./run_ldrobot_stl19p.sh
```

TF 平移由相机光心在 `drive_base_link` 中的位置 `(-0.25, 0, 0.91) m` 推得：
雷达在光心下方 `0.37 m`、车辆后方 `0.01 m`，所以雷达原点为
`(-0.26, 0, 0.54) m`。安装姿态通过完整刚体 TF 处理，不修改原始量测；如需
修正，可通过 `LDROBOT_STL19P_ROLL/PITCH/YAW` 覆盖姿态。

STL-19P 和蓝海驱动都默认发布 `/scan`，不得同时启动。

上游来源见 [`ldlidar_stl_ros2/UPSTREAM.md`](ldlidar_stl_ros2/UPSTREAM.md)。
