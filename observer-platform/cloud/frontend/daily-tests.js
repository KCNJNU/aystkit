"use strict";

(function startDailyTests(global) {
  const scopedFetch = (...args) => global.RobotFleet ? global.RobotFleet.fetch(...args) : fetch(...args);
  const page = document.getElementById("page-tests");
  if (!page) return;
  const byId = id => document.getElementById(id);
  const state = { active: global.RKObserverDashboard?.getRoute() === "tests", suspended: false,
    expired: false, request: null, controlRequest: null, exportRequest: null, timer: null,
    issueRequest: null, issueControlRequest: null, issues: null, issueError: false,
    pending: false, data: null, serviceLost: false, signature: null };
  const qualityNames = Object.freeze({ none: "无测试", waiting: "等待里程", partial: "部分记录", available: "完整记录" });
  const runNames = Object.freeze({ running: "进行中", paused: "已暂停", finished: "已结束" });
  const dateFormat = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  const numeric = value => typeof value === "number" && Number.isFinite(value);
  function duration(value) {
    if (!numeric(value) || value < 0) return "—";
    const seconds = Math.floor(value);
    return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map(v => String(v).padStart(2, "0")).join(":");
  }
  function distance(value) { return numeric(value) && value >= 0 ? value.toLocaleString("zh-CN", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "—"; }
  function testerName(value) { return typeof value === "string" && value.trim() ? value.trim() : "未登记"; }
  function accountName(value) { return typeof value === "string" && value.trim() ? value : "历史未登记账号"; }
  function count(value) { return numeric(value) && value >= 0 ? String(Math.trunc(value)) : "—"; }
  function dailyTesters(day) {
    if (Array.isArray(day.tester_names) && day.tester_names.length) return day.tester_names.map(testerName).join("、");
    return numeric(day.sessions) && day.sessions > 0 ? "未登记" : "—";
  }
  function time(value) {
    if (!numeric(value)) return "—";
    const date = new Date(value * 1000);
    if (!Number.isFinite(date.getTime())) return "—";
    const parts = Object.fromEntries(dateFormat.formatToParts(date).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
  }
  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }
  function text(id, value) { const node = byId(id); if (node) node.textContent = String(value); }
  function wanted() { return state.active && !state.suspended && !state.expired && !document.hidden; }
  function stopPoll() { clearTimeout(state.timer); state.timer = null; state.pending = false; state.request?.abort(); state.issueRequest?.abort(); }
  function announce(message, error = false) { text("tests-message", message); byId("tests-message").classList.toggle("is-error", error); }
  function controls() {
    const run = state.data?.active;
    const unavailable = !state.data || state.expired || state.serviceLost || Boolean(state.controlRequest);
    // The server remains authoritative while the issue list is loading.  Do not
    // make an already usable test page look disabled during the first request;
    // once the list is loaded (or fails), the explicit state below gates start.
    const issueGate = state.issues?.can_start === false || state.issueError === true;
    byId("tests-start").hidden = Boolean(run);
    byId("tests-name-wrap").hidden = Boolean(run);
    byId("tests-tester-name-wrap").hidden = Boolean(run);
    byId("tests-registration-fields").hidden = Boolean(run);
    byId("tests-run-tester").hidden = !run;
    byId("tests-pause").hidden = !run || run.state !== "running";
    byId("tests-resume").hidden = !run || run.state !== "paused";
    byId("tests-finish").hidden = !run;
    for (const action of ["start", "pause", "resume", "finish"]) byId(`tests-${action}`).disabled = unavailable || (action === "start" && issueGate);
    // A statistics read can fail while the operator is still writing a draft.
    // Only the submit controls need an authoritative revision and connection.
    const registrationBusy = state.expired || Boolean(state.controlRequest);
    byId("tests-name").disabled = registrationBusy;
    byId("tests-tester-name").disabled = registrationBusy;
    const issueBusy = state.expired || Boolean(state.issueControlRequest);
    byId("tests-issue-description").disabled = issueBusy;
    byId("tests-issue-category").disabled = issueBusy;
    byId("tests-issue-add").disabled = issueBusy;
    page.querySelectorAll('[data-delete-test-id]').forEach(button => {
      button.disabled = unavailable;
    });
  }
  async function auth(response) {
    if (response.status === 401) { state.expired = true; stopPoll(); controls(); global.CloudObserver?.unauthorized(); return false; }
    if (response.status === 403) {
      state.expired = true; stopPoll(); controls();
      const valid = await global.CloudObserver?.checkSession();
      if (valid !== false) global.location.replace("/pending");
      return false;
    }
    return true;
  }
  function sourceStatus(data) {
    const source = data.source || {};
    const syncing = source.syncing === undefined
      ? source.applied_revision !== data.revision : source.syncing !== false;
    const node = byId("tests-source-status");
    byId("tests-sync-status").hidden = !data.active || !syncing;
    let value = "等待里程数据", tone = "waiting";
    if (state.serviceLost) { value = "页面连接中断"; tone = "error"; }
    else if (!source.online) { value = "采集端离线"; tone = "error"; }
    else if (source.odom_status === "ERROR") { value = "里程数据异常"; tone = "error"; }
    else if (source.odom_status === "STALE") { value = "里程数据中断"; tone = "error"; }
    else if (source.odom_status !== "AVAILABLE") { value = "等待里程数据"; tone = "waiting"; }
    else if (syncing) { value = "同步中"; tone = "waiting"; }
    else if (source.odom_status === "AVAILABLE") { value = data.active?.state === "running" ? "里程记录中" : "里程已就绪"; tone = "available"; }
    node.dataset.tone = tone; node.textContent = value;
    text("tests-source-detail", source.online ? (syncing ? "等待采集端确认" : "导航里程计") : "当前里程可能不完整");
    const activeState = data.active?.state;
    const status = byId("tests-run-state"); status.dataset.state = activeState || "idle";
    status.textContent = activeState ? runNames[activeState] || "状态待确认" : "尚未开始";
  }
  function issueDate(value) {
    if (typeof value === "string" && value.trim()) return value.slice(0, 10);
    if (numeric(value)) return time(value).slice(0, 10);
    return "—";
  }
  function issueText(item) {
    return typeof item?.description === "string" && item.description.trim() ? item.description.trim() : "未填写问题说明";
  }
  function issueMeta(item) {
    const category = typeof item?.category === "string" && item.category.trim() ? item.category.trim() : "通用";
    const date = issueDate(item?.created_at ?? item?.createdAt ?? item?.date);
    const severity = typeof item?.severity === "string" && item.severity.trim() ? ` · ${item.severity.trim()}` : "";
    return `${category}${severity}${date === "—" ? "" : ` · ${date}`}`;
  }
  function normalizeIssues(payload) {
    const pending = Array.isArray(payload?.pending_review) ? payload.pending_review : [];
    const issues = Array.isArray(payload?.issues) ? payload.issues : [];
    const today = typeof payload?.today === "string" ? payload.today : payload?.today?.date;
    const todayIssues = Array.isArray(payload?.today_issues) ? payload.today_issues : issues.filter(item => (item?.issue_day || issueDate(item?.created_at ?? item?.createdAt)) === today);
    return {
      today: typeof payload?.today === "object" ? payload.today : { date: typeof payload?.today === "string" ? payload.today : todayDate() },
      issues, today_issues: todayIssues, pending_review: pending,
      can_start: payload?.can_start !== false && pending.length === 0,
      previous_date: payload?.previous_date || payload?.previousDate || ""
    };
  }
  function todayDate() {
    const date = new Date();
    const parts = Object.fromEntries(dateFormat.formatToParts(date).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  function issueRow(item, review = false) {
    const row = element("article", "dt-issue-row");
    const status = review ? "" : (item?.status === "resolved" || item?.status === "unresolved" ? item.status
      : item?.last_review?.status === "resolved" || item?.last_review?.status === "unresolved" ? item.last_review.status : "");
    if (status) row.dataset.status = status;
    const copy = element("div", "dt-issue-copy");
    copy.append(element("div", "dt-issue-description", issueText(item)), element("div", "dt-issue-meta", issueMeta(item)));
    row.append(copy);
    if (review) {
      const actions = element("div", "dt-issue-actions");
      for (const choice of [{ status: "resolved", label: "✓ 已解决" }, { status: "unresolved", label: "✕ 仍存在" }]) {
        const button = element("button", "", choice.label); button.type = "button"; button.dataset.review = choice.status;
        button.dataset.issueId = String(item?.id ?? ""); button.setAttribute("aria-pressed", status === choice.status ? "true" : "false");
        button.disabled = Boolean(state.issueControlRequest);
        button.addEventListener("click", () => { void reviewIssue(button.dataset.issueId, choice.status); });
        actions.append(button);
      }
      row.append(actions);
    } else if (status) {
      row.append(element("span", `dt-issue-state dt-issue-state-${status}`, status === "resolved" ? "已解决" : "仍存在"));
    }
    return row;
  }
  function renderIssues(data) {
    const pending = Array.isArray(data?.pending_review) ? data.pending_review : [];
    const review = byId("tests-issues-review"); review.hidden = !pending.length;
    const previousDate = data?.previous_date || (pending[0] && issueDate(pending[0].created_at ?? pending[0].createdAt)) || "前一天";
    text("tests-issues-review-title", `前一天的问题${previousDate && previousDate !== "前一天" ? ` · ${previousDate}` : ""}`);
    const pendingList = document.createDocumentFragment();
    pending.forEach(item => pendingList.append(issueRow(item, true)));
    byId("tests-issues-pending").replaceChildren(pendingList);
    const today = data?.today?.date || (typeof data?.today === "string" ? data.today : todayDate());
    text("tests-issues-today-date", today ? `北京时间 ${today}` : "北京时间");
    const current = Array.isArray(data?.today_issues) ? data.today_issues : [];
    const currentList = document.createDocumentFragment();
    current.forEach(item => currentList.append(issueRow(item)));
    if (!current.length) currentList.append(element("p", "dt-empty", "今天还没有记录问题"));
    byId("tests-issues-current").replaceChildren(currentList);
    const status = byId("tests-issues-status");
    if (state.issueError) { status.dataset.tone = "blocked"; status.textContent = "问题清单读取失败，暂不能开始"; }
    else if (pending.length) { status.dataset.tone = "blocked"; status.textContent = "请先完成前一天问题确认"; }
    else { status.dataset.tone = "ready"; status.textContent = "问题确认完成，可开始测试"; }
    controls();
  }
  async function refreshIssues() {
    if (!wanted() || state.issueRequest) return;
    const controller = new AbortController(); state.issueRequest = controller;
    const timeout = setTimeout(() => controller.abort("timeout"), 10000);
    try {
      const response = await scopedFetch("/api/tests/issues?days=365", { credentials: "same-origin", cache: "no-store", signal: controller.signal });
      if (!await auth(response)) return;
      if (!response.ok) throw new Error("issues unavailable");
      const payload = await response.json();
      if (!wanted() || controller.signal.aborted) return;
      state.issueError = false; state.issues = normalizeIssues(payload); renderIssues(state.issues);
    } catch (_) {
      if (wanted() && (!controller.signal.aborted || controller.signal.reason === "timeout")) {
        state.issueError = true; state.issues = null;
        const status = byId("tests-issues-status"); status.dataset.tone = "blocked"; status.textContent = "问题清单读取失败，暂不能开始";
        controls();
      }
    } finally {
      clearTimeout(timeout); if (state.issueRequest === controller) state.issueRequest = null;
    }
  }
  async function issueMutation(url, body, successMessage) {
    if (state.issueControlRequest || state.expired) return false;
    const controller = new AbortController(); state.issueControlRequest = controller; controls();
    const timeout = setTimeout(() => controller.abort("timeout"), 15000);
    try {
      const response = await scopedFetch(url, { method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!await auth(response)) {
        announce("登录状态或访问权限已变化，填写内容未清空", true);
        return false;
      }
      if (!response.ok) {
        const failure = await response.json().catch(() => ({}));
        announce(`${typeof failure.error === "string" ? failure.error : "问题记录保存失败"}；填写内容未清空`, true);
        return false;
      }
      announce(successMessage);
      state.issueError = false; await refreshIssues();
      return true;
    } catch (error) {
      if (!state.expired) {
        announce("提交结果待确认，填写内容未清空；请先刷新问题记录，确认未保存后再重试", true);
      }
      return false;
    } finally { clearTimeout(timeout); state.issueControlRequest = null; controls(); }
  }
  async function reviewIssue(id, status) {
    if (!id) { announce("问题记录缺少编号，无法保存复核结果", true); return; }
    await issueMutation(`/api/tests/issues/${encodeURIComponent(id)}/review`, { status }, status === "resolved" ? "已标记为已解决" : "已标记为仍存在");
  }
  async function addIssue(event) {
    event?.preventDefault();
    const input = byId("tests-issue-description"), draft = input.value, description = draft.trim(), category = byId("tests-issue-category").value;
    if (!description) { announce("请填写遇到的问题", true); input.focus(); return; }
    if (Array.from(description).length > 240) { announce("问题说明最多 240 个字", true); input.focus(); return; }
    const saved = await issueMutation("/api/tests/issues", { description, ...(category ? { category } : {}) }, "问题已记录");
    // IME completion or another local edit may have changed the draft while
    // the request was pending. Never clear text that was not submitted.
    if (saved && input.value === draft && byId("tests-issue-category").value === category) { input.value = ""; input.focus(); }
  }
  function badge(quality) { const node = element("span", "dt-quality", qualityNames[quality] || "待确认"); node.dataset.quality = quality || "none"; return node; }
  function svg(tag, attrs, content) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
    if (content !== undefined) node.textContent = String(content);
    return node;
  }
  function drawChart(id, days, field, unit) {
    const target = byId(id), chart = svg("svg", { viewBox: "0 0 560 210", role: "img", "aria-label": field === "duration_s" ? "近七日测试时长" : "近七日测试移动距离" });
    const series = days.slice(-7);
    const values = series.map(day => numeric(day[field]) ? Math.max(0, day[field] / (field === "duration_s" ? 60 : 1)) : null);
    const highest = Math.max(1, ...values.filter(v => v !== null));
    const power = 10 ** Math.floor(Math.log10(highest));
    const maximum = [1, 2, 5, 10].map(v => v * power).find(v => v >= highest) || highest;
    for (const fraction of [0, .5, 1]) {
      const y = 162 - fraction * 125;
      chart.append(svg("line", { x1: 43, x2: 548, y1: y, y2: y, class: "dt-chart-grid" }));
      chart.append(svg("text", { x: 34, y: y + 4, "text-anchor": "end", class: "dt-chart-axis" }, (maximum * fraction).toLocaleString("zh-CN", { maximumFractionDigits: 1 })));
    }
    if (!series.length) chart.append(svg("text", { x: 290, y: 100, "text-anchor": "middle", class: "dt-chart-axis" }, "暂无测试记录"));
    series.forEach((day, index) => {
      const x = 58 + index * (480 / Math.max(7, series.length));
      const value = values[index];
      const height = value === null ? 10 : Math.max(2, value / maximum * 125);
      const group = svg("g", {});
      group.append(svg("title", {}, `${day.date} · ${value === null ? "无有效里程" : value.toLocaleString("zh-CN", { maximumFractionDigits: 1 }) + unit}`));
      group.append(svg("rect", { x, y: 162 - height, width: 38, height, rx: 5, class: value === null ? "dt-bar-missing" : field === "duration_s" ? "dt-bar-time" : "dt-bar-distance" }));
      group.append(svg("text", { x: x + 19, y: 151 - height, "text-anchor": "middle", class: "dt-chart-value" }, value === null ? "—" : value.toLocaleString("zh-CN", { maximumFractionDigits: 1 })));
      group.append(svg("text", { x: x + 19, y: 187, "text-anchor": "middle", class: "dt-chart-axis" }, String(day.date || "").slice(5).replace("-", "/")));
      chart.append(group);
    });
    target.replaceChildren(chart);
  }
  function renderAccounts(data, administrator) {
    const panel = byId("tests-admin-overview");
    panel.hidden = !administrator;
    const rows = document.createDocumentFragment();
    if (administrator) {
      text("tests-all-duration", duration(data.totals?.duration_s));
      const users = Array.isArray(data.users) ? data.users : [];
      text("tests-account-count", `${users.length} 个账号`);
      for (const user of users) {
        if (!user || typeof user !== "object") continue;
        const row = element("tr"), status = element("td", "dt-account-state");
        const activeState = user.active_state;
        status.dataset.state = ["running", "paused"].includes(activeState) ? activeState : "idle";
        status.append(element("span", "", !activeState || activeState === "idle" ? "尚未开始" : runNames[activeState] || "状态待确认"));
        if (["running", "paused"].includes(activeState) && numeric(user.active_duration_s))
          status.append(element("small", "dt-account-active-time", duration(user.active_duration_s)));
        if (["running", "paused"].includes(activeState) && typeof user.active_run_id === "string")
          status.append(deleteTestButton({ id: user.active_run_id, created_by: user.username, state: activeState }));
        row.append(element("td", "dt-account-name", accountName(user.username)),
          element("td", "dt-number", duration(user.summary?.duration_s)),
          element("td", "dt-number", duration(user.total?.duration_s)),
          element("td", "dt-number", count(user.total?.sessions)), status);
        rows.append(row);
      }
      if (!users.length) {
        const row = element("tr"), cell = element("td", "dt-empty", "暂无账号测试记录");
        cell.colSpan = 5; row.append(cell); rows.append(row);
      }
    } else {
      text("tests-all-duration", "—"); text("tests-account-count", "");
    }
    // Clear former administrator rows when account permissions change.
    byId("tests-users-body").replaceChildren(rows);
  }
  function render(data) {
    state.data = data; state.serviceLost = false;
    const today = data.summary || {}, administrator = data.viewer?.can_view_all === true;
    const viewer = typeof data.viewer?.username === "string" && data.viewer.username ? data.viewer.username : "当前账号";
    const ownToday = data.own_summary || (administrator ? {} : today);
    text("tests-summary-scope", administrator ? "全部账号记录" : "我的记录");
    text("tests-today-duration-label", administrator ? "今日全部账号时长" : "今日我的测试时长");
    text("tests-today-distance-label", administrator ? "今日全部账号距离" : "今日我的移动距离");
    text("tests-today-sessions-label", administrator ? "今日全部账号测试" : "今日我的测试");
    text("tests-current-account", `账号：${viewer}`);
    text("tests-own-summary", `我的今日时长 ${duration(ownToday.duration_s)} · ${count(ownToday.sessions)} 次测试`);
    renderAccounts(data, administrator);
    text("tests-today-date", today.date ? `今日 · ${today.date}` : "今日 · 北京时间");
    text("tests-today-duration", duration(today.duration_s));
    text("tests-today-distance", distance(today.distance_m));
    text("tests-today-sessions", numeric(today.sessions) ? Math.max(0, Math.trunc(today.sessions)) : "—");
    text("tests-distance-quality", qualityNames[today.quality] || "等待里程数据");
    const run = data.active;
    text("tests-run-name", run?.name || "准备开始新的测试");
    text("tests-run-tester", run ? `测试人员：${testerName(run.tester_name)}` : "");
    text("tests-run-start", run ? `开始于 ${time(run.started_at)}` : "");
    text("tests-active-duration", run ? duration(run.duration_s) : "—");
    text("tests-active-distance", run ? `${distance(run.distance_m)} m` : "—");
    text("tests-active-quality", run ? qualityNames[run.quality] || "待确认" : "");
    sourceStatus(data); controls();
    if (Object.prototype.hasOwnProperty.call(data, "test_issues")) {
      state.issueError = false;
      state.issues = normalizeIssues(data.test_issues);
      renderIssues(state.issues);
    } else if (state.issues === null) {
      // Backward-compatible rolling upgrade: the server-side start endpoint
      // remains the authority if an older response omits this field.
      state.issueError = false;
      state.issues = normalizeIssues({ today: todayDate(), issues: [], pending_review: [], can_start: true });
      renderIssues(state.issues);
    }
    const days = Array.isArray(data.days) ? [...data.days].sort((a, b) => String(a.date).localeCompare(String(b.date))) : [];
    drawChart("tests-duration-chart", days, "duration_s", "分钟");
    drawChart("tests-distance-chart", days, "distance_m", "米");
    const daily = document.createDocumentFragment();
    for (const day of [...days].reverse()) {
      const row = element("tr");
      row.append(element("td", "dt-date", day.date || "—"), element("td", "dt-number", duration(day.duration_s)),
        element("td", "dt-number", numeric(day.distance_m) ? `${distance(day.distance_m)} m` : "—"),
        element("td", "dt-number", numeric(day.sessions) ? day.sessions : "—"));
      const quality = element("td"); quality.append(badge(day.quality)); row.append(quality, element("td", "dt-testers", dailyTesters(day))); daily.append(row);
    }
    if (!days.length) { const row = element("tr"), cell = element("td", "dt-empty", "暂无每日测试记录"); cell.colSpan = 6; row.append(cell); daily.append(row); }
    byId("tests-days-body").replaceChildren(daily);
    const runs = Array.isArray(data.runs) ? data.runs : [];
    text("tests-run-count", `${runs.length} 次测试`);
    const list = document.createDocumentFragment();
    for (const item of runs) {
      const article = element("article", "dt-session"); article.dataset.state = item.state;
      const name = element("div", "dt-session-name"); name.append(element("strong", "", item.name || "未命名测试"), element("span", "dt-session-state", runNames[item.state] || "待确认"));
      name.append(element("small", "dt-session-account", `账号：${accountName(item.created_by)}`),
        element("small", "dt-session-tester", `测试人员：${testerName(item.tester_name)}`));
      const dates = element("div", "dt-session-time", time(item.started_at));
      dates.append(element("small", "", item.ended_at === null ? (item.state === "paused" ? "测试已暂停" : "测试进行中") : `结束 ${time(item.ended_at)}`));
      const metrics = element("div", "dt-session-metrics");
      metrics.append(element("strong", "", duration(item.duration_s)), element("strong", "", numeric(item.distance_m) ? `${distance(item.distance_m)} m` : "—"), badge(item.quality));
      if (administrator) metrics.append(deleteTestButton(item));
      article.append(name, dates, metrics); list.append(article);
    }
    if (!runs.length) list.append(element("p", "dt-empty", "暂无测试会话"));
    byId("tests-runs-list").replaceChildren(list);
    text("tests-updated", `更新于 ${time(data.server_time).slice(11)} · 北京时间`);
    byId("tests-updated").classList.remove("is-error");
  }
  function deleteTestButton(run) {
    const button = element("button", "dt-delete-test", "删除测试");
    button.type = "button";
    button.dataset.deleteTestId = run.id;
    button.setAttribute("aria-label", `删除 ${accountName(run.created_by)} 的测试${run.name ? `：${run.name}` : ""}`);
    button.disabled = state.expired || state.serviceLost || Boolean(state.controlRequest);
    button.addEventListener("click", () => { void deleteTest(run); });
    return button;
  }
  async function deleteTest(run) {
    if (!state.data?.viewer?.can_view_all || state.controlRequest || state.expired || state.serviceLost) return;
    const active = ["running", "paused"].includes(run.state);
    if (!global.confirm(`确定删除账号“${accountName(run.created_by)}”的${run.name ? `测试“${run.name}”` : "这次测试"}吗？${active ? "进行中的计时将停止。" : ""}该记录将从测试列表与统计中移除。`)) return;
    const controller = new AbortController(); state.controlRequest = controller;
    clearTimeout(state.timer);
    // A read started before the deletion must not overwrite its refreshed state.
    state.request?.abort();
    controls();
    announce("正在删除测试…");
    const timeout = setTimeout(() => controller.abort("timeout"), 10000);
    try {
      const response = await scopedFetch(`/api/admin/tests/${encodeURIComponent(run.id)}`, {
        method: "DELETE", credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "Content-Type": "application/json" }, body: "{}"
      });
      if (!await auth(response)) return;
      if (response.status === 404) { announce("该测试已不存在，请刷新列表"); return; }
      if (response.status === 409) { announce("测试状态已更新，请刷新后重试", true); return; }
      if (response.status === 429) { announce("操作过于频繁，请稍后重试", true); return; }
      if (!response.ok) throw new Error("deletion failed");
      const result = await response.json();
      if (result.deleted !== true || result.id !== run.id) throw new Error("invalid deletion response");
      announce("测试已删除");
    } catch (_) { announce("删除结果待确认，请刷新列表核对", true); }
    finally {
      clearTimeout(timeout);
      if (state.controlRequest === controller) state.controlRequest = null;
      controls();
      requestRefresh();
    }
  }
  async function refresh() {
    if (!wanted() || state.request || state.controlRequest) return;
    clearTimeout(state.timer); state.pending = false;
    const controller = new AbortController(); state.request = controller;
    const timeout = setTimeout(() => controller.abort("timeout"), 10000);
    try {
      const response = await scopedFetch("/api/tests?days=30", { credentials: "same-origin", cache: "no-store", signal: controller.signal });
      if (!await auth(response)) return;
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (!wanted() || controller.signal.aborted) return;
      render(data);
    } catch (_) {
      if (wanted() && (!controller.signal.aborted || controller.signal.reason === "timeout")) {
        state.serviceLost = true; text("tests-updated", "连接中断 · 显示上次记录"); byId("tests-updated").classList.add("is-error");
        if (state.data) sourceStatus(state.data); controls();
        // Statistics and notes have separate endpoints. A failed initial
        // statistics read must not hide the notes and previous-day reviews.
        if (state.issues === null || state.issueError) void refreshIssues();
      }
    } finally {
      clearTimeout(timeout);
      if (state.request === controller) state.request = null;
      if (wanted() && !state.controlRequest) { if (state.pending) void refresh(); else state.timer = setTimeout(refresh, 5000); }
    }
  }
  function requestRefresh() {
    if (!wanted()) return;
    state.pending = true; clearTimeout(state.timer);
    if (state.request) state.request.abort(); else void refresh();
  }
  async function control(action) {
    if (!state.data || state.controlRequest || state.expired || state.serviceLost) return;
    if (action === "start" && (state.issueError || state.issues?.can_start === false)) {
      announce(state.issueError ? "问题清单暂时无法读取，请刷新后再开始测试" : "请先完成前一天的问题确认", true);
      void refreshIssues();
      return;
    }
    const name = byId("tests-name").value.trim();
    const testerInput = byId("tests-tester-name"), tester = testerInput.value.trim();
    if (action === "start") {
      let error = "";
      if (!tester) error = "请填写测试人员姓名";
      else if (Array.from(tester).length > 60) error = "测试人员姓名最多 60 个字";
      else if (/[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}]/u.test(testerInput.value)) error = "测试人员姓名不能包含控制字符";
      if (error) { testerInput.setAttribute("aria-invalid", "true"); announce(error, true); testerInput.focus(); return; }
      testerInput.setAttribute("aria-invalid", "false");
      if (Array.from(name).length > 60) { announce("测试名称最多 60 个字", true); byId("tests-name").focus(); return; }
    }
    stopPoll();
    const controller = new AbortController(); state.controlRequest = controller; controls();
    announce("正在保存统计状态…");
    const timeout = setTimeout(() => controller.abort("timeout"), 15000);
    let refreshAfter = false, focusTesterAfter = false;
    try {
      const body = { action, revision: state.data.revision, command_id: global.crypto.randomUUID() };
      if (action === "start") { body.tester_name = tester; if (name) body.name = name; }
      const response = await scopedFetch("/api/tests/control", { method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!await auth(response)) return;
      if (response.status === 409) {
        const conflict = await response.json().catch(() => ({}));
        if (conflict.reason === "test_issue_review_required") {
          state.issues = normalizeIssues({ today: todayDate(), issues: conflict.pending_issues || [], pending_review: conflict.pending_issues || [], can_start: false });
          renderIssues(state.issues); announce("请先完成前一天的问题确认", true);
        } else announce("本账号的其他页面已更新状态，正在同步…");
        refreshAfter = true; return;
      }
      if (response.status === 400) {
        const failure = await response.json().catch(() => ({}));
        const message = typeof failure.error === "string" ? failure.error : "登记信息无效，请检查测试人员姓名和测试名称";
        announce(message, true);
        if (action === "start" && message.includes("测试人员")) {
          testerInput.setAttribute("aria-invalid", "true"); focusTesterAfter = true;
        }
        return;
      }
      if (!response.ok) throw new Error("control unavailable");
      const data = await response.json();
      if (state.expired || controller.signal.aborted) return;
      render(data);
      if (action === "start") byId("tests-name").value = "";
      announce({ start: "测试统计已开始", pause: "测试统计已暂停", resume: "测试统计已继续", finish: "测试统计已结束" }[action] || "统计已保存");
    } catch (_) {
      if (!state.expired) { announce("提交结果待确认，正在刷新状态…", true); refreshAfter = true; }
    } finally {
      clearTimeout(timeout); state.controlRequest = null; controls();
      if (focusTesterAfter && !testerInput.disabled && !byId("tests-tester-name-wrap").hidden) testerInput.focus();
      if (wanted()) { if (refreshAfter) requestRefresh(); else state.timer = setTimeout(refresh, 5000); }
    }
  }
  async function download(format) {
    if (state.exportRequest || state.expired) return;
    const controller = new AbortController(); state.exportRequest = controller;
    const buttons = page.querySelectorAll("[data-tests-export]"); buttons.forEach(button => { button.disabled = true; });
    const timeout = setTimeout(() => controller.abort(), 45000); announce("正在生成每日测试记录…");
    try {
      const response = await scopedFetch(`/api/tests/export?days=30&format=${format}`, { credentials: "same-origin", cache: "no-store", signal: controller.signal });
      if (!await auth(response)) return;
      if (!response.ok) throw new Error("export unavailable");
      const blob = await response.blob();
      if (state.expired || controller.signal.aborted) return;
      const url = URL.createObjectURL(blob), anchor = element("a"); anchor.href = url;
      anchor.download = `每日测试_${time(state.data?.server_time).slice(0, 10)}.${format}`;
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      announce("已下载最近 30 天的每日测试记录");
    } catch (_) { if (!state.expired) announce("下载失败，请检查网络后重试", true); }
    finally { clearTimeout(timeout); state.exportRequest = null; buttons.forEach(button => { button.disabled = false; }); }
  }
  for (const action of ["start", "pause", "resume", "finish"]) byId(`tests-${action}`).addEventListener("click", () => { void control(action); });
  byId("tests-issue-form").addEventListener("submit", event => { void addIssue(event); });
  byId("tests-tester-name").addEventListener("input", () => byId("tests-tester-name").setAttribute("aria-invalid", "false"));
  byId("tests-refresh").addEventListener("click", requestRefresh);
  page.querySelectorAll("[data-tests-export]").forEach(button => button.addEventListener("click", () => { void download(button.dataset.testsExport); }));
  global.addEventListener("observer-page-change", event => { state.active = event.detail?.page === "tests"; if (state.active) requestRefresh(); else stopPoll(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopPoll(); else requestRefresh(); });
  global.addEventListener("pagehide", () => { state.suspended = true; stopPoll(); state.exportRequest?.abort(); });
  global.addEventListener("pageshow", () => { state.suspended = false; requestRefresh(); });
  global.addEventListener("cloud-auth-lost", () => { state.expired = true; stopPoll(); state.controlRequest?.abort(); state.issueControlRequest?.abort(); state.exportRequest?.abort(); controls(); });
  global.ObserverDailyTests = Object.freeze({ duration, distance, time });
  controls();
  if (global.CloudObserver) global.CloudObserver.ready.then(valid => { if (valid !== false) requestRefresh(); }); else requestRefresh();
})(window);
