"use strict";

(function startDashboard(global) {
  const scopedFetch = (...args) => global.RobotFleet ? global.RobotFleet.fetch(...args) : fetch(...args);
  const F = global.ObserverFormat;
  const Charts = global.ObserverCharts;
  const historyStore = new Charts.HistoryStore();
  const CAMERA_TRENDS = Object.freeze({
    usbLeft: "cameraFrontFps", usbFront: "cameraRearFps",
    usbHub2: "cameraLeftFps", usbHub3: "cameraRightFps"
  });
  const PAGE_NAMES = new Set(["overview", "system", "robot", "media", "logs", "tests"]);
  // This deployed interface list follows audited producer implementations.
  // Missing samples, null values and zero publishers never retire an interface.
  const RETIRED_SENSOR_KEYS = new Set(["amcl", "previewDocking", "fisheye", "usbRear", "usbPort21"]);
  const RETIRED_SENSOR_TOPICS = new Set(["/amcl_pose", "/aruco_docking_preview/state", "/fisheye_front/image_raw", "/observer_camera/fisheye/compressed"]);
  const SUBPAGES = Object.freeze({
    system: Object.freeze({ performance: "性能概览", environment: "温度与网络", stm32: "底盘通信 STM32", ros: "机器人通信 ROS2" }),
    robot: Object.freeze({ motion: "底盘与运动", actuators: "清洁与推杆", sensors: "感知设备", docking: "视觉对接" }),
    media: Object.freeze({ cameras: "相机", scans: "二维雷达", clouds: "点云", maps: "地图" })
  });
  const lastSubpages = Object.fromEntries(Object.entries(SUBPAGES).map(([page, children]) => {
    const names = Object.keys(children);
    return [page, readStringPreference(`observer.subpage.${page}`, names[0], names)];
  }));
  const state = {
    snapshot: null,
    socket: null,
    connected: false,
    everConnected: false,
    lastMessageAt: null,
    windowSeconds: readNumberPreference("observer.window", 120, [60, 120, 300]),
    viewMode: readStringPreference("observer.view", "compact", ["compact", "detailed"]),
    reconnectTimer: null,
    socketStartedAt: null,
    socketMessageAt: null,
    observationEpoch: 0,
    observationResume: null,
    pageSuspended: false,
    authExpired: false,
    route: null,
    expandedGroup: null,
    previousStatuses: null,
    events: [],
    nativeMetrics: null,
    nativeMetricsRequest: null
  };

  function readStringPreference(key, fallback, allowed) {
    try {
      const value = global.localStorage.getItem(key);
      return allowed.includes(value) ? value : fallback;
    } catch (_) {
      return fallback;
    }
  }

  function readNumberPreference(key, fallback, allowed) {
    const value = Number(readStringPreference(key, String(fallback), allowed.map(String)));
    return allowed.includes(value) ? value : fallback;
  }

  function savePreference(key, value) {
    try { global.localStorage.setItem(key, String(value)); } catch (_) { /* storage is optional */ }
  }

  function telemetryOf(snapshot = state.snapshot) {
    if (snapshot && snapshot.robot && snapshot.robot.telemetry) return snapshot.robot.telemetry;
    return snapshot && snapshot.ros2 && snapshot.ros2.topics
      ? snapshot.ros2.topics["/chassis_host/telemetry"] || {}
      : {};
  }

  function visionOf(snapshot = state.snapshot) {
    return snapshot && snapshot.devices ? snapshot.devices.visionDocking || {} : {};
  }

  function disconnectedTooLong() {
    return state.lastMessageAt !== null && performance.now() - state.lastMessageAt > 3500;
  }

  function effectiveStatus(value) {
    const normalized = F.normalizeStatus(value);
    return disconnectedTooLong() && normalized === "AVAILABLE" ? "STALE" : normalized;
  }

  function usable(value) {
    return effectiveStatus(value) === "AVAILABLE";
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = value;
  }

  function setHTML(id, value) {
    const element = document.getElementById(id);
    if (element) element.innerHTML = value;
  }

  function metricText(value, unit = "", digits = 1) {
    const formatted = F.formatValue(value, digits);
    return formatted === "—" || !unit ? formatted : `${formatted} ${F.unitLabel(unit)}`;
  }

  function statusCard(label, statusValue, mainValue, metadata, presentation = false) {
    const normalized = presentation ? F.normalizeStatus(statusValue) : effectiveStatus(statusValue);
    return `<article class="status-card ${normalized}">
      <div class="status-label"><span>${F.escapeHtml(label)}</span><i class="status-dot ${normalized}"></i></div>
      <div class="status-value">${F.escapeHtml(mainValue ?? F.shortStatus(normalized))}</div>
      <div class="status-meta">${F.escapeHtml(metadata ?? "—")}</div>
    </article>`;
  }

  function kpiCard({ label, value, unit = "", status: statusValue, series, meta = "", type = "" }) {
    const normalized = effectiveStatus(statusValue);
    const formattedValue = F.formatValue(value);
    const visibleUnit = formattedValue === "—" ? "" : F.unitLabel(unit);
    const sparkline = series
      ? `<canvas class="kpi-spark" data-spark-series="${F.escapeHtml(series)}"></canvas>`
      : "";
    const percentage = normalized === "AVAILABLE" && F.isFiniteNumber(value) ? Math.max(0, Math.min(100, value)) : null;
    const gauge = unit === "%" ? `<svg class="kpi-gauge${percentage === null ? " is-empty" : ""}" viewBox="0 0 56 56" aria-hidden="true" data-value="${percentage === null ? "unknown" : percentage}"><circle class="kpi-gauge-track" cx="28" cy="28" r="22"/><circle class="kpi-gauge-value" cx="28" cy="28" r="22" pathLength="100" stroke-dasharray="${percentage === null ? 0 : percentage} 100" transform="rotate(-90 28 28)"/><circle class="kpi-gauge-core" cx="28" cy="28" r="16"/></svg>` : "";
    return `<article class="kpi-card">
      <div class="kpi-top"><span class="kpi-label">${F.escapeHtml(label)}</span>${F.badge(normalized, true)}</div>
      <div class="kpi-reading${gauge ? " has-gauge" : ""}"><strong class="kpi-value${typeof value === "string" ? " kpi-text" : ""}" title="${F.escapeHtml(formattedValue)}">${F.escapeHtml(formattedValue)}</strong><small class="kpi-unit">${F.escapeHtml(visibleUnit)}</small>${gauge}</div>
      ${sparkline}
      ${meta ? `<div class="kpi-meta"><span>${F.escapeHtml(F.sourceLabel(meta))}</span></div>` : ""}
    </article>`;
  }

  function diagnosticItem(label, value, wide = false) {
    const translated = F.uiLabel(label);
    const rawKey = translated === label ? "" : ` title="原始字段：${F.escapeHtml(label)}"`;
    return `<div class="diagnostic-item${wide ? " wide" : ""}"><label${rawKey}>${F.escapeHtml(translated)}</label><span title="${F.escapeHtml(translated)} · 原始值：${F.escapeHtml(F.formatValue(value))}">${F.safe(F.detailValue(value, label))}</span></div>`;
  }

  function table(headers, rows) {
    return `<table class="data-table"><thead><tr>${headers.map(header => `<th>${F.escapeHtml(header)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function deriveSTM32Status(payload, telemetry) {
    if (usable(telemetry.status) && payload && payload.chassisAlive === true) return "AVAILABLE";
    if (effectiveStatus(telemetry.status) === "AVAILABLE") return "WAITING_UPSTREAM";
    return effectiveStatus(telemetry.status);
  }

  function observedStatuses(snapshot) {
    const system = snapshot.rk3588 || {};
    const ros = snapshot.ros2 || {};
    const telemetry = telemetryOf(snapshot);
    const payload = snapshot.robot && snapshot.robot.data;
    const vision = visionOf(snapshot);
    const cloud = snapshot.cloud || {};
    return {
      RK3588: effectiveStatus(system.status),
      DATA_CHAIN: effectiveStatus(cloud.online === true ? cloud.status : (cloud.lastSeen ? "STALE" : "WAITING_UPSTREAM")),
      ROS2: effectiveStatus(ros.status),
      TELEMETRY: effectiveStatus(telemetry.status),
      STM32: deriveSTM32Status(payload, telemetry),
      VISION: effectiveStatus(vision.status)
    };
  }

  function recordEvent(source, nextStatus, previousStatus = null) {
    state.events.unshift({
      time: new Date(),
      source,
      status: F.normalizeStatus(nextStatus),
      previous: previousStatus ? F.normalizeStatus(previousStatus) : null
    });
    state.events.splice(20);
  }

  function recordStateChanges(snapshot) {
    const next = observedStatuses(snapshot);
    if (state.previousStatuses) {
      Object.entries(next).forEach(([source, nextStatus]) => {
        const previousStatus = state.previousStatuses[source];
        if (previousStatus !== nextStatus) recordEvent(source, nextStatus, previousStatus);
      });
    }
    state.previousStatuses = next;
  }

  function recordHistory(snapshot) {
    const timestamp = F.get(snapshot, "meta.timestamp");
    const system = snapshot.rk3588 || {};
    const telemetry = telemetryOf(snapshot);
    const payload = snapshot.robot && snapshot.robot.data;
    const metric = (name, value, statusValue) => historyStore.record(name, timestamp, value, usable(statusValue));

    metric("cpu", F.get(system, "cpu.value"), F.get(system, "cpu.status"));
    metric("ram", F.get(system, "ram.usage.value"), F.get(system, "ram.usage.status"));
    metric("temp", F.get(system, "primaryThermal.temperatureC"), F.get(system, "primaryThermal.status"));
    metric("telemetryHz", telemetry.hz, telemetry.status);
    metric("telemetryAge", telemetry.ageS, telemetry.status);
    const interfaces = Array.isArray(system.network) ? system.network.filter(row => row.up === true && row.name !== "lo") : [];
    ["rx", "tx"].forEach(direction => {
      const values = interfaces.map(row => row[`${direction}BytesPerSec`]);
      const value = values.length && values.every(item => F.isFiniteNumber(item) && item >= 0)
        ? values.reduce((sum, item) => sum + item, 0) / 1024 : null;
      metric(direction === "rx" ? "networkRx" : "networkTx", value, system.status);
    });
    const sensors = F.get(snapshot, "devices.sensors") || {};
    metric("scanPoints", F.get(sensors, "bluesea.value.rayCount"), F.get(sensors, "bluesea.status"));
    metric("cloudPoints", F.get(sensors, "lidar.value.points"), F.get(sensors, "lidar.status"));
    const sample = state.nativeMetrics;
    const sourceFresh = usable(F.get(snapshot, "cloud.status")) && sample && performance.now() < sample.deadline;
    Object.entries(CAMERA_TRENDS).forEach(([key, series]) => {
      const row = sourceFresh && sample.value.enabled === true ? sample.value.cameras?.[key] : null;
      const valid = row && !row.error && !row.video_error && F.isFiniteNumber(row.frame_age_ms)
        && row.frame_age_ms >= 0 && row.frame_age_ms < 2000;
      metric(series, valid ? row.encoded_fps : null, valid ? "AVAILABLE" : "WAITING_UPSTREAM");
    });

    const robotFields = [
      ["voltage", "busVoltage"], ["actualVx", "actualVxMps"],
      ["actualWz", "actualWzRadps"],
      ["leftWheel", "leftWheelMps"], ["rightWheel", "rightWheelMps"]
    ];
    robotFields.forEach(([series, key]) => {
      const field = F.field(payload, key);
      metric(series, field.value, field.state);
    });

    const pages = snapshot.pages || {};
    [["page00", "0x00"], ["page10", "0x10"], ["page11", "0x11"], ["page12", "0x12"], ["page13", "0x13"]]
      .forEach(([series, key]) => {
        const page = pages[key] || {};
        metric(series, page.hz, page.status);
      });
  }

  function renderHeader(snapshot) {
    const ros = snapshot.ros2 || {};
    document.body.dataset.observationFresh = String(state.connected && !disconnectedTooLong());
    const live = document.getElementById("header-live");
    const liveState = state.connected && !disconnectedTooLong() ? "AVAILABLE" : (disconnectedTooLong() ? "STALE" : "WAITING_UPSTREAM");
    live.innerHTML = `<i class="status-dot ${liveState}${liveState === "AVAILABLE" ? " live-pulse" : ""}"></i><b>${liveState === "AVAILABLE" ? "页面在线" : (liveState === "STALE" ? "页面数据已中断" : "正在连接页面")}</b>`;
    const rosState = effectiveStatus(ros.status);
    const cloud = snapshot.cloud || {};
    const jetsonState = effectiveStatus(cloud.online === true ? "AVAILABLE" : (cloud.lastSeen ? "STALE" : "WAITING_UPSTREAM"));
    setHTML("header-ros", `<i class="status-dot ${rosState}"></i><b>机器人通信</b><em>${F.displayStatus(rosState, "ros2")}</em>`);
    setHTML("header-jetson", `<i class="status-dot ${jetsonState}"></i><b>机器人</b><em>${disconnectedTooLong() ? "状态待确认" : cloud.online === true ? "在线" : (cloud.lastSeen ? "离线" : "等待上线")}</em>`);
  }

  function focusMetric(label, value, unit, statusValue, icon, digits = 2) {
    const normalized = effectiveStatus(statusValue);
    const current = normalized === "AVAILABLE" && F.isFiniteNumber(value);
    const paths = {
      motion: '<path d="M3 13h16m-5-5 5 5-5 5M5 5h7"/>',
      rotation: '<path d="M5 9a7 7 0 1 1 0 7m0-12v5h5"/>',
      pulse: '<path d="M2 12h5l3-7 4 14 3-7h5"/>',
      power: '<rect x="3" y="6" width="16" height="12" rx="3"/><path d="M21 10v4M10 8l-3 5h5l-2 4"/>'
    };
    return `<div class="focus-metric ${normalized}"><span class="focus-metric-icon"><svg viewBox="0 0 24 24" aria-hidden="true">${paths[icon] || paths.pulse}</svg></span><div class="focus-metric-copy"><span>${F.escapeHtml(label)}</span><small>${F.escapeHtml(F.shortStatus(normalized))}</small></div><strong class="focus-metric-value">${current ? F.escapeHtml(F.formatValue(value, digits)) : "—"}<small>${current ? F.escapeHtml(F.unitLabel(unit)) : ""}</small></strong></div>`;
  }

  function renderOverview(snapshot) {
    const system = snapshot.rk3588 || {};
    const ros = snapshot.ros2 || {};
    const telemetry = telemetryOf(snapshot);
    const payload = snapshot.robot && snapshot.robot.data;
    const statuses = observedStatuses(snapshot);
    const voltage = F.field(payload, "busVoltage");
    const pages = snapshot.pages || {};
    const availablePages = Object.values(pages).filter(page => effectiveStatus(page.status) === "AVAILABLE").length;
    const lastPage = F.get(payload, "debug.lastTelemetryPage");

    setHTML("overview-status", [
      statusCard("工控机", statuses.RK3588, F.displayStatus(statuses.RK3588), `运行 ${F.formatDuration(system.uptimeS)}`),
      statusCard("机器人云端连接", statuses.DATA_CHAIN, snapshot.cloud && snapshot.cloud.online === true ? "在线" : (snapshot.cloud && snapshot.cloud.lastSeen ? "离线" : "等待上线"), telemetry.ageS === null || telemetry.ageS === undefined ? "暂无数据" : `距更新 ${F.formatAge(telemetry.ageS)}`),
      statusCard("机器人通信 ROS2", statuses.ROS2, F.displayStatus(statuses.ROS2, "ros2"), `通信域 ${F.formatValue(ros.domainId)}`),
      statusCard("机器人遥测", statuses.TELEMETRY, F.displayStatus(statuses.TELEMETRY, "telemetry"), `频率 ${metricText(telemetry.hz, "Hz")}`),
      statusCard("底盘控制器 STM32", statuses.STM32, F.displayStatus(statuses.STM32, "stm32"), `最近数据页 ${F.formatValue(lastPage)}`)
    ].join(""));

    setHTML("overview-kpis", [
      kpiCard({ label: "处理器使用率", value: F.get(system, "cpu.value"), unit: "%", status: F.get(system, "cpu.status"), series: "cpu", meta: "", type: "RUNTIME" }),
      kpiCard({ label: "内存使用率", value: F.get(system, "ram.usage.value"), unit: "%", status: F.get(system, "ram.usage.status"), series: "ram", meta: `${F.formatBytes(F.get(system, "ram.usedBytes"))} / ${F.formatBytes(F.get(system, "ram.totalBytes"))}`, type: "RUNTIME" }),
      kpiCard({ label: "芯片温度", value: F.get(system, "primaryThermal.temperatureC"), unit: "°C", status: F.get(system, "primaryThermal.status"), series: "temp", meta: F.thermalLabel(F.get(system, "primaryThermal.zone")), type: "ACTUAL" }),
      kpiCard({ label: "母线电压", value: voltage.value, unit: "V", status: voltage.state, series: "voltage", meta: voltage.source, type: "ACTUAL" }),
      kpiCard({ label: "遥测频率", value: telemetry.hz, unit: "Hz", status: telemetry.status, series: "telemetryHz", meta: "5 秒窗口", type: "ACTUAL" })
    ].join(""));

    const linear = F.field(payload, "actualVxMps"), angular = F.field(payload, "actualWzRadps");
    setHTML("overview-focus-metrics", [
      focusMetric("底盘线速度", linear.value, "m/s", linear.state, "motion"),
      focusMetric("转向角速度", angular.value, "rad/s", angular.state, "rotation"),
      focusMetric("遥测频率", telemetry.hz, "Hz", telemetry.status, "pulse", 1),
      focusMetric("母线电压", voltage.value, "V", voltage.state, "power", 1)
    ].join(""));

    setText("overview-cpu-now", metricText(F.get(system, "cpu.value"), "%"));
    setText("overview-ram-now", metricText(F.get(system, "ram.usage.value"), "%"));
    setText("overview-temp-now", metricText(F.get(system, "primaryThermal.temperatureC"), "°C"));
    setText("overview-hz-now", metricText(telemetry.hz, "Hz"));
    setText("overview-age-now", F.formatAge(telemetry.ageS));
    setText("overview-voltage-now", metricText(voltage.value, "V", 2));
    const timestamp = F.get(snapshot, "meta.timestamp");
    setText("overview-updated", F.isFiniteNumber(timestamp) ? `最近更新 ${new Date(timestamp * 1000).toLocaleTimeString("zh-CN", { hour12: false })}` : "最近更新 —");

    const healthRows = [
      [F.uiLabel("ROS2 Domain"), ros.domainId], ["通信中间件", F.detailValue(ros.rmwImplementation, "rmwImplementation")],
      [F.uiLabel("Telemetry"), F.displayStatus(statuses.TELEMETRY, "telemetry")], [F.uiLabel("STM32 Pages"), `${availablePages}/5 正常`]
    ];
    setHTML("system-health", healthRows.map(([label, value]) => `<div class="health-item"><label>${F.escapeHtml(label)}</label><span>${F.safe(value)}</span></div>`).join(""));
    renderEvents();
  }

  function renderEvents() {
    if (!state.events.length) {
      setHTML("state-events", '<div class="empty-state">暂无状态变化</div>');
      return;
    }
    setHTML("state-events", state.events.map(event => `<div class="event-row"><time>${event.time.toLocaleTimeString("zh-CN", { hour12: false })}</time><span>${F.escapeHtml(F.uiLabel(event.source))}${event.previous ? ` · ${F.displayStatus(event.previous)} →` : ""}</span>${F.badge(event.status, true)}</div>`).join(""));
  }

  function renderSystem(snapshot) {
    const system = snapshot.rk3588 || {};
    const primaryIp = Array.isArray(system.ips) && system.ips.length ? system.ips[0] : null;
    setHTML("system-kpis", [
      kpiCard({ label: "处理器使用率", value: F.get(system, "cpu.value"), unit: "%", status: F.get(system, "cpu.status"), series: "cpu", meta: "", type: "RUNTIME" }),
      kpiCard({ label: "内存使用率", value: F.get(system, "ram.usage.value"), unit: "%", status: F.get(system, "ram.usage.status"), series: "ram", meta: `总计 ${F.formatBytes(F.get(system, "ram.totalBytes"))}`, type: "RUNTIME" }),
      kpiCard({ label: "磁盘使用率", value: F.get(system, "disk.usage.value"), unit: "%", status: F.get(system, "disk.usage.status"), meta: `总计 ${F.formatBytes(F.get(system, "disk.totalBytes"))}`, type: "RUNTIME" }),
      kpiCard({ label: "1 分钟负载", value: F.get(system, "loadAverage.0"), status: system.status, meta: `5 分钟 ${F.formatValue(F.get(system, "loadAverage.1"))} · 15 分钟 ${F.formatValue(F.get(system, "loadAverage.2"))}`, type: "RUNTIME" }),
      kpiCard({ label: "运行时长", value: F.formatDuration(system.uptimeS), status: system.status, meta: system.hostname, type: "RUNTIME" }),
      kpiCard({ label: "主网络地址", value: primaryIp, status: primaryIp === null ? "NO_DATA" : "AVAILABLE", meta: system.hostname, type: "NETWORK" })
    ].join(""));
    setText("system-cpu-now", metricText(F.get(system, "cpu.value"), "%"));
    setText("system-ram-now", metricText(F.get(system, "ram.usage.value"), "%"));
    setText("system-temp-now", metricText(F.get(system, "primaryThermal.temperatureC"), "°C"));

    const zones = Array.isArray(system.thermalZones) ? system.thermalZones : [];
    setHTML("thermal-zones", zones.length ? zones.map(zone => {
      const temperature = F.isFiniteNumber(zone.temperatureC) ? zone.temperatureC : null;
      const width = temperature === null ? 0 : Math.max(0, Math.min(100, temperature));
      return `<div class="thermal-item"><div class="thermal-item-head"><strong title="${F.escapeHtml(F.thermalLabel(zone.label || zone.zone))}">${F.safe(F.thermalLabel(zone.label || zone.zone))}</strong><output>${F.safe(temperature)} °C</output></div><div class="thermal-track"><i style="width:${width}%"></i></div><div class="thermal-meta"><span>${F.safe(F.thermalLabel(zone.zone))}</span>${F.badge(effectiveStatus(zone.status), true)}</div></div>`;
    }).join("") : '<div class="empty-state">暂无温区数据</div>');

    const interfaces = Array.isArray(system.network) ? system.network : [];
    setHTML("network-interfaces", interfaces.length ? interfaces.map(network => {
      const addresses = Array.isArray(network.addresses) ? network.addresses : [];
      const ipv4 = addresses.filter(address => !String(address).includes(":"));
      const ipv6 = addresses.filter(address => String(address).includes(":"));
      const networkStatus = network.up === true ? "AVAILABLE" : (network.up === false ? "NO_DATA" : "UNAVAILABLE");
      const traffic = direction => usable(system.status) && network.up === true && F.isFiniteNumber(network[`${direction}BytesPerSec`])
        ? metricText(network[`${direction}BytesPerSec`] / 1024, "KB/s") : "—";
      return `<div class="network-card"><div class="network-card-head"><h3>${F.safe(F.networkLabel(network.name))}</h3>${F.badge(networkStatus, true)}</div><div class="network-addresses"><span>地址（IPv4）${F.safe(ipv4.join(", ") || null)}</span><span>地址（IPv6）${F.safe(ipv6.join(", ") || null)}</span></div><div class="network-stats"><div class="network-stat"><label>链路速度</label><span>${F.isFiniteNumber(network.speedMbps) ? `${F.safe(network.speedMbps)} ${F.escapeHtml(F.unitLabel("Mbps"))}` : "—"}</span></div><div class="network-stat"><label>接收</label><span>${traffic("rx")}</span></div><div class="network-stat"><label>发送</label><span>${traffic("tx")}</span></div></div></div>`;
    }).join("") : '<div class="empty-state">暂无网络接口数据</div>');
  }

  function actualFeedbackBlock(fieldValue, unit) {
    const statusValue = effectiveStatus(fieldValue.state);
    const numeric = F.isFiniteNumber(fieldValue.value) ? fieldValue.value : null;
    const span = numeric === null ? 0 : Math.min(50, Math.abs(numeric) / Math.max(1, Math.abs(numeric)) * 42);
    const left = numeric !== null && numeric < 0 ? 50 - span : 50;
    return `<div class="comparison-value"><header><label>实际反馈</label>${F.badge(statusValue, true)}</header><output>${F.safe(numeric)}${numeric === null ? "" : `<small>${F.escapeHtml(unit)}</small>`}</output><div class="comparison-track"><i style="left:${left}%;width:${span}%"></i></div><div class="comparison-meta"><span>${F.escapeHtml(F.sourceLabel(fieldValue.source ?? "—"))}</span><span>${F.escapeHtml(F.uiLabel(fieldValue.type ?? "ACTUAL"))}</span></div></div>`;
  }

  function actuatorCard(title, objectValue, actualField) {
    const actual = F.field(objectValue.root, actualField);
    const data = objectValue.value || {};
    return `<article class="actuator-card"><header><h3>${F.escapeHtml(title)}</h3>${F.badge(effectiveStatus(actual.state), true)}</header><div class="actuator-readings actual-only"><div class="actuator-reading"><label>实际转速</label><output>${F.safe(actual.value)}</output>${actual.value === null ? "" : "<small>转/分钟</small>"}</div></div><div class="actuator-meta"><div><label>在线状态</label><span>${F.safe(data.online)}</span></div><div><label>故障码</label><span>${F.safe(data.faultCode)}</span></div><div><label>通信丢失</label><span>${F.safe(data.commLost)}</span></div></div></article>`;
  }

  function arucoPreviewCards(preview, elapsedMs = 0) {
    const cameras = [
      ["usbLeft", "前视相机"], ["usbFront", "后视相机"],
      ["usbHub2", "左视相机"], ["usbHub3", "右视相机"], ["color", "深度相机 · 彩色"]
    ];
    const elapsed = F.isFiniteNumber(elapsedMs) && elapsedMs >= 0 ? elapsedMs : Infinity;
    const summaryFresh = preview && effectiveStatus(preview.status) === "AVAILABLE"
      && preview.dictionary === "DICT_5X5_100" && F.isFiniteNumber(preview.ageMs)
      && preview.ageMs >= 0 && preview.ageMs + elapsed < 5000;
    return cameras.map(([key, label]) => {
      const row = preview?.cameras?.[key] || {};
      const idsValid = Array.isArray(row.ids) && row.ids.length <= 12
        && row.ids.every(id => Number.isInteger(id) && id >= 0 && id <= 99);
      const fresh = summaryFresh && effectiveStatus(row.status) === "AVAILABLE"
        && row.dictionary === "DICT_5X5_100" && idsValid && F.isFiniteNumber(row.ageMs)
        && row.ageMs >= 0 && row.ageMs + elapsed < 5000;
      const status = fresh ? "AVAILABLE" : row.status === "ERROR" && summaryFresh ? "ERROR" : "WAITING_UPSTREAM";
      const ids = fresh ? [...new Set(row.ids)].sort((a, b) => a - b) : [];
      return statusCard(label, status,
        fresh ? ids.length ? `ID ${ids.join(" · ")}` : "未发现码" : status === "ERROR" ? "识别异常" : "等待",
        fresh ? `检测中 · ${F.formatAge((row.ageMs + elapsed) / 1000)}` : "—");
    }).join("");
  }

  function renderArucoPreview(snapshot) {
    const elapsed = state.lastMessageAt === null ? Infinity : Math.max(0, performance.now() - state.lastMessageAt);
    setHTML("aruco-preview-cameras", arucoPreviewCards(visionOf(snapshot).arucoPreview, elapsed));
  }

  function renderRobot(snapshot) {
    const telemetry = telemetryOf(snapshot);
    const payload = snapshot.robot && snapshot.robot.data;
    const telemetryStatus = effectiveStatus(telemetry.status);
    const voltage = F.field(payload, "busVoltage");
    const actualVx = F.field(payload, "actualVxMps");
    const actualWz = F.field(payload, "actualWzRadps");
    const leftWheel = F.field(payload, "leftWheelMps");
    const rightWheel = F.field(payload, "rightWheelMps");
    const chassisAlive = payload && typeof payload.chassisAlive === "boolean" ? payload.chassisAlive : null;
    const chassisStatus = telemetryStatus;
    setHTML("robot-summary", `${F.badge(telemetryStatus)}<strong>机器人遥测</strong><span>${F.escapeHtml(F.sourceLabel(snapshot.robot.source))} · ${metricText(telemetry.hz, "Hz")} · ${F.formatAge(telemetry.ageS)}</span>`);
    setHTML("robot-kpis", [
      kpiCard({ label: "母线电压", value: voltage.value, unit: "V", status: voltage.state, series: "voltage", meta: voltage.source, type: "ACTUAL" }),
      kpiCard({ label: "左轮实际速度", value: leftWheel.value, unit: "m/s", status: leftWheel.state, series: "leftWheel", meta: leftWheel.source, type: "ACTUAL" }),
      kpiCard({ label: "右轮实际速度", value: rightWheel.value, unit: "m/s", status: rightWheel.state, series: "rightWheel", meta: rightWheel.source, type: "ACTUAL" }),
      kpiCard({ label: "实际线速度", value: actualVx.value, unit: "m/s", status: actualVx.state, series: "actualVx", meta: actualVx.source, type: "ACTUAL" }),
      kpiCard({ label: "实际角速度", value: actualWz.value, unit: "rad/s", status: actualWz.state, series: "actualWz", meta: actualWz.source, type: "ACTUAL" }),
      kpiCard({ label: "底盘在线状态", value: chassisAlive, status: chassisStatus, meta: snapshot.robot.source, type: "STATUS" })
    ].join(""));
    setHTML("linear-comparison", actualFeedbackBlock(actualVx, "m/s"));
    setHTML("angular-comparison", actualFeedbackBlock(actualWz, "rad/s"));

    const root = payload;
    const brushes = [
      ["前滚刷", "frontRollerState", "frontRollerStateActualRpm"],
      ["后滚刷", "rearRollerState", "rearRollerStateActualRpm"],
      ["左边刷", "leftSideBrushState", "leftSideBrushStateActualRpm"],
      ["右边刷", "rightSideBrushState", "rightSideBrushStateActualRpm"]
    ];
    setHTML("brush-cards", brushes.map(([title, objectKey, actualKey]) => actuatorCard(title, { root, value: payload ? payload[objectKey] : null }, actualKey)).join(""));

    const lifts = [["推杆 1", "lift1State"], ["推杆 2", "lift2State"]];
    setHTML("lift-cards", lifts.map(([title, key]) => {
      const info = F.field(payload, key);
      const lift = payload && payload[key] ? payload[key] : {};
      return `<article class="actuator-card"><header><h3>${title}</h3>${F.badge(effectiveStatus(info.state), true)}</header><div class="actuator-readings"><div class="actuator-reading"><label>霍尔计数</label><output>${F.safe(lift.hallCount)}</output><small>次</small></div><div class="actuator-reading"><label>方向</label><output>${F.safe(F.detailValue(lift.direction, "direction"))}</output></div></div><div class="actuator-meta"><div><label>最小限位</label><span>${F.safe(lift.atMin)}</span></div><div><label>最大限位</label><span>${F.safe(lift.atMax)}</span></div><div><label>数据年龄</label><span>${F.isFiniteNumber(info.ageMs) ? `${F.safe(info.ageMs)} 毫秒` : "—"}</span></div><div><label>字段类型</label><span>${F.safe(F.uiLabel(info.type))}</span></div></div></article>`;
    }).join(""));

    const vision = visionOf(snapshot);
    renderArucoPreview(snapshot);
    const visionTopics = vision.topics || {};
    const detectionTopic = visionTopics.detectionStatus || {};
    const idsTopic = visionTopics.detectedIds || {};
    const poseTopic = visionTopics.boardPose || {};
    const dockingTopic = visionTopics.dockingState || {};
    const ids = Array.isArray(vision.detectedIds) ? vision.detectedIds : null;
    const pose = vision.boardPose && typeof vision.boardPose === "object" ? vision.boardPose : null;
    const x = F.get(pose, "position.xM");
    const y = F.get(pose, "position.yM");
    const z = F.get(pose, "position.zM");
    const yawRad = F.get(pose, "yawRad");
    const yawDeg = F.isFiniteNumber(yawRad) ? yawRad * 180 / Math.PI : null;
    const targetStatus = effectiveStatus(vision.targetStatus);
    const dockingStatus = vision.dockingState === "DISABLED"
      ? "IDLE"
      : effectiveStatus(dockingTopic.status);
    setHTML("vision-docking-summary", [
      statusCard("鱼眼识别接口", vision.status, F.displayStatus(vision.status), `频率 ${metricText(detectionTopic.hz, "Hz")}`),
      statusCard("视觉标记检测", targetStatus, F.deviceStateLabel("detection", vision.detectionState), `编号 ${ids && ids.length ? ids.join(", ") : "—"}`),
      statusCard("目标板位姿", targetStatus, F.isFiniteNumber(x) ? `前向 ${metricText(x, "m", 3)}` : "—", `横向 ${metricText(y, "m", 3)} · 高度 ${metricText(z, "m", 3)}`),
      statusCard("对接控制器", dockingStatus, F.deviceStateLabel("docking", vision.dockingState), `更新 ${F.formatAge(dockingTopic.ageS)}`)
    ].join(""));
    setHTML("vision-docking-details", [
      diagnosticItem("检测状态话题", detectionTopic.topic),
      diagnosticItem("检测状态", F.deviceStateLabel("detection", vision.detectionState)),
      diagnosticItem("标记编号话题", idsTopic.topic),
      diagnosticItem("当前标记编号", ids && ids.length ? ids.join(", ") : null),
      diagnosticItem("目标位姿话题", poseTopic.topic),
      diagnosticItem("坐标系", pose && pose.frameId),
      diagnosticItem("目标 X", F.isFiniteNumber(x) ? `${F.formatValue(x, 3)} m` : null),
      diagnosticItem("目标 Y", F.isFiniteNumber(y) ? `${F.formatValue(y, 3)} m` : null),
      diagnosticItem("目标 Z", F.isFiniteNumber(z) ? `${F.formatValue(z, 3)} m` : null),
      diagnosticItem("目标偏航角", F.isFiniteNumber(yawDeg) ? `${F.formatValue(yawDeg, 1)}°` : null),
      diagnosticItem("对接状态话题", dockingTopic.topic),
      diagnosticItem("对接状态", F.deviceStateLabel("docking", vision.dockingState))
    ].join(""));
  }

  // Presentation follows received data; protocol freshness and fault history remain unchanged.
  function sensorHasData(item) {
    const present = value => {
      if (value === null || value === undefined) return false;
      if (typeof value === "number") return Number.isFinite(value);
      if (typeof value === "boolean") return true;
      if (typeof value === "string") return value.trim().length > 0;
      if (Array.isArray(value)) return value.some(present);
      return typeof value === "object" && Object.values(value).some(present);
    };
    return present(item?.value);
  }

  function sensorPresentationStatus(item) {
    const status = F.normalizeStatus(item?.status);
    if (status === "ERROR" || status === "UNAVAILABLE" || status === "IDLE") return status;
    if (sensorHasData(item)) return "AVAILABLE";
    return status === "STALE" ? "WAITING_UPSTREAM" : status;
  }

  function renderSensors(snapshot) {
    const sensors = Object.fromEntries(Object.entries(F.get(snapshot, "devices.sensors") || {})
      .filter(([key, item]) => !RETIRED_SENSOR_KEYS.has(key) && !RETIRED_SENSOR_TOPICS.has(item?.topic)));
    const cameras = F.get(snapshot, "devices.cameras") || {};
    Object.entries(cameras).filter(([key, camera]) => !RETIRED_SENSOR_KEYS.has(key) && !RETIRED_SENSOR_TOPICS.has(camera?.topic)).forEach(([key, camera]) => {
      sensors[key] = { ...camera, label: `${F.deviceLabel(camera.label || key)}（${F.sourceLabel(camera.source || "来源待确认")}）`, hz: camera.previewHz ?? camera.hz,
        value: F.isFiniteNumber(camera.width) && camera.width > 0 && F.isFiniteNumber(camera.height) && camera.height > 0
          ? { width: camera.width, height: camera.height, encoding: camera.encoding } : null };
    });
    const sensorLabels = { lidar: "MID360 雷达", bluesea: "蓝海二维雷达", scan: "二维雷达扫描",
      imu: "惯性测量单元（IMU）", odometry: "定位里程计", map: "静态地图" };
    setHTML("sensor-overview", ["lidar", "bluesea", "scan", "imu", "odometry", "map"].map(key => {
      const item = sensors[key] || {};
      const status = sensorPresentationStatus(item);
      return statusCard(F.deviceLabel(item.label || sensorLabels[key]), status, F.displayStatus(status),
        item.updateMode === "event" ? "按需更新" : `实收 ${metricText(item.hz, "Hz")} · ${F.formatAge(item.ageS)}`, true);
    }).join(""));
    setHTML("sensor-health", table(["设备 / 模块", "状态", "实收频率", "距上次更新", "数据摘要"],
      Object.values(sensors).map(item => {
        const status = sensorPresentationStatus(item);
        const value = status === "AVAILABLE" && sensorHasData(item) ? item.value : null;
        const summary = value ? Object.entries(value).map(([key, v]) => {
          const text = Array.isArray(v) ? v.map(x => x && typeof x === "object"
            ? `${F.detailValue(x.name, "name") ?? "诊断项"}：${F.detailValue(x.message, "message") ?? "—"}`
            : F.formatValue(F.detailValue(x, key), 3)).join(" / ") : F.formatValue(F.detailValue(v, key), 3);
          return `${F.detailLabel(key)}：${text}`;
        }).join(" · ") : "—";
        return [F.safe(F.deviceLabel(item.label)), F.badge(status),
          item.updateMode === "event" ? "按需更新" : metricText(item.hz, "Hz"),
          F.escapeHtml(F.formatAge(item.ageS)), F.escapeHtml(summary)];
      })));
  }

  function renderSTM32(snapshot) {
    const telemetry = telemetryOf(snapshot);
    const payload = snapshot.robot && snapshot.robot.data;
    const serial = payload && payload.serialDebug ? payload.serialDebug : {};
    const pages = snapshot.pages || {};
    const availableRates = Object.values(pages).filter(page => usable(page.status) && F.isFiniteNumber(page.hz)).map(page => page.hz);
    const pageRate = availableRates.length ? availableRates.reduce((sum, value) => sum + value, 0) : null;
    const ages = Object.values(pages).filter(page => usable(page.status) && F.isFiniteNumber(page.ageMs)).map(page => page.ageMs);
    const lastAge = ages.length ? Math.min(...ages) : null;
    const serialValue = key => Object.prototype.hasOwnProperty.call(serial, key) ? serial[key] : null;
    const serialStatus = usable(telemetry.status) ? "AVAILABLE" : telemetry.status;
    setHTML("stm32-kpis", [
      kpiCard({ label: "机器人遥测", value: telemetry.hz, unit: "Hz", status: telemetry.status, series: "telemetryHz", meta: snapshot.robot.source, type: telemetry.transport || "ROS2" }),
      kpiCard({ label: "数据页总频率", value: pageRate, unit: "Hz", status: pageRate === null ? telemetry.status : "AVAILABLE", meta: "", type: "DERIVED" }),
      kpiCard({ label: "有效帧", value: serialValue("validFrameCount"), unit: "帧", status: serialValue("validFrameCount") === null ? "UNAVAILABLE" : serialStatus, meta: telemetry.transport === "LOCAL_WS_RX" ? "机器人采集端接收计数" : "上游进程累计值", type: "DERIVED" }),
      kpiCard({ label: "校验错误", value: serialValue("checksumErrorCount"), status: serialValue("checksumErrorCount") === null ? "UNAVAILABLE" : serialStatus, meta: "", type: "DERIVED" }),
      kpiCard({ label: "无效帧", value: serialValue("invalidFrameCount"), status: serialValue("invalidFrameCount") === null ? "UNAVAILABLE" : serialStatus, meta: "", type: "DERIVED" }),
      kpiCard({ label: "距最近数据页", value: lastAge, unit: "ms", status: lastAge === null ? telemetry.status : "AVAILABLE", meta: "", type: "DERIVED" })
    ].join(""));

    const pageOrder = ["0x00", "0x10", "0x11", "0x12", "0x13"];
    setHTML("page-health", pageOrder.map(key => {
      const page = pages[key] || { name: null, count: null, hz: null, ageMs: null, status: "WAITING_UPSTREAM" };
      const pageStatus = effectiveStatus(page.status);
      const pageLabel = F.PAGE_LABELS[key] || { protocol: page.name, description: "—" };
      return `<article class="page-card ${pageStatus}"><div class="page-card-head"><code>数据页 ${key}</code>${F.badge(pageStatus, true)}</div><h3>${F.safe(pageLabel.protocol)}<span>${F.safe(pageLabel.description)}</span></h3><div class="page-card-stats"><div class="page-card-stat"><label>帧计数</label><span>${F.safe(page.count, 0)}</span></div><div class="page-card-stat"><label>频率</label><span>${metricText(page.hz, "Hz")}</span></div><div class="page-card-stat"><label>数据年龄</label><span>${F.isFiniteNumber(page.ageMs) ? `${F.safe(page.ageMs)} 毫秒` : "—"}</span></div></div></article>`;
    }).join(""));
    const diagnosticKeys = ["actualOpenDevice", "baud", "portOpen", "rxActive", "frameValid", "validFrameCount", "invalidFrameCount", "checksumErrorCount", "validFramesPerSec"];
    setHTML("serial-diagnostics", diagnosticKeys.map((key, index) => diagnosticItem(key, serialValue(key), index === diagnosticKeys.length - 1)).join("") + diagnosticItem("conclusion", serialValue("conclusion"), true));
  }

  function renderROS(snapshot) {
    const ros = snapshot.ros2 || {};
    const telemetry = telemetryOf(snapshot);
    const rosStatus = effectiveStatus(ros.status);
    const telemetryStatus = effectiveStatus(telemetry.status);
    const topics = ros.topics && typeof ros.topics === "object"
      ? Object.values(ros.topics).filter(topic => !RETIRED_SENSOR_TOPICS.has(topic?.topic)) : [];
    const sensorTopics = new Set([
      ...Object.values(F.get(snapshot, "devices.sensors") || {}),
      ...Object.values(F.get(snapshot, "devices.cameras") || {})
    ].map(item => item?.topic).filter(Boolean));
    const trackedCount = topics.length;
    const conflicts = topics.filter(topic => topic && topic.contractConflict);
    setHTML("ros-page-state", `${F.badge(rosStatus)}<strong>通信域 ${F.safe(ros.domainId)}</strong><span>${F.safe(F.detailValue(ros.rmwImplementation, "rmwImplementation"))}</span>`);
    setHTML("ros-kpis", [
      statusCard("话题通信状态", rosStatus, F.displayStatus(rosStatus, "ros2"), `通信域 ${F.formatValue(ros.domainId)}`),
      statusCard("机器人遥测", telemetryStatus, F.displayStatus(telemetryStatus, "telemetry"), telemetry.transport === "LOCAL_WS_RX" ? "机器人端上行广播（非 ROS 话题）" : `发布者 ${F.formatValue(telemetry.publisherCount, 0)} 个`),
      statusCard("监测话题", "AVAILABLE", `${F.formatValue(trackedCount, 0)} 个`, ""),
      statusCard("话题类型检查",
        conflicts.length ? "ERROR" : (topics.some(topic => Array.isArray(topic.observedTypes) && topic.observedTypes.length) ? "AVAILABLE" : "WAITING_UPSTREAM"),
        conflicts.length ? "类型冲突" : (topics.some(topic => Array.isArray(topic.observedTypes) && topic.observedTypes.length) ? "已验证" : "未完成"),
        conflicts.length ? `${conflicts.length} 个冲突` : "")
    ].join(""));
    setText("ros-hz-now", metricText(telemetry.hz, "Hz"));
    setText("ros-age-now", F.formatAge(telemetry.ageS));
    setHTML("topic-health", table(
      ["ROS2 话题", "消息类型", "发布者", "订阅者", "频率 Hz", "数据年龄", "状态"],
      topics.map(topic => [
        F.safe(topic.topic), F.safe(F.detailValue(topic.topicType, "topicType")), F.safe(topic.publisherCount, 0), "观测端",
        F.safe(topic.hz), F.escapeHtml(F.formatAge(topic.ageS)),
        F.badge(sensorTopics.has(topic.topic) ? sensorPresentationStatus(topic) : effectiveStatus(topic.status))
      ])
    ));
    const observedTypes = topics.flatMap(topic => Array.isArray(topic.observedTypes) ? topic.observedTypes : []);
    setHTML("dds-environment", [
      diagnosticItem("ROS_DOMAIN_ID", ros.domainId), diagnosticItem("RMW_IMPLEMENTATION", ros.rmwImplementation),
      diagnosticItem("ROS_LOCALHOST_ONLY", ros.localhostOnly), diagnosticItem("ROS_DISCOVERY_SERVER", ros.discoveryServer),
      diagnosticItem("Observed Types", observedTypes.length ? [...new Set(observedTypes)].join(", ") : null),
      diagnosticItem("Parse Errors", telemetry.parseErrorCount),
      diagnosticItem("Type Contract Conflict", conflicts.length ? conflicts.map(topic => topic.contractConflict).join("；") : null, true)
    ].join(""));
  }

  function renderSnapshot(snapshot, ingest = true) {
    if (!snapshot || !snapshot.meta) return;
    state.snapshot = snapshot;
    if (ingest) {
      state.lastMessageAt = performance.now();
      recordStateChanges(snapshot);
      recordHistory(snapshot);
    }
    renderHeader(snapshot);
    renderOverview(snapshot);
    renderSystem(snapshot);
    renderRobot(snapshot);
    renderSensors(snapshot);
    renderSTM32(snapshot);
    renderROS(snapshot);
    if (global.CloudObserver) global.CloudObserver.update(snapshot, !disconnectedTooLong());
    // Cached UI redraws (connection changes / clock) are not new observations.
    if (ingest && global.ObserverMedia) global.ObserverMedia.update(snapshot);
    const now = F.get(snapshot, "meta.timestamp");
    if (F.isFiniteNumber(now)) requestAnimationFrame(() => Charts.drawAll(historyStore, now, state.windowSeconds));
  }

  function applyViewMode(mode) {
    state.viewMode = mode;
    document.body.dataset.view = mode;
    document.querySelectorAll("[data-view-mode]").forEach(button => {
      const active = button.dataset.viewMode === mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    savePreference("observer.view", mode);
    if (state.snapshot) requestAnimationFrame(() => Charts.drawAll(historyStore, state.snapshot.meta.timestamp, state.windowSeconds));
  }

  function applyWindow(seconds) {
    if (![60, 120, 300].includes(seconds)) return;
    state.windowSeconds = seconds;
    document.querySelectorAll("[data-window]").forEach(button => {
      const active = Number(button.dataset.window) === seconds;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    savePreference("observer.window", seconds);
    if (state.snapshot) requestAnimationFrame(() => Charts.drawAll(historyStore, state.snapshot.meta.timestamp, state.windowSeconds));
  }

  function hasSubpages(page) {
    return Object.prototype.hasOwnProperty.call(SUBPAGES, page);
  }

  function mobileNavigation() {
    return typeof global.matchMedia === "function"
      ? global.matchMedia("(max-width: 760px)").matches
      : global.innerWidth <= 760;
  }

  function focusSubpagePicker(page) {
    document.querySelectorAll(".subpage-picker[data-subpage-picker]").forEach(picker => {
      if (picker.dataset.subpagePicker === page) picker.focus();
    });
  }

  function resolveRoute(name) {
    // Accept only known route segments. URL text must never become a selector or HTML.
    // Keep previously bookmarked communication pages under their new parent.
    const legacyRoutes = { stm32: "system/stm32", ros: "system/ros" };
    const route = typeof name === "string" ? name : "";
    const canonical = Object.prototype.hasOwnProperty.call(legacyRoutes, route) ? legacyRoutes[route] : route;
    const parts = canonical.split("/");
    const page = parts[0];
    if (!PAGE_NAMES.has(page) || parts.length > 2) return { page: "overview", subpage: null, route: "overview" };
    if (!hasSubpages(page)) {
      return parts.length === 1 ? { page, subpage: null, route: page } : { page: "overview", subpage: null, route: "overview" };
    }
    const subpage = parts.length === 1 ? lastSubpages[page] : parts[1];
    if (!Object.prototype.hasOwnProperty.call(SUBPAGES[page], subpage)) return { page: "overview", subpage: null, route: "overview" };
    return { page, subpage, route: `${page}/${subpage}` };
  }

  function renderNavigation() {
    const current = state.route;
    if (!current) return;
    const mobile = mobileNavigation();
    document.querySelectorAll(".nav").forEach(button => {
      const active = button.dataset.page === current.page;
      button.classList.toggle("active", active);
      if (active && !hasSubpages(current.page)) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    document.querySelectorAll(".nav-group").forEach(group => {
      const page = group.dataset.navGroup;
      const expanded = !mobile && page === state.expandedGroup;
      group.classList.toggle("active", page === current.page);
      group.classList.toggle("expanded", expanded);
      const button = group.querySelector(".nav");
      const menu = group.querySelector(".subnav");
      if (button) button.setAttribute("aria-expanded", String(expanded));
      if (menu) menu.hidden = !expanded;
    });
    document.querySelectorAll(".subnav-button[data-route]").forEach(button => {
      const active = button.dataset.route === current.route;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }

  function setExpandedGroup(page) {
    state.expandedGroup = hasSubpages(page) ? page : null;
    renderNavigation();
  }

  function openPage(name, updateHash = true) {
    const next = resolveRoute(name);
    const nextHash = `#${next.route}`;
    if (global.location.hash !== nextHash) {
      // Clicks add history entries; direct/legacy URLs are canonicalized in place.
      global.history[updateHash ? "pushState" : "replaceState"](null, "", nextHash);
    }
    if (state.route && state.route.route === next.route) return next.route;
    state.route = next;
    if (next.subpage) {
      lastSubpages[next.page] = next.subpage;
      savePreference(`observer.subpage.${next.page}`, next.subpage);
    }
    document.querySelectorAll(".page").forEach(page => {
      const active = page.dataset.pageName === next.page;
      page.classList.toggle("active", active);
      page.hidden = !active;
      page.querySelectorAll(".subpage[data-subpage]").forEach(subpage => {
        const selected = active && subpage.dataset.subpage === next.subpage;
        subpage.hidden = !selected;
        subpage.classList.toggle("active", selected);
      });
    });
    Object.entries(SUBPAGES).forEach(([page, children]) => {
      setText(`${page}-subpage-title`, children[lastSubpages[page]]);
    });
    document.querySelectorAll(".subpage-picker[data-subpage-picker]").forEach(picker => {
      if (hasSubpages(picker.dataset.subpagePicker)) picker.value = lastSubpages[picker.dataset.subpagePicker];
    });
    setExpandedGroup(next.subpage ? next.page : null);
    // Hide/show the existing nodes so chart history stays intact.
    global.dispatchEvent(new CustomEvent("observer-page-change", { detail: { ...next } }));
    if (state.snapshot) requestAnimationFrame(() => Charts.drawAll(historyStore, state.snapshot.meta.timestamp, state.windowSeconds));
    if (updateHash) global.scrollTo(0, 0);
    return next.route;
  }

  function observationsWanted() {
    return !state.authExpired && !state.pageSuspended && !document.hidden;
  }

  function clearObservationRetry() {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }

  function detachObservationSocket() {
    const socket = state.socket;
    // A half-open socket need not deliver close. Detach before closing so late
    // events from it cannot clear or refresh its replacement.
    state.socket = null;
    state.connected = false;
    state.socketStartedAt = state.socketMessageAt = null;
    if (socket) {
      try { socket.close(); } catch (_) { /* CONNECTING may already be closing. */ }
    }
  }

  function pauseObservations() {
    clearObservationRetry();
    state.observationEpoch += 1;
    state.observationResume?.cancel();
    state.observationResume = null;
    detachObservationSocket();
    state.nativeMetricsRequest?.abort();
  }

  function scheduleObservationRetry() {
    clearObservationRetry();
    if (!observationsWanted()) return;
    const epoch = state.observationEpoch;
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      if (epoch === state.observationEpoch) void resumeObservations();
    }, 2000);
  }

  async function resumeObservations(sessionReady) {
    if (!observationsWanted() || state.observationResume) return;
    if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) return;
    clearObservationRetry();
    const pending = { epoch: state.observationEpoch, timeout: null, cancel: null };
    state.observationResume = pending;
    let valid = null;
    try {
      // checkSession normally aborts after five seconds. Bound our wait too so
      // a stalled check cannot lock all future online / watchdog recovery.
      const deadline = new Promise(resolve => {
        pending.cancel = () => { clearTimeout(pending.timeout); resolve(null); };
        pending.timeout = setTimeout(pending.cancel, 5500);
      });
      valid = await Promise.race([sessionReady ?? (global.CloudObserver ? global.CloudObserver.checkSession() : true), deadline]);
    } catch (_) { /* A failed network check is not an authentication rejection. */ }
    finally { clearTimeout(pending.timeout); }
    if (state.observationResume !== pending || pending.epoch !== state.observationEpoch) return;
    state.observationResume = null;
    if (valid !== false && observationsWanted()) connectWebSocket();
  }

  function retryObservationSocket(socket) {
    if (state.socket !== socket) return;
    const wasConnected = state.connected;
    detachObservationSocket();
    if (wasConnected) recordEvent("WebSocket", "STALE", "AVAILABLE");
    if (state.snapshot) renderSnapshot(state.snapshot, false);
    scheduleObservationRetry();
  }

  function observationSocketStalled(timeout = 10000) {
    const last = state.socketMessageAt ?? state.socketStartedAt;
    return state.socket !== null && last !== null && performance.now() - last >= timeout;
  }

  function watchObservationSocket() {
    // Use the existing page clock, not another perpetual timer. This also
    // covers CONNECTING, no first snapshot, and sockets receiving only garbage.
    if (observationsWanted() && observationSocketStalled()) retryObservationSocket(state.socket);
  }

  function recoverObservationsOnline() {
    if (!observationsWanted()) return;
    if (state.socket) {
      const connecting = state.socket.readyState === WebSocket.CONNECTING;
      const open = state.socket.readyState === WebSocket.OPEN;
      if ((connecting || open) && !observationSocketStalled(connecting ? 10000 : 3500)) return;
      retryObservationSocket(state.socket);
    }
    // A network-return event bypasses the two-second retry delay, but repeated
    // events share one session check / connection attempt.
    void resumeObservations();
  }

  function connectWebSocket() {
    if (!observationsWanted()) return;
    if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) return;
    clearObservationRetry();
    const protocol = global.location.protocol === "https:" ? "wss:" : "ws:";
    let socket;
    try { socket = new WebSocket(`${protocol}//${global.location.host}${global.RobotFleet?.socketUrl("/ws") || "/ws"}`); }
    catch (_) { scheduleObservationRetry(); return; }
    state.socket = socket;
    state.socketStartedAt = performance.now();
    state.socketMessageAt = null;
    socket.addEventListener("open", () => {
      if (state.socket !== socket || !observationsWanted()) { socket.close(); return; }
      state.connected = true;
      if (state.everConnected) recordEvent("WebSocket", "AVAILABLE", "STALE");
      state.everConnected = true;
      if (state.snapshot) renderSnapshot(state.snapshot, false);
    });
    socket.addEventListener("message", event => {
      if (state.socket !== socket || !observationsWanted()) return;
      try {
        const snapshot = JSON.parse(event.data);
        if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)
            || !snapshot.meta || typeof snapshot.meta !== "object" || Array.isArray(snapshot.meta)
            || !F.isFiniteNumber(snapshot.meta.timestamp)) {
          recordEvent("WebSocket payload", "ERROR");
          return;
        }
        renderSnapshot(snapshot, true);
        state.socketMessageAt = performance.now();
      } catch (_) {
        recordEvent("WebSocket payload", "ERROR");
      }
    });
    socket.addEventListener("close", () => retryObservationSocket(socket));
    socket.addEventListener("error", () => retryObservationSocket(socket));
  }

  function updateClock() {
    watchObservationSocket();
    setText("clock", new Date().toLocaleString("zh-CN", { hour12: false }));
    if (state.snapshot && disconnectedTooLong()) renderSnapshot(state.snapshot, false);
    else if (state.snapshot) renderArucoPreview(state.snapshot);
  }

  function receiveNativeMetrics(value, requestStarted = performance.now()) {
    if (!value || typeof value.enabled !== "boolean" || !F.isFiniteNumber(requestStarted)) return;
    state.nativeMetrics = { value, deadline: requestStarted + 5000 };
  }

  async function pollOverviewMetrics() {
    // Read source counters only. A visit to overview never opens video streams.
    if (state.authExpired || state.route?.page !== "overview" || document.hidden || state.nativeMetricsRequest) return;
    const controller = new AbortController(), requestStarted = performance.now();
    state.nativeMetricsRequest = controller;
    const timeout = setTimeout(() => controller.abort(), 3000);
    try {
      const response = await scopedFetch("/native-camera/status", { cache: "no-store", signal: controller.signal });
      if (controller.signal.aborted || !observationsWanted() || state.route?.page !== "overview") {
        await response.body?.cancel(); return;
      }
      if (response.status === 401 || response.status === 403) { global.CloudObserver?.unauthorized(); return; }
      if (!response.ok) throw new Error("Source metrics unavailable");
      const value = await response.json();
      if (!controller.signal.aborted && state.route?.page === "overview" && !document.hidden) receiveNativeMetrics(value, requestStarted);
    } catch (_) {
      if (!controller.signal.aborted) state.nativeMetrics = null;
    } finally {
      clearTimeout(timeout);
      if (state.nativeMetricsRequest === controller) state.nativeMetricsRequest = null;
    }
  }

  global.addEventListener("observer-native-metrics", event => receiveNativeMetrics(event.detail?.value, event.detail?.requestStarted));
  global.addEventListener("observer-page-change", () => {
    state.nativeMetricsRequest?.abort();
    pollOverviewMetrics();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) pauseObservations();
    else { void resumeObservations(); pollOverviewMetrics(); }
  });
  global.addEventListener("pagehide", () => {
    state.pageSuspended = true;
    pauseObservations();
  });
  global.addEventListener("online", recoverObservationsOnline);
  global.addEventListener("pageshow", () => {
    state.pageSuspended = false;
    void resumeObservations();
  });
  global.addEventListener("cloud-auth-lost", () => {
    state.nativeMetrics = null;
    state.nativeMetricsRequest?.abort();
    clearInterval(nativeMetricsTimer);
  });
  const nativeMetricsTimer = setInterval(pollOverviewMetrics, 2000);

  document.querySelectorAll(".nav").forEach(button => button.addEventListener("click", () => {
    const page = button.dataset.page;
    if (hasSubpages(page) && state.route && state.route.page === page) {
      if (mobileNavigation()) focusSubpagePicker(page);
      else setExpandedGroup(state.expandedGroup === page ? null : page);
    } else {
      openPage(page);
    }
  }));
  document.querySelectorAll(".subnav-button[data-route]").forEach(button => button.addEventListener("click", () => openPage(button.dataset.route)));
  document.querySelectorAll(".subpage-picker[data-subpage-picker]").forEach(picker => picker.addEventListener("change", () => {
    openPage(`${picker.dataset.subpagePicker}/${picker.value}`);
  }));
  document.querySelectorAll(".nav-group").forEach(group => group.addEventListener("keydown", event => {
    const button = group.querySelector(".nav");
    if (mobileNavigation()) {
      if (event.key === "ArrowDown" && event.target === button) {
        event.preventDefault();
        openPage(group.dataset.navGroup);
        focusSubpagePicker(group.dataset.navGroup);
      }
      return;
    }
    if (event.key === "Escape" && state.expandedGroup === group.dataset.navGroup) {
      event.preventDefault();
      setExpandedGroup(null);
      if (button) button.focus();
    } else if (event.key === "ArrowDown" && event.target === button) {
      event.preventDefault();
      setExpandedGroup(group.dataset.navGroup);
      const selected = group.querySelector('.subnav-button[aria-current="page"]') || group.querySelector(".subnav-button");
      if (selected) selected.focus();
    }
  }));
  document.querySelectorAll("[data-window]").forEach(button => button.addEventListener("click", () => applyWindow(Number(button.dataset.window))));
  document.querySelectorAll("[data-view-mode]").forEach(button => button.addEventListener("click", () => applyViewMode(button.dataset.viewMode)));
  global.addEventListener("resize", () => {
    // Keep ARIA/hidden in sync with the mobile picker, preserving desktop expansion.
    renderNavigation();
    if (state.snapshot) requestAnimationFrame(() => Charts.drawAll(historyStore, state.snapshot.meta.timestamp, state.windowSeconds));
  });
  global.addEventListener("hashchange", () => openPage(global.location.hash.slice(1), false));
  global.addEventListener("popstate", () => openPage(global.location.hash.slice(1), false));
  global.addEventListener("cloud-auth-lost", () => {
    state.authExpired = true;
    pauseObservations();
  });

  applyViewMode(state.viewMode);
  applyWindow(state.windowSeconds);
  openPage(global.location.hash.slice(1), false);
  updateClock();
  setInterval(updateClock, 1000);
  if (global.CloudObserver) void resumeObservations(global.CloudObserver.ready);
  else connectWebSocket();

  global.RKObserverDashboard = Object.freeze({
    getConnection: () => state.socket,
    getHistoryCount: name => historyStore.pointCount(name),
    getViewMode: () => state.viewMode,
    getRoute: () => state.route && state.route.route,
    openPage
  });
})(window);
