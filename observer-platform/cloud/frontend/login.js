"use strict";
(function () {
  const form = document.getElementById("login-form");
  const button = document.getElementById("login-submit");
  const message = document.getElementById("login-message");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    button.disabled = true;
    message.textContent = "正在验证登录…";
    try {
      const response = await fetch("/api/login", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: form.username.value.trim(), password: form.password.value })
      });
      if (response.ok) {
        const value = await response.json();
        form.password.value = "";
        window.location.replace(value.approved === false ? "/pending" : "/");
        return;
      }
      message.textContent = response.status === 429 ? "尝试次数过多，请稍后重试"
        : response.status === 401 ? "账号或密码不正确"
        : "暂时无法登录，请稍后重试";
    } catch (_) { message.textContent = "连接失败，请检查网络后重试"; }
    finally { button.disabled = false; }
  });
})();
