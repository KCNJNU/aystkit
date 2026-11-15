"use strict";

// Session cookies are managed by the browser. Device credentials never reach this UI.
(function (global) {
  let expired = false;
  function unauthorized() {
    if (expired) return;
    expired = true;
    global.dispatchEvent(new Event("cloud-auth-lost"));
    global.location.replace("/login");
  }
  async function checkSession() {
    if (expired) return false;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/api/session", { credentials: "same-origin", cache: "no-store", signal: controller.signal });
      if (response.status === 401) { unauthorized(); return false; }
      if (!response.ok) return null;
      const value = await response.json();
      if (value.authenticated !== true) { unauthorized(); return false; }
      if (value.approved === false) {
        expired = true;
        global.dispatchEvent(new Event("cloud-auth-lost"));
        global.location.replace("/pending");
        return false;
      }
      const usersLink = document.getElementById("users-link");
      if (usersLink) usersLink.hidden = value.role !== "admin";
      const name = document.getElementById("viewer-name");
      if (name) name.textContent = `已登录 · ${value.username || "观察者"}`;
      return true;
    } catch (_) { return null; }
    finally { clearTimeout(timeout); }
  }
  function time(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
  }
  function update(snapshot, linkFresh = true) {
    const cloud = snapshot.cloud || {};
    const lastSeen = cloud.lastSeen ?? cloud.last_seen;
    const lastObservation = cloud.lastObservation ?? cloud.last_observation;
    const online = cloud.online === true;
    const status = !online ? (lastSeen === null || lastSeen === undefined ? "WAITING_UPSTREAM" : "STALE")
      : (cloud.status === "AVAILABLE" ? "AVAILABLE" : "STALE");
    const text = !linkFresh ? "页面连接中断" : !online ? (status === "STALE" ? "机器人离线 · 数据过期" : "等待机器人上线")
      : (status === "AVAILABLE" ? "机器人在线" : "机器人在线 · 数据过期");
    const set = (id, value) => { const element = document.getElementById(id); if (element) element.textContent = value; };
    set("cloud-robot-status", text);
    set("cloud-robot-id", cloud.robotId ?? cloud.robot_id ?? "—");
    set("cloud-source-id", `工控机 · ${cloud.sourceId ?? cloud.source_id ?? "—"}`);
    set("cloud-last-seen", time(lastSeen));
    set("cloud-last-observation", time(lastObservation));
    set("cloud-last-received", time(cloud.lastReceived ?? snapshot.meta.lastReceived ?? snapshot.meta.received_timestamp));
    set("cloud-actual-timestamp", time(cloud.actualTimestamp ?? snapshot.meta.actualTimestamp ?? snapshot.meta.message_timestamp));
    set("cloud-source-note", !linkFresh ? "当前状态待确认，保留最后一次观测。" : !online && status === "STALE"
      ? "仅显示最后一次真实观测，画面已暂停。"
      : online && status !== "AVAILABLE" ? "等待新观测。" : online ? "" : "等待采集端上传。");
    const note = document.getElementById("cloud-source-note");
    if (note) note.hidden = linkFresh && online && status === "AVAILABLE";
    const dot = document.getElementById("cloud-robot-dot");
    if (dot) dot.className = `status-dot ${linkFresh ? status : "STALE"}`;
  }
  const button = document.getElementById("logout-button");
  if (button) button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const response = await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
      if (response.ok || response.status === 401) { unauthorized(); return; }
      throw new Error("Logout failed");
    } catch (_) {
      document.getElementById("account-message").textContent = "退出失败，请检查网络后重试";
      button.disabled = false;
    }
  });
  global.CloudObserver = Object.freeze({ unauthorized, checkSession, update, isExpired: () => expired, ready: checkSession() });
})(window);

// Display preferences only. No sensor lifecycle, network request, or media source
// is owned here; the existing dashboard remains the sole route/data controller.
(function (global) {
  function ready() {
    const body = document.body;
    if (!body.classList.contains("observer-workspace")) return;
    const sidebar = document.getElementById("workspace-sidebar");
    const toggle = document.getElementById("workspace-nav-toggle");
    const accountToggle = document.getElementById("workspace-account-toggle");
    const account = accountToggle?.closest(".viewer-account");
    const top = document.getElementById("workspace-to-top");
    const layoutButtons = [...document.querySelectorAll("[data-camera-layout-choice]")];
    let sidebarCollapsed = readPreference("observer.sidebar", "open") === "closed";
    let cameraLayout = readPreference("observer.cameraLayout", "two");
    if (!["one", "two", "three"].includes(cameraLayout)) cameraLayout = "two";
    function readPreference(key, fallback) {
      try { return global.localStorage.getItem(key) || fallback; } catch (_) { return fallback; }
    }
    function savePreference(key, value) {
      try { global.localStorage.setItem(key, value); } catch (_) { /* Private browsing still works. */ }
    }
    function mobile() { return global.matchMedia("(max-width: 760px)").matches; }
    function resizeCharts() { global.dispatchEvent(new Event("resize")); }
    function renderSidebar() {
      const collapsed = sidebarCollapsed && !mobile();
      body.dataset.navCollapsed = String(collapsed);
      if (sidebar) sidebar.inert = collapsed;
      if (toggle) {
        toggle.setAttribute("aria-expanded", String(!collapsed));
        toggle.setAttribute("aria-label", collapsed ? "展开导航" : "收起导航");
        toggle.title = collapsed ? "展开导航" : "收起导航";
      }
    }
    function renderCameraLayout() {
      const effective = mobile() ? "one" : cameraLayout === "three" && global.innerWidth < 1100 ? "two" : cameraLayout;
      body.dataset.cameraLayout = effective;
      layoutButtons.forEach(button => {
        const mode = button.dataset.cameraLayoutChoice;
        const selected = mode === effective;
        button.classList.toggle("active", selected);
        button.setAttribute("aria-pressed", String(selected));
        button.disabled = mobile() ? mode !== "one" : mode === "three" && global.innerWidth < 1100;
      });
    }
    function closeAccount(focus = false) {
      account?.classList.remove("workspace-account-open");
      accountToggle?.setAttribute("aria-expanded", "false");
      accountToggle?.setAttribute("aria-label", "展开账号操作");
      if (focus) accountToggle?.focus();
    }
    toggle?.addEventListener("click", () => {
      sidebarCollapsed = !sidebarCollapsed;
      savePreference("observer.sidebar", sidebarCollapsed ? "closed" : "open");
      renderSidebar();
      resizeCharts();
    });
    layoutButtons.forEach(button => button.addEventListener("click", () => {
      if (button.disabled) return;
      cameraLayout = button.dataset.cameraLayoutChoice;
      savePreference("observer.cameraLayout", cameraLayout);
      renderCameraLayout();
      resizeCharts();
    }));
    accountToggle?.addEventListener("click", () => {
      const open = !account.classList.contains("workspace-account-open");
      account.classList.toggle("workspace-account-open", open);
      accountToggle.setAttribute("aria-expanded", String(open));
      accountToggle.setAttribute("aria-label", open ? "收起账号操作" : "展开账号操作");
    });
    document.addEventListener("click", event => {
      if (account && !account.contains(event.target)) closeAccount();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && account?.classList.contains("workspace-account-open")) closeAccount(true);
    });
    const pageLinks = [];
    document.querySelectorAll(".subpage-picker[data-subpage-picker]").forEach(picker => {
      const page = picker.dataset.subpagePicker;
      if (!["system", "robot", "media"].includes(page)) return;
      const heading = picker.closest(".page")?.querySelector(".page-head");
      if (!heading) return;
      const nav = document.createElement("nav");
      nav.className = "workspace-subnav";
      nav.setAttribute("aria-label", `${page === "system" ? "工控机系统" : page === "robot" ? "机器人状态" : "实时画面"}分类`);
      [...picker.options].forEach(option => {
        if (!/^[a-z]+$/.test(option.value)) return;
        const link = document.createElement("a");
        const route = `${page}/${option.value}`;
        link.href = `#${route}`;
        link.textContent = option.textContent;
        link.addEventListener("click", event => {
          if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          if (!global.RKObserverDashboard) return;
          event.preventDefault();
          global.RKObserverDashboard.openPage(route);
        });
        nav.append(link);
        pageLinks.push({ link, route });
      });
      heading.after(nav);
    });
    function renderRoute() {
      const current = global.RKObserverDashboard?.getRoute();
      pageLinks.forEach(({ link, route }) => {
        if (route === current) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
      });
      closeAccount();
    }
    document.querySelectorAll(".media-card").forEach(card => {
      if (typeof card.requestFullscreen !== "function") return;
      const heading = card.querySelector(".panel-head");
      if (!heading) return;
      const label = heading.querySelector("h2")?.textContent || "画面";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "workspace-icon-button workspace-media-expand";
      button.setAttribute("aria-label", `${label}全屏`);
      button.title = "全屏";
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 20 20");
      svg.setAttribute("aria-hidden", "true");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", "M7 3H3v4m10-4h4v4M3 13v4h4m10-4v4h-4");
      svg.append(path);
      button.append(svg);
      button.addEventListener("click", async () => {
        try {
          if (document.fullscreenElement === card) await document.exitFullscreen();
          else await card.requestFullscreen();
        } catch (_) { button.title = "浏览器未允许全屏"; }
      });
      document.addEventListener("fullscreenchange", () => {
        const active = document.fullscreenElement === card;
        button.setAttribute("aria-label", `${label}${active ? "退出全屏" : "全屏"}`);
        button.title = active ? "退出全屏" : "全屏";
      });
      heading.append(button);
    });
    function renderScroll() { if (top) top.hidden = global.scrollY < 440; }
    top?.addEventListener("click", () => {
      global.scrollTo({ top: 0, behavior: global.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    });
    global.addEventListener("scroll", renderScroll, { passive: true });
    global.addEventListener("resize", () => { renderSidebar(); renderCameraLayout(); if (!mobile()) closeAccount(); });
    global.addEventListener("observer-page-change", renderRoute);
    renderSidebar();
    renderCameraLayout();
    renderRoute();
    renderScroll();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready, { once: true });
  else ready();
})(window);
