"use strict";
(function () {
  const $ = id => document.getElementById(id);
  const PAGE_SIZE = 20;
  let busy = false, authenticated = false, user = null, dataset = null;
  let page = 1, total = 0, folderPath = null, leaving = false;

  function announce(text, state = "") {
    $("dbprogMessage").textContent = text;
    $("dbprogMessage").className = `dbprog-message ${state}`;
  }
  function cell(row, value) {
    const node = document.createElement("td");
    node.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
    row.append(node);
    return node;
  }
  function dateText(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
  }
  function bytes(value) {
    if (value === null || value === undefined || value === "") return "—";
    const size = Number(value);
    if (!Number.isFinite(size) || size < 0) return "—";
    if (size < 1024) return `${size} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let amount = size / 1024, index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index++; }
    return `${amount.toFixed(1)} ${units[index]}`;
  }
  function clearRows(message = "等待查询") {
    const row = document.createElement("tr"), node = cell(row, message);
    node.colSpan = dataset ? 5 : 4;
    node.className = "empty-block";
    $("dbprogTableBody").replaceChildren(row);
    total = 0;
    $("dbprogPageInfo").textContent = "—";
    $("dbprogFileDetail").hidden = true;
    $("dbprogPrev").disabled = true;
    $("dbprogNext").disabled = true;
  }
  function setAuthentication(value, account = null) {
    authenticated = value;
    if (account) user = account;
    if (!authenticated) {
      user = null; dataset = null; page = 1; folderPath = null;
      $("dbprogSummary").replaceChildren();
      $("dbprogSummary").hidden = true;
      $("dbprogServices").replaceChildren();
      clearRows("请先登录 DBProg");
    }
    $("dbprogLoginForm").hidden = authenticated;
    $("dbprogLogout").hidden = !authenticated;
    $("dbprogBrowser").hidden = !authenticated;
    $("dbprogAccountText").textContent = authenticated ? user?.displayName || user?.name || user?.email || "已登录 DBProg" : "尚未登录 DBProg";
    $("dbprogAccountNote").textContent = authenticated ? user?.email || "按当前 DBProg 账号的数据访问权限浏览。" : "数仓账号与观测平台账号独立，请使用 DBProg 原有账号登录。";
  }
  function setBusy(value) {
    busy = value;
    $("dbprogBrowser").setAttribute("aria-busy", String(value));
    for (const node of document.querySelectorAll(".dbprog-root button,.dbprog-root input,.dbprog-root select")) node.disabled = value;
    $("dbprogPrev").disabled = value || page <= 1;
    $("dbprogNext").disabled = value || page * PAGE_SIZE >= total;
  }
  async function request(path, options = {}) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/dbprog/${path}`, { credentials: "same-origin", cache: "no-store", ...options, signal: controller.signal });
      let data;
      try { data = await response.json(); }
      catch (_) { throw Error("DBProg 返回了无法读取的响应，请刷新重试。"); }
      if (response.status === 401 && data.error === "observer_auth_required") {
        leaving = true;
        window.location.replace("/login?next=/dbprog");
        throw Object.assign(Error("观测平台登录已过期。"), { observerAuth: true, status: 401 });
      }
      if (!response.ok) {
        const message = typeof data.message === "string" ? data.message : response.status === 401 ? "DBProg 登录已过期，请重新登录数仓。"
          : response.status === 403 ? "当前数仓账号没有访问权限，请联系管理员。" : `数仓请求失败（${response.status}）。`;
        throw Object.assign(Error(message), { status: response.status });
      }
      return data;
    } finally { clearTimeout(timer); }
  }
  async function run(task) {
    if (busy || leaving) return;
    setBusy(true);
    try { await task(); }
    catch (error) {
      if (error.observerAuth) return;
      if (error.status === 401) setAuthentication(false);
      else if (error.status === 403) { clearRows("当前账号没有访问此数据的权限"); $("dbprogSummary").hidden = true; }
      else {
        clearRows("数据暂时不可用，请刷新重试");
        $("dbprogSummary").hidden = true;
        $("dbprogServices").replaceChildren();
        $("dbprogConnectionPanel").dataset.health = "ERROR";
        $("dbprogConnectionDot").className = "status-dot ERROR";
        $("dbprogConnectionText").textContent = "DBProg 请求失败";
      }
      announce(error.name === "AbortError" ? "DBProg 响应超时，请稍后刷新重试。" : error.message || "数仓连接失败，请稍后重试。", "ERROR");
    } finally { setBusy(false); }
  }
  function renderServices(health) {
    const root = $("dbprogServices"), services = health?.services || {};
    const states = { healthy: ["正常", "AVAILABLE"], slow: ["响应较慢", "STALE"], connection_failed: ["连接失败", "ERROR"], not_configured: ["未配置", "WAITING_CONFIG"] };
    root.replaceChildren();
    for (const [key, label] of [["postgresql", "PostgreSQL"], ["redis", "Redis"], ["objectStorage", "MinIO / 存储"]]) {
      if (!services[key]) continue;
      const [text, state] = states[services[key].status] || ["状态未知", "NO_DATA"];
      const chip = document.createElement("span");
      chip.className = `dbprog-service ${state}`;
      chip.dataset.health = state;
      chip.textContent = `${label} · ${text}`;
      root.append(chip);
    }
  }
  function renderStatus(data) {
    const available = Boolean(data.available), state = available ? "AVAILABLE" : data.configured ? "ERROR" : "WAITING_CONFIG";
    $("dbprogConnectionPanel").dataset.health = state;
    $("dbprogConnectionDot").className = `status-dot ${state}`;
    $("dbprogConnectionText").textContent = available ? "DBProg 服务在线" : data.configured ? "DBProg 暂不可用" : "等待连接 DBProg";
    $("dbprogHealthText").textContent = data.message || (available ? "数仓接口已连接，登录后可查看数据库与存储状态。" : "请检查 DBProg 服务后刷新重试。");
    setAuthentication(Boolean(data.authenticated), data.user || null);
    renderServices(data.health);
    if (!available) { clearRows("DBProg 服务暂不可用"); $("dbprogSummary").hidden = true; }
    $("dbprogLoginForm").hidden = !available || authenticated;
    return available;
  }
  async function loadOverview() {
    const data = await request("overview"), summary = data.summary || {}, root = $("dbprogSummary");
    root.replaceChildren();
    for (const [label, value] of [["数据集", summary.dataset_count], ["数据版本", summary.version_count], ["文件", summary.file_count], ["文件总量", bytes(summary.managed_bytes)]]) {
      const item = document.createElement("div"), name = document.createElement("span"), count = document.createElement("b");
      item.className = "dbprog-summary-item";
      name.textContent = label;
      count.textContent = value === null || value === undefined ? "—" : String(value);
      item.append(name, count); root.append(item);
    }
    root.hidden = false;
  }
  function tableHeader(labels) {
    const row = document.createElement("tr");
    for (const text of labels) { const node = document.createElement("th"); node.textContent = text; row.append(node); }
    $("dbprogTableHead").replaceChildren(row);
  }
  function action(label, handler) {
    const button = document.createElement("button");
    button.type = "button"; button.textContent = label;
    button.addEventListener("click", handler);
    return button;
  }
  function renderFileDetail(file) {
    const root = $("dbprogFileFields");
    root.replaceChildren();
    const tags = Array.isArray(file.tags) ? file.tags.map(item => typeof item === "string" ? item : item?.name || item?.tag?.name).filter(Boolean).join("、") : null;
    for (const [label, value] of [["文件名", file.originalName], ["文件 ID", file.id], ["文件类型", file.mimeType], ["文件大小", bytes(file.fileSize)], ["所在目录", file.folderPath || "根目录"], ["识别类型", file.recognitionType], ["机器人", file.robot?.name], ["采集时间", dateText(file.capturedAt)], ["入库时间", dateText(file.createdAt)], ["标签", tags]]) {
      const term = document.createElement("dt"), description = document.createElement("dd");
      term.textContent = label;
      description.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
      root.append(term, description);
    }
    const term = document.createElement("dt"), description = document.createElement("dd"), link = document.createElement("a");
    term.textContent = "下载"; link.className = "button"; link.textContent = "下载文件";
    link.href = `/api/dbprog/files/${encodeURIComponent(file.id)}/download`;
    link.setAttribute("download", "");
    description.append(link); root.append(term, description);
    $("dbprogFileDetail").hidden = false;
  }
  async function loadRows() {
    const keyword = $("dbprogKeyword").value.trim(), params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
    if (keyword) params.set("keyword", keyword);
    if (dataset) { params.set("dataset_id", dataset.id); if (folderPath !== null) params.set("folder_path", folderPath); }
    $("dbprogBreadcrumb").textContent = dataset ? ` / ${dataset.name || dataset.id}` : "全部数据集";
    $("dbprogSearchLabel").textContent = dataset ? "搜索文件" : "搜索数据集";
    $("dbprogFolderField").hidden = !dataset;
    tableHeader(dataset ? ["文件名", "类型 / 大小", "目录", "入库时间", "操作"] : ["数据集", "版本", "更新时间", "操作"]);
    clearRows("正在读取…");
    const data = await request(`${dataset ? "files" : "datasets"}?${params}`), items = Array.isArray(data.items) ? data.items : [], body = $("dbprogTableBody");
    body.replaceChildren();
    total = Number.isFinite(Number(data.total)) ? Number(data.total) : items.length;
    for (const item of items) {
      const row = document.createElement("tr");
      if (dataset) {
        const name = cell(row, null), button = action(item.originalName || item.id, () => renderFileDetail(item));
        button.className = "dbprog-file-name"; name.replaceChildren(button);
        cell(row, `${item.mimeType || "—"} · ${bytes(item.fileSize)}`);
        cell(row, item.folderPath || "根目录"); cell(row, dateText(item.createdAt));
        cell(row, null).replaceChildren(action("详情 / 下载", () => renderFileDetail(item)));
      } else {
        const name = cell(row, item.name || item.id);
        if (item.description) { const note = document.createElement("small"); note.textContent = item.description; name.append(note); }
        cell(row, item.versionCount ?? "—"); cell(row, dateText(item.updatedAt || item.createdAt));
        cell(row, null).replaceChildren(action("查看文件", () => openDataset(item)));
      }
      body.append(row);
    }
    if (!items.length) { const row = document.createElement("tr"), node = cell(row, keyword ? "没有匹配的结果" : "此处暂无数据"); node.colSpan = dataset ? 5 : 4; node.className = "empty-block"; body.append(row); }
    $("dbprogPageInfo").textContent = `第 ${page} / ${Math.max(1, Math.ceil(total / PAGE_SIZE))} 页 · 共 ${total} 项`;
    announce(`已读取${dataset ? "文件" : "数据集"}，数据来源：DBProg。`, "AVAILABLE");
  }
  async function loadFolders() {
    const data = await request(`datasets/${encodeURIComponent(dataset.id)}/folders`), root = $("dbprogFolder");
    root.replaceChildren();
    for (const [value, label] of [["all", "全部目录"], ["root", "根目录"]]) { const node = document.createElement("option"); node.value = value; node.textContent = label; root.append(node); }
    for (const folder of Array.isArray(data.items) ? data.items : []) {
      if (typeof folder.path !== "string" || !folder.path) continue;
      const node = document.createElement("option"); node.value = `path:${folder.path}`; node.textContent = folder.path; root.append(node);
    }
    root.value = "all";
  }
  function openDataset(value) {
    run(async () => { dataset = value; page = 1; folderPath = null; $("dbprogKeyword").value = ""; await loadFolders(); await loadRows(); });
  }
  async function refresh() {
    await run(async () => {
      announce("正在连接 DBProg…");
      const data = await request("status");
      if (!renderStatus(data)) { announce(data.message || "DBProg 服务暂不可用。", "ERROR"); return; }
      if (authenticated) { await loadOverview(); await loadRows(); }
      else announce("DBProg 服务在线，请使用数仓账号登录。");
    });
  }
  $("dbprogRefresh").addEventListener("click", refresh);
  $("dbprogLoginForm").addEventListener("submit", event => {
    event.preventDefault();
    run(async () => {
      const email = $("dbprogEmail").value.trim(), password = $("dbprogPassword").value;
      $("dbprogPassword").value = "";
      announce("正在登录 DBProg…");
      const data = await request("login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) });
      setAuthentication(Boolean(data.authenticated), data.user || null);
      if (!authenticated) throw Error(data.message || "DBProg 登录未完成。");
      dataset = null; page = 1;
      const status = await request("status");
      if (!renderStatus(status)) throw Error(status.message || "DBProg 服务暂不可用。");
      await loadOverview(); await loadRows();
    });
  });
  $("dbprogLogout").addEventListener("click", () => run(async () => {
    await request("logout", { method: "POST" });
    setAuthentication(false); $("dbprogServices").replaceChildren();
    announce("已退出 DBProg 数仓。观测平台仍保持登录。"); $("dbprogPassword").value = "";
  }));
  $("dbprogSearchForm").addEventListener("submit", event => { event.preventDefault(); run(async () => { page = 1; await loadRows(); }); });
  $("dbprogDatasetsHome").addEventListener("click", () => run(async () => { dataset = null; page = 1; folderPath = null; $("dbprogKeyword").value = ""; await loadRows(); }));
  $("dbprogFolder").addEventListener("change", () => run(async () => { const value = $("dbprogFolder").value; folderPath = value === "all" ? null : value === "root" ? "" : value.slice(5); page = 1; await loadRows(); }));
  $("dbprogPrev").addEventListener("click", () => run(async () => { if (page > 1) { page--; await loadRows(); } }));
  $("dbprogNext").addEventListener("click", () => run(async () => { if (page * PAGE_SIZE < total) { page++; await loadRows(); } }));
  $("dbprogCloseDetail").addEventListener("click", () => { $("dbprogFileDetail").hidden = true; });
  refresh();
})();
