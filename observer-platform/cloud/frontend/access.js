"use strict";
(function () {
  const page = document.body.dataset.accessPage;
  const message = document.getElementById("access-message");
  const username = document.getElementById("access-username");
  const logout = document.getElementById("access-logout");
  let leaving = false;
  let timer = null;
  let checking = false;
  let loadingUsers = false;
  let mutatingUser = false;
  let canManageUsers = false;
  let users = [];
  let usersSignature = null;

  function announce(text, tone = "error") {
    message.textContent = text;
    message.dataset.tone = tone;
  }
  function redirect(path) {
    leaving = true;
    clearTimeout(timer);
    window.location.replace(path);
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      return await fetch(path, { ...options, credentials: "same-origin", cache: "no-store", signal: controller.signal });
    } finally { clearTimeout(timeout); }
  }
  function schedule(callback, delay) {
    clearTimeout(timer);
    if (!leaving) timer = setTimeout(callback, delay);
  }
  function whenVisible(callback) {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && !leaving) callback();
    });
  }
  function dateText(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "—";
    return date.toLocaleString("zh-CN", { hour12: false });
  }
  function make(tag, className, text) {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  if (logout) logout.addEventListener("click", async () => {
    logout.disabled = true;
    clearTimeout(timer);
    try {
      const response = await request("/api/logout", { method: "POST" });
      if (response.ok || response.status === 401) { redirect("/login"); return; }
      throw new Error("Logout failed");
    } catch (_) {
      announce("退出失败，请检查网络后重试");
      logout.disabled = false;
      schedule(page === "pending" ? checkPending : loadUsers, 5000);
    }
  });

  if (page === "register") {
    const form = document.getElementById("register-form");
    const submit = document.getElementById("register-submit");
    const account = document.getElementById("username");
    const password = document.getElementById("password");
    const confirm = document.getElementById("password-confirm");
    function comparePasswords() {
      confirm.setCustomValidity(confirm.value && confirm.value !== password.value ? "两次输入的密码不一致" : "");
    }
    password.addEventListener("input", comparePasswords);
    confirm.addEventListener("input", comparePasswords);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      comparePasswords();
      if (!form.reportValidity()) return;
      if (submit.disabled) return;
      account.value = account.value.trim().toLowerCase();
      submit.disabled = true;
      announce("正在注册…", "neutral");
      try {
        const response = await request("/api/register", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: account.value, password: password.value })
        });
        if (response.ok) {
          password.value = "";
          confirm.value = "";
          redirect("/pending");
          return;
        }
        announce(response.status === 409 ? "该账号已被注册，请更换账号或返回登录"
          : response.status === 400 ? "请检查账号格式，密码不能为空"
          : response.status === 429 ? "注册尝试过多，请稍后重试"
          : "暂时无法注册，请稍后重试");
      } catch (_) { announce("连接失败，请检查网络后重试"); }
      finally { submit.disabled = false; }
    });
    return;
  }

  async function getSession() {
    const response = await request("/api/session");
    if (response.status === 401) { redirect("/login"); return null; }
    if (!response.ok) throw new Error("Session unavailable");
    const value = await response.json();
    if (value.authenticated !== true) { redirect("/login"); return null; }
    if (username) username.textContent = value.username;
    return value;
  }
  if (page === "password") {
    const form = document.getElementById("password-form");
    const current = document.getElementById("current-password");
    const password = document.getElementById("new-password");
    const confirm = document.getElementById("new-password-confirm");
    const submit = document.getElementById("password-submit");
    const login = document.getElementById("password-login");
    function compare() {
      confirm.setCustomValidity(confirm.value && confirm.value !== password.value ? "两次输入的密码不一致" : "");
    }
    function clear() { current.value = ""; password.value = ""; confirm.value = ""; }
    password.addEventListener("input", compare);
    confirm.addEventListener("input", compare);
    getSession().catch(() => announce("连接失败，请刷新页面重试"));
    form.addEventListener("submit", async event => {
      event.preventDefault();
      compare();
      if (submit.disabled || !form.reportValidity()) return;
      submit.disabled = true;
      announce("正在保存…", "neutral");
      try {
        const response = await request("/api/account/password", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ currentPassword: current.value, newPassword: password.value })
        });
        if (response.status === 401) { clear(); redirect("/login"); return; }
        if (response.ok) {
          const value = await response.json();
          if (value.changed !== true) throw new Error("Invalid password response");
          clear();
          form.hidden = true;
          login.hidden = false;
          announce("密码已修改，请重新登录", "success");
          return;
        }
        announce(response.status === 400 ? "当前密码不正确或新密码为空"
          : response.status === 409 ? "密码已更新，请重新登录后再试"
          : response.status === 429 ? "操作过于频繁，请稍后重试"
          : response.status === 413 ? "提交内容过大"
          : "保存失败，请稍后重试");
      } catch (_) { announce("提交结果未确认，请重新登录核实"); }
      finally { submit.disabled = false; }
    });
    window.addEventListener("pagehide", clear);
    window.addEventListener("pageshow", event => { if (event.persisted) getSession().catch(() => announce("连接失败，请刷新页面重试")); });
    return;
  }
  async function checkPending() {
    if (checking || leaving || logout.disabled) return;
    checking = true;
    try {
      const value = await getSession();
      if (!value) return;
      if (value.approved === true) { redirect("/"); return; }
      announce("", "neutral");
    } catch (_) { announce("连接暂时中断，正在重试…"); }
    finally {
      checking = false;
      schedule(checkPending, 5000);
    }
  }

  function renderUsers() {
    const signature = JSON.stringify(users);
    if (signature === usersSignature) return;
    usersSignature = signature;
    for (const status of ["pending", "approved"]) {
      const list = document.getElementById(`${status}-users`);
      const matching = users.filter(user => user.status === status);
      const fragment = document.createDocumentFragment();
      for (const user of matching) {
        const row = make("li", "user-row");
        const info = make("div", "user-info");
        info.append(make("h3", "user-name", user.username));
        const times = make("p", "user-times");
        times.append(make("span", "", `注册于 ${dateText(user.created_at)}`));
        if (status === "approved") times.append(make("span", "", `批准于 ${dateText(user.approved_at)}`));
        info.append(times);
        row.append(info);
        const actions = make("div", "user-actions");
        if (status === "pending") {
          const button = make("button", "access-button primary", "批准权限");
          button.type = "button";
          button.dataset.approveId = String(user.id);
          button.setAttribute("aria-label", `批准 ${user.username} 的访问权限`);
          button.disabled = mutatingUser;
          actions.append(button);
        } else actions.append(make("span", "user-status", "已批准"));
        if (canManageUsers) {
          const reset = make("button", "access-button", "重置密码");
          reset.type = "button";
          reset.dataset.resetPasswordId = String(user.id);
          reset.setAttribute("aria-label", `重置 ${user.username} 的密码`);
          reset.disabled = mutatingUser || loadingUsers;
          actions.append(reset);
          const button = make("button", "access-button danger", "删除账号");
          button.type = "button";
          button.dataset.deleteId = String(user.id);
          button.setAttribute("aria-label", `删除账号 ${user.username}`);
          button.disabled = mutatingUser || loadingUsers;
          actions.append(button);
        }
        row.append(actions);
        fragment.append(row);
      }
      list.replaceChildren(fragment);
      document.getElementById(`${status}-count`).textContent = String(matching.length);
      const empty = document.getElementById(`${status}-empty`);
      empty.textContent = status === "pending" ? "暂无待审批账号" : "暂无已批准账号";
      empty.hidden = matching.length > 0;
    }
  }
  function usersBusy(busy) {
    document.getElementById("users-refresh").disabled = busy;
    document.querySelectorAll("[data-approve-id], [data-delete-id], [data-reset-password-id]").forEach(button => { button.disabled = busy; });
  }
  async function loadUsers() {
    if (!canManageUsers || loadingUsers || mutatingUser || leaving || logout.disabled) return;
    loadingUsers = true;
    usersBusy(true);
    try {
      const response = await request("/api/admin/users");
      if (response.status === 401) { redirect("/login"); return; }
      if (response.status === 403) {
        const value = await getSession();
        if (value) redirect(value.approved === true ? "/" : "/pending");
        return;
      }
      if (!response.ok) throw new Error("User list unavailable");
      const value = await response.json();
      if (!Array.isArray(value.users)) throw new Error("Invalid user list");
      users = value.users;
      renderUsers();
      if (message.dataset.tone !== "success") announce("", "neutral");
    } catch (_) {
      announce("列表加载失败，请稍后刷新");
      if (usersSignature === null) {
        document.getElementById("pending-empty").textContent = "暂时无法加载";
        document.getElementById("approved-empty").textContent = "暂时无法加载";
      }
    } finally {
      loadingUsers = false;
      usersBusy(false);
      schedule(loadUsers, 15000);
    }
  }
  async function approveUser(id) {
    if (!canManageUsers || mutatingUser || loadingUsers || leaving) return;
    const user = users.find(value => String(value.id) === id && value.status === "pending");
    if (!user) return;
    mutatingUser = true;
    usersBusy(true);
    announce(`正在批准 ${user.username}…`, "neutral");
    try {
      const response = await request(`/api/admin/users/${encodeURIComponent(id)}/approve`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
      });
      if (response.status === 401) { redirect("/login"); return; }
      if (response.status === 403) {
        announce("当前账号无审批权限");
        return;
      }
      if (!response.ok) throw new Error("Approval failed");
      const value = await response.json();
      if (!value.user || value.user.status !== "approved") throw new Error("Invalid approval response");
      users = users.map(item => String(item.id) === id ? value.user : item);
      renderUsers();
      announce(`已批准 ${user.username} 的访问权限`, "success");
    } catch (_) { announce("批准失败，请刷新列表后重试"); }
    finally {
      mutatingUser = false;
      usersBusy(false);
      schedule(loadUsers, 15000);
    }
  }

  async function deleteUser(id) {
    if (!canManageUsers || mutatingUser || loadingUsers || leaving) return;
    const user = users.find(value => String(value.id) === id);
    if (!user || !window.confirm(`确定删除账号“${user.username}”吗？删除后该账号将无法继续登录。`)) return;
    mutatingUser = true;
    usersBusy(true);
    let refresh = false;
    announce(`正在删除 ${user.username}…`, "neutral");
    try {
      const response = await request(`/api/admin/users/${encodeURIComponent(id)}`, {
        method: "DELETE", headers: { "Content-Type": "application/json" }, body: "{}"
      });
      if (response.status === 401) { redirect("/login"); return; }
      if (response.status === 403) { announce("当前账号无删除权限"); return; }
      if (response.status === 409) { announce("该账号不能删除"); return; }
      if (response.status === 404) {
        announce("该账号已不存在", "success");
        refresh = true;
        return;
      }
      if (!response.ok) throw new Error("Deletion failed");
      const value = await response.json();
      if (value.deleted !== true || !value.user || String(value.user.id) !== id) throw new Error("Invalid deletion response");
      users = users.filter(item => String(item.id) !== id);
      renderUsers();
      announce(`已删除账号 ${user.username}`, "success");
      refresh = true;
    } catch (_) { announce("删除失败，请刷新列表后重试"); }
    finally {
      mutatingUser = false;
      usersBusy(false);
      if (refresh && !leaving) await loadUsers();
      else schedule(loadUsers, 15000);
    }
  }

  if (page === "pending") {
    checkPending();
    whenVisible(checkPending);
  } else if (page === "users") {
    const dialog = document.getElementById("reset-password-dialog");
    const resetForm = document.getElementById("reset-password-form");
    const resetPassword = document.getElementById("reset-new-password");
    const resetConfirm = document.getElementById("reset-new-password-confirm");
    const resetMessage = document.getElementById("reset-password-message");
    const resetCancel = document.getElementById("reset-password-cancel");
    const resetSubmit = document.getElementById("reset-password-submit");
    let resetUser = null;
    function clearReset() {
      resetUser = null;
      resetPassword.value = "";
      resetConfirm.value = "";
      resetConfirm.setCustomValidity("");
      resetMessage.textContent = "";
    }
    function compareReset() {
      resetConfirm.setCustomValidity(resetConfirm.value && resetConfirm.value !== resetPassword.value ? "两次输入的密码不一致" : "");
    }
    function openReset(id) {
      if (!canManageUsers || loadingUsers || mutatingUser || leaving) return;
      const user = users.find(value => String(value.id) === id);
      if (!user) return;
      clearReset();
      resetUser = { ...user };
      document.getElementById("reset-password-username").textContent = user.username;
      dialog.showModal();
      resetPassword.focus();
    }
    resetPassword.addEventListener("input", compareReset);
    resetConfirm.addEventListener("input", compareReset);
    resetCancel.addEventListener("click", () => { if (!mutatingUser) dialog.close(); });
    dialog.addEventListener("close", clearReset);
    dialog.addEventListener("cancel", event => { if (mutatingUser) event.preventDefault(); });
    window.addEventListener("pagehide", clearReset);
    resetForm.addEventListener("submit", async event => {
      event.preventDefault();
      compareReset();
      if (!resetUser || mutatingUser || loadingUsers || !resetForm.reportValidity()) return;
      const user = resetUser;
      mutatingUser = true;
      usersBusy(true);
      resetCancel.disabled = true;
      resetSubmit.disabled = true;
      resetMessage.textContent = "正在保存…";
      let refresh = false;
      try {
        const response = await request(`/api/admin/users/${encodeURIComponent(user.id)}/password`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ newPassword: resetPassword.value })
        });
        if (response.status === 401) { clearReset(); redirect("/login"); return; }
        if (response.ok) {
          const value = await response.json();
          if (value.changed !== true || String(value.user?.id) !== String(user.id)) throw new Error("Invalid password response");
          dialog.close();
          announce(`已重置 ${user.username} 的密码`, "success");
          refresh = true;
          return;
        }
        resetMessage.textContent = response.status === 403 ? "当前账号无重置权限"
          : response.status === 404 ? "该账号已不存在"
          : response.status === 409 ? "账号或密码已更新，请关闭后刷新重试"
          : response.status === 429 ? "操作过于频繁，请稍后重试"
          : response.status === 400 ? "新密码不能为空"
          : response.status === 413 ? "提交内容过大"
          : "保存失败，请稍后重试";
      } catch (_) { resetMessage.textContent = "提交结果未确认，请重新登录核实"; }
      finally {
        mutatingUser = false;
        usersBusy(false);
        resetCancel.disabled = false;
        resetSubmit.disabled = false;
        if (refresh && !leaving) await loadUsers();
        else schedule(loadUsers, 15000);
      }
    });
    document.getElementById("users-refresh").addEventListener("click", () => { announce("", "neutral"); loadUsers(); });
    for (const status of ["pending", "approved"]) {
      document.getElementById(`${status}-users`).addEventListener("click", event => {
        const button = event.target.closest("button[data-approve-id], button[data-delete-id], button[data-reset-password-id]");
        if (!button || button.disabled) return;
        if (button.dataset.resetPasswordId) openReset(button.dataset.resetPasswordId);
        else if (button.dataset.deleteId) deleteUser(button.dataset.deleteId);
        else if (button.dataset.approveId) approveUser(button.dataset.approveId);
      });
    }
    (async () => {
      try {
        const value = await getSession();
        if (!value) return;
        if (value.role !== "admin") { redirect(value.approved === true ? "/" : "/pending"); return; }
        canManageUsers = true;
        await loadUsers();
      } catch (_) {
        announce("连接失败，请刷新页面重试");
        document.getElementById("pending-empty").textContent = "暂时无法加载";
        document.getElementById("approved-empty").textContent = "暂时无法加载";
      }
    })();
    whenVisible(loadUsers);
  }
  window.addEventListener("pagehide", () => { leaving = true; clearTimeout(timer); });
  window.addEventListener("pageshow", event => {
    if (!event.persisted) return;
    leaving = false;
    if (page === "pending") checkPending();
    if (page === "users") loadUsers();
  });
})();
