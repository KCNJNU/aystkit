# 地库原相机 JPEG 归档

归档 tap 接在原 `camera_native_web.py` 的同一次相机读取中。每帧原 JPEG 更新 sequence 后、网页限帧和 H.264 编码前进入有界内存队列；落盘线程保存相机提供的 JPEG 字节，保留原尺寸，不缩放、不重编码，不重新打开相机。网页预览和本机窗口仍共享原来的采集。

## 显式开启

默认 `archive_sink=None`，不加载归档模块、不创建归档文件。开启需要安装同一事实源版本的 `collector/archive_camera.py`、`archive_core.py`，并提供只允许本人读写的 JSON 配置，例如源机的 `~/.config/robot-cloud/native-camera-archive.json`：

```json
{
  "schemaVersion": 1,
  "root": "/home/nvidia/.local/share/robot-native-archive",
  "controlDir": "/home/nvidia/.local/state/robot-cloud/garage-control",
  "robotId": "填写数仓已授权的实际机器人UUID",
  "cameraNames": {
    "camera1": "原相机_camera1",
    "camera2": "原相机_camera2",
    "camera3": "原相机_camera3",
    "camera4": "原相机_camera4"
  },
  "segmentSeconds": 3600,
  "segmentBytes": 134217728,
  "minFreeBytes": 10737418240,
  "maxSpoolBytes": 21474836480,
  "queueFrames": 64,
  "queueBytes": 67108864
}
```

配置需 `chmod 600`，路径必须为绝对路径，四路 identity 必须与实际采集配置完全相同。可按现场已核实的配置将名称改为前/后/左/右，不能按设备编号猜方向。2026-11-08 用户确认前后、左右标签互换，当前配置为 camera1 前、camera2 后、camera3 右、camera4 左；实际 by-path、模式和帧率沿用原配置。历史采集清单与目录保留当时记录的元数据，不自动移动或重写原数据。

`run_camera_native_web.sh` 仅在这份配置存在时自动导出 `NATIVE_CAMERA_ARCHIVE_CONFIG` 并加载归档包。也可显式传 `--archive-config /绝对路径/配置.json`。已有共享相机 unit 使用环境 drop-in 指定同一配置和包路径，不改变原 `ExecStart` 的设备、像素、帧率或本机窗口参数。配置和代码部署不会启动相机，也不另建采集服务；原采集未运行时，相机原件状态必须显示等待。

本地 `controlDir/garage.json` 是控制端同步的地库声明，归档默认关闭。没有有效声明或声明过期时拒绝记录；离开地库应关闭共享声明并确认源侧状态已同步。断网时沿用源侧已同步的声明，不能把尚未送达源机的关闭状态显示为已经停止。

## 文件与时间

片段按上海日期、已确认的传感器名称及实际时间段组织，默认每小时或 128 MiB 切片。`frames.mjpg` 是原 JPEG 的顺序拼接，`index.jsonl` 逐帧记录偏移、长度、SHA-256、sequence、时间和 JPEG 头部尺寸；`archive.json` 与数仓归档上传器使用同一清单格式。

tap 的 `timestampNs` 和 `wallTime` 是原进程读完这一 JPEG 后的接收时间，**不是相机曝光时间**。保留原 sequence，重启产生的序号回退单独记录。原始接收时间可用于同机采集记录对照，跨机比较仍需真实时钟同步证据。

队列或磁盘跟不上时不阻塞原采集。队列上限、空间预留和缓存配额会拒收新帧，并在 `camera-status.json` 的 sink/store 计数中报告；清单及帧索引记录序号缺口、重复、重置和可检测的 JPEG 包损坏。不能把“已入队”当成“已持久保存”，也不能宣称发生掉样的时段完整无损。

后台每两秒写 `controlDir/camera-status.json`，结构为 `updatedAt`、`store` 和 `sink`。正常退出先停止原 capture，再收尾 JPEG 和索引。意外断电的 `.camera-active` 原件需校验完整索引行及逐帧 SHA 后恢复；损坏或不完整尾部保留在 `recovery`，不能伪造为已成功入仓。现有源侧归档上传 daemon 负责断网重试和已完成清单的入仓；tap 自身不建立网络连接、不删除原件。

新采集的默认归档配置为 `compact-v1`，相机 tap 仍完整保存原始 JPEG 中转文件。后台归档器为新录像生成最高 720p、约 2 Mbps 的平衡画质 HEVC MKV（`videoArchivePreset=720p-balanced-v1`），等比高质量缩小、不放大、不裁剪、不抽帧，原帧索引作为附件嵌入；该颜色视频有损。源尺寸与输出尺寸分别记录，原 CameraInfo 仍对应源图像。深度图保持 FFV1 无损，非图像传感器 bag 使用 Zstandard 无损压缩。已有归档不重编码。仅在正式对象上传成功、再次确认对象和本地校验值后清理重复中转文件；未入仓的原件继续保留。详细策略见 [地库归档](garage_archive.md)。


### 深度相机采集状态与保存状态

P100R 的采集状态由现有原始图像订阅的真实回调计数独立上报。编码器旁路的 `capture_health.py` 每秒原子更新 `garage-control/berxel-capture.json`，只包含彩色、深度原始 Image 话题、接收计数、帧龄、当前 PID 和本次开机标识。源端文件同步辅助服务只读取这份小文件，不新增相机打开操作或原始视频网络传输。

关闭地库保存后，原始归档订阅仍按既有规则关闭，页面可继续显示相机“采集中”。保存状态继续由地库开关确认和原始归档写入计数增长判断；采集正常不等于已保存。状态文件过期、进程退出、开机标识改变或原始帧停止时，不沿用历史正常状态。预览编码失败也不会把真实的原始采集计数清零。


### 当前安装的深度视频压缩策略（2026-10-31）

新片段使用 `ARCHIVE_DEPTH_VIDEO_MODE=pseudocolor`：P100R 原始 16 位深度先转换为与网页一致的 0～5 米 TURBO 伪彩画面，再使用与普通相机相同的平衡压缩设置（最高 1280×720、HEVC 约 2 Mbps，保持原帧数和时间戳，不放大小尺寸原图）。新保存的视频只供观察，不能从视频恢复原始距离值；清单明确标记 `depthMeasurementAvailable=false` 和 `losslessPixelValues=false`。标定与帧时间索引仍嵌入视频。

策略随采集片段封存，旧片段没有该模式时继续沿用原来的 16 位无损处理，不自动重写历史文件。伪彩压缩器不可用时会保留已校验的伪彩中间视频，并明确记录回退原因。原始临时缓存仍按既有入仓确认规则处理，不能把缓存或待传状态视为已成功上传。

原生文件校验、上传与 SSH 文件接收已取消固定 2 GiB 文件大小判断，仍使用有界块读写。SSH 文件接收按 180 秒无进展超时管理；持续传输不会因总时长超过 180 秒被终止。磁盘保留、缓存配额、每帧和元数据内存边界保持独立。
