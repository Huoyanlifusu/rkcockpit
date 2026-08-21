(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  const STATE_CLASS = {
    PREPARING: "state-prepairing", WAIT_FSYNC: "state-wait_fsync",
    RUNNING: "state-running", DEGRADED: "state-degraded",
    FAILED: "state-failed", UNREACHABLE: "state-unreachable",
    UNKNOWN: "state-unknown", READY: "state-armed", OFFLINE: "state-unreachable",
    IDLE: "state-idle", ARMED: "state-armed", STOPPING: "state-pending",
    ONLINE: "state-ok", REACHABLE: "state-ok", UNAUTHORIZED: "state-warn",
  };
  const stateCls = (s) => STATE_CLASS[(s || "").toUpperCase()] || "state-pending";

  const badge = (state, text) => el("span", "badge " + stateCls(state), text || state);

  const kvRow = (k, v) => {
    const item = el("div", "item");
    item.append(el("span", "muted", k), el("b", "", v === null || v === undefined ? "-" : String(v)));
    return item;
  };

  const fill = (container, rows) => {
    container.innerHTML = "";
    for (const [k, v] of rows) container.append(kvRow(k, v));
  };

  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  const fmtSize = (n) => {
    if (n === null || n === undefined || n < 0) return "-";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  };

  const fmtTime = (ms) => {
    if (!ms) return "-";
    return new Date(ms).toLocaleString("zh-CN", { hour12: false });
  };

  R.dom = R.dom || {};
  R.dom.$ = $;
  R.dom.el = el;
  R.dom.fill = fill;
  R.dom.badge = badge;
  R.dom.kvRow = kvRow;
  R.dom.fmtSize = fmtSize;
  R.dom.fmtTime = fmtTime;
  R.dom.esc = esc;
})();
