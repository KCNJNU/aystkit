# Robot Observation Protocol v1

设备通过 `POST /api/v1/ingest`，`Content-Type: application/json`，`Authorization: Bearer <设备token>`。
生产URL为HTTPS；无重定向跟随，不把凭据放查询参数或日志。
schema严格根字段如下：

| 字段 | 语义 |
| --- | --- |
| schema_version | 整数1 |
| robot_id/source_id | 1~64字符身份，与云端设备配置匹配 |
| session_id | 每次Collector进程的UUID，用于sequence重新开始 |
| sequence | source/session内单调正整数，不由原设备count代替 |
| kind/topic | observation/heartbeat/media 与真实来源标识 |
| message_timestamp | 原ROS header的UTC采样时间；无header、0、仿真时间或无采样时间的HTTP预览为null |
| collector_timestamp | 本次上传消息的封装UTC秒，不等同于真实采样时间 |
| payload | 下述版本化DTO，不是任意URL或文件上传 |

媒体payload另外保留 `received_timestamp`（机器人首次收到并编码该帧的UTC秒，缓存重发不刷新）；采样不明时message_timestamp为null。组合观测无单一采样时间，envelope.message_timestamp为null，各字段保留原来源/age；不能虚构STM32未提供的采样UTC。
云端另外记录 `server_timestamp`、lastSeen、lastObservation、lastReceived、actualTimestamp及每媒体来源和接收时间。actualTimestamp仅记录真实采样时间，组合观测为空；lastObservation可为采集封装时间，不能解释为STM32采样时间。
三个时间不能混为一谈；机器人与ECS应由现有时间同步服务维护时间，不能以请求接收时间把旧采样标记为新数据。

## 观测与心跳

`observation` topic固定 `/observer/observation`；payload严格包含六个对象：system/robot/devices/ros2/pages/errors。
system由机器人psutil/sys采集，云端适配为UI的rk3588；robot携带实际Telemetry及debug.fields来源/单位/状态；sensors保留真实Topic/Type/QoS相关摘要。
所有数字、字符串、嵌套深度和集合受限；最大256 KiB，额外根字段拒绝。整数超64bit范围拒绝，JSON重复键拒绝。
0保留，null保留，非有限float归null，布尔不当作数字。状态枚举沿用 AVAILABLE/WAITING_UPSTREAM/STALE/NO_DATA/ERROR/UNAVAILABLE/IDLE/ONLINE/OFFLINE/DISCONNECTED。
收到 `/cmd_vel` 从不成为实际反馈；Collector不订阅或发布运动控制。未收到的Page不通过其他字段推断填充。
`heartbeat` topic `/observer/heartbeat`，payload仅uptime_s；只更新设备连接状态，不刷新观测、Page或媒体的采样年龄。

## 媒体

全局共17个媒体key：fisheye/color/depth/lidar/registered/depthPoints/bluesea/scan/map/localCostmap/globalCostmap，以及HTTP相机 usbRear/usbFront/usbLeft/usbHub2/usbHub3/usbPort21；协议支持不等于Collector启用。
ROS媒体topic为真实ROS topic；HTTP相机使用下述逻辑接口标识，不能解释为ROS Topic。payload限制在protocol.py列出的元数据字段。
图片使用JPEG、地图使用PNG（base64置于data，mime_type明确），最多512000字节；云端检查签名、尺寸与PNG结构/压缩大小，不接受任意内容。
点云仅XYZ，最多4000个有限点，默认1500；保留frameId，不做坐标变换。
`bluesea`、`scan` 同为 `kind=cloud`，输入分别默认 `/bluesea_new/scan`、`/scan` 的 LaserScan。按距离与角度投影为传感器本地 XY，Z=0 只是平面绘图约定，不代表三维测量或 TF 融合。单次输入最多100000条距离；过滤非有限、非正及范围外距离，再有界抽样。`totalPoints` 是原始距离条数，`points` 是有效抽样点；空有效扫描为 NO_DATA，坏元数据为 ERROR，无输入 WAITING_UPSTREAM，超过2秒未更新为 STALE，并移除旧点。
地图保留原尺寸、分辨率、frameId、origin和orientation，PNG缩小长边≤800px。图像原/预览尺寸区分。
上游未发送/过期/异常时上传状态，不能制造图片。静态地图根据内容hash去重，健康重发和重连重同步；流式costmap仍有过期限制。
总请求上限1 MiB。HTTP413、429、401/403、409分别表示大小、限流、凭据/权限、顺序冲突；日志不输出完整请求或token。

### 可选 HTTP 相机媒体

六个HTTP key均为 `kind=image`。默认 `legacy3` 保留历史映射：rear→camera1、front→camera2、left→camera3。当前显式 `COLLECTOR_HTTP_CAMERA_PROFILE=jetson-direct3` 为front→camera2、left→camera1、usbPort21→camera3，不启用rear或Hub key，扩展接口2.1方向未知。历史兼容 `jetson-hub4` 保留front→camera2、left→camera1、usbHub2→camera4、usbHub3→camera3；Hub接口仅标2.1.2/2.1.3，不推断方向。各topic为对应 `/http_camera/cameraN`，更改profile前须审计真实设备映射，不能按端点编号推断方向。`note` 明确 HTTP 只读来源，`source` 仍为负责上传的 primary source_id。它们不替代 `fisheye/color/depth`，不声明 ROS 发布者、相机采样时间或已知坐标系：`sourcePublisherCount`、`sourceHz`、`message_timestamp`、`frameId` 均为 null。

Collector 需显式设置 `COLLECTOR_HTTP_CAMERA_URL=http://127.0.0.1:18080`。先在 Jetson 单独部署 `collector/http_camera_relay.py`（依赖 Python 标准库、OpenCV 和 NumPy；服务模板 `deploy/systemd/robot-cloud-camera-relay.user.service`）：relay 只绑定 `127.0.0.1:18081`，就地只读获取原 `127.0.0.1:8080` 状态或单个 JPEG part。取帧后先关闭原 stream，再不裁剪地等比缩小至不超过 640×480、JPEG quality 60，返回有限响应；不改变原相机采集设置。再建立 RK3588 `127.0.0.1:18080` 到 Jetson `127.0.0.1:18081` 的 SSH 隧道，不直接转发原连续 MJPEG 流，避免额外帧在 SSH 通道缓存中积累。
仅 GET 白名单接口，不接受任意 URL 或重定向。RK3588 每路独立、错峰请求 `/stream/cameraN`，最高 1 FPS/路；状态检查在 Jetson relay 本机完成，单路慢请求不再阻塞其他相机。已满足 960×720 上限的 JPEG 经解码及协议校验后直接上传，不重复有损编码；更大输入仍等比缩小、quality 70 编码。这些都是普通相机预览，不是深度图；`width/height` 记录接收到的 relay JPEG 尺寸，不能当作未提供的相机 native／原始采集分辨率。不开设备、不控制机器人、不修改原相机程序，审计模式不连接 HTTP。relay 不持有云端 token，新增 relay 与 SSH 转发端口仅本机回环可见，不为原 8080 新增公网入口；这些接口约定不表示公网验收全部通过。
relay 模板将 OpenBLAS/OMP/MKL 限为单线程，资源上限为 256 MiB／32 个任务。Jetson 当前用户 `Linger=no`，用户服务随用户管理器／会话启动，不应据此宣称整机重启后无人登录自启已配置或验收。

relay 的有限响应包含私有头 `X-Preview-Contract: 1`、`X-Source-SHA256`（原始、压缩前 JPEG 的 SHA256）和 `X-Source-Age-S`（源 `age_s` 加本机状态检查、取帧和编码耗时）。RK3588 严格校验三者，并加上跨机请求和本机处理耗时。源 JSON/JPEG/元数据损坏返回 422；502/503/504 只用于传输临时失败。保留 `/status` 以兼容旧适配器，升级须先部署 relay。

上游没有可靠帧号或采样时间，因此只在源年龄新鲜且原始 JPEG hash 变化时增加 `frameSeq`/`receivedFrames`，不能用缩小后 JPEG 的 hash 代替源身份：有损压缩可能把不同原帧压成同一预览。`received_timestamp` 为首次验证该新帧的 UTC 秒，不是相机曝光时间；重复 GET、重复原始 hash 或重连重发均不刷新它。原始字节完全相同超过 2 秒仍保守转 STALE。临时网络失败仅允许保留原帧剩余的真实有效期，不重传旧图、不续期、不把既有 ERROR 恢复为正常；到期或明确坏数据仍隐藏画面。

云端图像年龄包含 `源年龄 + Collector→云端上传耗时 + 云端驻留时间`，并保留真实接收／消息时间下界；未来时钟不能减小源年龄。相机快照新增只读 `frameIdentity=session:sequence`，图片响应保留 `X-Frame-Sequence` 并增加 `X-Frame-Age-S`、`X-Frame-TTL-S`。这些是显示接口字段，不改变 schema v1 上传字段。元数据、身份与 JPEG 从同一缓存条目读取。浏览器按剩余 TTL 再扣完整请求和解码耗时；相同身份不可续期，迟到的同会话旧序号状态不可清掉更新且未过期的图片，真实到期仍清图并停录。已退休会话的响应／状态丢弃；切换会话即使暂无可见图片，也作废旧在途响应。相机请求用同一次单调时钟采样安排下次拉取，避免双重限频在边界跳过整个周期。

Collector未开启HTTP时为11媒体/13队列项；开启且未指定媒体列表时，当前显式 `jetson-direct3` 与默认兼容 `legacy3` 均为14媒体/16队列项，兼容 `jetson-hub4` 为15媒体/17队列项。主源队列为启用媒体数加heartbeat/observation，并非全局17key全部启用；4 MiB字节上限不变。显式媒体列表与云端授权仍须按唯一 owner 对齐，不能因协议支持新 key 就推断已接收到真实数据。

## 权限与顺序

一个robot仅一个primary来源负责观测。可选cameras来源仅允许配置的媒体key，不能修改system/robot/pages。
媒体启用列表和云端 `media_keys` 授权须分别核对：primary 凭据未显式列出 `media_keys` 时，云端沿用默认授权全部 `MEDIA_KEYS` 的策略，包含六个 HTTP key；这不等于 Collector 默认启用 HTTP。已有显式授权列表仍严格限制允许项，增加二维扫描或 HTTP 预览时才按需追加 `bluesea,scan` 或相应 HTTP key，不必因此更换 token。
云端 `cameras` 权限检查允许显式授权的共享 `CAMERA_KEYS`（含六个 HTTP key）及 depthPoints，但当前独立 `cameras` Collector 仅支持 fisheye/color/depth/depthPoints；HTTP 适配器在 Collector 中仅供显式启用的 primary 使用，扫描也不属于 cameras 权限。先升级云端协议并核对授权，再重启独立 Collector。
每个媒体key只有一个权威上传source；启用Jetson相机Collector时，RK端和云端primary都必须排除相机key。
设备token绑定robot/source；用户会话只能查看，不能上传。更新设备凭据后重启新Collector即可，本地控制不受影响。
云端持久化sequence与session检查点以拒绝重复、乱序与退休session。高sequence不能把过旧时间戳恢复为实时。
网络失败保留有界最新值并退避；不保存无限历史。重启Collector启动新session，云端验证新session开始与时间先后。


## 2026-10-28 分组预览与帧有效期

`GET /visuals.json` 保持完整响应；`group=clouds` 只返回 MID360、配准点云和深度点云，`group=scans` 只返回蓝海与底部扫描，`group=maps` 只返回三类地图。`group=overview` 保持最多一个真实来源、768点和48 KiB上限。参数必须是唯一的已知group。已批准账号的snapshot和visuals成功GET按Accept-Encoding协商gzip；错误、账号接口与HEAD不压缩。

点数据增加`frameIdentity`（来源、会话和帧序号组成的不透明身份）、`ttlS`、`sourceAgeS`（组装上传时源上报帧龄）和`uploadAgeS`（上行延迟及云端驻留时间）。`ageS`仍为综合真实帧龄。弱ETag绑定当前分组、身份、点值和状态，忽略增长的年龄；条件命中返回304空体，不重置已有租约。状态变为STALE后返回新状态且不包含旧points。

浏览器只请求当前分类；点数据最多每秒检查两次，地图一次，单个在途请求。暂时失败保留原剩余有效期；已成功绘制且有可信身份的过期点图最多保留5秒，标注非实时、显示样本年龄、禁录并禁止交互。重复响应、304或错误均不延长有效期和保留期；离页、后台及登录失效清理。Collector、ROS频率及原始坐标精度不因本次网页优化改变。

地图 PNG 返回绑定媒体键、来源帧身份和原始字节的强 ETag，以及 `X-Frame-Age-S` / `X-Frame-TTL-S`。静态地图使用 `event` 有效期，仍由来源心跳决定可用性；代价地图沿用后端动态有效期。浏览器将下载和解码耗时计入帧龄，过期后清除旧图并停止录制；304 只确认已解码字节，没有延长同一帧有效期的权限。


## 2026-10-28 逐路增量点云与迟到候选

点云和扫描页面使用`/visuals.json?group=clouds&delta=1`或`group=scans&delta=1`。`X-Visual-Seen`最多2048字节，内容为组内key到64位十六进制previewTag的JSON对象；重复或未知键拒绝。AVAILABLE每路最多均匀选取768个既有真实点，保留首尾，坐标不取整；previewInputPoints保留采集预览的原点数，totalPoints保留设备原始点数。previewTag绑定来源会话、帧、坐标系、话题和实际预览坐标。匹配时pointsUnchanged=true并省略points，年龄和状态仍每次返回；增量模式不使用整组ETag。旧无delta接口继续提供完整预览。

浏览器仅为成功绘制、身份及标签完全匹配且仍有效的缓存发送seen。普通同源新序号迟到时保留上一帧原租约及固定保留期；真正换源、ERROR、NO_DATA或明确无输入仍清除。总览固定优先MID360，可用时不按两路上传相位交替换源；缺少旧来源的单源响应不意味着旧来源失效。拒收的候选不登记ETag，避免无缓存却收到304。ResourceTiming使用本次合法requestStart排除浏览器连接等待、DNS与TLS，仍扣除全部实际请求、下载及解析耗时；无可信timing时保持保守预算。TTL维持2秒，非实时保留仍不超过5秒。
