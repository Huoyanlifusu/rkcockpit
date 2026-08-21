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
  const fmtSize = R.dom.fmtSize;
  const fmtTime = R.dom.fmtTime;
  const fill = R.dom.fill;

  const POLL_MS = 5000;
  const LIMIT = 100;
  let device = "local";
  let timer = null;
  let rows = [];

  async function refresh() {
    const errBox = $("#proc-error");
    const qs = [
      "limit=" + LIMIT,
      "sort=" + encodeURIComponent($("#proc-sort").value),
      "order=" + ($("#proc-order").value || "desc"),
    ];
    const pat = $("#proc-search").value.trim();
    if (pat) qs.push("pattern=" + encodeURIComponent(pat));
    try {
      const r = await api("/api/proc/" + encodeURIComponent(device) + "/list?" + qs.join("&"));
      if (!r.ok) throw new Error(r.error);
      rows = r.processes || [];
      $("#proc-total").textContent = "总数 " + (r.total || 0);
      renderRows();
      errBox.innerHTML = "";
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  function renderRows() {
    const tb = $("#proc-table tbody");
    tb.innerHTML = "";
    if (!rows.length) {
      const tr = el("tr");
      const td = el("td", "empty", "无进程");
      td.colSpan = 8;
      tr.append(td);
      tb.append(tr);
      return;
    }
    for (const p of rows) {
      const tr = el("tr");
      tr.append(el("td", "pid", String(p.pid)));
      tr.append(el("td", "", p.ppid === null ? "-" : String(p.ppid)));
      tr.append(el("td", "", p.stat || "-"));
      tr.append(el("td", "", p.pcpu === null ? "-" : p.pcpu.toFixed(1)));
      tr.append(el("td", "", p.pmem === null ? "-" : p.pmem.toFixed(1)));
      tr.append(el("td", "", p.rss_kb === null ? "-" : fmtSize(p.rss_kb * 1024)));
      tr.append(el("td", "dname", p.comm || "-"));
      const ops = el("td", "ops");
      const bDetail = el("button", "", "详情");
      bDetail.onclick = () => openDetail(p);
      const bKill = el("button", "del", "杀");
      bKill.onclick = () => killProc(p);
      ops.append(bDetail, bKill);
      tr.append(ops);
      tb.append(tr);
    }
  }

  async function killProc(p) {
    if (!confirm("确认终止进程 PID=" + p.pid + "（" + (p.comm || "") + "）？")) return;
    const errBox = $("#proc-error");
    try {
      const r = await api("/api/proc/" + encodeURIComponent(device) + "/" + p.pid + "/signal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sig: "TERM" }),
      });
      if (!r.ok) throw new Error(r.error);
      await refresh();
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  async function openDetail(p) {
    const box = $("#proc-modal");
    try {
      const r = await api("/api/proc/" + encodeURIComponent(device) + "/" + p.pid);
      if (!r.ok) throw new Error(r.error);
      const proc = r.process || {};
      $("#proc-modal-title").textContent = "进程详情 PID=" + p.pid;
      fill($("#proc-modal-kv"), [
        ["命令", proc.cmdline || "-"],
        ["线程数", proc.threads === null ? "-" : proc.threads],
        ["FD 数", proc.fd_count === null ? "-" : proc.fd_count],
        ["oom_score", proc.oom_score === null ? "-" : proc.oom_score],
        ["nice", proc.nice === null ? "-" : proc.nice],
        ["启动时间", fmtTime(proc.start_ms)],
      ]);
      box.style.display = "flex";
    } catch (e) {
      hostError($("#proc-error"), e.message);
    }
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
    const errBox = $("#proc-error");
    errBox.innerHTML = "";
    await loadDevices();
    deviceOptions($("#proc-device"), true, true);
    device = $("#proc-device").value || "local";
    startPoll();
  }

  function deactivate() {
    stopPoll();
  }

  function bind() {
    $("#proc-device").onchange = (e) => { device = e.target.value; render(); };
    $("#proc-search").onchange = refresh;
    $("#proc-sort").onchange = refresh;
    $("#proc-order").onchange = refresh;
    $("#proc-close").onclick = () => { $("#proc-modal").style.display = "none"; };
  }

  R.pages = R.pages || {};
  R.pages.process = { render, bind, deactivate };
})();
