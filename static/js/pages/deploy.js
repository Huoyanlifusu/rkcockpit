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

  const POLL_MS = 1000;
  const MODES = ["0644", "0755", "0777"];
  const STAGE_ICON = { done: "✓", running: "⟳", failed: "✗", pending: "…" };
  const STAGE_CLS = { done: "level-info", running: "level-warn", failed: "level-critical" };
  let device = "local";
  let planId = null;
  let timer = null;
  let seq = 0;

  function addRow(src, dest) {
    const row = el("div", "dep-row");
    row.dataset.seq = String(seq++);
    const iSrc = el("input", "dep-src");
    iSrc.placeholder = "本地路径（如 /home/operator/x/start.sh）";
    iSrc.spellcheck = false;
    if (src) iSrc.value = src;
    const iDest = el("input", "dep-dest");
    iDest.placeholder = "目标路径（留空 = 目标目录 + 文件名）";
    iDest.spellcheck = false;
    if (dest) iDest.value = dest;
    const sel = el("select", "dep-mode");
    for (const m of MODES) {
      const o = el("option", "", m);
      o.value = m;
      sel.append(o);
    }
    sel.value = "0755";
    const bDel = el("button", "btn danger", "删除");
    bDel.onclick = () => row.remove();
    row.append(iSrc, iDest, sel, bDel);
    $("#dep-files").append(row);
  }

  function collectFiles() {
    const files = [];
    for (const row of $("#dep-files").children) {
      const src = row.querySelector(".dep-src").value.trim();
      if (!src) continue;
      let dest = row.querySelector(".dep-dest").value.trim();
      if (!dest) {
        const dir = $("#dep-dir").value.trim().replace(/\/+$/, "");
        const name = src.split(/[\\/]/).pop();
        dest = (dir ? dir + "/" : "") + name;
      }
      files.push({
        src,
        dest,
        mode: row.querySelector(".dep-mode").value,
      });
    }
    return files;
  }

  async function startDeploy() {
    const errBox = $("#dep-error");
    const files = collectFiles();
    if (!files.length) { hostError(errBox, "请至少填写一行本地路径"); return; }
    const body = {
      files,
      cmd: $("#dep-cmd").value.trim() || "",
      timeout: parseInt($("#dep-timeout").value || "60", 10) || 60,
    };
    try {
      const r1 = await api("/api/deploy/" + encodeURIComponent(device) + "/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r1.ok) throw new Error(r1.error);
      planId = r1.plan_id;
      const r2 = await api("/api/deploy/" + encodeURIComponent(device) + "/" + planId + "/start",
        { method: "POST" });
      if (!r2.ok) throw new Error(r2.error);
      $("#dep-progress").style.display = "block";
      startPoll();
      errBox.innerHTML = "";
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  function renderJob(job) {
    const box = $("#dep-progress");
    box.style.display = "block";
    box.innerHTML = "";
    if (!job) { box.append(el("div", "empty", "任务不存在")); return; }

    const head = el("div", "");
    head.append(el("b", "", "部署计划 " + (planId || "-")));
    head.append(el("span", "muted", "  " + (job.state || "-")));
    const pct = job.progress && job.progress.bytes_total > 0
      ? Math.min(100, Math.round(job.progress.bytes_done / job.progress.bytes_total * 100)) : 0;
    if (job.progress && job.progress.bytes_total > 0) {
      head.append(el("span", "muted",
        "  " + fmtSize(job.progress.bytes_done) + "/" + fmtSize(job.progress.bytes_total)));
    }
    if (job.state === "running") {
      const cancel = el("button", "btn danger", "取消");
      cancel.onclick = cancelDeploy;
      head.append(cancel);
    }
    box.append(head);
    if (job.progress && job.progress.bytes_total > 0) {
      const bar = el("div", "job-bar");
      const f = el("div");
      f.style.width = pct + "%";
      bar.append(f);
      box.append(bar);
    }

    for (const st of job.stages || []) {
      const item = el("div", "dep-stage");
      item.append(el("span", STAGE_CLS[st.state] || "muted",
        (STAGE_ICON[st.state] || "·") + " " + (st.name || "")));
      if (st.detail) item.append(el("span", "muted", "  " + st.detail));
      box.append(item);
    }
    if (job.result && job.result.output_tail) {
      box.append(el("pre", "log-view dep-tail", job.result.output_tail));
    }
    if (job.error) box.append(el("div", "level-critical", "错误: " + job.error));

    if (job.state === "done" || job.state === "error" || job.state === "cancelled") {
      stopPoll();
    }
  }

  async function pollJob() {
    const errBox = $("#dep-error");
    try {
      const r = await api("/api/deploy/" + encodeURIComponent(device) + "/" + planId);
      if (!r.ok) throw new Error(r.error);
      renderJob(r.job);
    } catch (e) {
      hostError(errBox, e.message);
      stopPoll();
    }
  }

  async function cancelDeploy() {
    const errBox = $("#dep-error");
    try {
      const r = await api("/api/deploy/" + encodeURIComponent(device) + "/" + planId + "/cancel",
        { method: "POST" });
      if (!r.ok) throw new Error(r.error);
      pollJob();
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  function startPoll() {
    stopPoll();
    timer = setInterval(pollJob, POLL_MS);
    pollJob();
  }

  function stopPoll() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  function basename(p) { return String(p).split(/[\\/]/).pop(); }
  function joinDest(dir, name) {
    const d = String(dir || "").replace(/\/+$/, "");
    return d ? d + "/" + name : "/" + name;
  }

  async function render() {
    const errBox = $("#dep-error");
    errBox.innerHTML = "";

    const ho = R.state.deployHandoff;
    if (ho && Array.isArray(ho.srcs) && ho.srcs.length) {
      for (const src of ho.srcs) addRow(src, joinDest(ho.destDir, basename(src)));
      R.state.deployHandoff = null;
    } else if (!$("#dep-files").children.length) {
      addRow();
    }
    await loadDevices();
    deviceOptions($("#dep-device"), true, true);
    device = $("#dep-device").value || "local";
  }

  function deactivate() {
    stopPoll();
  }

  function bind() {
    $("#dep-device").onchange = (e) => { device = e.target.value; render(); };
    $("#dep-add").onclick = () => addRow();
    $("#dep-clear").onclick = () => { $("#dep-files").innerHTML = ""; addRow(); };
    $("#dep-start").onclick = startDeploy;
  }

  R.pages = R.pages || {};
  R.pages.deploy = { render, bind, deactivate };
})();
