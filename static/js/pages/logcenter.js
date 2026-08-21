(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const el = R.dom.el;
  const api = R.api.fetch;
  const hostError = R.ui.hostError;

  const POLL_MS = 3000;
  let timer = null;
  let following = false;

  function section() {
    let sec = document.getElementById("tab-logcenter");
    if (!sec) {
      sec = el("section", "pane");
      sec.id = "tab-logcenter";
      const main = document.querySelector("main") || document.body;
      main.append(sec);
    }
    if (!sec.querySelector("#logc-device")) {
      sec.innerHTML =
      '<div class="toolbar">' +
      '<label>设备 <select id="logc-device"></select></label>' +
      '<label>源 <select id="logc-source"></select></label>' +
      '<label>过滤 <input id="logc-filter" placeholder="如 error"></label>' +
      '<label>行数 <input id="logc-lines" type="number" value="200" ' +
      'min="1" max="5000"></label>' +
      '<button id="logc-follow" class="btn">▶ 跟随</button>' +
      '<button id="logc-refresh" class="btn">刷新</button>' +
      '<span id="logc-status" class="muted"></span>' +
      "</div>" +
      '<div id="logc-error"></div>' +
      '<pre id="logc-view" class="log-view"></pre>';
    }
    return sec;
  }

  function currentDid() {
    return document.getElementById("logc-device").value;
  }

  async function loadSources() {
    const did = currentDid();
    if (!did) return;
    const sel = document.getElementById("logc-source");
    const keep = sel.value;
    sel.innerHTML = "";
    try {
      const r = await api("/api/logcenter/" + did + "/sources");
      const list = r.sources || [];
      let first = null;
      for (const s of list) {
        const o = el("option", "",
          (s.accessible ? "✓ " : "✗ ") + s.name + "  " + s.path);
        o.value = s.path;
        if (s.accessible && !first) first = s.path;
        sel.append(o);
      }
      if (keep && [...sel.options].some(o => o.value === keep)) sel.value = keep;
      else if (first) sel.value = first;
      document.getElementById("logc-status").textContent =
        "探测到 " + list.length + " 个日志源";
    } catch (e) {
      hostError(document.getElementById("logc-error"), e.message);
    }
  }

  async function refresh() {
    const errBox = document.getElementById("logc-error");
    const did = currentDid();
    if (!did) return;
    const qs = [
      "lines=" + (document.getElementById("logc-lines").value || 200),
      "source=" + encodeURIComponent(document.getElementById("logc-source").value),
    ];
    const f = document.getElementById("logc-filter").value.trim();
    if (f) qs.push("filter=" + encodeURIComponent(f));
    try {
      const r = await api("/api/logcenter/" + did + "/tail?" + qs.join("&"));
      const lines = r.lines || [];
      document.getElementById("logc-view").textContent =
        lines.join("\n") + (lines.length ? "\n" : "");
      errBox.innerHTML = "";
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  async function toggleFollow() {
    const errBox = document.getElementById("logc-error");
    const did = currentDid();
    if (!did) return;
    try {
      if (following) {
        await api("/api/logcenter/" + did + "/unfollow", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        stopPoll();
        setFollow(false);
      } else {
        const body = { source: document.getElementById("logc-source").value };
        const f = document.getElementById("logc-filter").value.trim();
        if (f) body.filter = f;
        await api("/api/logcenter/" + did + "/follow", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        setFollow(true);
        startPoll();
      }
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  function setFollow(on) {
    following = on;
    const btn = document.getElementById("logc-follow");
    btn.textContent = on ? "■ 停止跟随" : "▶ 跟随";
    btn.classList.toggle("danger", on);
    document.getElementById("logc-status").textContent = on ?
      "跟随中（3s 轮询 /tail）" : "探测到日志源";
  }

  function startPoll() {
    stopPoll();
    refresh();
    timer = setInterval(refresh, POLL_MS);
  }

  function stopPoll() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  async function render() {
    const errBox = document.getElementById("logc-error");
    errBox.innerHTML = "";
    try {
      await R.ui.loadDevices();
      R.ui.deviceOptions(document.getElementById("logc-device"), true, true);
      await loadSources();
      await refresh();
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  async function deactivate() {
    stopPoll();
    if (following && currentDid()) {
      following = false;
      try {
        await api("/api/logcenter/" + currentDid() + "/unfollow", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
      } catch (e) { }
    }
  }

  function bind() {
    section();
    document.getElementById("logc-device").onchange = () => {
      loadSources();
      refresh();
    };
    document.getElementById("logc-refresh").onclick = refresh;
    document.getElementById("logc-follow").onclick = toggleFollow;
    document.getElementById("logc-filter").onkeydown = (e) => {
      if (e.key === "Enter") refresh();
    };
  }

  R.pages = R.pages || {};
  R.pages.logcenter = { render: render, bind: bind, deactivate: deactivate };
})();
