# Berxel iHawk P100R 接入说明

## 软件来源

- 厂商 SDK：V2.0.161
- 官方下载：<https://supporttof.yunshis.com/uploadfile/ueditor/file/202503/V2.0.161.zip>
- 完整 ZIP SHA-256：`c66e3b0d2376e54c9967bfaff314f3221c1e27ddf66f2e8be39f0333cddc650e`
- ARM64 子包：`Linux/arm64/linaro_7.5.0/BerxelSDK-Linux-2.0.161_arm64_7.5.0.tar.gz`
- ARM64 子包 SHA-256：`ef1a92143a63de69fa4099b2630c86be9ffc9859e3c17a1c2ef5e334f4d8c827`
- Jetson 安装位置：`/opt/berxel`
- P100R 实测 USB ID：`0603:0009`

仓库中的 `berxel_camera_ros2` 来自上述厂商 ARM64 SDK。厂商 `package.xml` 声明 Apache License 2.0，但发布包未附独立 LICENSE 文件；不要脱离厂商 SDK 单独再分发。

## ROS 2 接口

| 话题 | 类型 | 说明 |
|---|---|---|
| `/berxel/color/image_raw` | `sensor_msgs/msg/Image` | RGB 图像，`rgb8` |
| `/berxel/color/camera_info` | `sensor_msgs/msg/CameraInfo` | 彩色相机内参 |
| `/berxel/depth/image_raw` | `sensor_msgs/msg/Image` | 毫米深度，`mono16` |
| `/berxel/depth/camera_info` | `sensor_msgs/msg/CameraInfo` | 深度相机内参 |
| `/berxel/points` | `sensor_msgs/msg/PointCloud2` | 有序 XYZ 点云，单位米 |

相机驱动仍发布以 `berxel_link` 为根的内部 TF 树。接入 local costmap 时由 `mid360_nav2` 发布 `livox_frame → berxel_link`（数值见 `src/mid360_nav2/config/lidar_camera_extrinsic.yaml`），`stvl_camera` 在 `camera`/`dual` 模式下订阅 `/berxel/points`（mark + 视锥 clear），与 `stvl_lidar` 在主图上 max 合并。单独跑 `ros2 launch berxel_camera_ros2 berxel_p100r.launch.py` 时不桥接到雷达树，也不要与 `local_costmap.launch.py` 同时开。

USB 权限规则为 `config/99-berxel-p100r.rules`，只授权实测的 `0603:0009` 给 `plugdev` 组，不使用厂商覆盖其它设备的宽泛规则。

## 构建与运行

SDK 安装完成后，在 Jetson 工作空间执行：

```bash
source /opt/ros/humble/setup.bash
source /home/nvidia/code/upper_computer/env.sh
cd /home/nvidia/code/upper_computer
colcon build --packages-select berxel_camera_ros2 --event-handlers console_direct+
```

相机接入 Jetson 并完成 USB 权限配置后，由用户运行：

```bash
source /opt/ros/humble/setup.bash
source /home/nvidia/code/upper_computer/install/setup.bash
source /home/nvidia/code/upper_computer/env.sh
ros2 launch berxel_camera_ros2 berxel_p100r.launch.py
```

在 Jetson 屏幕启动相机并打开配套 RViz：

```bash
/home/nvidia/code/upper_computer/run_berxel_rviz.sh
```

接入局部代价地图时不要单独跑上面的相机 launch。三种入口（RViz 只显示 2D costmap，不叠相机点云）：

```bash
/home/nvidia/code/upper_computer/run_local_costmap_lidar.sh    # STVL 只用 MID360
/home/nvidia/code/upper_computer/run_local_costmap_camera.sh   # STVL 只用 P100R；雷达仍跑 FAST-LIO2
/home/nvidia/code/upper_computer/run_local_costmap_dual.sh     # STVL 雷达 + 相机
```

`camera`/`dual` 会按 `berxel_ihawk_100R.yaml` 加上 `mid360_nav2` 的 QVGA/15 fps overlay 拉起相机，并发布 `livox_frame → berxel_link`。Direct 采集仍用 640×400 Mix，不要改厂商 `berxel_ihawk_100R.yaml`。

指定相机序列号：

```bash
ros2 launch berxel_camera_ros2 berxel_p100r.launch.py serial_number:=设备序列号
```
