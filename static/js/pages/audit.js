(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const $ = R.dom.$;
  const el = R.dom.el;
  const api = R.api.fetch;
  const hostError = R.ui.hostError;
  const loadDevices = R.ui.loadDevices;
  const deviceOptions = R.ui.deviceOptions;
  const deviceName = R.ui.deviceName;
  const badge = R.dom.badge;
  const esc = R.dom.esc;

  const POLL_MS = 3000;
  const LIMIT = 200;
  const ACTIONS = [
    "exec.run", "exec.kill", "fs.rm", "fs.chmod", "fs.upload", "fs.download",
    "fs.copy", "fs.copyfrom", "fs.mkdir", "fs.rename", "fs.mv",
    "dev.add", "dev.update", "dev.delete",
    "proc.signal", "deploy.start", "deploy.stage",
    "groups.create", "groups.update", "groups.delete", "groups.exec",
    "logcenter.tail", "logcenter.follow", "logcenter.unfollow",
    "keys.generate", "keys.delete", "keys.install",
    "diag.stream_test",
  ];
  const RANGES = { "1h": 3600e3, "24h": 86400e3, "7d": 7 * 86400e3 };
  let timer = null;
  let events = [];

  function rangeMs() {
    const v = $("#aud-range").value || "24h";
    const now = Date.now();
    return { from: now - (RANGES[v] || 86400e3), to: now };
  }

  function fmtHHMMSS(ms) {
    const d = new Date(ms);
    const p = n => (n < 10 ? "0" : "") + n;
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function detailText(ev) {
    const d = ev.detail || {};
    if (!Object.keys(d).length) return "-";
    const keys = ["cmd", "path", "sig", "mode", "src", "dest"];
    for (const k of keys) {
      if (d[k] !== undefined && d[k] !== null && d[k] !== "") return String(d[k]);
    }
    return JSON.stringify(d);
  }

  async function refresh() {
    const errBox = $("#aud-error");
    const { from, to } = rangeMs();
    const qs = ["from=" + from, "to=" + to, "limit=" + LIMIT];
    const action = $("#aud-action").value;
    if (action) qs.push("action=" + encodeURIComponent(action));
    const dev = $("#aud-device").value;
    if (dev) qs.push("device=" + encodeURIComponent(dev));
    const res = $("#aud-result").value;
    if (res) qs.push("result=" + encodeURIComponent(res));
    try {
      const r = await api("/api/audit?" + qs.join("&"));
      if (!r.ok) throw new Error(r.error);
      events = r.events || [];
      $("#aud-total").textContent = "共 " + (r.total || 0) + " 条";
      renderRows();
      $("#aud-export").href = "/api/audit/export?from=" + from + "&to=" + to;
      errBox.innerHTML = "";
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  function renderRows() {
    const tb = $("#aud-table tbody");
    tb.innerHTML = "";
    if (!events.length) {
      const tr = el("tr");
      const td = el("td", "empty", "暂无审计记录");
      td.colSpan = 6;
      tr.append(td);
      tb.append(tr);
      return;
    }
    for (const ev of events) {
      const t = ev.target || {};
      const tr = el("tr", "aud-row");
      tr.dataset.id = ev.id || "";
      tr.append(el("td", "", fmtHHMMSS(ev.ts)));
      tr.append(el("td", "", ev.action || "-"));
      tr.append(el("td", "", deviceName(t.id) || t.id || "-"));
      tr.append(el("td", "", (t.kind || "") + (t.path ? " " + t.path : "")));
      tr.append(el("td", "", detailText(ev)));
      tr.append(el("td", "", badge(ev.result || "unknown", ev.result || "-")));
      tr.onclick = () => toggleDetail(tr, ev);
      tb.append(tr);
    }
  }

  function toggleDetail(tr, ev) {
    const tb = tr.parentElement;
    const existing = tb.querySelector("tr.aud-detail[data-parent='" +
      (tr.dataset.id || "") + "']");
    if (existing) { existing.remove(); return; }
    const detail = {
      id: ev.id, ts: ev.ts, actor: ev.actor, ip: ev.ip,
      action: ev.action, target: ev.target,
      detail: ev.detail, result: ev.result, err: ev.err || "",
    };
    const dtr = el("tr", "aud-detail");
    dtr.dataset.parent = tr.dataset.id || "";
    const td = el("td");
    td.colSpan = 6;
    td.innerHTML = "<pre class='aud-json'>" + esc(JSON.stringify(detail, null, 2)) + "</pre>";
    dtr.append(td);
    tr.after(dtr);
  }

  function startPoll() {
    stopPoll();
    timer = setInterval(refresh, POLL_MS);
    refresh();
  }

  function stopPoll() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  async function render() {
    const errBox = $("#aud-error");
    errBox.innerHTML = "";
    await loadDevices();
    deviceOptions($("#aud-device"), true, true);
    if (!$("#aud-action").options.length) {
      const sel = $("#aud-action");
      const any = el("option", "", "全部动作");
      any.value = "";
      sel.append(any);
      for (const a of ACTIONS) {
        const o = el("option", "", a);
        o.value = a;
        sel.append(o);
      }
    }
    startPoll();
  }

  function deactivate() {
    stopPoll();
  }

  function bind() {
    $("#aud-action").onchange = refresh;
    $("#aud-device").onchange = refresh;
    $("#aud-result").onchange = refresh;
    $("#aud-range").onchange = () => { refresh(); };
  }

  R.pages = R.pages || {};
  R.pages.audit = { render, bind, deactivate };
})();
