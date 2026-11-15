"use strict";
// Detection metadata never opens cameras, delays a video frame, or uploads pixels.
(function (global) {
  const scopedFetch = (...args) => global.RobotFleet ? global.RobotFleet.fetch(...args) : fetch(...args);
  const KEYS = ["usbFront", "usbLeft", "usbHub3", "usbHub2", "color"];
  const MAX_AGE_MS = 5000, MAX_VIDEO_GAP = .6, POLL_MS = 500;
  const SESSION = /^[a-f0-9]{32}$/, IMAGE = /^[a-f0-9]{64}$/;
  const LABELS = ["人", "自行车", "汽车", "摩托车", "飞机", "公交车", "火车", "卡车", "船", "交通信号灯",
    "消防栓", "停车标志", "停车计费器", "长椅", "鸟", "猫", "狗", "马", "羊", "牛", "大象", "熊", "斑马", "长颈鹿",
    "背包", "雨伞", "手提包", "领带", "行李箱", "飞盘", "双板滑雪板", "单板滑雪板", "运动球", "风筝", "棒球棒", "棒球手套",
    "滑板", "冲浪板", "网球拍", "瓶子", "高脚杯", "杯子", "叉子", "刀", "勺子", "碗", "香蕉", "苹果", "三明治", "橙子",
    "西兰花", "胡萝卜", "热狗", "披萨", "甜甜圈", "蛋糕", "椅子", "沙发", "盆栽", "床", "餐桌", "马桶", "电视",
    "笔记本电脑", "鼠标", "遥控器", "键盘", "手机", "微波炉", "烤箱", "烤面包机", "水槽", "冰箱", "书", "时钟", "花瓶",
    "剪刀", "泰迪熊", "吹风机", "牙刷"];
  const states = new Map(), leases = new Map(), retiredOrders = new Map();
  let options = null, pending = null, generation = 0, nextPoll = 0, detectorSession = null, failedPolls = 0;
  const finite = (value, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) =>
    typeof value === "number" && Number.isFinite(value) && value >= minimum && value <= maximum;
  const record = value => !!value && typeof value === "object" && !Array.isArray(value);
  function contain(width, height, sourceWidth, sourceHeight) {
    if (![width, height, sourceWidth, sourceHeight].every(value => finite(value, Number.EPSILON))) return null;
    const scale = Math.min(width / sourceWidth, height / sourceHeight);
    return { x: (width - sourceWidth * scale) / 2, y: (height - sourceHeight * scale) / 2,
      width: sourceWidth * scale, height: sourceHeight * scale };
  }
  function cleanMarkers(value) {
    if (value === undefined) return [];
    if (!Array.isArray(value) || value.length > 12) return null;
    const markers = [];
    for (const marker of value) {
      if (!record(marker) || marker.dictionary !== "DICT_5X5_100" ||
        !Number.isInteger(marker.id) || !finite(marker.id, 0, 99) ||
        !Array.isArray(marker.corners) || marker.corners.length !== 4 ||
        !marker.corners.every(point => Array.isArray(point) && point.length === 2 &&
          point.every(coordinate => finite(coordinate, 0, 1)))) return null;
      const corners = marker.corners.map(point => [...point]);
      let direction = 0;
      for (let index = 0; index < 4; index++) {
        const [a, b, c] = [corners[index], corners[(index + 1) % 4], corners[(index + 2) % 4]];
        const cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]);
        if (Math.abs(cross) <= 1e-10 || direction && Math.sign(cross) !== direction) return null;
        direction = Math.sign(cross);
      }
      markers.push({ dictionary: marker.dictionary, id: marker.id, corners });
    }
    return markers;
  }
  function cleanResult(raw, key, session, started, knownLeases, retired) {
    if (!record(raw) || typeof raw.id !== "string" || !raw.id.startsWith(session + ":")) return null;
    const counter = raw.id.slice(33);
    if (!/^[1-9][0-9]{0,15}$/.test(counter) || !Number.isSafeInteger(Number(counter)) ||
      !finite(raw.ageMs, 0, MAX_AGE_MS) || raw.ageMs >= MAX_AGE_MS ||
      !Number.isInteger(raw.width) || !finite(raw.width, 1, 8192) ||
      !Number.isInteger(raw.height) || !finite(raw.height, 1, 8192) ||
      !Array.isArray(raw.boxes) || raw.boxes.length > 12) return null;
    if (key === "color" ? typeof raw.imageId !== "string" || !IMAGE.test(raw.imageId) ||
        raw.streamSession !== null || raw.mediaTime !== null :
        typeof raw.streamSession !== "string" || !SESSION.test(raw.streamSession) ||
        !finite(raw.mediaTime) || raw.imageId !== null) return null;
    const boxes = [];
    for (const box of raw.boxes) {
      if (!record(box) || !Number.isInteger(box.classId) || !finite(box.classId, 0, LABELS.length - 1) ||
        !finite(box.confidence, 0, 1) || !Array.isArray(box.xyxy) || box.xyxy.length !== 4 ||
        !box.xyxy.every(value => finite(value, 0, 1)) ||
        box.xyxy[0] >= box.xyxy[2] || box.xyxy[1] >= box.xyxy[3]) return null;
      boxes.push({ classId: box.classId, confidence: box.confidence, xyxy: [...box.xyxy] });
    }
    const markers = cleanMarkers(raw.markers);
    if (!markers || Number(counter) <= (retired.get(key) ?? 0)) return null;
    const item = { id: raw.id, order: Number(counter), streamSession: raw.streamSession,
      mediaTime: raw.mediaTime, imageId: raw.imageId, width: raw.width, height: raw.height, boxes, markers };
    const fingerprint = JSON.stringify(item), previous = knownLeases.get(key + ":" + item.id);
    // A repeated result cannot obtain a new lease or change its coordinates.
    if (previous && previous.fingerprint !== fingerprint) return null;
    item.deadline = Math.min(started + MAX_AGE_MS - raw.ageMs, previous?.deadline ?? Infinity);
    knownLeases.set(key + ":" + item.id, { deadline: item.deadline, fingerprint, key, order: item.order });
    return item;
  }
  function normalize(value, started, knownLeases = new Map(), retired = new Map()) {
    if (!record(value) || value.version !== 1 || !record(value.cameras) ||
      !(value.session === null || typeof value.session === "string" && SESSION.test(value.session)))
      throw new Error("Invalid detection metadata");
    const rows = {};
    for (const key of KEYS) {
      const row = value.cameras[key];
      rows[key] = [];
      if (!value.session || !record(row) || row.status !== "AVAILABLE" ||
        !Array.isArray(row.results) || row.results.length > 10) continue;
      const identities = new Set();
      for (const raw of row.results) {
        const item = cleanResult(raw, key, value.session, started, knownLeases, retired);
        if (item && !identities.has(item.id)) { rows[key].push(item); identities.add(item.id); }
      }
    }
    return { session: value.session, rows };
  }
  function select(results, frame, now) {
    if (!frame || !finite(now)) return null;
    const color = frame.kind === "image";
    if (color ? !IMAGE.test(frame.imageId || "") || !finite(frame.deadline) || now >= frame.deadline :
      !SESSION.test(frame.streamSession || "") || !finite(frame.mediaTime) ||
      !finite(frame.presentedAt) || now - frame.presentedAt >= 1000) return null;
    let selected = null;
    for (const item of results) {
      if (now >= item.deadline) continue;
      if (color ? item.imageId !== frame.imageId : item.streamSession !== frame.streamSession ||
        item.mediaTime > frame.mediaTime + 1e-6 || frame.mediaTime - item.mediaTime > MAX_VIDEO_GAP + 1e-6) continue;
      if (!selected || (!color && item.mediaTime > selected.mediaTime) ||
        (color || item.mediaTime === selected.mediaTime) && item.order > selected.order) selected = item;
    }
    return selected; // Empty detections are an actual result, not a fallback to older overlays.
  }
  function paintBoxes(context, rectangle, boxes) {
    if (!rectangle || rectangle.width <= 0 || rectangle.height <= 0 || !boxes.length) return;
    context.save();
    context.beginPath(); context.rect(rectangle.x, rectangle.y, rectangle.width, rectangle.height); context.clip();
    const fontSize = Math.max(11, Math.min(16, rectangle.width / 32));
    context.font = `600 ${fontSize}px sans-serif`; context.textBaseline = "top";
    context.lineWidth = Math.max(1.5, Math.min(2.5, rectangle.width / 320));
    for (const box of boxes) {
      const [x1, y1, x2, y2] = box.xyxy;
      const x = rectangle.x + x1 * rectangle.width, y = rectangle.y + y1 * rectangle.height;
      const width = (x2 - x1) * rectangle.width, height = (y2 - y1) * rectangle.height;
      const color = box.classId === 0 ? "#76f1c4" : "#7fddff";
      context.strokeStyle = color; context.strokeRect(x, y, width, height);
      const label = `${LABELS[box.classId]} ${Math.round(box.confidence * 100)}%`;
      const labelWidth = Math.min(rectangle.width, context.measureText(label).width + 10), labelHeight = fontSize + 8;
      const labelX = Math.max(rectangle.x, Math.min(x, rectangle.x + rectangle.width - labelWidth));
      const labelY = Math.max(rectangle.y, y - labelHeight);
      context.fillStyle = "rgba(5,22,31,.88)"; context.fillRect(labelX, labelY, labelWidth, labelHeight);
      context.fillStyle = color; context.fillText(label, labelX + 5, labelY + 4, Math.max(1, labelWidth - 10));
    }
    context.restore();
  }
  function paintMarkers(context, rectangle, markers) {
    if (!rectangle || rectangle.width <= 0 || rectangle.height <= 0 || !markers.length) return;
    context.save();
    context.beginPath(); context.rect(rectangle.x, rectangle.y, rectangle.width, rectangle.height); context.clip();
    const fontSize = Math.max(11, Math.min(16, rectangle.width / 32));
    context.font = `600 ${fontSize}px sans-serif`; context.textBaseline = "top";
    context.lineWidth = Math.max(1.5, Math.min(2.5, rectangle.width / 320));
    const color = "#ffc66d";
    for (const marker of markers) {
      const points = marker.corners.map(([x, y]) => [rectangle.x + x * rectangle.width, rectangle.y + y * rectangle.height]);
      context.beginPath(); context.moveTo(...points[0]);
      for (const point of points.slice(1)) context.lineTo(...point);
      context.closePath(); context.strokeStyle = color; context.stroke();
      const label = `ArUco #${marker.id}`;
      const labelWidth = Math.min(rectangle.width, context.measureText(label).width + 10), labelHeight = fontSize + 8;
      const x = Math.min(...points.map(point => point[0])), y = Math.min(...points.map(point => point[1]));
      const labelX = Math.max(rectangle.x, Math.min(x, rectangle.x + rectangle.width - labelWidth));
      const labelY = Math.max(rectangle.y, y - labelHeight);
      context.fillStyle = "rgba(5,22,31,.88)"; context.fillRect(labelX, labelY, labelWidth, labelHeight);
      context.fillStyle = color; context.fillText(label, labelX + 5, labelY + 4, Math.max(1, labelWidth - 10));
    }
    context.restore();
  }
  function paintResult(context, rectangle, item) {
    paintBoxes(context, rectangle, item.boxes);
    paintMarkers(context, rectangle, item.markers);
  }
  function visible() {
    return !!options && !document.hidden && !global.CloudObserver?.isExpired() && options.isVisible();
  }
  function currentResult(state, now = performance.now()) {
    return visible() && state.frame && !state.frame.source.hidden &&
      options.isDrawable(state.key, state.frame.source) ? select(state.results, state.frame, now) : null;
  }
  function erase(state) {
    state.canvas.getContext("2d").clearRect(0, 0, state.canvas.width, state.canvas.height);
    state.canvas.hidden = true; state.signature = null;
  }
  function render(state) {
    const item = currentResult(state);
    if (!item || !item.boxes.length && !item.markers.length) { if (!state.canvas.hidden) erase(state); return; }
    const viewport = state.canvas.parentElement, source = state.frame.source;
    const width = viewport.clientWidth, height = viewport.clientHeight;
    const rectangle = contain(width, height, source.videoWidth || source.naturalWidth, source.videoHeight || source.naturalHeight);
    if (!rectangle) { erase(state); return; }
    const ratio = Math.min(2, global.devicePixelRatio || 1);
    const signature = `${item.id}:${width}:${height}:${ratio}:${rectangle.width}:${rectangle.height}`;
    if (signature === state.signature && !state.canvas.hidden) return;
    state.canvas.width = Math.max(1, Math.round(width * ratio)); state.canvas.height = Math.max(1, Math.round(height * ratio));
    const context = state.canvas.getContext("2d"); context.setTransform(ratio, 0, 0, ratio, 0, 0);
    paintResult(context, rectangle, item);
    state.canvas.hidden = false; state.signature = signature;
  }
  function clear(key) {
    const state = states.get(key);
    if (state) { state.frame = null; erase(state); }
  }
  function invalidate() {
    generation += 1; pending?.abort(); pending = null; nextPoll = 0; detectorSession = null;
    failedPolls = 0;
    leases.clear(); retiredOrders.clear(); states.forEach(state => { state.results = []; clear(state.key); });
  }
  async function poll() {
    if (!visible() || pending || performance.now() < nextPoll) return;
    const started = performance.now(), epoch = generation, controller = new AbortController();
    pending = controller; nextPoll = started + POLL_MS;
    const timeout = setTimeout(() => controller.abort(), 1500);
    try {
      const response = await scopedFetch("/native-camera/detections", { cache: "no-store", signal: controller.signal });
      if (epoch !== generation || !visible()) { await response.body?.cancel(); return; }
      if (response.status === 401 || response.status === 403) { invalidate(); global.CloudObserver?.unauthorized(); return; }
      if (!response.ok) throw new Error("Detection source unavailable");
      const value = await response.json();
      if (epoch !== generation || !visible()) return;
      if (value.session !== detectorSession) {
        leases.clear(); retiredOrders.clear(); states.forEach(state => { state.results = []; erase(state); });
      }
      const normalized = normalize(value, started, leases, retiredOrders); detectorSession = normalized.session;
      for (const [key, state] of states) { state.results = normalized.rows[key]; render(state); }
      failedPolls = 0;
    } catch (_) {
      if (epoch === generation) {
        states.forEach(state => { state.results = []; erase(state); });
        // A timed-out request already outlived its regular polling interval.
        // Leave the congested video link time to recover before trying again.
        failedPolls = Math.min(failedPolls + 1, 3);
        nextPoll = performance.now() + POLL_MS * 2 ** failedPolls;
      }
    } finally {
      clearTimeout(timeout); if (pending === controller) pending = null;
      const now = performance.now();
      for (const [identity, lease] of leases) if (lease.deadline <= now) {
        retiredOrders.set(lease.key, Math.max(retiredOrders.get(lease.key) || 0, lease.order));
        leases.delete(identity);
      }
      // Source history and lease records have fixed bounds even with malformed polls.
      if (leases.size > 512) {
        for (const lease of leases.values()) retiredOrders.set(lease.key, Math.max(retiredOrders.get(lease.key) || 0, lease.order));
        leases.clear(); states.forEach(state => { state.results = []; erase(state); });
      }
    }
  }
  function attach(settings) {
    if (options) return;
    options = settings;
    KEYS.forEach(key => {
      const source = document.getElementById(`media-${key}`);
      if (!source) return;
      const canvas = document.createElement("canvas");
      canvas.className = "detection-overlay"; canvas.hidden = true; canvas.setAttribute("aria-hidden", "true");
      source.closest(".media-viewport").appendChild(canvas);
      states.set(key, { key, canvas, frame: null, results: [], signature: null });
    });
    setInterval(() => {
      if (!visible()) { if (pending || [...states.values()].some(state => state.frame)) invalidate(); return; }
      poll(); states.forEach(render);
    }, 100);
  }
  function frame(key, value) {
    const state = states.get(key);
    if (!state || !visible()) return;
    state.frame = { ...value, kind: key === "color" ? "image" : "video", presentedAt: performance.now() };
    render(state);
  }
  function paintForSource(source, context, rectangle) {
    const state = states.get(source.id?.replace(/^media-/, ""));
    if (!state || state.frame?.source !== source) return;
    const item = currentResult(state);
    if (item) paintResult(context, rectangle, item);
  }
  global.addEventListener("observer-page-change", invalidate);
  global.addEventListener("cloud-auth-lost", invalidate);
  global.addEventListener("pagehide", invalidate);
  document.addEventListener("visibilitychange", invalidate);
  global.ObserverDetections = { attach, frame, clear, invalidate, paintForSource,
    contain, normalize, select, paintBoxes, paintMarkers, labels: Object.freeze(LABELS) };
})(window);
