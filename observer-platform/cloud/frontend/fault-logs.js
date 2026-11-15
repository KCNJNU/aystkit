"use strict";

(function startFaultLogs(global) {
  const scopedFetch = (...args) => global.RobotFleet ? global.RobotFleet.fetch(...args) : fetch(...args);
  const page = document.getElementById("page-logs");
  if (!page) return;
  const byId = id => document.getElementById(id);
  const levelNames = Object.freeze({ INFO: "信息", WARN: "关键告警", ERROR: "错误", FATAL: "严重" });
  const categoryNames = Object.freeze({
    lidar_3d:"三维激光雷达",lidar_2d:"二维激光雷达",imu:"惯性传感器（IMU）",
    camera:"普通相机",depth_camera:"深度相机",lidar_imu:"雷达与惯性融合",
    navigation:"定位与导航",chassis:"底盘",host:"工控机",network:"网络与数据链路",system:"系统与其他"
  });
  const state = { active: global.RKObserverDashboard?.getRoute() === "logs", suspended: false, expired: false,
    request: null, exportRequest: null, timer: null, pending: false, offset: 0, limit: 50, total: 0,
    revision: 0, rows: new Map(), listFilterParams: "", filters: { range: "24h", category: "", level: "", q: "", start: null, end: null }, hasRows: false, signature: null, categorySummary: null, categorySignature: null };
  const dateFormat = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  function formatTime(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    const date = new Date(value * 1000);
    if (!Number.isFinite(date.getTime())) return "—";
    const parts = Object.fromEntries(dateFormat.formatToParts(date).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}.${String(date.getUTCMilliseconds()).padStart(3, "0")}`;
  }
  // datetime-local has no time zone. The form is explicitly Beijing time.
  function parseBeijing(value) {
    if (!value) return null;
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) return NaN;
    const parsed = Date.parse(`${value}+08:00`) / 1000;
    const expected = value.length === 16 ? `${value}:00` : value;
    return Number.isFinite(parsed) && formatTime(parsed).slice(0, 19).replace(" ", "T") === expected ? parsed : NaN;
  }
  function text(id, value) { const node = byId(id); if (node) node.textContent = String(value); }
  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }
  function wanted() { return state.active && !state.suspended && !state.expired && !document.hidden; }
  function filterParams(now = Date.now() / 1000) {
    const params = new URLSearchParams();
    const filter = state.filters;
    if (filter.category) params.set("category", filter.category);
    if (filter.level) params.set("level", filter.level);
    if (filter.q) params.set("q", filter.q);
    const duration = { "1h": 3600, "24h": 86400, "7d": 604800 }[filter.range];
    const start = duration ? now - duration : filter.range === "custom" ? filter.start : null;
    const end = filter.range === "custom" ? filter.end : null;
    if (start !== null) params.set("start", String(start));
    if (end !== null) params.set("end", String(end));
    return params;
  }
  function stop() {
    clearTimeout(state.timer); state.timer = null; state.pending = false;
    state.request?.abort();
    for (const row of state.rows.values()) abortHistory(row);
  }
  async function handleAuth(response) {
    if (response.status === 401) { state.expired = true; stop(); global.CloudObserver?.unauthorized(); return false; }
    if (response.status === 403) {
      state.expired = true; stop();
      // Session endpoint owns the existing approved/pending redirect behavior.
      const valid = await global.CloudObserver?.checkSession();
      if (valid !== false) global.location.replace("/pending");
      return false;
    }
    return true;
  }
  function status(message, error = false) {
    text("fault-updated", message);
    byId("fault-updated").classList.toggle("is-error", error);
  }
  function renderCategories(summary) {
    if (summary) state.categorySummary=summary;
    const current=state.categorySummary,counts=Object.create(null);
    for (const item of Array.isArray(current?.categories)?current.categories:[]) {
      if (Object.hasOwn(categoryNames,item.id)&&Number.isFinite(item.total)) counts[item.id]=Math.max(0,Math.trunc(item.total));
    }
    const signature=JSON.stringify([state.filters.category,current?.total,counts]);
    if (signature===state.categorySignature) return;
    const focus=document.activeElement?.dataset?.faultCategory,fragment=document.createDocumentFragment();
    for (const [id,label] of [["","全部分类"],...Object.entries(categoryNames)]) {
      const raw=id?counts[id]:current?.total,count=Number.isFinite(raw)?Math.max(0,Math.trunc(raw)):null,selected=state.filters.category===id;
      const button=element("button","fault-category");
      button.type="button";button.dataset.faultCategory=id;button.dataset.category=id||"all";
      button.classList.toggle("is-selected",selected);button.classList.toggle("is-empty",count===0&&!selected);
      button.setAttribute("aria-pressed",String(selected));
      button.setAttribute("aria-label",label+"，"+(count===null?"类型数待确认":count+" 类故障"));
      button.append(element("span","fault-category-name",label),element("strong","fault-category-count",count===null?"—":count.toLocaleString("zh-CN")));
      button.addEventListener("click",()=>{
        if(state.filters.category===id)return;
        state.filters.category=id;state.offset=0;state.revision++;renderCategories();requestRefresh();
      });
      fragment.append(button);
    }
    byId("fault-categories").replaceChildren(fragment);state.categorySignature=signature;
    if(focus!==undefined)Array.from(byId("fault-categories").querySelectorAll("button")).find(node=>node.dataset.faultCategory===focus)?.focus();
  }
  function renderSummary(summary) {
    renderCategories(summary);
    const number = value => Number.isFinite(value) ? Math.max(0, Math.trunc(value)).toLocaleString("zh-CN") : "—";
    text("fault-summary-total", number(summary.total));
    text("fault-summary-error", number(summary.error));
    text("fault-summary-warning", number(summary.warning));
    text("fault-summary-latest", formatTime(summary.last_timestamp));
    const monitoring = summary.monitoring;
    if (monitoring && typeof monitoring.online === "boolean") {
      text("fault-monitor-status", monitoring.online ? "采集端在线 · 后台持续记录" : "采集端离线 · 当前显示历史记录");
      byId("fault-monitor-status").classList.toggle("is-offline", !monitoring.online);
    } else { text("fault-monitor-status", "采集状态待确认"); byId("fault-monitor-status").classList.remove("is-offline"); }
    if (Number.isFinite(summary.retention_days)) text("fault-retention", `近 ${summary.retention_days} 天 · 最多 ${number(summary.max_records || 20000)} 条`);
  }
  function setText(node, value) { if (node.textContent !== String(value)) node.textContent = String(value); }
  function abortHistory(row) {
    row.history.request?.abort(); row.history.request = null; row.history.loading = false;
  }
  function historyCurrent(row) {
    return wanted() && row.revision === state.revision && state.rows.get(row.key) === row;
  }
  function renderHistory(row) {
    const history = row.history;
    const fragment = document.createDocumentFragment();
    for (const item of history.items) {
      const entry = element("li", "fault-timestamp");
      const time = element("time", "", formatTime(item.timestamp));
      const date = new Date(item.timestamp * 1000);
      if (Number.isFinite(date.getTime())) time.dateTime = date.toISOString();
      entry.append(time);
      const count = Number.isFinite(item.count) ? Math.max(1, Math.trunc(item.count)) : 1;
      if (item.kind === "historical_summary") {
        if (Number.isFinite(item.last_timestamp) && item.last_timestamp > item.timestamp) entry.append(element("time", "fault-timestamp-end", `至 ${formatTime(item.last_timestamp)}`));
        entry.append(element("span", "fault-timestamp-note", `历史汇总 · ${item.details?.legacy_count_is_lower_bound === true ? "至少 " : ""}${count.toLocaleString("zh-CN")} 次`));
      } else if (item.kind === "reported_summary") {
        entry.append(element("span", "fault-timestamp-note", `来源合计 ${count.toLocaleString("zh-CN")} 次`));
      }
      fragment.append(entry);
    }
    history.list.replaceChildren(fragment);
    history.more.hidden = !history.failed && (history.loading || history.items.length >= history.total);
    history.more.disabled = history.loading;
    setText(history.more, history.failed ? "重试" : "加载更多");
    setText(history.status, history.loading ? "正在读取时间记录…" : history.failed ? "时间记录读取失败" : history.total ? `已显示 ${history.items.length} / ${history.total} 条时间记录` : history.loaded ? "当前范围内暂无时间记录" : "");
    history.container.setAttribute("aria-busy", String(history.loading));
  }
  async function loadHistory(row, reset = false) {
    const history = row.history;
    if (!historyCurrent(row) || !history.container.open || history.request || !row.item.group_id) return;
    if (!reset && history.loaded && history.items.length >= history.total && !history.failed) return;
    const controller = new AbortController(), revision = state.revision, version = row.historyVersion;
    const params = new URLSearchParams(state.listFilterParams || filterParams());
    params.set("limit", "50"); params.set("offset", String(reset ? 0 : history.items.length));
    history.request = controller; history.loading = true; history.failed = false; renderHistory(row);
    const timeout = setTimeout(() => controller.abort("timeout"), 10000);
    try {
      const response = await scopedFetch(`/api/fault-logs/groups/${encodeURIComponent(row.item.group_id)}/timestamps?${params}`, { credentials: "same-origin", cache: "no-store", signal: controller.signal });
      if (!await handleAuth(response)) return;
      if (!response.ok) throw new Error("history unavailable");
      const data = await response.json();
      if (!historyCurrent(row) || revision !== state.revision || controller.signal.aborted || history.request !== controller) return;
      if (reset) history.items = [];
      const known = new Set(history.items.map(item => item.id));
      for (const item of Array.isArray(data.items) ? data.items : []) if (!known.has(item.id)) { history.items.push(item); known.add(item.id); }
      history.total = Number.isFinite(data.total) ? Math.max(history.items.length, Math.trunc(data.total)) : history.items.length;
      history.loaded = true; history.version = version;
    } catch (_) {
      if (historyCurrent(row) && history.request === controller && (!controller.signal.aborted || controller.signal.reason === "timeout")) history.failed = true;
    } finally {
      clearTimeout(timeout);
      if (history.request === controller) {
        history.request = null; history.loading = false; renderHistory(row);
        if (historyCurrent(row) && history.container.open && history.loaded && !history.failed && history.version !== row.historyVersion) void loadHistory(row, true);
      }
    }
  }
  function makeRow(key) {
    const node = element("article", "fault-row"); node.dataset.groupId = key;
    const time = element("div", "fault-time"), first = element("span"), latest = element("small"); time.append(first, latest);
    const type = element("div", "fault-type"), severity = element("span"); type.append(severity);
    const source = element("div", "fault-source"), category = element("span", "fault-category-label"), device = element("strong", "fault-device"); source.append(category, device);
    const body = element("div", "fault-body"), title = element("h3", "fault-title"), meaning = element("p", "fault-message"); body.append(title, meaning);
    const historyContainer = element("details", "fault-history"); historyContainer.dataset.id = `history:${key}`;
    const historySummary = element("summary"), historyList = element("ol", "fault-timestamps"), historyStatus = element("span", "fault-history-status"), more = element("button", "fault-history-more", "加载更多");
    historyStatus.setAttribute("role", "status"); more.type = "button"; more.hidden = true;
    historyContainer.append(historySummary, historyList, historyStatus, more); body.append(historyContainer);
    const details = element("details", "fault-details"); details.dataset.id = `raw:${key}`;
    details.append(element("summary", "", "详情与原始记录"));
    const guidance = element("dl", "fault-guidance"), raw = element("pre"); details.append(guidance, raw); body.append(details);
    const count = element("span", "fault-count"); node.append(time, type, source, body, count);
    const row = { key, node, time, first, latest, severity, category, device, title, meaning, guidance, raw, count, item: null, revision: state.revision,
      history: { container: historyContainer, summary: historySummary, list: historyList, status: historyStatus, more, items: [], total: 0, loaded: false, loading: false, failed: false, request: null, version: null } };
    historyContainer.addEventListener("toggle", () => {
      if (historyContainer.open) void loadHistory(row, row.history.loaded && row.history.version !== row.historyVersion);
      else abortHistory(row);
    });
    more.addEventListener("click", () => { void loadHistory(row, row.history.loaded && row.history.version !== row.historyVersion); });
    return row;
  }
  function updateRow(row, item) {
    row.item = item;
    const level = Object.hasOwn(levelNames, item.level) ? item.level : "INFO";
    const category = Object.hasOwn(categoryNames, item.category) ? item.category : "system";
    row.node.dataset.level = level; row.node.dataset.category = category;
    setText(row.first, `首次 ${formatTime(item.timestamp)}`);
    setText(row.latest, `最近 ${formatTime(item.last_timestamp ?? item.timestamp)}`);
    row.severity.className = "fault-level " + level; setText(row.severity, levelNames[level]);
    setText(row.category, item.category_label || categoryNames[category]); setText(row.device, item.device_label || "具体设备待确认");
    setText(row.title, item.title || "设备或程序报告异常"); setText(row.meaning, item.meaning || "来源节点报告了异常，具体原因请查看原始记录。");
    const guidanceSignature = JSON.stringify([item.impact, item.suggestion]);
    if (guidanceSignature !== row.guidanceSignature) {
      const parts = [];
      for (const [label, value] of [["可能影响", item.impact], ["排查建议", item.suggestion]]) if (value) {
        const part = element("div"); part.append(element("dt", "", label), element("dd", "", value)); parts.push(part);
      }
      row.guidance.replaceChildren(...parts); row.guidanceSignature = guidanceSignature;
    }
    const raw = {"发生时间（北京时间）":formatTime(item.timestamp),"最近发生（北京时间）":formatTime(item.last_timestamp),"云端接收（北京时间）":formatTime(item.received_at),
      "记录编号":item.id,"异常代码":item.code,"来源组件":item.component,"采集来源":item.source_id,"原始消息":item.message,"详情":item.details||{}};
    setText(row.raw, JSON.stringify(raw, null, 2));
    const count = Number.isFinite(item.count) ? Math.max(1, Math.trunc(item.count)) : 1;
    const lowerBound = item.details?.legacy_count_is_lower_bound === true;
    setText(row.count, `${lowerBound ? "≥" : ""}${count.toLocaleString("zh-CN")}`); row.count.setAttribute("aria-label", `${lowerBound ? "至少" : ""}出现 ${count} 次`);
    setText(row.history.summary, `时间记录（${lowerBound ? "至少 " : ""}${count.toLocaleString("zh-CN")} 次）`);
    row.historyVersion = JSON.stringify([count, item.timestamp, item.last_timestamp, lowerBound]);
    if (row.history.container.open && row.history.loaded && row.history.version !== row.historyVersion) {
      if (!row.history.request) void loadHistory(row, true);
    } else if (row.history.container.open && !row.history.loaded) void loadHistory(row);
  }
  function renderRows(data, params) {
    const list = byId("fault-log-list");
    state.listFilterParams = params;
    state.total = Number.isFinite(data.total) ? Math.max(0, Math.trunc(data.total)) : 0;
    const items = Array.isArray(data.items) ? data.items : [], keys = new Set();
    const rows = items.map(item => {
      const key = String(item.group_id || item.id || ""); keys.add(key);
      let row = state.rows.get(key);
      if (!row || row.revision !== state.revision) { if (row) abortHistory(row); row = makeRow(key); state.rows.set(key, row); }
      updateRow(row, item); return row;
    });
    for (const [key, row] of state.rows) if (!keys.has(key)) { abortHistory(row); state.rows.delete(key); }
    const nodes = rows.map(row => row.node), current = Array.from(list.children);
    if (nodes.length !== current.length || nodes.some((node, index) => node !== current[index])) {
      const focus = document.activeElement;
      list.replaceChildren(...nodes);
      if (focus && nodes.some(node => node.contains?.(focus))) focus.focus({ preventScroll: true });
    }
    if (!items.length) list.replaceChildren(element("p", "fault-empty", state.offset ? "此页暂无记录，请返回上一页" : "当前筛选范围内暂无关键故障记录"));
    state.hasRows = items.length > 0;
    text("fault-result-count", `共 ${state.total.toLocaleString("zh-CN")} 类故障`);
    const pages = Math.max(1, Math.ceil(state.total / state.limit));
    text("fault-page-label", `第 ${Math.floor(state.offset / state.limit) + 1} / ${pages} 页 · 每页 ${state.limit} 类`);
    byId("fault-prev").disabled = state.offset <= 0;
    byId("fault-next").disabled = state.offset + state.limit >= state.total;
  }
  async function refresh() {
    if (!wanted() || state.request) return;
    clearTimeout(state.timer); state.pending = false;
    const controller = new AbortController(), revision = state.revision;
    state.request = controller; byId("fault-log-list").setAttribute("aria-busy", "true");
    const timeout = setTimeout(() => controller.abort("timeout"), 10000);
    const params = filterParams(), historyParams = params.toString(); params.set("grouped", "1"); params.set("limit", String(state.limit)); params.set("offset", String(state.offset));
    try {
      const responses = await Promise.all([scopedFetch(`/api/fault-logs?${params}`, { credentials: "same-origin", cache: "no-store", signal: controller.signal }),
        scopedFetch("/api/fault-logs/summary?grouped=1", { credentials: "same-origin", cache: "no-store", signal: controller.signal })]);
      for (const response of responses) if (!await handleAuth(response)) return;
      if (responses.some(response => !response.ok)) throw new Error("unavailable");
      const [data, summary] = await Promise.all(responses.map(response => response.json()));
      if (!wanted() || controller.signal.aborted || revision !== state.revision) return;
      if (Number.isFinite(data.total) && state.offset > 0 && state.offset >= data.total) {
        state.offset = Math.max(0, Math.floor((data.total - 1) / state.limit) * state.limit);
        if (data.total > 0) { state.pending = true; return; }
      }
      renderRows(data, historyParams); renderSummary(summary); status(`更新于 ${formatTime(Date.now() / 1000).slice(11)} · 北京时间`);
    } catch (_) {
      if (wanted() && revision === state.revision && (!controller.signal.aborted || controller.signal.reason === "timeout")) {
        status("日志服务连接中断 · 请重试", true);
        if (!state.hasRows) { byId("fault-log-list").replaceChildren(element("p", "fault-empty", "暂时无法读取日志，请检查网络后刷新")); state.signature = null; }
      }
    } finally {
      clearTimeout(timeout);
      // If either fetch fails, cancel its sibling before allowing the next batch.
      controller.abort();
      if (state.request === controller) { state.request = null; byId("fault-log-list").setAttribute("aria-busy", "false"); }
      if (wanted()) { if (state.pending) void refresh(); else state.timer = setTimeout(refresh, 5000); }
    }
  }
  function requestRefresh() {
    if (!wanted()) return;
    for (const row of state.rows.values()) if (row.revision !== state.revision) abortHistory(row);
    state.pending = true; clearTimeout(state.timer);
    if (state.request) state.request.abort(); else void refresh();
  }
  function applyFilters() {
    const range = byId("fault-range").value, start = parseBeijing(byId("fault-start").value), end = parseBeijing(byId("fault-end").value);
    const error = byId("fault-filter-error");
    if (range === "custom" && ((!Number.isFinite(start) && start !== null) || (!Number.isFinite(end) && end !== null) || (start === null && end === null) || (start !== null && end !== null && start > end))) {
      error.textContent = "请填写有效的北京时间范围，开始时间应早于结束时间"; error.hidden = false; return;
    }
    error.hidden = true;
    state.filters = { range, category: state.filters.category, level: byId("fault-level").value, q: byId("fault-query").value.trim(), start, end };
    state.offset = 0; state.revision++; renderCategories(); requestRefresh();
  }
  async function download(format) {
    if (state.exportRequest || state.expired) return;
    const categoryLabel=categoryNames[state.filters.category]||"全部分类";
    const controller = new AbortController(); state.exportRequest = controller;
    const buttons = page.querySelectorAll("[data-fault-export]"); buttons.forEach(button => { button.disabled = true; });
    const timeout = setTimeout(() => controller.abort(), 45000);
    text("fault-export-status", "正在生成筛选范围内的关键故障日志…");
    try {
      const params = filterParams(); params.set("format", format);
      const response = await scopedFetch(`/api/fault-logs/export?${params}`, { credentials: "same-origin", cache: "no-store", signal: controller.signal });
      if (!await handleAuth(response)) return;
      if (!response.ok) throw new Error("export unavailable");
      const blob = await response.blob(); if (state.expired || controller.signal.aborted) return;
      const url = URL.createObjectURL(blob), anchor = element("a");
      anchor.href = url; anchor.download = `关键故障日志_${categoryLabel}_${formatTime(Date.now() / 1000).replace(/[: .]/g, "-")}.${format}`;
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      text("fault-export-status","已下载"+categoryLabel+"的全部筛选记录");
    } catch (_) { if (!state.expired) text("fault-export-status", "下载失败，请检查网络后重试"); }
    finally { clearTimeout(timeout); state.exportRequest = null; buttons.forEach(button => { button.disabled = false; }); }
  }
  byId("fault-filter-form").addEventListener("submit", event => { event.preventDefault(); applyFilters(); });
  byId("fault-range").addEventListener("change", () => { byId("fault-custom-range").hidden = byId("fault-range").value !== "custom"; });
  byId("fault-reset").addEventListener("click", () => { byId("fault-filter-form").reset(); byId("fault-custom-range").hidden = true; state.filters.category=""; applyFilters(); });
  byId("fault-refresh").addEventListener("click", requestRefresh);
  byId("fault-prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); state.revision++; requestRefresh(); });
  byId("fault-next").addEventListener("click", () => { if (state.offset + state.limit < state.total) { state.offset += state.limit; state.revision++; requestRefresh(); } });
  page.querySelectorAll("[data-fault-export]").forEach(button => button.addEventListener("click", () => { void download(button.dataset.faultExport); }));
  global.addEventListener("observer-page-change", event => { state.active = event.detail?.page === "logs"; if (state.active) requestRefresh(); else stop(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); else requestRefresh(); });
  global.addEventListener("pagehide", () => { state.suspended = true; stop(); state.exportRequest?.abort(); });
  global.addEventListener("pageshow", () => { state.suspended = false; requestRefresh(); });
  global.addEventListener("cloud-auth-lost", () => { state.expired = true; stop(); state.exportRequest?.abort(); });
  renderCategories();
  global.ObserverFaultLogs = Object.freeze({ formatTime, parseBeijing });
  if (global.CloudObserver) global.CloudObserver.ready.then(valid => { if (valid !== false) requestRefresh(); }); else requestRefresh();
})(window);
