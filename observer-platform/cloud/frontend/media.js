"use strict";
// A canvas can retain pixels and dimensions while an ancestor is hidden. Check
// the selected media category explicitly before fetching or drawing.
const MEDIA_SUBPAGES = new Set(["cameras", "scans", "clouds", "maps"]);
let mediaPageSuspended = false;
function cameraPlaybackAllowed() { return window.ObserverVideoLease?.state().active !== false; }
function cameraViewportVisible(element) {
  const viewport = element?.closest(".media-viewport");
  // Older embedded browsers can lack the geometry API. Preserve their stream
  // rather than permanently hiding a camera because visibility is unknown.
  if (!viewport?.getBoundingClientRect) return true;
  const fullscreen = document.fullscreenElement || document.webkitFullscreenElement;
  if (fullscreen) return fullscreen === element || fullscreen === viewport || fullscreen.contains(element);
  const rectangle = viewport.getBoundingClientRect();
  const width = window.innerWidth || document.documentElement?.clientWidth;
  const height = window.innerHeight || document.documentElement?.clientHeight;
  if (!Number.isFinite(width) || !Number.isFinite(height)) return true;
  const header = document.querySelector?.(".topbar")?.getBoundingClientRect?.();
  const navigation = document.querySelector?.(".sidebar")?.getBoundingClientRect?.();
  const top = header?.width > 0 ? Math.max(0, Math.min(height, header.bottom)) : 0;
  const bottom = navigation?.width >= width * .8 && navigation.top > height / 2
    ? Math.min(height, navigation.top) : height;
  return rectangle.width > 0 && rectangle.height > 0 && rectangle.right > 0 &&
    rectangle.left < width && rectangle.bottom > top && rectangle.top < bottom;
}
function activeMediaSubpage() {
  const page = document.getElementById("page-media");
  if (mediaPageSuspended || document.hidden || !page?.classList.contains("active") ||
    (window.CloudObserver && window.CloudObserver.isExpired())) return null;
  const section = page.querySelector(".subpage:not([hidden])");
  return section && MEDIA_SUBPAGES.has(section.dataset.subpage) ? section.dataset.subpage : null;
}
function mediaElementVisible(element) {
  const section = element?.closest(".subpage");
  return !!section && !section.hidden && section.closest(".page")?.id === "page-media" &&
    section.dataset.subpage === activeMediaSubpage() &&
    (section.dataset.subpage !== "cameras" || cameraPlaybackAllowed() && cameraViewportVisible(element));
}
(function (global) {
  const scopedFetch = (...args) => global.RobotFleet ? global.RobotFleet.fetch(...args) : fetch(...args);
  const F = global.ObserverFormat;
  // Display the current four-USB topology, plus the distinct depth-camera
  // outputs. Historical entrances remain protocol-compatible, not UI cards.
  const httpCameras = { usbLeft: "前视相机", usbFront: "后视相机",
    usbHub2: "左视相机", usbHub3: "右视相机" };
  const cameras = { ...httpCameras, color: "深度相机 · 彩色", depth: "深度相机 · 深度伪彩（中心 Z）" };
  const clouds = { lidar: "MID360 雷达点云", registered: "激光惯性配准点云（FAST-LIO）", depthPoints: "深度相机点云" };
  const scans = { bluesea: "蓝海二维雷达", scan: "二维雷达扫描" };
  const maps = { map: "静态地图", localCostmap: "局部代价地图", globalCostmap: "全局代价地图" };
  const pointViews = { ...clouds, ...scans }, visuals = { ...pointViews, ...maps };
  const groups = { cameras, scans, clouds, maps };
  const urls = {}, busy = new Map(), last = {}, lastRequest = {}, sequence = {}, views = {}, cloudData = {}, frameRevision = {};
  const cameraFrames = {}, cameraStates = {}, cameraSessions = {}, cameraStale = {}, cameraEtags = {}, nextCameraPoll = {}, cameraPollTimers = {};
  const mapImageEtags = {}, mapImageLeases = {};
  let mapImageExpiryTimer = null;
  const nativePlayers = new Map(), nativeSources = {}, nativeDisplayStates = {};
  const nativeUsage = {}, nativeUsageItems = {};
  let nativeMode = null, nativeStatusRequest = null, nextNativeStatus = 0, nativeUsageSupported = false;
  const LAST_FRAME_HOLD_MS = 10000, CAMERA_BUFFER_GRACE_MS = 3000;
  let visualRequest = null, generation = 0, latestSnapshot = null, lastSnapshotTimestamp = -Infinity;
  // One scheduler serves either overview or the selected media category. The
  // lease never exceeds the backend's two-second dynamic visual TTL.
  const VISUAL_TTL_MS = 2000, VISUAL_HOLD_MS = 5000, MAX_RENDER_POINTS = 4000;
  const visualLeases = new Map(), visualValidators = new Map(), spatialViews = new Map();
  const visualDiagnostics = [];
  const visualResourceEntries = [];
  let visualPerformanceObserver = null;
  let visualExpiryTimer = null, visualWakeTimer = null, lastVisualRequestAt = -Infinity;
  let overviewData = null, overviewView = "perspective";
  function overviewVisible() {
    return !mediaPageSuspended && !document.hidden && !global.CloudObserver?.isExpired() &&
      !!document.getElementById("page-overview")?.classList.contains("active") &&
      !!document.getElementById("overview-spatial-canvas");
  }
  function visualTarget() {
    if (overviewVisible()) return "overview";
    const subpage = activeMediaSubpage();
    return subpage && subpage !== "cameras" ? subpage : null;
  }
  function spatialState(key) {
    if (!spatialViews.has(key)) spatialViews.set(key, { yaw: -.85, elevation: .65, zoom: 1 });
    return spatialViews.get(key);
  }
  function clearPointDataset(canvas, status = "WAITING_UPSTREAM") {
    if (!canvas) return;
    Object.assign(canvas.dataset, { pointCount: "0", source: "", frameId: "", sourceId: "", topic: "", status, dimension: "0" });
  }
  function clearOverview(text = "等待真实点云", status = "WAITING_UPSTREAM") {
    overviewData = null; visualLeases.delete("overview");
    const canvas = document.getElementById("overview-spatial-canvas");
    if (!canvas) return;
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    canvas.hidden = true; clearPointDataset(canvas, status);
    canvas.closest(".spatial-viewport")?.classList.remove("is-stale");
    const overlay = document.getElementById("overview-spatial-empty");
    if (overlay) { overlay.hidden = false; overlay.textContent = text; overlay.classList.remove("is-stale"); }
    const set = (id, value) => { const element = document.getElementById(id); if (element) element.textContent = value; };
    set("overview-spatial-status", "等待数据");
    set("overview-spatial-source", "暂无实时数据源");
    set("overview-spatial-meta", "—");
  }
  function visualIdentity(item) {
    const value = item?.frameIdentity;
    return typeof value === "string" && value.length > 0 && value.length <= 256 &&
      !/[\u0000-\u0020\u007f]/.test(value) ? value : null;
  }
  function visualAuthority(sourceKey, item) {
    const identity = frameIdentity(visualIdentity(item));
    if (!identity || typeof sourceKey !== "string") return null;
    return JSON.stringify([sourceKey, identity.session, item.frameId ?? null, item.topic ?? null,
      item.source_id ?? null, item.source ?? null]);
  }
  function visualPreviewTag(item) {
    return typeof item?.previewTag === "string" && /^[a-f0-9]{64}$/.test(item.previewTag) ? item.previewTag : null;
  }
  function visualCachedPoints(key, item) {
    const cached = cloudData[key], lease = visualLeases.get(key), tag = visualPreviewTag(item);
    if (!visualDrawable(key) || !lease.painted || !cached || !tag ||
      visualPreviewTag(cached) !== tag || visualIdentity(item) !== lease.identity ||
      visualIdentity(cached) !== lease.identity || visualAuthority(key, item) !== lease.authority ||
      visualAuthority(key, cached) !== lease.authority) return null;
    return cached.points;
  }
  function visualSeen(group) {
    const seen = {};
    if (group !== "clouds" && group !== "scans") return seen;
    for (const key of Object.keys(groups[group]).slice(0, 3)) {
      const cached = cloudData[key];
      if (cached && visualCachedPoints(key, cached)) seen[key] = visualPreviewTag(cached);
    }
    return seen;
  }
  function visualAge(lease, now = performance.now()) {
    return lease ? Math.max(0, lease.ageS + (now - lease.started) / 1000) : null;
  }
  function visualDrawable(key, now = performance.now()) {
    const lease = visualLeases.get(key);
    return !!lease && !lease.stale && now < lease.deadline;
  }
  function resetVisualStyle(key) {
    const canvas = document.getElementById(key === "overview" ? "overview-spatial-canvas" : `media-${key}`);
    const overlay = document.getElementById(key === "overview" ? "overview-spatial-empty" : `media-empty-${key}`);
    canvas?.closest(key === "overview" ? ".spatial-viewport" : ".media-viewport")?.classList.remove("is-stale");
    overlay?.classList.remove("is-stale", "is-buffering");
  }
  function clearVisual(key, text = "等待数据", status = "STALE") {
    if (key === "overview") clearOverview(text, status);
    else { clear(key, text); stateLabel(key, status); clearPointDataset(document.getElementById(`media-${key}`), status); }
  }
  function showStaleVisual(key, now = performance.now()) {
    const lease = visualLeases.get(key), cached = key === "overview" ? overviewData : cloudData[key];
    const visible = key === "overview" ? overviewVisible() : keyVisible(key);
    // An opaque server identity binds the retained pixels to one real sample.
    // Repeated failures, 304s and STALE metadata never restart this hold.
    const until = lease?.holdUntil ?? (lease && Math.min(lease.deadline + VISUAL_HOLD_MS, now + VISUAL_HOLD_MS));
    if (!visible || !lease?.identity || !lease.painted || !cached || !Number.isFinite(until) || now >= until) {
      clearVisual(key); return false;
    }
    lease.stale = true; lease.holdUntil = until;
    const canvas = document.getElementById(key === "overview" ? "overview-spatial-canvas" : `media-${key}`);
    const overlay = document.getElementById(key === "overview" ? "overview-spatial-empty" : `media-empty-${key}`);
    if (!canvas || !overlay || canvas.hidden) { clearVisual(key); return false; }
    canvas.dataset.status = "STALE";
    resetVisualStyle(key);
    overlay.hidden = true; overlay.textContent = "";
    if (key === "overview") {
      document.getElementById("overview-spatial-status").textContent = "正常";
      document.getElementById("overview-spatial-meta").textContent = `样本 ${F.formatAge(visualAge(lease, now))}`;
    } else {
      stateLabel(key, "STALE");
      document.getElementById(`media-state-${key}`).removeAttribute("title");
    }
    return true;
  }
  function discardVisualCandidate(key, item = null, transient = false, sourceKey = key) {
    const lease = visualLeases.get(key);
    // A late newer sequence is not a new source. Preserve the old sample's
    // original lease/hold, but fail closed on explicit loss or authority change.
    if (item && (["ERROR", "NO_DATA", "WAITING_UPSTREAM"].includes(item.status) ||
      !lease?.authority || visualAuthority(sourceKey, item) !== lease.authority)) {
      const status = item.status === "AVAILABLE" ? "WAITING_UPSTREAM" : item.status;
      clearVisual(key, status === "STALE" ? "等待数据" : F.reasonLabel(item.reason) || F.displayStatus(status), status); return;
    }
    if (transient && visualDrawable(key)) {
      if (key === "overview") document.getElementById("overview-spatial-status").textContent = "正常";
      else stateLabel(key, "WAITING_UPSTREAM", "连接重试", true);
      return; // Keep only the original remaining lease, without extending it.
    }
    showStaleVisual(key);
  }
  function expireVisuals() {
    clearTimeout(visualExpiryTimer); visualExpiryTimer = null;
    const now = performance.now();
    for (const [key, lease] of visualLeases) {
      if (lease.stale || now >= lease.deadline) showStaleVisual(key, now);
    }
    if (visualLeases.size) {
      const deadline = Math.min(...[...visualLeases.values()].map(lease => lease.stale ? Math.min(lease.holdUntil, now + 1000) : lease.deadline));
      visualExpiryTimer = setTimeout(expireVisuals, Math.max(1, deadline - performance.now()));
    }
  }
  function leaseVisual(key, item, started, sourceKey = key) {
    if (item.status !== "AVAILABLE" || !Number.isFinite(item.ageS) || item.ageS < 0) return false;
    const ttl = Number.isFinite(item.ttlS) && item.ttlS > 0 ? Math.min(VISUAL_TTL_MS, item.ttlS * 1000) : VISUAL_TTL_MS;
    const identity = visualIdentity(item), previous = visualLeases.get(key);
    const authority = visualAuthority(sourceKey, item);
    let deadline = started + ttl - item.ageS * 1000;
    const sameFrame = identity && previous?.identity === identity && previous.sourceKey === sourceKey && previous.authority === authority;
    if (sameFrame) {
      deadline = Math.min(previous.deadline, deadline);
      if (previous.stale) return false; // Old pixels cannot become real-time again.
    }
    if (performance.now() >= deadline) return false;
    const ageS = sameFrame ? Math.max(item.ageS, visualAge(previous, started)) : item.ageS;
    visualLeases.set(key, { deadline, identity, authority, sourceKey, ageS, started, stale: false, painted: sameFrame && !!previous.painted });
    resetVisualStyle(key); expireVisuals(); return true;
  }
  const keyVisible = key => mediaElementVisible(document.getElementById(`media-${key}`));
  const nativeKey = key => nativeMode === true && Object.prototype.hasOwnProperty.call(httpCameras, key);
  const requestCurrent = (key, epoch) => epoch === generation && keyVisible(key);
  function frameIdentity(value) {
    if (typeof value !== "string" || value.length > 256) return null;
    const separator = value.lastIndexOf(":"), session = value.slice(0, separator), counter = value.slice(separator + 1);
    if (separator < 1 || !/^[0-9]{1,20}$/.test(counter)) return null;
    return { value, session, counter: BigInt(counter) };
  }
  function frameDeadline(ageHeader, ttlHeader, started, previous, identity) {
    if (typeof ageHeader !== "string" || typeof ttlHeader !== "string" || !ageHeader.trim() || !ttlHeader.trim()) return null;
    const age = Number(ageHeader), ttl = Number(ttlHeader);
    if (!Number.isFinite(age) || !Number.isFinite(ttl) || age < 0 || ttl <= 0 || age >= ttl) return null;
    // Request start includes GET queueing, transit, body and decode time. Never
    // renew an already-observed frame, even if an upstream repeats an old Age.
    const deadline = started + (ttl - age) * 1000;
    return previous && previous.identity.value === identity.value ? Math.min(previous.deadline, deadline) : deadline;
  }
  function strongFrameEtag(value) {
    // The validator is opaque. Accept only one bounded, strong quoted tag;
    // weak tags, wildcard/list forms and control characters are not usable.
    return typeof value === "string" && value.length <= 256 && /^"[\x21\x23-\x7e]{1,254}"$/.test(value) ? value : null;
  }
  function mapImageLease(key, identity, tag, ageHeader, ttlHeader, started, previous) {
    if (!["map", "localCostmap", "globalCostmap"].includes(key) || !strongFrameEtag(tag) ||
      typeof identity !== "string" || !identity || identity.length > 256 ||
      typeof ageHeader !== "string" || !ageHeader.trim() || !Number.isFinite(started)) return null;
    const age = Number(ageHeader);
    if (!Number.isFinite(age) || age < 0) return null;
    let deadline;
    if (key === "map" && ttlHeader === "event") deadline = Infinity;
    else {
      if (typeof ttlHeader !== "string" || !ttlHeader.trim()) return null;
      const ttl = Number(ttlHeader);
      if (!Number.isFinite(ttl) || ttl <= 0 || age >= ttl) return null;
      deadline = started + (ttl - age) * 1000;
    }
    if (previous?.identity === identity) deadline = Math.min(previous.deadline, deadline);
    return { identity, tag, deadline };
  }
  function expireMapImages() {
    clearTimeout(mapImageExpiryTimer); mapImageExpiryTimer = null;
    const now = performance.now();
    for (const [key, lease] of Object.entries(mapImageLeases)) {
      if (now >= lease.deadline) { clear(key, "地图已过期，等待新数据"); stateLabel(key, "STALE"); }
    }
    const deadlines = Object.values(mapImageLeases).map(lease => lease.deadline).filter(Number.isFinite);
    if (deadlines.length) mapImageExpiryTimer = setTimeout(expireMapImages, Math.max(1, Math.min(...deadlines) - performance.now()));
  }
  function cameraPollInterval(key) {
    if (Object.prototype.hasOwnProperty.call(httpCameras, key)) return cameraEtags[key] && urls[key] ? 250 : 1000;
    return cameraStates[key]?.status !== "AVAILABLE" ? 1000 : 250;
  }
  function freshCamera(key, now = performance.now()) {
    return !!cameraFrames[key] && now < cameraFrames[key].deadline && !!urls[key] && !cameraStale[key];
  }
  function retainedCameraUntil(previousUntil, deadline, now) {
    if (!Number.isFinite(deadline) || !Number.isFinite(now)) return null;
    // Retaining pixels never changes the original frame deadline. Repeated
    // STALE snapshots and 204 responses cannot restart this display-only hold.
    const until = previousUntil ?? Math.min(deadline + LAST_FRAME_HOLD_MS, now + LAST_FRAME_HOLD_MS);
    return Number.isFinite(until) && now < until ? until : null;
  }
  function cameraBuffering(deadline, now) {
    // This changes only the short-gap presentation, never the frame's TTL.
    return Number.isFinite(deadline) && Number.isFinite(now) &&
      now >= deadline && now - deadline < CAMERA_BUFFER_GRACE_MS;
  }
  function acceptCameraSession(key, identity) {
    let session = cameraSessions[key];
    if (!session) { cameraSessions[key] = { current: identity.session, retired: new Set() }; return true; }
    if (session.retired.has(identity.session)) return false;
    if (session.current !== identity.session) {
      session.retired.add(session.current);
      if (session.retired.size > 64) session.retired.delete(session.retired.values().next().value);
      session.current = identity.session;
      // Also invalidate a previous session's pending GET when no image is shown.
      clear(key, "数据源已切换");
    }
    return true;
  }
  function cameraPollDue(previous, started, interval) {
    return previous !== undefined && started - previous < interval ? previous + interval : started;
  }
  function cards(items, kind) {
    return Object.entries(items).map(([key, label]) => `<article class="panel media-card"><header class="panel-head"><h2>${label}</h2><span id="media-state-${key}">等待数据</span></header><div class="media-viewport${kind === "cloud" || kind === "scan" ? " radar-stage" : ""}">${kind === "cloud" || kind === "scan" ? `<canvas id="media-${key}" aria-label="${label}实时视图"></canvas>` : `<img id="media-${key}" alt="${label}实时画面" hidden>`}<div id="media-empty-${key}" class="media-overlay">等待真实数据</div></div><div class="media-meta" id="media-meta-${key}">—</div>${kind === "cloud" ? `<button class="media-view" data-cloud-view="${key}" aria-label="${label}：切换俯视或斜视">斜视 / 俯视</button>` : ""}</article>`).join("");
  }
  document.getElementById("camera-gallery").innerHTML = cards(cameras, "camera");
  document.getElementById("cloud-gallery").innerHTML = cards(clouds, "cloud");
  document.getElementById("scan-gallery").innerHTML = cards(scans, "scan");
  document.getElementById("map-gallery").innerHTML = cards(maps, "map");
  global.ObserverDetections?.attach({
    isVisible: () => activeMediaSubpage() === "cameras" && cameraPlaybackAllowed(),
    isDrawable: (key, source) => keyVisible(key) && !source.hidden &&
      (nativeKey(key) ? nativeSourceReady(key) && global.NativeCameraPlayer.isDrawable(source) :
        freshCamera(key) && source.complete && source.naturalWidth > 0 &&
          document.getElementById(`media-empty-${key}`).hidden)
  });
  function suspendOffscreenCamera(key) {
    busy.get(key)?.abort(); busy.delete(key);
    if (nativePlayers.has(key) || urls[key]) clear(key, "滚动到此处播放");
    nativePlayers.delete(key);
    delete nativeSources[key];
    clearTimeout(cameraPollTimers[key]); delete cameraPollTimers[key];
  }
  function clear(key, text = "等待真实数据") {
    global.ObserverDetections?.clear(key);
    frameRevision[key] = (frameRevision[key] || 0) + 1;
    const element = document.getElementById(`media-${key}`);
    if (element.tagName === "IMG") { element.hidden = true; element.removeAttribute("src"); }
    else if (element.tagName === "VIDEO") { nativePlayers.get(key)?.stop(); element.hidden = true; }
    else { element.hidden = true; element.getContext("2d").clearRect(0, 0, element.width, element.height); }
    if (urls[key]) { URL.revokeObjectURL(urls[key]); delete urls[key]; }
    delete sequence[key];
    delete last[key];
    delete cloudData[key];
    visualLeases.delete(key);
    if (pointViews[key]) clearPointDataset(element);
    delete cameraStale[key];
    delete cameraEtags[key];
    delete mapImageEtags[key]; delete mapImageLeases[key];
    delete nativeDisplayStates[key];
    clearTimeout(cameraPollTimers[key]);
    delete cameraPollTimers[key];
    const overlay = document.getElementById(`media-empty-${key}`);
    overlay.hidden = false;
    overlay.textContent = text;
    overlay.classList.remove("is-stale", "is-buffering");
    element.closest(".media-viewport").classList.remove("is-stale", "is-buffering");
    document.getElementById(`media-state-${key}`).removeAttribute("title");
    stateLabel(key, "WAITING_UPSTREAM");
  }
  function showStaleCamera(key, now = performance.now()) {
    global.ObserverDetections?.clear(key);
    const frame = cameraFrames[key];
    const until = frame && retainedCameraUntil(cameraStale[key]?.until, frame.deadline, now);
    if (!keyVisible(key) || !frame || !urls[key] || until === null || cameraStates[key]?.status === "ERROR") {
      clear(key, "等待画面");
      stateLabel(key, "WAITING_UPSTREAM");
      return false;
    }
    cameraStale[key] = { until };
    const element = document.getElementById(`media-${key}`);
    element.hidden = false;
    element.alt = `${cameras[key]}画面`;
    element.closest(".media-viewport").classList.remove("is-stale", "is-buffering");
    const overlay = document.getElementById(`media-empty-${key}`);
    overlay.hidden = true;
    overlay.classList.remove("is-stale", "is-buffering");
    overlay.textContent = "";
    stateLabel(key, "STALE");
    document.getElementById(`media-state-${key}`).removeAttribute("title");
    return true;
  }
  function discardCameraCandidate(key, text) {
    // A failed candidate is not a reason to erase already decoded pixels.
    // Keep their original lease; after it expires they are display-only.
    if (!urls[key] || !cameraFrames[key]) { clear(key, text); return; }
    if (!freshCamera(key)) { showStaleCamera(key); return; }
    stateLabel(key, "WAITING_UPSTREAM", "缓冲中", true);
    document.getElementById(`media-state-${key}`).removeAttribute("title");
  }
  function invalidateView(text = "等待真实数据") {
    clearTimeout(mapImageExpiryTimer); mapImageExpiryTimer = null;
    clearTimeout(visualExpiryTimer); visualExpiryTimer = null; visualLeases.clear();
    visualValidators.clear();
    clearTimeout(visualWakeTimer); visualWakeTimer = null;
    clearOverview(text);
    global.ObserverDetections?.invalidate();
    generation += 1;
    nativeStatusRequest?.abort(); nativeStatusRequest = null; nextNativeStatus = 0;
    nativePlayers.forEach(player => player.stop()); nativePlayers.clear();
    Object.keys(nativeSources).forEach(key => delete nativeSources[key]);
    Object.keys(nativeDisplayStates).forEach(key => delete nativeDisplayStates[key]);
    Object.keys(nativeUsage).forEach(key => delete nativeUsage[key]);
    Object.keys(nativeUsageItems).forEach(key => delete nativeUsageItems[key]);
    busy.forEach(controller => controller.abort()); busy.clear();
    if (visualRequest) visualRequest.controller.abort();
    visualRequest = null;
    Object.keys(lastRequest).forEach(key => delete lastRequest[key]);
    Object.keys(nextCameraPoll).forEach(key => delete nextCameraPoll[key]);
    Object.keys(cameraPollTimers).forEach(key => { clearTimeout(cameraPollTimers[key]); delete cameraPollTimers[key]; });
    Object.keys({ ...cameras, ...visuals }).forEach(key => clear(key, text));
  }
  global.addEventListener("cloud-auth-lost", () => {
    latestSnapshot = null;
    invalidateView("登录已失效");
  });
  function hasDisplayedData(key) {
    const source = document.getElementById(`media-${key}`);
    if (!source || source.hidden) return false;
    if (source.tagName === "VIDEO") return source.readyState >= 2 && source.videoWidth > 0;
    if (source.tagName === "IMG") return source.complete && source.naturalWidth > 0;
    return Number(source.dataset.pointCount) > 0;
  }
  function stateLabel(key, status, label = null, buffering = false) {
    // Presentation only: sample validity and source clocks stay independent.
    const normal = hasDisplayedData(key) || buffering || label === "缓冲中" || status === "AVAILABLE";
    const shown = normal ? "AVAILABLE" : status === "STALE" ? "WAITING_UPSTREAM" : status;
    const element = document.getElementById(`media-state-${key}`);
    element.innerHTML = F.badge(shown, true);
    if (normal) element.firstElementChild.textContent = "正常";
    else if (label !== null && label !== "数据过期") element.firstElementChild.textContent = label;
    element.firstElementChild.classList.remove("is-buffering");
  }
  function metadata(key, parts) {
    // Keep engineering fields accessible in detail mode without parsing upstream HTML.
    const elements = parts.map(([text, detail = false]) => {
      const element = document.createElement("span");
      element.textContent = detail ? text : text.replace(/^\s*·\s*/, "");
      element.className = detail ? "detail-only" : "media-metric";
      return element;
    });
    const container = document.getElementById(`media-meta-${key}`);
    container.replaceChildren(...elements);
    container.classList.toggle("detail-only", parts.every(([, detail]) => detail));
  }
  function originParts(item, key) {
    if (Object.prototype.hasOwnProperty.call(httpCameras, key)) {
      return [["来源 Jetson 边缘计算机 · 网页预览", true], [` · 采集端 ${F.sourceLabel(item.source || "—")}`, true],
        [` · 接口 ${item.topic || "—"}`, true]];
    }
    return [[`来源 ${F.sourceLabel(item.source || "来源待确认")}`, true], [` · 话题 ${item.topic || "—"}`, true]];
  }
  function reasonParts(item) {
    // An actionable error must remain visible; ordinary status already has a badge.
    if (item.status === "STALE") return [];
    return item.reason && item.reason !== F.displayStatus(item.status) ? [[` · ${F.reasonLabel(item.reason)}`]] : [];
  }
  async function image(key, path) {
    if (busy.has(key) || !keyVisible(key) || nativeKey(key) ||
      (nativeMode === null && Object.prototype.hasOwnProperty.call(httpCameras, key))) return;
    // A decoded HTTP frame with a strong validator can be checked frequently
    // without retransmitting unchanged JPEGs. Legacy sources keep 1 Hz polls.
    const camera = Object.prototype.hasOwnProperty.call(cameras, key);
    const mapImage = Object.prototype.hasOwnProperty.call(maps, key);
    if (camera) { clearTimeout(cameraPollTimers[key]); delete cameraPollTimers[key]; }
    const started = performance.now();
    if (camera) {
      const interval = cameraPollInterval(key);
      const due = cameraPollDue(lastRequest[key], started, interval);
      if (due > started) { nextCameraPoll[key] = due; return; }
      // Schedule only an actual request, using the same clock sample as its
      // rate limit. A sub-ms early tick must not skip a whole polling period.
      nextCameraPoll[key] = started + interval;
    }
    lastRequest[key] = started;
    const epoch = generation, controller = new AbortController();
    const validator = camera ? cameraEtags[key] : mapImage ? mapImageEtags[key] : null;
    let revision = frameRevision[key] || 0;
    const current = () => requestCurrent(key, epoch) && (frameRevision[key] || 0) === revision;
    busy.set(key, controller);
    const timeout = setTimeout(() => controller.abort(), 1800);
    try {
      const response = await scopedFetch(path, { cache: "no-store", signal: controller.signal,
        ...(validator ? { headers: { "If-None-Match": validator } } : {}) });
      if (!current()) { await response.body?.cancel(); return; }
      if (response.status === 401) { clear(key, "登录已失效"); global.CloudObserver.unauthorized(); return; }
      if (response.status === 304) {
        if (mapImage) {
          const previous = mapImageLeases[key];
          const lease = mapImageLease(key, response.headers.get("X-Frame-Sequence"),
            response.headers.get("ETag"), response.headers.get("X-Frame-Age-S"),
            response.headers.get("X-Frame-TTL-S"), started, previous);
          if (!validator || !urls[key] || !lease || lease.tag !== validator || lease.identity !== previous?.identity || performance.now() >= lease.deadline) {
            clear(key, "地图已过期或无法验证，等待新数据"); stateLabel(key, "STALE");
          } else { mapImageLeases[key] = lease; expireMapImages(); }
          return;
        }
        const identity = frameIdentity(response.headers.get("X-Frame-Sequence"));
        const tag = strongFrameEtag(response.headers.get("ETag"));
        const previous = cameraFrames[key];
        const deadline = identity && frameDeadline(response.headers.get("X-Frame-Age-S"),
          response.headers.get("X-Frame-TTL-S"), started, previous, identity);
        if (!camera || !validator || validator !== cameraEtags[key] || tag !== validator ||
          !urls[key] || !previous || identity?.value !== previous.identity.value || deadline === null) {
          // Never get trapped in an empty-body loop if the server sends an
          // unexpected 304; the next request must fetch and decode a JPEG.
          delete cameraEtags[key];
          return;
        }
        previous.deadline = deadline;
        const digest = response.headers.get("X-Frame-Image-SHA256");
        // A 304 can update the existing lease, never bind other pixels to it.
        if (digest !== previous.imageId) previous.imageId = null;
        if (cameraStale[key] || performance.now() >= deadline) showStaleCamera(key);
        else global.ObserverDetections?.frame(key, { source: document.getElementById(`media-${key}`),
          imageId: previous.imageId, deadline });
        return; // No new pixels or fresh-frame promotion.
      }
      if (response.status === 204) {
        if (camera) discardCameraCandidate(key, "等待新画面");
        else clear(key, "等待图像 / 图像已过期");
        return;
      }
      if (!response.ok) {
        if (camera && response.status !== 403) { discardCameraCandidate(key, "取帧失败，正在重试"); return; }
        clear(key, "图像不可用"); return;
      }
      const seq = response.headers.get("X-Frame-Sequence");
      const tag = camera || mapImage ? strongFrameEtag(response.headers.get("ETag")) : null;
      const lease = mapImage ? mapImageLease(key, seq, tag, response.headers.get("X-Frame-Age-S"),
        response.headers.get("X-Frame-TTL-S"), started, mapImageLeases[key]) : null;
      if (mapImage && !lease) { clear(key, "地图帧无法验证，等待新数据"); return; }
      const identity = camera ? frameIdentity(seq) : null;
      const deadline = camera && identity ? frameDeadline(response.headers.get("X-Frame-Age-S"),
        response.headers.get("X-Frame-TTL-S"), started, cameraFrames[key], identity) : null;
      if (camera && (!identity || deadline === null)) { discardCameraCandidate(key, "已跳过无法验证的帧"); return; }
      // A header on an undecodable candidate cannot replace a source session.
      // A WS-confirmed switch still clears immediately in updateCameras.
      if (camera && cameraSessions[key]?.retired.has(identity.session)) return;
      if (camera && cameraFrames[key]?.identity.session === identity.session &&
        identity.counter < cameraFrames[key].identity.counter) return;
      const blob = await response.blob();
      if (!current()) return;
      if (mapImage && performance.now() >= lease.deadline) { clear(key, "地图已过期，等待新数据"); stateLabel(key, "STALE"); return; }
      if (camera && performance.now() >= deadline) { discardCameraCandidate(key, "已跳过过期帧"); return; }
      if (seq && sequence[key] === seq && (!camera || !tag || cameraEtags[key] === tag) && (!mapImage || mapImageEtags[key] === tag)) {
        if (camera) {
          cameraFrames[key].deadline = deadline;
          // A changed validator for the same identity cannot validate the
          // pixels already decoded. Fetch unconditionally on the next poll.
          if (!tag || cameraEtags[key] !== tag) delete cameraEtags[key];
        }
        else { last[key] = performance.now(); if (mapImage) { mapImageLeases[key] = lease; expireMapImages(); } }
        return;
      }
      // A 200 response is not evidence of a drawable frame. Decode before
      // replacing the previous image.
      const candidateUrl = URL.createObjectURL(blob), candidate = new Image();
      candidate.src = candidateUrl;
      try { await candidate.decode(); }
      catch (_) {
        URL.revokeObjectURL(candidateUrl);
        if (current()) { if (camera) discardCameraCandidate(key, "已跳过无效图像帧"); else clear(key, "图像解码失败"); }
        return;
      }
      if (!current()) { URL.revokeObjectURL(candidateUrl); return; }
      if (mapImage && performance.now() >= lease.deadline) { URL.revokeObjectURL(candidateUrl); clear(key, "地图已过期，等待新数据"); stateLabel(key, "STALE"); return; }
      if (camera && performance.now() >= deadline) { URL.revokeObjectURL(candidateUrl); discardCameraCandidate(key, "已跳过过期帧"); return; }
      if (camera) {
        if (!acceptCameraSession(key, identity)) { URL.revokeObjectURL(candidateUrl); return; }
        revision = frameRevision[key] || 0;
      }
      last[key] = performance.now();
      if (mapImage) { mapImageEtags[key] = tag; mapImageLeases[key] = lease; }
      const preserveStale = camera && cameraStale[key] && cameraFrames[key]?.identity.value === identity.value;
      if (camera) cameraFrames[key] = { identity, deadline,
        imageId: response.headers.get("X-Frame-Image-SHA256") };
      if (camera) { if (tag) cameraEtags[key] = tag; else delete cameraEtags[key]; }
      const previous = urls[key];
      urls[key] = candidateUrl;
      sequence[key] = seq;
      const element = document.getElementById(`media-${key}`);
      element.src = urls[key]; element.hidden = false;
      const overlay = document.getElementById(`media-empty-${key}`);
      overlay.hidden = true;
      overlay.classList.remove("is-stale", "is-buffering");
      element.closest(".media-viewport").classList.remove("is-stale", "is-buffering");
      if (camera) {
        if (preserveStale) showStaleCamera(key);
        else {
          delete cameraStale[key]; element.alt = `${cameras[key]}实时画面`;
          stateLabel(key, "AVAILABLE"); document.getElementById(`media-state-${key}`).removeAttribute("title");
        }
        // The first unconditional request was scheduled at the legacy 1 Hz.
        // Once decoded, enter conditional checks immediately rather than
        // letting that initial one-second gap hide a decoded frame.
        nextCameraPoll[key] = Math.min(nextCameraPoll[key] ?? Infinity, started + cameraPollInterval(key));
        if (!preserveStale) global.ObserverDetections?.frame(key, { source: element,
          imageId: cameraFrames[key].imageId, deadline });
      }
      if (previous) URL.revokeObjectURL(previous);
      if (mapImage) expireMapImages();
    } catch (error) {
      if (current()) {
        if (camera && (error.name === "AbortError" || error.name === "TypeError")) discardCameraCandidate(key, "连接重试中");
        else clear(key, "图像链路中断，等待重连");
      }
    } finally {
      clearTimeout(timeout);
      if (busy.get(key) === controller) {
        busy.delete(key);
        if (camera && current() && cameraEtags[key] && urls[key]) {
          // Do not add cameraTick's 100 ms quantization after a slow GET. The
          // next conditional check has one bounded timer, still one in-flight
          // request per camera, and honors the original 250 ms start limit.
          const delay = Math.max(1, Math.ceil((nextCameraPoll[key] ?? started + 250) - performance.now()));
          const timer = setTimeout(() => {
            if (cameraPollTimers[key] !== timer) return;
            delete cameraPollTimers[key];
            if (current()) image(key, path);
          }, delay);
          cameraPollTimers[key] = timer;
        }
      }
    }
  }
  function metricStep(value) {
    const magnitude = 10 ** Math.floor(Math.log10(Math.max(0.000001, value)));
    const ratio = value / magnitude;
    return (ratio >= 5 ? 5 : ratio >= 2 ? 2 : 1) * magnitude;
  }
  function validCloudPoints(item) {
    if (!Array.isArray(item?.points)) return [];
    // The server is bounded too; retain a local hard cap before projection/sort.
    const result = [], stride = Math.max(1, Math.ceil(item.points.length / MAX_RENDER_POINTS));
    for (let index = 0; index < item.points.length && result.length < MAX_RENDER_POINTS; index += stride) {
      const point = item.points[index];
      if (Array.isArray(point) && point.length >= 2 &&
        Number.isFinite(point[0]) && Math.abs(point[0]) <= 1000000 &&
        Number.isFinite(point[1]) && Math.abs(point[1]) <= 1000000 &&
        (point.length < 3 || Number.isFinite(point[2]) && Math.abs(point[2]) <= 1000000)) result.push(point);
    }
    return result;
  }
  function paintSpatial(canvas, points, isScan, view, state) {
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return false;
    const pixelRatio = Math.min(2, Math.max(1, global.devicePixelRatio || 1), 2048 / width, 1400 / height);
    canvas.width = Math.max(1, Math.round(width * pixelRatio)); canvas.height = Math.max(1, Math.round(height * pixelRatio));
    const ctx = canvas.getContext("2d"); ctx.scale(canvas.width / width, canvas.height / height);
    const background = ctx.createRadialGradient(width * .48, height * .38, 0, width * .48, height * .38, Math.max(width, height) * .8);
    background.addColorStop(0, "#123141"); background.addColorStop(.55, "#091b2a"); background.addColorStop(1, "#050d17");
    ctx.fillStyle = background; ctx.fillRect(0, 0, width, height);
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity, maxZ = -Infinity;
    points.forEach(([x, y, z = 0]) => {
      minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y);
      minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z);
    });
    if (isScan) {
      // A scan remains a measured XY plane. No extrapolated contour or sweep.
      const extent = Math.max(.25, Math.abs(minX), Math.abs(maxX), Math.abs(minY), Math.abs(maxY));
      minX = minY = -extent; maxX = maxY = extent;
    }
    const span = Math.max(.5, maxX - minX, maxY - minY);
    const gridStep = metricStep(span / 9), gridMinX = Math.floor(minX / gridStep) * gridStep,
      gridMaxX = Math.ceil(maxX / gridStep) * gridStep, gridMinY = Math.floor(minY / gridStep) * gridStep,
      gridMaxY = Math.ceil(maxY / gridStep) * gridStep;
    const center = [(minX + maxX) / 2, (minY + maxY) / 2, (Math.min(0, minZ) + Math.max(0, maxZ)) / 2];
    const radius = Math.max(.35, Math.hypot((gridMaxX - gridMinX) / 2, (gridMaxY - gridMinY) / 2,
      (Math.max(0, maxZ) - Math.min(0, minZ)) / 2));
    const perspective = view !== "top", focal = Math.min(width * .94, height * 1.28),
      distance = radius * 3.05 / state.zoom, orthographic = Math.min((width - 76) / Math.max(.5, maxX - minX),
        (height - 94) / Math.max(.5, maxY - minY)) * state.zoom;
    const sy = Math.sin(state.yaw), cy = Math.cos(state.yaw), se = Math.sin(state.elevation), ce = Math.cos(state.elevation);
    const project = (x, y, z = 0) => {
      if (!perspective) return { x: width / 2 + (x - center[0]) * orthographic,
        y: height * .48 - (y - center[1]) * orthographic, depth: -z, factor: orthographic };
      const dx = x - center[0], dy = y - center[1], dz = z - center[2];
      const depth = distance - (dx * cy * ce + dy * sy * ce + dz * se);
      if (depth <= radius * .035) return null;
      const factor = focal / depth;
      return { x: width / 2 + (-dx * sy + dy * cy) * factor,
        y: height * .48 - (-dx * cy * se - dy * sy * se + dz * ce) * factor, depth, factor };
    };
    const visible = point => point && point.x >= -width && point.x <= width * 2 && point.y >= -height && point.y <= height * 2;
    const line = (a, b, color, lineWidth = 1) => {
      if (!visible(a) || !visible(b)) return;
      ctx.strokeStyle = color; ctx.lineWidth = lineWidth;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    };
    const worldLine = (a, b, color, lineWidth = 1) => line(project(...a), project(...b), color, lineWidth);
    const groundCorners = [[gridMinX, gridMinY, 0], [gridMaxX, gridMinY, 0], [gridMaxX, gridMaxY, 0], [gridMinX, gridMaxY, 0]].map(point => project(...point));
    if (groundCorners.every(visible)) {
      ctx.beginPath(); groundCorners.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y)); ctx.closePath();
      const floor = ctx.createLinearGradient(0, height * .2, 0, height);
      floor.addColorStop(0, "rgba(63,134,154,.025)"); floor.addColorStop(1, "rgba(46,100,129,.16)");
      ctx.fillStyle = floor; ctx.fill();
      for (let index = 0; index < 4; index++) line(groundCorners[index], groundCorners[(index + 1) % 4], "rgba(114,180,202,.23)");
    }
    ctx.font = "11px ui-monospace, SFMono-Regular, monospace";
    for (let index = 0, count = Math.min(32, Math.round((gridMaxX - gridMinX) / gridStep)); index <= count; index++) {
      const x = gridMinX + index * gridStep;
      worldLine([x, gridMinY, 0], [x, gridMaxY, 0], Math.abs(x) < gridStep * .001 ? "rgba(92,208,215,.4)" : "rgba(105,165,187,.13)");
    }
    for (let index = 0, count = Math.min(32, Math.round((gridMaxY - gridMinY) / gridStep)); index <= count; index++) {
      const y = gridMinY + index * gridStep;
      worldLine([gridMinX, y, 0], [gridMaxX, y, 0], Math.abs(y) < gridStep * .001 ? "rgba(158,155,229,.4)" : "rgba(105,165,187,.13)");
    }
    if (isScan) {
      const extent = maxX, ringStep = metricStep(extent / 3), count = Math.min(7, Math.floor(extent / ringStep));
      for (let ring = 1; ring <= count; ring++) {
        const radius = ring * ringStep; let previous = null;
        for (let index = 0; index <= 96; index++) {
          const angle = index / 96 * Math.PI * 2, point = project(Math.cos(angle) * radius, Math.sin(angle) * radius);
          if (previous) line(previous, point, ring === count ? "rgba(104,202,216,.27)" : "rgba(104,202,216,.13)"); previous = point;
        }
        const label = project(0, radius);
        if (visible(label)) { ctx.fillStyle = "#7fa6b9"; ctx.fillText(`${F.formatValue(radius, Math.max(0, -Math.floor(Math.log10(ringStep))))} m`, label.x + 5, label.y + 13); }
      }
    }
    const samples = points.map(([x, y, z = 0]) => ({ x, y, z, screen: project(x, y, isScan ? 0 : z) })).filter(sample => visible(sample.screen));
    samples.sort((a, b) => b.screen.depth - a.screen.depth);
    // Ground projections and sparse verticals are derived from actual points.
    // They are reference geometry, not additional synthetic measurements.
    if (perspective && !isScan) {
      ctx.fillStyle = "rgba(42,156,181,.13)";
      const stride = Math.max(1, Math.ceil(samples.length / 360));
      for (let index = 0; index < samples.length; index += stride) {
        const point = samples[index], floor = project(point.x, point.y, 0);
        if (!visible(floor)) continue;
        ctx.fillRect(floor.x - 1, floor.y - 1, 2, 2);
        if (index % (stride * 3) === 0 && Math.abs(point.z) > .12) line(floor, point.screen, "rgba(76,176,197,.065)");
      }
    }
    const colors = Array.from({ length: 32 }, (_, index) => `hsl(${193 - index / 31 * 154},88%,${62 + index / 31 * 10}%)`);
    const zSpan = Math.max(.05, maxZ - minZ), baseFactor = focal / Math.max(.01, distance);
    samples.forEach(sample => {
      const { x, y, factor, depth } = sample.screen;
      if (x < 3 || y < 3 || x > width - 3 || y > height - 3) return;
      const level = Math.max(0, Math.min(31, Math.round((sample.z - minZ) / zSpan * 31)));
      const proximity = perspective ? Math.max(.45, Math.min(1.6, factor / baseFactor)) : 1;
      const size = Math.max(1.1, Math.min(3.6, (isScan ? 2.45 : 1.85) * proximity));
      ctx.fillStyle = isScan ? "#76dfeb" : colors[level];
      ctx.globalAlpha = (isScan ? .24 : .16) * proximity; ctx.fillRect(x - size, y - size, size * 2, size * 2);
      ctx.globalAlpha = perspective ? Math.max(.35, Math.min(.94, distance / Math.max(.1, depth) * .79)) : .9;
      ctx.fillRect(x - size / 2, y - size / 2, size, size);
    });
    ctx.globalAlpha = 1;
    // Mark the source frame's origin only if it is visible; it is not assumed
    // to be the vehicle centre, nor do we infer a vehicle-forward direction.
    const origin = project(0, 0, 0);
    if (visible(origin) && origin.x > 12 && origin.x < width - 12 && origin.y > 12 && origin.y < height - 48) {
      ctx.strokeStyle = "#efbf7c"; ctx.lineWidth = 1.4; ctx.beginPath(); ctx.arc(origin.x, origin.y, 4, 0, Math.PI * 2); ctx.stroke();
      line({ x: origin.x - 8, y: origin.y }, { x: origin.x + 8, y: origin.y }, "rgba(239,191,124,.8)");
      line({ x: origin.x, y: origin.y - 8 }, { x: origin.x, y: origin.y + 8 }, "rgba(239,191,124,.8)");
    }
    // A perspective ruler is measured on the Z=0 plane at the grid centre.
    const rulerY = gridMinY, rulerX = (gridMinX + gridMaxX) / 2;
    const rulerA = project(rulerX, rulerY), rulerB = project(rulerX + gridStep, rulerY);
    if (visible(rulerA) && visible(rulerB)) {
      line(rulerA, rulerB, "rgba(177,212,222,.9)", 2);
      [rulerA, rulerB].forEach(point => line({ x: point.x, y: point.y - 4 }, { x: point.x, y: point.y + 4 }, "#abcbd9"));
      ctx.fillStyle = "#c0d8e2"; ctx.fillText(`${F.formatValue(gridStep, Math.max(0, -Math.floor(Math.log10(gridStep))))} m`, (rulerA.x + rulerB.x) / 2 + 5, (rulerA.y + rulerB.y) / 2 - 8);
    }
    const axisOrigin = { x: 36, y: height - 42 }, axisLength = 23;
    const axisVector = ([x, y, z]) => perspective ? [-x * sy + y * cy, x * cy * se + y * sy * se - z * ce] : [x, -y];
    [[[1, 0, 0], "X", "#71d9db"], [[0, 1, 0], "Y", "#b4a8f0"], ...(!isScan && perspective ? [[[0, 0, 1], "Z", "#f2bf79"]] : [])].forEach(([vector, label, color]) => {
      const [dx, dy] = axisVector(vector), end = { x: axisOrigin.x + dx * axisLength, y: axisOrigin.y + dy * axisLength };
      line(axisOrigin, end, color, 1.5); ctx.fillStyle = color; ctx.fillText(label, end.x + (dx < -.2 ? -9 : 3), end.y + 3);
    });
    ctx.fillStyle = "#7198ad"; ctx.fillText(isScan ? "XY · m" : "XYZ · m", 16, height - 13);
    if (!isScan) {
      const keyWidth = Math.min(110, width * .23), keyX = width - keyWidth - 18;
      for (let index = 0; index < colors.length; index++) { ctx.fillStyle = colors[index]; ctx.fillRect(keyX + index * keyWidth / colors.length, 17, keyWidth / colors.length + .5, 3); }
      ctx.fillStyle = "#91b0c1";
      const tickCount = width >= 420 ? 5 : 3;
      const tickY = 35;
      ctx.font = "10px ui-monospace, SFMono-Regular, monospace";
      ctx.textAlign = "center";
      for (let tick = 0; tick < tickCount; tick++) {
        const ratio = tick / (tickCount - 1);
        const x = keyX + ratio * keyWidth;
        const value = minZ + ratio * zSpan;
        ctx.strokeStyle = "rgba(195,220,231,.7)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, 20); ctx.lineTo(x, 26); ctx.stroke();
        ctx.fillStyle = "#91b0c1";
        ctx.fillText(`${F.formatValue(value, 1)}m`, x, tickY);
      }
      ctx.textAlign = "start";
      ctx.fillStyle = "#7198ad"; ctx.fillText("Z 色阶（米）", keyX, 48);
      ctx.fillText(`网格 1 格约 ${F.formatValue(gridStep, 2)}m`, keyX, 61);
    }
    Object.assign(canvas.dataset, { pointCount: String(points.length), view, yaw: state.yaw.toFixed(4),
      elevation: state.elevation.toFixed(4), zoom: state.zoom.toFixed(4), dimension: isScan ? "2" : "3" });
    return true;
  }
  function drawCloud(key, item) {
    if (!keyVisible(key) || !visualDrawable(key)) return;
    const points = validCloudPoints(item), isScan = Object.prototype.hasOwnProperty.call(scans, key);
    if (!points.length) { clear(key, isScan ? "当前扫描无有效测距点" : "暂无有效点云"); return; }
    const canvas = document.getElementById(`media-${key}`); canvas.hidden = false;
    if (paintSpatial(canvas, points, isScan, isScan || views[key] === "top" ? "top" : "perspective", spatialState(key))) {
      visualLeases.get(key).painted = true;
      Object.assign(canvas.dataset, { source: key, frameId: String(item.frameId || ""), sourceId: String(item.source || ""), topic: String(item.topic || ""), status: "AVAILABLE" });
      document.getElementById(`media-empty-${key}`).hidden = true;
      stateLabel(key, "AVAILABLE");
    }
  }
  function drawOverview() {
    if (!overviewVisible() || !overviewData) return;
    if (!visualDrawable("overview")) { expireVisuals(); return; }
    const { key, item, points, isScan } = overviewData, canvas = document.getElementById("overview-spatial-canvas");
    canvas.hidden = false;
    if (!paintSpatial(canvas, points, isScan, overviewView, spatialState("overview"))) return;
    visualLeases.get("overview").painted = true;
    Object.assign(canvas.dataset, { source: key, frameId: String(item.frameId || ""), sourceId: String(item.source || ""), topic: String(item.topic || ""), status: "AVAILABLE" });
    const overlay = document.getElementById("overview-spatial-empty"); if (overlay) overlay.hidden = true;
    const set = (id, value) => { const element = document.getElementById(id); if (element) element.textContent = value; };
    set("overview-spatial-status", "正常");
    set("overview-spatial-source", pointViews[key]);
    set("overview-spatial-meta", `${F.formatValue(points.length, 0)} 点 · 样本 ${F.formatAge(visualAge(visualLeases.get("overview")))} · ${item.frameId || "坐标系未提供"}`);
  }
  function updateOverview(values, started) {
    const candidates = Object.keys(pointViews).flatMap(key => {
      const item = values[key];
      if (!item || item.status !== "AVAILABLE" || !Number.isFinite(item.ageS) || item.ageS < 0 ||
        started + VISUAL_TTL_MS - item.ageS * 1000 <= performance.now()) return [];
      const isScan = Object.prototype.hasOwnProperty.call(scans, key), points = validCloudPoints(item).filter(point => isScan || point.length >= 3);
      return points.length ? [{ key, item, points, isScan }] : [];
    });
    // Prefer a real 3D source, then the freshest frame; never merge clouds in
    // unrelated frames or promote a planar scan into a fabricated point cloud.
    candidates.sort((a, b) => Number(a.isScan) - Number(b.isScan) || a.item.ageS - b.item.ageS);
    const selected = candidates[0];
    if (!selected || !leaseVisual("overview", selected.item, started, selected.key)) {
      const previous = overviewData && values[overviewData.key];
      if (overviewData) {
        if (selected?.key === overviewData.key) discardVisualCandidate("overview", selected.item, true, selected.key);
        else if (previous && previous.status !== "AVAILABLE") discardVisualCandidate("overview", previous, false, overviewData.key);
        else discardVisualCandidate("overview", null, true, overviewData.key);
      } else {
        const stale = Object.values(values).some(item => item?.status === "STALE");
        clearOverview("等待数据", stale ? "STALE" : "WAITING_UPSTREAM");
      }
      return false;
    }
    overviewData = selected; drawOverview();
    return visualDrawable("overview") && visualLeases.get("overview").painted;
  }
  function attachSpatialInteraction(canvas, key) {
    if (!canvas) return;
    canvas.style.touchAction = "none";
    canvas.setAttribute("aria-label", key === "overview" ? "实时空间感知，可拖动旋转、滚轮缩放" : `${pointViews[key]}，可拖动旋转、滚轮缩放`);
    let drag = null;
    const active = () => key === "overview" ? overviewVisible() && !!overviewData && visualDrawable("overview") :
      keyVisible(key) && !!cloudData[key] && visualDrawable(key);
    const redraw = () => key === "overview" ? drawOverview() : drawCloud(key, cloudData[key]);
    canvas.addEventListener("pointerdown", event => {
      if (event.button !== 0 || !active()) return;
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY };
      try { canvas.setPointerCapture(event.pointerId); } catch (_) { /* detached canvas */ }
      canvas.style.cursor = "grabbing";
    });
    canvas.addEventListener("pointermove", event => {
      if (!drag || drag.id !== event.pointerId || !active()) return;
      const state = spatialState(key);
      if (key === "overview") setOverviewView("perspective", false); else views[key] = "oblique";
      state.yaw -= (event.clientX - drag.x) * .008;
      state.elevation = Math.max(.12, Math.min(1.42, state.elevation + (event.clientY - drag.y) * .006));
      drag.x = event.clientX; drag.y = event.clientY; redraw();
    });
    const stopDrag = () => { drag = null; canvas.style.cursor = "grab"; };
    canvas.addEventListener("pointerup", stopDrag); canvas.addEventListener("pointercancel", stopDrag); canvas.addEventListener("lostpointercapture", stopDrag);
    canvas.addEventListener("wheel", event => {
      if (!active()) return;
      event.preventDefault(); const state = spatialState(key);
      state.zoom = Math.max(.55, Math.min(2.8, state.zoom * Math.exp(-Math.max(-200, Math.min(200, event.deltaY)) * .0016))); redraw();
    }, { passive: false });
    canvas.style.cursor = "grab";
    if (typeof ResizeObserver === "function") new ResizeObserver(() => { if (active()) redraw(); }).observe(canvas);
  }
  function setOverviewView(view, redraw = true) {
    overviewView = view === "top" ? "top" : "perspective";
    const canvas = document.getElementById("overview-spatial-canvas"); if (canvas) canvas.dataset.view = overviewView;
    document.querySelectorAll("[data-overview-view]").forEach(button => {
      const active = button.dataset.overviewView === overviewView;
      button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active));
    });
    if (redraw) drawOverview();
  }
  clearOverview();
  Object.keys(pointViews).forEach(key => clearPointDataset(document.getElementById(`media-${key}`)));
  attachSpatialInteraction(document.getElementById("overview-spatial-canvas"), "overview");
  Object.keys(clouds).forEach(key => attachSpatialInteraction(document.getElementById(`media-${key}`), key));
  document.querySelectorAll("[data-overview-view]").forEach(button => button.addEventListener("click", () => setOverviewView(button.dataset.overviewView)));
  setOverviewView("perspective", false);
  function visualValidator(value) {
    // Point JSON includes a changing age, so semantic frame validators may be
    // weak. This is separate from the exact-byte strong JPEG validator.
    return typeof value === "string" && value.length <= 256 && /^(?:W\/)?"[\x21\x23-\x7e]{1,254}"$/.test(value) ? value : null;
  }
  function rememberVisualResources(entries) {
    try {
      const paths = ["overview", "maps", "clouds&delta=1", "scans&delta=1"];
      const names = new Set(paths.map(group => new URL(`/visuals.json?group=${group}`, global.location.href).href));
      for (const entry of entries.slice(-64)) {
        if (entry.initiatorType === "fetch" && names.has(entry.name)) visualResourceEntries.push(entry);
      }
      if (visualResourceEntries.length > 16) visualResourceEntries.splice(0, visualResourceEntries.length - 16);
    } catch (_) { /* Timing support is optional and never changes freshness. */ }
  }
  function observeVisualResources() {
    if (typeof global.PerformanceObserver !== "function") return;
    try {
      visualPerformanceObserver = new global.PerformanceObserver(list => rememberVisualResources(list.getEntries()));
      visualPerformanceObserver.observe({ type: "resource", buffered: true });
    } catch (_) { visualPerformanceObserver?.disconnect(); visualPerformanceObserver = null; }
  }
  function visualTiming(path, started, headersAt, finishedAt = performance.now()) {
    const fallback = { started, ttfbMs: Number.isFinite(headersAt) ? Math.max(0, headersAt - started) : null,
      bodyMs: Number.isFinite(headersAt) ? Math.max(0, finishedAt - headersAt) : null };
    try {
      const url = new URL(path, global.location.href).href;
      // The browser's shared timing buffer can fill during long viewing. The
      // observer owns only sixteen visual entries and leaves that buffer alone.
      rememberVisualResources(visualPerformanceObserver?.takeRecords?.() || []);
      const entries = [...visualResourceEntries, ...(performance.getEntriesByName?.(url, "resource") || []).slice(-8)]
        .sort((a, b) => a.startTime - b.startTime);
      for (let index = entries.length - 1; index >= 0; index--) {
        const entry = entries[index];
        if (entry.name !== url || entry.initiatorType !== "fetch" ||
          ![entry.startTime, entry.requestStart, entry.responseStart, entry.responseEnd].every(Number.isFinite) ||
          entry.startTime < started || entry.startTime > finishedAt || entry.requestStart < entry.startTime || entry.requestStart <= 0 ||
          Number.isFinite(headersAt) && (entry.startTime > headersAt || entry.requestStart > headersAt) ||
          entry.responseStart < entry.requestStart || entry.responseEnd < entry.responseStart ||
          entry.responseEnd <= 0 || entry.responseEnd > finishedAt) continue;
        // DNS/TLS and browser queueing precede requestStart. Keep the complete
        // HTTP request, response transfer and parsing inside the sample budget.
        return { started: entry.requestStart, ttfbMs: entry.responseStart - entry.requestStart,
          bodyMs: entry.responseEnd - entry.responseStart };
      }
    } catch (_) { /* Older browsers keep the original conservative budget. */ }
    return fallback;
  }
  observeVisualResources();
  function recordVisualDiagnostic(group, status, started, timing, decodedPoints, ageS = null) {
    visualDiagnostics.push({ group, status, requestMs: Math.max(0, performance.now() - started),
      ttfbMs: timing?.ttfbMs ?? null, bodyMs: timing?.bodyMs ?? null,
      decodedPoints, ageS: Number.isFinite(ageS) ? ageS : null });
    if (visualDiagnostics.length > 16) visualDiagnostics.shift();
  }
  function visualMapsCached(values) {
    const available = Object.entries(values).filter(([key, item]) => maps[key] && item.status === "AVAILABLE");
    return available.length > 0 && available.every(([key, item]) => {
      const lease = mapImageLeases[key];
      return lease && urls[key] && !document.getElementById(`media-${key}`).hidden &&
        lease.identity === visualIdentity(item) && performance.now() < lease.deadline;
    });
  }
  function commitVisualValidator(group, value, accepted) {
    const tag = visualValidator(value);
    if (accepted && tag) visualValidators.set(group, tag); else visualValidators.delete(group);
  }
  function visualPollInterval(target = visualTarget()) { return target === "maps" ? 1000 : 500; }
  function scheduleVisualTick() {
    clearTimeout(visualWakeTimer); visualWakeTimer = null;
    const target = visualTarget();
    if (!target || visualRequest) return;
    const delay = Math.max(1, Math.ceil(lastVisualRequestAt + visualPollInterval(target) - performance.now()));
    visualWakeTimer = setTimeout(visualTick, delay);
  }
  async function visualTick() {
    clearTimeout(visualWakeTimer); visualWakeTimer = null;
    expireVisuals();
    const subpage = visualTarget(), started = performance.now();
    if (!subpage || visualRequest) return;
    const cooldown = visualPollInterval(subpage) - (started - lastVisualRequestAt);
    if (cooldown > 0) {
      // The existing interval can fire a fraction early. One cancellable wake
      // avoids skipping a whole poll while preserving the strict 2 Hz bound.
      visualWakeTimer = setTimeout(visualTick, Math.ceil(cooldown) + 1); return;
    }
    lastVisualRequestAt = started;
    const epoch = generation, keys = subpage === "overview" ? [] : Object.keys(groups[subpage]);
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 2000);
    const request = { controller, epoch, subpage };
    const delta = subpage === "clouds" || subpage === "scans";
    const path = `/visuals.json?group=${subpage}${delta ? "&delta=1" : ""}`;
    let responseStatus = 0, headersAt = null, timing = null, decodedPoints = 0, reportedAge = null;
    visualRequest = request;
    try {
      const canValidate = !delta && (subpage !== "overview" || visualDrawable("overview") && visualLeases.get("overview").painted);
      const validator = canValidate ? visualValidators.get(subpage) : null;
      if (!canValidate) visualValidators.delete(subpage);
      const seen = visualSeen(subpage);
      const headers = { ...(validator ? { "If-None-Match": validator } : {}),
        ...(Object.keys(seen).length ? { "X-Visual-Seen": JSON.stringify(seen) } : {}) };
      const response = await scopedFetch(path, { cache: "no-store", signal: controller.signal,
        ...(Object.keys(headers).length ? { headers } : {}) });
      headersAt = performance.now(); responseStatus = response.status;
      if (epoch !== generation || visualTarget() !== subpage) { await response.body?.cancel(); return; }
      if (response.status === 401 || response.status === 403) { global.CloudObserver.unauthorized(); return; }
      if (response.status === 304) {
        if (!validator || visualValidator(response.headers.get("ETag")) !== validator) visualValidators.delete(subpage);
        await response.body?.cancel();
        timing = visualTiming(path, started, headersAt);
        expireVisuals(); return; // Empty confirmation never renews a sample.
      }
      if (!response.ok) throw new Error("Visuals unavailable");
      const values = await response.json();
      timing = visualTiming(path, started, headersAt);
      if (epoch !== generation || visualTarget() !== subpage) return;
      if (!values || typeof values !== "object" || Array.isArray(values)) throw new Error("Invalid visual metadata");
      const ages = Object.values(values).map(item => item?.ageS).filter(Number.isFinite);
      reportedAge = ages.length ? Math.max(...ages) : null;
      const observedAt = timing.started;
      if (subpage === "overview") {
        decodedPoints = Object.values(values).reduce((sum, item) => sum + (Array.isArray(item?.points) ? Math.min(MAX_RENDER_POINTS, item.points.length) : 0), 0);
        commitVisualValidator(subpage, response.headers.get("ETag"), updateOverview(values, observedAt));
        return;
      }
      for (const key of keys) {
        let item = values[key] || { status: "WAITING_UPSTREAM" };
        if (pointViews[key] && item.status === "AVAILABLE") {
          if (item.pointsUnchanged === true) {
            const points = visualCachedPoints(key, item);
            if (!points || Object.prototype.hasOwnProperty.call(item, "points")) {
              discardVisualCandidate(key, item, true, key); continue;
            }
            item = { ...item, points };
          } else if (Array.isArray(item.points)) decodedPoints += Math.min(MAX_RENDER_POINTS, item.points.length);
        }
        stateLabel(key, item.status);
        const geometry = scans[key]
          ? [[` · 有效采样 ${(item.points || []).length} 点`], [` / 原始 ${F.formatValue(item.totalPoints, 0)} 条距离`, true]]
          : pointViews[key] ? [[`${(item.points || []).length} 点`], [` / 原始 ${F.formatValue(item.totalPoints, 0)} 点`, true]]
          : [[` · ${F.formatValue(item.width, 0)} × ${F.formatValue(item.height, 0)} · ${F.formatValue(item.resolutionM, 3)} m/格`]];
        const depthHelp = key === "depth"
          ? [["中心 Z：中心 5×5 邻域稳健值 · cm", true], ["颜色：0–5 m；≥5 m 为末端色 · 黑色无效", true]]
          : [];
        const pointHelp = key === "depthPoints"
          ? [["独立坐标系 · XYZ 单位：m", true]]
          : [];
        const validPoints = pointViews[key] && item.status === "AVAILABLE" ? validCloudPoints(item) : [];
        const closest = validPoints.length ? validPoints.reduce((nearest, [x, y, z = 0]) =>
          Math.min(nearest, Math.hypot(x, y, scans[key] ? 0 : z)), Infinity) : null;
        metadata(key, [...originParts(item), ...depthHelp, ...pointHelp, ...(item.status === "AVAILABLE" ? [
          [` · 坐标系 ${item.frameId || "—"}`, true], ...geometry,
          ...(closest !== null ? [[`采样最近 ${F.formatValue(closest, 2)} m`]] : []),
          [`源实收 ${F.formatFrequency(item.sourceHz ?? item.hz)}`],
          [`接收时帧龄 ${F.formatAge(item.ageS + (performance.now() - observedAt) / 1000)}`],
          [`网页往返 ${F.formatAge((performance.now() - started) / 1000)}`],
          ...(F.isFiniteNumber(item.sourceAgeS) ? [[`采集时帧龄 ${F.formatAge(item.sourceAgeS)}`, true]] : []),
          ...(F.isFiniteNumber(item.uploadAgeS) ? [[`上行与云端 ${F.formatAge(item.uploadAgeS)}`, true]] : [])
        ] : []), ...reasonParts(item), ...(item.note && item.status !== "STALE" ? [[` · ${F.reasonLabel(item.note)}`, true]] : [])]);
        if (item.status !== "AVAILABLE") {
          if (pointViews[key]) discardVisualCandidate(key, item);
          else clear(key, F.reasonLabel(item.reason) || F.displayStatus(item.status));
          continue;
        }
        if (pointViews[key]) {
          if (!validPoints.length) { clearVisual(key, "当前采样无有效测距点", "WAITING_UPSTREAM"); continue; }
          if (!leaseVisual(key, item, observedAt)) { discardVisualCandidate(key, item, true, key); continue; }
          cloudData[key] = item; drawCloud(key, item);
        } else image(key, `/visual/${key}.png`);
      }
      commitVisualValidator(subpage, response.headers.get("ETag"), subpage === "maps" && visualMapsCached(values));
    } catch (_) {
      visualValidators.delete(subpage);
      if (epoch === generation && visualTarget() === subpage) {
        if (subpage === "overview") discardVisualCandidate("overview", null, true);
        else keys.forEach(key => pointViews[key] ? discardVisualCandidate(key, null, true) : clear(key, "数据链路中断"));
      }
    } finally {
      clearTimeout(timeout);
      if (epoch === generation) recordVisualDiagnostic(subpage, responseStatus, started, timing, decodedPoints, reportedAge);
      if (visualRequest === request) { visualRequest = null; scheduleVisualTick(); }
    }
  }
  document.querySelectorAll("[data-cloud-view]").forEach(button => button.addEventListener("click", () => {
    const key = button.dataset.cloudView; views[key] = views[key] === "top" ? "oblique" : "top";
    if (keyVisible(key) && cloudData[key]) drawCloud(key, cloudData[key]);
  }));
  function nativeLabel(key) {
    return ({ usbLeft: "前视相机", usbFront: "后视相机", usbHub2: "左视相机", usbHub3: "右视相机" })[key];
  }
  function verifyNativeUsage(key, item, requestStarted, now = performance.now()) {
    // The lease starts before the request, so transport time cannot renew an
    // old ownership sample. This is separate from decoded video freshness.
    if (!item || typeof item !== "object" || Array.isArray(item) ||
      !["WEB", "OTHER", "NONE", "UNKNOWN"].includes(item.owner) ||
      !(item.connected === null || typeof item.connected === "boolean") ||
      typeof item.checkedAgeMs !== "number" || !Number.isFinite(item.checkedAgeMs) ||
      item.checkedAgeMs < 0 || item.checkedAgeMs > 6000 ||
      !Number.isFinite(requestStarted) || requestStarted > now) {
      delete nativeUsage[key]; return false;
    }
    nativeUsage[key] = { owner: item.owner, connected: item.connected,
      deadline: requestStarted + 6000 - item.checkedAgeMs };
    return true;
  }
  function nativeUsageNotice(key, item, now = performance.now()) {
    if (!nativeUsageSupported) return null; // Older servers remain compatible.
    const sample = nativeUsage[key], fresh = sample && now < sample.deadline;
    const owner = fresh ? sample.owner : "UNKNOWN", connected = fresh ? sample.connected : null;
    if (connected === false) return { label: "未连接", message: "相机未连接" };
    // Another program may own capture and provide a preview for this viewer.
    // Only validated H.264 metadata admits that passive playback path.
    const preview = item && typeof item === "object" && !Array.isArray(item) &&
      typeof item.codec === "string" && /^(?:h264|avc|avc1(?:\.[0-9a-f]{6})?)$/i.test(item.codec) &&
      !item.error && !item.video_error;
    if (owner === "OTHER" && !preview) return { label: "未共享", message: "相机正在使用，尚未共享画面" };
    if (owner === "NONE" && !item) return { label: "未启动", message: "相机预览未启动" };
    // Metadata loss must not tear down a real stream already decoding. Its
    // existing independent source verification still guards detection overlays.
    if (item || nativePlayers.has(key)) return null;
    if (owner === "UNKNOWN") return { label: "状态未知", message: "占用状态未知 · 等待视频" };
    return { label: "等待视频", message: "等待视频数据" };
  }
  function showNativeUsage(key, notice) {
    const signature = `usage:${notice.label}:${notice.message}`;
    nativeUsageItems[key] = null;
    if (nativeDisplayStates[key] === signature) return;
    // Clearing stops only this viewer's stream, not a capture process.
    clear(key, notice.message); nativePlayers.delete(key); delete nativeSources[key];
    metadata(key, [["—"]]);
    stateLabel(key, "WAITING_UPSTREAM", notice.label);
    nativeDisplayStates[key] = signature;
    document.getElementById(`media-state-${key}`).title = notice.message;
  }
  function nativeVerificationStatus(key, now = performance.now()) {
    const source = nativeSources[key];
    if (!source || now - source.verifiedAt >= 6000) return "WAITING_UPSTREAM";
    if (!source.known) return "WAITING_UPSTREAM";
    return source.fresh ? "AVAILABLE" : "STALE";
  }
  function verifyNativeSource(key, item, now = performance.now()) {
    const age = item.frame_age_ms;
    const known = typeof age === "number" && Number.isFinite(age) && age >= 0;
    const previous = nativeSources[key], fresh = known && age < 2000;
    const continuouslyVerified = previous?.fresh && now - previous.verifiedAt < 6000;
    // The producer reports an age at the status sample. Do not extrapolate it
    // across each two-second poll and manufacture a repeated expiry cycle.
    nativeSources[key] = { known, fresh, verifiedAt: now,
      freshSince: fresh && continuouslyVerified ? previous.freshSince : now };
  }
  function nativeSourceReady(key) {
    const source = nativeSources[key], player = nativePlayers.get(key);
    // A fresh status cannot turn already buffered, older pixels into a newly
    // validated picture; wait for a subsequent actually presented frame.
    return nativeVerificationStatus(key) === "AVAILABLE" && !!player &&
      player.lastFrameAt >= source.freshSince;
  }
  function cameraElement(key, native) {
    const old = document.getElementById(`media-${key}`), label = native ? nativeLabel(key) : cameras[key];
    let element = old;
    if (old.tagName !== (native ? "VIDEO" : "IMG")) {
      clear(key, "等待真实数据");
      element = document.createElement(native ? "video" : "img");
      element.id = old.id; element.hidden = true;
      if (native) { element.muted = true; element.playsInline = true; element.setAttribute("aria-label", `${label}实时画面`); }
      else element.alt = `${label}实时画面`;
      old.replaceWith(element);
      delete cameraFrames[key]; delete cameraSessions[key]; delete cameraStates[key];
    }
    element.closest(".media-card").querySelector(".panel-head h2").textContent = label;
    return element;
  }
  function nativeState(key, status, info) {
    if (!nativeKey(key) || !keyVisible(key)) return;
    const element = document.getElementById(`media-${key}`), overlay = document.getElementById(`media-empty-${key}`);
    const verified = nativeVerificationStatus(key);
    if (status === "AVAILABLE") {
      status = verified === "STALE" ? "STALE" :
        verified === "AVAILABLE" && nativePlayers.get(key)?.drawable() ? "AVAILABLE" : "WAITING_UPSTREAM";
    } else if (status === "WAITING_UPSTREAM" && verified === "STALE") status = "STALE";
    const decoded = element.readyState >= 2 && element.videoWidth > 0;
    element.hidden = !decoded;
    overlay.hidden = decoded;
    overlay.classList.remove("is-stale", "is-buffering");
    element.closest(".media-viewport").classList.remove("is-stale", "is-buffering");
    const message = status === "STALE" ? "等待新画面" : F.reasonLabel(info.message) || F.displayStatus(status);
    if (overlay.textContent !== message) overlay.textContent = message;
    const buffering = info.phase === "buffering" || info.message === "缓冲中";
    if (buffering && !decoded) overlay.textContent = "正常";
    const label = status === "STALE" ? "等待新画面" :
      status === "WAITING_UPSTREAM" ? info.phase === "limited" ? "播放名额已满" :
        verified === "AVAILABLE" ? "缓冲中" : "等待数据" : null;
    const signature = `${status}:${label}:${decoded}:${buffering}`;
    if (nativeDisplayStates[key] !== signature) {
      nativeDisplayStates[key] = signature; stateLabel(key, status, label, buffering);
    }
    const target = global.NativeCameraPlayer?.bufferTarget?.(element) ?? .4;
    document.getElementById(`media-state-${key}`).title = info.phase === "limited" ? info.message :
      `H.264 播放 · 自适应缓冲 ${target.toFixed(1)} 秒`;
    if (status !== "AVAILABLE") {
      global.ObserverDetections?.clear(key);
    }
  }
  function nativeMetadata(key, item) {
    const element = document.getElementById(`media-${key}`);
    const width = element.videoWidth || item.width, height = element.videoHeight || item.height;
    metadata(key, [[`${F.formatValue(width, 0)} × ${F.formatValue(height, 0)}`],
      [`${F.formatValue(global.NativeCameraPlayer?.decodedFps(element), 1)} 帧/秒`],
      [`${F.formatValue(item.h264_mbps, 2)} ${F.unitLabel("Mbps")}`],
      [` · 编码 ${F.detailValue(item.codec || "H.264", "encoding")}`, true],
      [` · 采集 ${F.formatValue(item.actual_fps, 1)} 帧/秒`, true],
      [` · 编码 ${F.formatValue(item.encoded_fps, 1)} 帧/秒`, true],
      [` · 码率 ${F.formatValue(item.h264_mbps, 2)} ${F.unitLabel("Mbps")}`, true],
      [` · 自适应缓冲 ${(global.NativeCameraPlayer?.bufferTarget?.(element) ?? .4).toFixed(1)} 秒`, true]]);
  }
  function applyNativeStatus(value, requestStarted = performance.now()) {
    if (typeof value.enabled !== "boolean") throw new Error("Invalid native status");
    global.dispatchEvent(new CustomEvent("observer-native-metrics", { detail: { value, requestStarted } }));
    const changed = nativeMode !== value.enabled;
    nativeMode = value.enabled;
    const keys = Object.keys(httpCameras);
    if (changed) {
      nativePlayers.forEach(player => player.stop()); nativePlayers.clear();
      keys.forEach(key => {
        delete nativeSources[key]; delete nativeDisplayStates[key];
        delete nativeUsage[key]; delete nativeUsageItems[key];
        busy.get(key)?.abort(); busy.delete(key);
        cameraElement(key, nativeMode);
      });
      if (!nativeMode) {
        const now = performance.now();
        keys.forEach((key, index) => { nextCameraPoll[key] = now + index * 1000 / keys.length; });
      }
    }
    if (!nativeMode) return;
    const usagePresent = Object.prototype.hasOwnProperty.call(value, "cameraUsage");
    if (usagePresent) nativeUsageSupported = true;
    const usageRows = value.cameraUsage && typeof value.cameraUsage === "object" && !Array.isArray(value.cameraUsage)
      ? value.cameraUsage : {};
    const rows = value.cameras && typeof value.cameras === "object" ? value.cameras : {};
    const maximum = Number.isInteger(value.maxPlaying) ? Math.max(1, Math.min(4, value.maxPlaying)) : 4;
    keys.forEach((key, index) => {
      if (!cameraViewportVisible(document.getElementById(`media-${key}`))) {
        suspendOffscreenCamera(key); return;
      }
      const item = rows[key], element = cameraElement(key, true);
      nativeUsageItems[key] = item || null;
      if (usagePresent) verifyNativeUsage(key, usageRows[key], requestStarted);
      const usageNotice = nativeUsageNotice(key, item);
      if (usageNotice) { showNativeUsage(key, usageNotice); return; }
      if (index >= maximum) {
        clear(key, "等待播放名额"); nativePlayers.delete(key); stateLabel(key, "WAITING_UPSTREAM"); return;
      }
      if (!item) {
        delete nativeSources[key];
        if (!nativePlayers.has(key)) nativeState(key, "WAITING_UPSTREAM", { message: "等待视频数据" });
        else nativeState(key, "WAITING_UPSTREAM", { message: "等待新画面" });
        return;
      }
      verifyNativeSource(key, item);
      nativeMetadata(key, item);
      if (item.error || item.video_error) {
        clear(key, "视频暂不可用"); nativePlayers.delete(key); stateLabel(key, "ERROR"); return;
      }
      const identity = `${item.id}:${item.direction}`;
      let player = nativePlayers.get(key);
      if (player && player.identity !== identity) { clear(key, "数据源已切换"); nativePlayers.delete(key); player = null; }
      if (!player && global.NativeCameraPlayer) {
        player = new global.NativeCameraPlayer(element, {
          isCurrent: () => nativeKey(key) && keyVisible(key),
          isSourceReady: () => nativeSourceReady(key),
          onState: (status, info) => nativeState(key, status, info),
          onFrame: info => {
            nativeState(key, player.status, { phase: player.phase, message: player.message });
            if (nativeSourceReady(key) && player.drawable())
              global.ObserverDetections?.frame(key, { ...info, source: element });
          },
          onAuthLost: () => global.CloudObserver.unauthorized()
        });
        player.identity = identity; nativePlayers.set(key, player);
        player.start(`/native-camera/${key}/video.mp4`);
      }
      if (player) nativeState(key, player.status, { phase: player.phase, message: player.message });
    });
  }
  async function nativeStatusTick() {
    if (activeMediaSubpage() !== "cameras" || !cameraPlaybackAllowed() || nativeStatusRequest || performance.now() < nextNativeStatus) return;
    const epoch = generation, controller = new AbortController(), requestStarted = performance.now();
    nativeStatusRequest = controller; nextNativeStatus = requestStarted + 2000;
    const timeout = setTimeout(() => controller.abort(), 3000);
    try {
      const response = await scopedFetch("/native-camera/status", { cache: "no-store", signal: controller.signal });
      if (epoch !== generation || activeMediaSubpage() !== "cameras" || !cameraPlaybackAllowed()) {
        await response.body?.cancel(); return;
      }
      if (response.status === 401 || response.status === 403) { global.CloudObserver.unauthorized(); return; }
      if (response.status === 404) { applyNativeStatus({ enabled: false }); return; }
      if (!response.ok) return;
      const value = await response.json();
      if (epoch === generation && activeMediaSubpage() === "cameras" && cameraPlaybackAllowed()) applyNativeStatus(value, requestStarted);
    } catch (_) { /* Status transport is not decoded video progress. */ }
    finally { clearTimeout(timeout); if (nativeStatusRequest === controller) nativeStatusRequest = null; }
  }
  function cameraTick() {
    if (activeMediaSubpage() !== "cameras" || !cameraPlaybackAllowed()) return;
    nativeStatusTick();
    const now = performance.now();
    Object.keys(cameras).forEach(key => {
      if (!keyVisible(key)) { suspendOffscreenCamera(key); return; }
      if (nativeKey(key)) {
        const usageNotice = nativeUsageNotice(key, nativeUsageItems[key], now);
        if (usageNotice) { showNativeUsage(key, usageNotice); return; }
        const player = nativePlayers.get(key);
        if (player && nativeVerificationStatus(key, now) !== "AVAILABLE") nativeState(key, player.status, { phase: player.phase, message: player.message });
        return;
      }
      if (nativeMode === null && Object.prototype.hasOwnProperty.call(httpCameras, key)) return;
      if (urls[key] && !freshCamera(key, now)) showStaleCamera(key, now);
      if (!busy.has(key) && now >= (nextCameraPoll[key] ?? Infinity)) {
        image(key, `/camera/${key}.jpg`);
      }
    });
  }
  setInterval(cameraTick, 100);
  // Observe stable viewports, never the video/img nodes that get replaced or
  // hidden during initialization. Scrolling must not restart the other feeds.
  const viewportChanged = () => {
    if (activeMediaSubpage() !== "cameras" || !cameraPlaybackAllowed()) return;
    nextNativeStatus = 0;
    cameraTick();
  };
  if (typeof global.IntersectionObserver === "function") {
    const cameraObserver = new global.IntersectionObserver(viewportChanged);
    document.querySelectorAll("#camera-gallery .media-viewport").forEach(viewport => cameraObserver.observe(viewport));
  } else {
    global.addEventListener("scroll", viewportChanged, { passive: true });
  }
  global.addEventListener("resize", viewportChanged, { passive: true });
  document.addEventListener("fullscreenchange", viewportChanged);
  document.addEventListener("webkitfullscreenchange", viewportChanged);
  setInterval(visualTick, 500);
  function updateCameras(snapshot) {
    if (activeMediaSubpage() !== "cameras" || !cameraPlaybackAllowed()) return;
    const values = snapshot.devices && snapshot.devices.cameras || {};
    Object.keys(cameras).forEach(key => {
      if (nativeKey(key)) return; // JPEG metadata is not native H.264 playback progress.
      const item = values[key] || { status: "WAITING_UPSTREAM" };
      const incoming = frameIdentity(item.frameIdentity), verified = cameraFrames[key];
      if (incoming && !acceptCameraSession(key, incoming)) return;
      // HTTP and WS are independently scheduled. An older same-session status
      // must not invalidate a newer decoded JPEG inside its original deadline.
      if (incoming && verified && incoming.session === verified.identity.session &&
        incoming.counter < verified.identity.counter && (freshCamera(key) || !!cameraStale[key])) return;
      cameraStates[key] = item;
      stateLabel(key, item.status);
      metadata(key, [...originParts(item, key),
        [` · ${F.formatValue(item.width, 0)} × ${F.formatValue(item.height, 0)} · 实收 ${F.formatValue(item.previewHz ?? item.hz)} 帧/秒`, item.status !== "AVAILABLE"],
        ...(F.isFiniteNumber(item.ageS) ? [[` · 更新 ${F.formatAge(item.ageS)}`, item.status !== "AVAILABLE"]] : []),
        ...reasonParts(item), ...(item.note && item.status !== "STALE" ? [[` · ${F.reasonLabel(item.note)}`, true]] : [])]);
      if (["STALE", "WAITING_UPSTREAM", "NO_DATA"].includes(item.status)) discardCameraCandidate(key, F.displayStatus(item.status));
      else if (item.status !== "AVAILABLE") clear(key, F.displayStatus(item.status));
      else if (cameraStale[key]) showStaleCamera(key); // Metadata is not a freshly decoded frame.
    });
  }
  function refreshView() {
    // Never resurrect a hidden category's cached frame on a later visit.
    const subpage = activeMediaSubpage();
    global.ObserverVideoLease?.setWanted(subpage === "cameras");
    const blocked = subpage === "cameras" && !cameraPlaybackAllowed();
    const lease = global.ObserverVideoLease?.state();
    invalidateView(blocked ? (lease?.phase === "blocked" ? "视频已在本浏览器的其他页面播放" : "正在协调视频播放…") : "等待真实数据");
    const notice = document.getElementById("video-lease-notice");
    if (notice) notice.hidden = !blocked || lease?.phase !== "blocked";
    if (overviewVisible()) { visualTick(); return; }
    if (!subpage || blocked) return;
    Object.keys(groups[subpage]).forEach(key => {
      stateLabel(key, "WAITING_UPSTREAM");
      metadata(key, [["—"]]);
    });
    if (subpage === "cameras") {
      if (latestSnapshot) updateCameras(latestSnapshot);
      const now = performance.now();
      Object.keys(httpCameras).forEach((key, index) => { nextCameraPoll[key] = now + index * 1000 / Object.keys(httpCameras).length; });
      Object.keys(cameras).filter(key => !Object.prototype.hasOwnProperty.call(httpCameras, key))
        .forEach((key, index) => { nextCameraPoll[key] = now + (index * 2 + 1) * 1000 / 6; });
      cameraTick();
    } else visualTick();
  }
  global.addEventListener("observer-page-change", refreshView);
  global.addEventListener("observer-video-lease-change", refreshView);
  document.addEventListener("visibilitychange", refreshView);
  global.addEventListener("pagehide", () => { mediaPageSuspended = true; refreshView(); });
  global.addEventListener("pageshow", () => {
    if (!mediaPageSuspended) return; // Initial pageshow must not restart a new stream.
    mediaPageSuspended = false; refreshView();
  });
  document.getElementById("video-lease-takeover")?.addEventListener("click", () => global.ObserverVideoLease?.takeover());
  global.ObserverMedia = { update(snapshot) {
    const timestamp = snapshot?.meta?.timestamp;
    if (!Number.isFinite(timestamp) || timestamp <= lastSnapshotTimestamp) return;
    lastSnapshotTimestamp = timestamp;
    latestSnapshot = snapshot;
    updateCameras(snapshot);
  }, diagnostics() { return visualDiagnostics.map(item => ({ ...item })); }};
})(window);
