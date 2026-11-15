"use strict";

(function exposeFormatters(global) {
  const KNOWN_STATUSES = new Set([
    "ERROR", "AVAILABLE", "STALE", "NO_DATA", "WAITING_UPSTREAM", "UNAVAILABLE", "IDLE"
  ]);

  const STATUS_LABELS = Object.freeze({
    AVAILABLE: "正常",
    WAITING_UPSTREAM: "等待数据",
    STALE: "数据过期",
    NO_DATA: "暂无数据",
    ERROR: "异常",
    UNAVAILABLE: "不可用",
    IDLE: "空闲",
    DISCONNECTED: "已断开",
    ONLINE: "在线",
    OFFLINE: "离线"
  });

  const CONTEXT_STATUS_LABELS = Object.freeze({
    jetson: Object.freeze({ AVAILABLE: "在线", WAITING_UPSTREAM: "等待连接" }),
    ros2: Object.freeze({ AVAILABLE: "正常", WAITING_UPSTREAM: "等待发现" }),
    discovery: Object.freeze({ AVAILABLE: "已发现", WAITING_UPSTREAM: "等待节点" }),
    telemetry: Object.freeze({ AVAILABLE: "正常", WAITING_UPSTREAM: "等待数据" }),
    stm32: Object.freeze({ AVAILABLE: "正常", WAITING_UPSTREAM: "等待数据" }),
    npu: Object.freeze({ AVAILABLE: "正常", WAITING_UPSTREAM: "等待数据" })
  });

  const UI_LABELS = Object.freeze({
    RK3588: "工控机",
    JETSON: "Jetson 边缘计算机",
    DATA_CHAIN: "机器人云端连接",
    ROS2: "机器人通信 ROS2",
    TELEMETRY: "机器人遥测",
    STM32: "底盘控制器 STM32",
    NPU: "神经网络处理器 NPU",
    VISION: "视觉对接",
    "ROS2 Domain": "ROS2 通信域",
    Telemetry: "机器人遥测",
    "STM32 Pages": "底盘数据页",
    "NPU Driver": "神经网络处理器驱动",
    "Control API": "控制接口",
    "Observed Types": "观测到的消息类型",
    "Parse Errors": "解析错误",
    "Control Conflict": "控制冲突检查",
    "Type Contract Conflict": "话题类型冲突",
    ROS_DOMAIN_ID: "通信域编号",
    RMW_IMPLEMENTATION: "通信中间件",
    ROS_LOCALHOST_ONLY: "仅限本机通信",
    ROS_DISCOVERY_SERVER: "节点发现服务器",
    RUNTIME: "运行指标",
    ACTUAL: "实际反馈",
    COMMAND: "控制指令",
    DERIVED: "计算值",
    NETWORK: "网络",
    STATUS: "状态",
    KERNEL: "内核",
    DEVICE: "设备",
    LOCAL_WS_RX: "本机实时数据接收",
    actualOpenDevice: "实际打开设备",
    baud: "波特率",
    portOpen: "端口已打开",
    rxActive: "接收活跃",
    frameValid: "有效帧",
    validFrameCount: "有效帧计数",
    invalidFrameCount: "无效帧计数",
    checksumErrorCount: "校验错误计数",
    validFramesPerSec: "有效帧频率",
    conclusion: "诊断结论"
  });

  // Translate display text only. Protocol keys, model numbers, paths and
  // unknown device names remain intact so diagnostics keep their identity.
  const DEVICE_LABELS = Object.freeze({
    Jetson: "Jetson 边缘计算机",
    "Jetson Orin NX": "Jetson Orin NX 边缘计算机",
    "Intel Core Ultra 5 225H": "英特尔酷睿 Ultra 5 225H 工控机",
    "Intel(R) Core(TM) Ultra 5 225H": "英特尔酷睿 Ultra 5 225H 工控机",
    "USB Camera": "USB 相机",
    "HDR Camera": "高动态范围相机（HDR）",
    "Berxel iHawk100": "Berxel iHawk100 深度相机",
    "MID360 点云": "MID360 三维雷达点云",
    "Livox MID360": "览沃 MID360 三维雷达",
    "雷达 IMU": "雷达惯性测量单元（IMU）",
    IMU: "惯性测量单元（IMU）",
    "FAST-LIO 里程计": "激光惯性里程计（FAST-LIO）",
    "FAST-LIO 配准点云": "激光惯性配准点云（FAST-LIO）",
    "RTAB-Map 定位（可选）": "建图与定位（RTAB-Map，可选）",
    "AMCL 定位（可选）": "粒子滤波定位（AMCL，可选）",
    "Jetson HTTP preview": "Jetson 边缘计算机网页预览",
    "Jetson HTTP": "Jetson 边缘计算机网页预览",
    "ROS2 read-only device interfaces": "ROS2 设备只读接口",
    usbFront: "后视相机",
    usbLeft: "前视相机",
    usbHub3: "右视相机",
    usbHub2: "左视相机",
    usbRear: "旧后视相机接口",
    usbPort21: "旧 USB 相机接口 2.1",
    fisheye: "鱼眼相机",
    color: "深度相机彩色图",
    depth: "深度相机深度图",
    coretemp: "处理器核心温度",
    x86_pkg_temp: "处理器封装温度",
    acpitz: "固件温区",
    "INT3400 Thermal": "英特尔平台温控",
    TCPU: "处理器温区",
    TCPU_PCI: "处理器温控接口",
    iwlwifi_1: "无线网卡温区",
    nvme: "固态硬盘温区",
    soc_thermal: "芯片温区",
    cpu_thermal: "处理器温区",
    gpu_thermal: "图形处理器温区"
  });

  const DETAIL_LABELS = Object.freeze({
    frameId: "坐标系", width: "宽度", height: "高度", points: "点数", encoding: "编码",
    resolutionM: "分辨率（米）", xM: "X 坐标（米）", yM: "Y 坐标（米）", zM: "Z 坐标（米）",
    vxMps: "定位线速度（米/秒）", wzRadps: "定位角速度（弧度/秒）",
    angularVelocity: "角速度（驱动原值）", acceleration: "加速度（驱动原值）",
    poseCount: "路径点数", rayCount: "扫描点数", state: "状态", diagnostics: "节点诊断",
    name: "名称", level: "级别", message: "诊断信息", direction: "方向", value: "数值",
    status: "状态", source: "来源", reason: "原因", topic: "话题", topicType: "消息类型"
  });

  const UNIT_LABELS = Object.freeze({
    FPS: "帧/秒", RPM: "转/分钟", s: "秒", ms: "毫秒", Mbps: "兆比特/秒",
    // Network rates are divided by 1024 in the existing renderer.
    "KB/s": "KiB/秒", "KiB/s": "KiB/秒"
  });

  const REASON_LABELS = Object.freeze({
    "Publisher visible; no message received": "已发现发布节点，等待消息",
    "Publisher not discovered": "尚未发现发布节点",
    "Publisher gone or last message expired": "发布节点已离线或消息已过期",
    "Publisher visible; no valid telemetry received": "已发现发布节点，等待有效遥测",
    "Telemetry publisher not discovered": "尚未发现遥测发布节点",
    "Field metadata absent": "暂无字段元数据",
    "No data": "暂无数据",
    "No image received": "尚未收到图像",
    "Camera unavailable": "相机暂不可用",
    "Connection refused": "连接被拒绝",
    "Connection timed out": "连接超时"
  });

  const DEVICE_STATE_LABELS = Object.freeze({
    detection: Object.freeze({
      ok: "双标记识别正常",
      ok_single: "单标记跟踪",
      no_markers: "未检测到标记",
      pose_jump_rejected: "位姿跳变已拒绝",
      pnp_or_reprojection_rejected: "位姿解算未通过",
      single_marker_uninitialized: "等待双标记初始化",
      single_marker_rejected: "单标记结果未通过"
    }),
    docking: Object.freeze({
      DISABLED: "未启用",
      HOLD_NO_MARKERS: "等待识别标记",
      HOLD_STABILIZING: "位姿稳定中",
      HOLD_NO_POSE: "等待目标位姿",
      HOLD_STALE_POSE: "目标位姿已过期",
      ALIGNING: "正在对正",
      LATERAL_APPROACH: "侧向接近",
      ALIGNMENT_GATE: "近距离对正",
      APPROACHING: "正在接近",
      NEEDS_RECOVERY: "需要重新调整",
      FINAL_MISALIGNED: "最终位置未对正",
      RECOVERY_READY: "恢复完成",
      RECOVERY_REACQUIRE: "重新捕获标记",
      RETRY_EXHAUSTED: "调整次数已用尽",
      DOCKED: "对接完成",
      STOPPED: "已停止"
    })
  });

  const PAGE_LABELS = Object.freeze({
    "0x00": Object.freeze({ protocol: "兼容协议", description: "兼容数据" }),
    "0x10": Object.freeze({ protocol: "底盘运动", description: "运动数据" }),
    "0x11": Object.freeze({ protocol: "清洁机构", description: "滚刷与边刷" }),
    "0x12": Object.freeze({ protocol: "推杆反馈", description: "推杆数据" }),
    "0x13": Object.freeze({ protocol: "故障诊断", description: "故障状态" })
  });

  function isFiniteNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function formatValue(value, digits = 1) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "number" && !Number.isFinite(value)) return "—";
    if (typeof value === "number") {
      if (Object.is(value, -0)) return "0";
      return Number.isInteger(value) ? String(value) : value.toFixed(digits);
    }
    if (value === "") return "—";
    if (typeof value === "boolean") return value ? "是" : "否";
    return String(value);
  }

  function formatBytes(value) {
    if (!isFiniteNumber(value)) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let scaled = value;
    let index = 0;
    while (Math.abs(scaled) >= 1024 && index < units.length - 1) {
      scaled /= 1024;
      index += 1;
    }
    return `${scaled.toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function formatDuration(value) {
    if (!isFiniteNumber(value)) return "—";
    const seconds = Math.max(0, value);
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return hours ? `${days} 天 ${hours} 小时` : `${days} 天`;
    if (hours) return minutes ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`;
    return `${minutes} 分钟`;
  }

  function formatAge(seconds) {
    if (!isFiniteNumber(seconds)) return "—";
    if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
    if (seconds < 60) return `${formatValue(seconds, 1)} 秒`;
    return formatDuration(seconds);
  }

  function normalizeStatus(value, fallback = "WAITING_UPSTREAM") {
    return KNOWN_STATUSES.has(value) ? value : fallback;
  }

  function shortStatus(value) {
    const normalized = normalizeStatus(value);
    return STATUS_LABELS[normalized] || normalized;
  }

  function displayStatus(value, context = "") {
    const normalized = Object.prototype.hasOwnProperty.call(STATUS_LABELS, value)
      ? value
      : normalizeStatus(value);
    const contextual = CONTEXT_STATUS_LABELS[context];
    return contextual && contextual[normalized]
      ? contextual[normalized]
      : (STATUS_LABELS[normalized] || normalized);
  }

  function statusDescription(value) {
    const normalized = normalizeStatus(value);
    if (normalized === "WAITING_UPSTREAM") return "尚未收到上游数据";
    if (normalized === "STALE") return "已保留最后一次数据，但数据已过期";
    if (normalized === "NO_DATA") return "当前暂无可展示数据";
    if (normalized === "ERROR") return "监测到异常状态";
    if (normalized === "UNAVAILABLE") return "当前接口或指标不可用";
    if (normalized === "IDLE") return "当前没有活动任务";
    return "数据状态正常";
  }

  function uiLabel(key, fallback = key) {
    return Object.prototype.hasOwnProperty.call(UI_LABELS, key) ? UI_LABELS[key] : fallback;
  }

  function deviceLabel(value) {
    if (value === null || value === undefined || value === "") return "—";
    const text = String(value);
    if (Object.prototype.hasOwnProperty.call(DEVICE_LABELS, text)) return DEVICE_LABELS[text];
    if (/^Thermal Zone \d+$/.test(text)) return text.replace("Thermal Zone", "温区");
    if (/^thermal_zone\d+$/.test(text)) return `温区 ${text.slice(12)}`;
    if (/^Package id \d+$/.test(text)) return text.replace("Package id", "处理器封装");
    if (/^Core \d+$/.test(text)) return text.replace("Core", "处理器核心");
    return text;
  }

  function reasonLabel(value) {
    if (typeof value !== "string" || value === "") return value;
    const text = value;
    if (Object.prototype.hasOwnProperty.call(REASON_LABELS, text)) return REASON_LABELS[text];
    if (/^Last valid telemetry older than [\d.]+s$/.test(text)) {
      return text.replace(/^Last valid telemetry older than ([\d.]+)s$/, "有效遥测已超过 $1 秒未更新");
    }
    if (text.startsWith("Contract expects ") && text.includes("; graph reports ")) {
      return text.replace("Contract expects ", "预期消息类型：").replace("; graph reports ", "；实际发现：");
    }
    return text;
  }

  function thermalLabel(value) {
    if (value === null || value === undefined || value === "") return "—";
    const text = String(value);
    const label = deviceLabel(text);
    if (/^thermal_zone\d+$/.test(text) || /^Thermal Zone \d+$/.test(text)) return label;
    // Thermal/ACPI names identify driver interfaces, not necessarily a distinct
    // physical sensor. Preserve the raw identifier instead of guessing location.
    if (label !== text) return `${label}（${text}）`;
    if (/[\u3400-\u9fff]/.test(text)) return text;
    return `温区（${text}）`;
  }

  function networkLabel(value) {
    const text = formatValue(value);
    if (text === "—") return text;
    if (text === "lo") return "本机回环（lo）";
    if (text === "Meta") return "代理虚拟网卡（Meta）";
    if (/^(?:eth\d|en[opsx])/.test(text)) return `有线网口（${text}）`;
    if (/^(?:wlan\d|wl[px])/.test(text)) return `无线网卡（${text}）`;
    if (/^(?:docker\d|br-)/.test(text)) return `容器网桥（${text}）`;
    if (/^veth/.test(text)) return `虚拟网口（${text}）`;
    if (/^(?:tun\d|tap\d|utun\d)/.test(text)) return `隧道网卡（${text}）`;
    if (/^wg\d/.test(text)) return `加密隧道网卡（${text}）`;
    return /[\u3400-\u9fff]/.test(text) ? text : `网络接口（${text}）`;
  }

  function detailLabel(key) {
    return Object.prototype.hasOwnProperty.call(DETAIL_LABELS, key)
      ? DETAIL_LABELS[key] : `其他字段（${key}）`;
  }

  function detailValue(value, key = "") {
    if (typeof value !== "string" || !value) return value;
    if (key === "RMW_IMPLEMENTATION") key = "rmwImplementation";
    if (key === "ROS_LOCALHOST_ONLY" && (value === "0" || value === "1")) return value === "1" ? "是" : "否";
    if (key === "rmwImplementation") {
      const names = { rmw_fastrtps_cpp: "Fast DDS 通信中间件", rmw_fastrtps_dynamic_cpp: "Fast DDS 动态通信中间件",
        rmw_cyclonedds_cpp: "Cyclone DDS 通信中间件", rmw_connextdds: "Connext DDS 通信中间件" };
      return `${names[value] || "通信中间件"}（${value}）`;
    }
    if (key === "state" || key === "status" || key === "direction") {
      const states = { STOP: "停止", STOPPED: "已停止", IDLE: "空闲", RUNNING: "运行中", ENABLED: "已启用",
        DISABLED: "未启用", FORWARD: "正向", REVERSE: "反向", BACKWARD: "反向", UP: "向上", DOWN: "向下",
        LEFT: "向左", RIGHT: "向右", OK: "正常", WARN: "警告", WARNING: "警告", UNKNOWN: "未知" };
      const normalized = value.toUpperCase();
      if (Object.prototype.hasOwnProperty.call(STATUS_LABELS, normalized)) return STATUS_LABELS[normalized];
      if (Object.prototype.hasOwnProperty.call(states, normalized)) return states[normalized];
      return /[\u3400-\u9fff]/.test(value) ? value : `未识别状态（${value}）`;
    }
    if (key === "encoding") {
      if (/^avc1\.[0-9a-f]{6}$/i.test(value)) return `H.264 视频（${value}）`;
      if (value === "H.264") return "H.264 视频";
      const encodings = { rgb8: "彩色图像", bgr8: "彩色图像", rgba8: "带透明度彩色图像", bgra8: "带透明度彩色图像",
        mono8: "8 位灰度图像", mono16: "16 位灰度图像", "16UC1": "16 位单通道图像", "32FC1": "32 位浮点单通道图像" };
      return `${encodings[value] || "图像编码"}（${value}）`;
    }
    if (key === "topicType") {
      const types = { "sensor_msgs/msg/Image": "图像消息", "sensor_msgs/msg/CompressedImage": "压缩图像消息",
        "sensor_msgs/msg/Imu": "惯性测量消息", "sensor_msgs/msg/PointCloud2": "三维点云消息",
        "sensor_msgs/msg/LaserScan": "二维扫描消息", "nav_msgs/msg/Odometry": "里程计消息",
        "nav_msgs/msg/OccupancyGrid": "栅格地图消息", "nav_msgs/msg/Path": "路径消息",
        "std_msgs/msg/String": "文本消息", "std_msgs/msg/Int32MultiArray": "整数数组消息",
        "geometry_msgs/msg/PoseStamped": "带时间戳位姿消息", "geometry_msgs/msg/PoseWithCovarianceStamped": "带协方差位姿消息",
        "diagnostic_msgs/msg/DiagnosticArray": "诊断消息" };
      return `${types[value] || "消息类型"}（${value}）`;
    }
    if (key === "message" || key === "reason") return reasonLabel(value);
    if (key === "name") return deviceLabel(value);
    if (["topic", "frameId", "path"].includes(key)) return value;
    return reasonLabel(value);
  }

  function unitLabel(value) {
    return Object.prototype.hasOwnProperty.call(UNIT_LABELS, value) ? UNIT_LABELS[value] : value;
  }

  function sourceLabel(value) {
    const text = deviceLabel(value);
    return text.replace(/\bPage (0x[0-9a-f]+)\b/gi, "数据页 $1");
  }

  function deviceStateLabel(group, value) {
    if (value === null || value === undefined || value === "") return "—";
    const labels = DEVICE_STATE_LABELS[group] || {};
    if (Object.prototype.hasOwnProperty.call(labels, value)) return labels[value];
    if (group === "docking" && String(value).startsWith("RECOVERY_RETREAT_")) {
      return `正在后退调整 · 第 ${String(value).split("_").pop()} 次`;
    }
    return `未知状态（${String(value)}）`;
  }

  function formatNumber(value, digits = 1) {
    return formatValue(value, digits);
  }

  function withUnit(value, unit, digits = 1) {
    const formatted = formatValue(value, digits);
    return formatted === "—" ? formatted : `${formatted} ${unit}`;
  }

  function formatPercent(value) {
    return withUnit(value, "%", 1);
  }

  function formatTemperature(value) {
    return withUnit(value, "°C", 1);
  }

  function formatVoltage(value) {
    return withUnit(value, "V", 2);
  }

  function formatFrequency(value) {
    return withUnit(value, "Hz", 1);
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function safe(value, digits = 1) {
    return escapeHtml(formatValue(value, digits));
  }

  function get(object, path) {
    return path.split(".").reduce((current, key) => (
      current !== null && current !== undefined && typeof current === "object"
        ? current[key]
        : undefined
    ), object);
  }

  function field(payload, key) {
    const info = get(payload, `debug.fields.${key}`);
    if (!info || typeof info !== "object") {
      return {
        value: null,
        state: "WAITING_UPSTREAM",
        type: null,
        source: null,
        ageMs: null,
        unit: null,
        reason: "Field metadata absent"
      };
    }
    return {
      value: isFiniteNumber(info.value) || typeof info.value === "boolean" || typeof info.value === "string"
        ? info.value : null,
      state: normalizeStatus(info.state),
      type: info.type ?? null,
      source: info.source ?? null,
      ageMs: isFiniteNumber(info.ageMs) ? info.ageMs : null,
      unit: info.unit ?? null,
      reason: info.reason ?? null
    };
  }

  function badge(value, compact = false) {
    const normalized = normalizeStatus(value);
    const className = compact ? "mini-badge" : "badge";
    const title = `${statusDescription(normalized)} · ${normalized}`;
    return `<span class="${className} ${normalized}" title="${escapeHtml(title)}">${escapeHtml(displayStatus(normalized))}</span>`;
  }

  function boolStatus(value, availableStatus = "AVAILABLE") {
    if (value === null || value === undefined) return "WAITING_UPSTREAM";
    return availableStatus;
  }

  global.ObserverFormat = Object.freeze({
    badge,
    boolStatus,
    CONTEXT_STATUS_LABELS,
    DEVICE_STATE_LABELS,
    detailLabel,
    detailValue,
    deviceLabel,
    deviceStateLabel,
    displayStatus,
    escapeHtml,
    field,
    formatAge,
    formatBytes,
    formatDuration,
    formatFrequency,
    formatNumber,
    formatPercent,
    formatTemperature,
    formatValue,
    formatVoltage,
    get,
    isFiniteNumber,
    normalizeStatus,
    networkLabel,
    PAGE_LABELS,
    safe,
    reasonLabel,
    sourceLabel,
    shortStatus,
    STATUS_LABELS,
    statusDescription,
    thermalLabel,
    uiLabel,
    unitLabel,
    UI_LABELS
  });
})(window);
