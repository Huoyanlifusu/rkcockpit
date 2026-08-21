/* Optional single-admin login. The token is exchanged for an HttpOnly cookie
   and is never persisted in localStorage or a URL. */
(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});

  function nodes() {
    return {
      mask: document.getElementById("auth-modal"),
      token: document.getElementById("auth-token"),
      submit: document.getElementById("auth-submit"),
      error: document.getElementById("auth-error"),
    };
  }

  function requireLogin(message) {
    const n = nodes();
    if (!n.mask) return;
    n.mask.style.display = "flex";
    if (n.error) n.error.textContent = message || "请输入管理员访问令牌";
    if (n.token) {
      n.token.value = "";
      n.token.focus();
    }
  }

  function hideLogin() {
    const n = nodes();
    if (n.mask) n.mask.style.display = "none";
    if (n.token) n.token.value = "";
  }

  async function login() {
    const n = nodes();
    const token = n.token ? n.token.value : "";
    if (!token) { requireLogin("访问令牌不能为空"); return; }
    if (n.submit) n.submit.disabled = true;
    try {
      const resp = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      let data = null;
      try { data = await resp.json(); } catch (e) { /* ignore */ }
      if (!resp.ok || !data || data.ok === false) {
        throw new Error((data && data.error) || ("HTTP " + resp.status));
      }
      hideLogin();
      window.location.reload();
    } catch (e) {
      requireLogin("登录失败: " + e.message);
    } finally {
      if (n.submit) n.submit.disabled = false;
    }
  }

  async function checkStatus() {
    try {
      const resp = await fetch("/api/auth/status");
      const data = await resp.json();
      if (data.enabled && !data.authenticated) requireLogin();
      else hideLogin();
    } catch (e) {
      /* Normal page errors remain responsible for reporting connectivity. */
    }
  }

  function bind() {
    const n = nodes();
    if (n.submit) n.submit.onclick = login;
    if (n.token) n.token.onkeydown = (e) => {
      if (e.key === "Enter") login();
    };
    checkStatus();
  }

  R.auth = { requireLogin, checkStatus };
  bind();
})();
