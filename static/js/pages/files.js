(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const $ = R.dom.$;
  const el = R.dom.el;
  const HOST = R.store;
  const FM = R.state.fm;
  const api = R.api.fetch;
  const hostError = R.ui.hostError;
  const loadDevices = R.ui.loadDevices;
  const deviceOptions = R.ui.deviceOptions;
  const deviceName = R.ui.deviceName;
  const fmtSize = R.dom.fmtSize;
  const fmtTime = R.dom.fmtTime;

  const lastNav = {};
  const lastFileClick = {};
  const crumbOpen = {};
  const crumbPath = {};
  const loadSeq = {};

  function recentNav(side) {
    return !!(lastNav[side] && Date.now() - lastNav[side] < 400);
  }

  async function renderFilesPage() {
    await loadDevices();
    deviceOptions($("#fm-device"), false, true);
    FM.device = $("#fm-device").value || (HOST.devices.length ? HOST.devices[0].id : "local");
    if (!HOST.devices.length) {
      $("#fm-device").innerHTML = '<option value="local">上位机本地（local）</option>';
      FM.device = "local";
    }
    $("#fm-remote-name").textContent = deviceName(FM.device) || "本地（local）";
    $("#fm-hidden").checked = FM.hidden;
    await Promise.all([loadPane("local"), loadPane("remote")]);
    startJobsPoll();
  }

  async function loadPane(side) {
    const seq = (loadSeq[side] = (loadSeq[side] || 0) + 1);
    const id = side === "local" ? "local" : FM.device;
    const path = FM.paths[side];
    const tb = $("#fm-tb-" + side);
    const errBox = $("#fm-error");
    renderCrumbs(side);
    try {
      const r = await api("/api/fs/" + id + "/list?path=" + encodeURIComponent(path));
      if (loadSeq[side] !== seq) return;
      tb.innerHTML = "";
      if (!r.ok) throw new Error(r.error);
      renderPaneRows(tb, side, r.entries || []);
    } catch (e) {
      if (loadSeq[side] !== seq) return;
      hostError(errBox, "[" + (side === "local" ? "上位机" : "设备") + "] " + e.message);
    }
  }

  function pathSegments(side, p) {
    if (!p) return side === "local" ? ["~"] : ["/"];
    if (p === "~") return ["~"];
    if (side === "local" && p.startsWith("~")) {
      return ["~"].concat(p.slice(2).split("/").filter(Boolean));
    }
    const parts = p.split("/").filter(Boolean);
    return ["/"].concat(parts);
  }

  function pathForSegment(side, segs, idx) {
    if (idx <= 0) return segs[0] === "~" ? "~" : "/";
    return (segs[0] === "~" ? "~/" : "/") + segs.slice(1, idx + 1).join("/");
  }

  function renderCrumbs(side) {
    const bar = $("#fm-crumbs-" + side);
    if (!bar) return;
    const p = FM.paths[side];
    if (crumbPath[side] !== p) { crumbPath[side] = p; crumbOpen[side] = false; }
    bar.setAttribute("aria-label", "当前路径 " + p);


    bar.setAttribute("aria-expanded", crumbOpen[side] ? "true" : "false");
    const segs = pathSegments(side, p);
    let items = segs.map((s, i) => ({ s, i }));
    if (segs.length > 6 && !crumbOpen[side]) {
      items = [items[0], null].concat(items.slice(-2));
    }
    bar.innerHTML = "";
    for (let k = 0; k < items.length; k++) {
      if (k > 0) bar.append(el("span", "sep", "/"));
      const it = items[k];
      if (it === null) {
        const ell = el("button", "crumb ellipsis", "…");
        ell.title = "展开全部路径";
        ell.setAttribute("aria-expanded", "false");
        ell.setAttribute("aria-label", "展开全部路径");
        ell.onclick = () => { crumbOpen[side] = true; renderCrumbs(side); };
        bar.append(ell);
        continue;
      }
      const isLast = it.i === segs.length - 1;
      const full = pathForSegment(side, segs, it.i);
      const c = el(isLast ? "span" : "button", "crumb" + (isLast ? " cur" : ""), it.s);
      c.title = full;
      if (!isLast) c.onclick = () => {
        lastNav[side] = Date.now();
        FM.paths[side] = full;
        loadPane(side);
      };
      bar.append(c);
    }
  }

  function renderPaneRows(tb, side, entries) {
    tb.innerHTML = "";
    const sel = FM.sel[side];
    const upRow = el("tr", "dir");
    upRow.append(el("td"), el("td", "", "..（上级目录）"));
    upRow.append(el("td", "", "-"), el("td", "", "-"), el("td", "", "-"));
    upRow.onclick = () => goUpOnce(side);
    tb.append(upRow);
    for (const e of entries) {
      if (!FM.hidden && e.name.startsWith(".")) continue;
      const tr = el("tr", e.is_dir ? "dir" : "");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = sel.has(e.name);
      cb.onchange = () => setSel(side, e.name, cb.checked);
      const tdCb = el("td");
      tdCb.append(cb);
      tdCb.onclick = (ev) => ev.stopPropagation();
      const nameTd = el("td", e.is_dir ? "dname" : "fname", e.name);
      tr.append(tdCb, nameTd,
        el("td", "size", e.is_dir ? "-" : fmtSize(e.size)),
        el("td", "mtime", fmtTime(e.mtime_ms)),
        el("td", "muted", e.mode || "-"));
      if (e.is_dir) {
        const target = joinPath(FM.paths[side], e.name);
        tr.onclick = () => {
          if (recentNav(side)) return;
          enterDir(side, target);
        };
        tr.ondblclick = () => {
          if (recentNav(side)) return;
          enterDir(side, target);
        };
      } else {
        tr.onclick = () => {


          if (recentNav(side)) return;



          const fkey = joinPath(FM.paths[side], e.name);
          const prev = lastFileClick[side];
          if (prev && prev.key === fkey && Date.now() - prev.t < 400) return;
          lastFileClick[side] = { key: fkey, t: Date.now() };
          const on = !sel.has(e.name);
          cb.checked = on;
          setSel(side, e.name, on);
        };
      }
      tb.append(tr);
    }
  }

  function setSel(side, name, checked) {
    const sel = FM.sel[side];
    if (checked) sel.add(name); else sel.delete(name);
    syncTransferBtns();
  }

  function enterDir(side, target) {
    lastNav[side] = Date.now();
    FM.paths[side] = target;
    loadPane(side);
  }

  function goUpOnce(side) {
    if (recentNav(side)) return;
    lastNav[side] = Date.now();
    goUp(side);
  }

  function joinPath(base, name) {
    if (base === "/") return "/" + name;
    return (base === "~" ? "~" : base.replace(/\/+$/, "")) + "/" + name;
  }

  function parentPath(side, p) {
    if (side === "local" && p === "~") return "/";
    if (p === "/" || p === "") return p;
    const segs = pathSegments(side, p);
    if (segs.length <= 1) return p;
    return pathForSegment(side, segs, segs.length - 2);
  }

  function goUp(side) {
    const p = FM.paths[side];
    const target = parentPath(side, p);
    if (target === p) return;
    FM.paths[side] = target;
    loadPane(side);
  }

  function syncTransferBtns() {
    $("#fm-to-device").disabled = FM.sel.local.size === 0;
    $("#fm-to-host").disabled = FM.sel.remote.size === 0;
  }

  async function fmMkdir(side) {
    const name = prompt("文件夹名称：");
    if (!name) return;
    const id = side === "local" ? "local" : FM.device;
    try {
      const r = await api("/api/fs/" + id + "/mkdir", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: joinPath(FM.paths[side], name) }),
      });
      if (!r.ok) throw new Error(r.error);
      await loadPane(side);
    } catch (e) {
      hostError($("#fm-error"), e.message);
    }
  }

  async function fmChmodRemote() {
    const names = [...FM.sel.remote];
    if (!names.length) { hostError($("#fm-error"), "请先在右侧选中文件"); return; }
    const mode = prompt("权限（如 755 / 0644）：", "0755");
    if (!mode) return;
    for (const name of names) {
      try {
        const r = await api("/api/fs/" + FM.device + "/chmod", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: joinPath(FM.paths.remote, name), mode }),
        });
        if (!r.ok) throw new Error(r.error);
      } catch (e) {
        hostError($("#fm-error"), e.message);
      }
    }
    await loadPane("remote");
  }

  async function fmRename(side) {
    const names = [...FM.sel[side]];
    if (names.length !== 1) { hostError($("#fm-error"), "请选中一个文件/目录"); return; }
    const name = names[0];
    const newName = prompt("新名称：", name);
    if (!newName || newName === name) return;
    const id = side === "local" ? "local" : FM.device;
    try {
      const r = await api("/api/fs/" + id + "/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: joinPath(FM.paths[side], name), new_name: newName }),
      });
      if (!r.ok) throw new Error(r.error);
      FM.sel[side].delete(name);
      await loadPane(side);
    } catch (e) {
      hostError($("#fm-error"), e.message);
    }
  }

  async function fmDel(side) {
    const names = [...FM.sel[side]];
    if (!names.length) { hostError($("#fm-error"), "请先选中要删除的文件"); return; }
    if (!confirm("确认删除 " + names.length + " 个文件/目录？")) return;
    const id = side === "local" ? "local" : FM.device;
    for (const name of names) {
      try {
        const r = await api("/api/fs/" + id + "/rm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: joinPath(FM.paths[side], name), recursive: true }),
        });
        if (!r.ok) throw new Error(r.error);
      } catch (e) {
        hostError($("#fm-error"), e.message);
      }
    }
    FM.sel[side].clear();
    await loadPane(side);
  }

  async function fmTransfer(toDevice) {
    const srcSide = toDevice ? "local" : "remote";
    const dstSide = toDevice ? "remote" : "local";
    const names = [...FM.sel[srcSide]];
    if (!names.length) {
      hostError($("#fm-error"), "请先在" + (toDevice ? "左侧(上位机)" : "右侧(设备)") + "选中文件");
      return;
    }
    const action = toDevice ? "copy" : "copyfrom";
    const id = FM.device;
    for (const name of names) {
      try {
        const body = toDevice
          ? { src: joinPath(FM.paths.local, name), dest: joinPath(FM.paths.remote, name) }
          : { src: joinPath(FM.paths.remote, name), dest: joinPath(FM.paths.local, name) };
        const r = await api("/api/fs/" + id + "/" + action, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error(r.error);
      } catch (e) {
        hostError($("#fm-error"), e.message);
      }
    }
    FM.sel[srcSide].clear();
    syncTransferBtns();
    $("#fm-jobs-toggle").textContent = "传输任务";
    $("#fm-jobs").style.display = "block";
    renderJobs();
    startJobsPoll();
    setTimeout(() => { loadPane("local"); loadPane("remote"); }, 1500);
  }

  async function fmUploadBrowser() {
    const input = $("#fm-file");
    input.value = "";
    input.onchange = async () => {
      for (const f of input.files) {
        try {
          const url = "/api/fs/" + FM.device + "/upload?path=" +
            encodeURIComponent(FM.paths.remote) + "&name=" + encodeURIComponent(f.name);
          const r = await fetch(url, { method: "POST", body: f });
          const j = await r.json();
          if (!j.ok) throw new Error(j.error || "上传失败");
        } catch (e) {
          hostError($("#fm-error"), "上传 " + f.name + " 失败: " + e.message);
        }
      }
      startJobsPoll();
      setTimeout(() => loadPane("remote"), 800);
    };
    input.click();
  }

  function renderJobs() {
    const box = $("#fm-jobs");
    box.innerHTML = "";
    box.append(el("h2", "", "传输任务"));
    const J = HOST._jobs || [];
    if (!J.length) {
      box.append(el("div", "empty", "暂无任务"));
      return;
    }
    for (const j of J) {
      const item = el("div", "job-item");
      const pct = j.bytes_total > 0 ? Math.min(100, Math.round(j.bytes_done / j.bytes_total * 100)) : 0;
      const head = el("div", "");
      head.append(el("span", "", `[${j.device}] ${j.action === "upload" ? "上传" : j.action === "download" ? "下载" : "直传"} ${j.name}`));
      head.append(el("span", "muted", ` ${fmtSize(j.bytes_done)}/${fmtSize(j.bytes_total)} · ${j.status}`));
      if (j.status === "running") {
        const cancel = el("button", "del", "取消");
        cancel.onclick = () => cancelJob(j.id);
        head.append(cancel);
      }
      item.append(head);
      if (j.status === "running") {
        const bar = el("div", "job-bar");
        const fill = el("div");
        fill.style.width = pct + "%";
        bar.append(fill);
        item.append(bar);
      }
      if (j.error) item.append(el("div", "level-critical", j.error));
      box.append(item);
    }
  }

  async function cancelJob(id) {
    try {
      await api("/api/jobs/" + id + "/cancel", { method: "POST" });
      renderJobs();
    } catch (e) {
      hostError($("#fm-error"), e.message);
    }
  }

  async function pollJobs() {
    if (!FM.jobsOpen && $("#fm-jobs").style.display === "none") return;
    try {
      const r = await api("/api/jobs");
      HOST._jobs = r.jobs || [];
      const running = HOST._jobs.filter(j => j.status === "running").length;
      $("#fm-jobs-toggle").textContent = running ? `传输任务(${running})` : "传输任务";
      renderJobs();
    } catch (e) {
      }
  }

  function startJobsPoll() {
    if (FM.jobsTimer) clearInterval(FM.jobsTimer);
    FM.jobsTimer = setInterval(pollJobs, 1000);
  }

  function bind() {
    $("#fm-device").onchange = async (e) => {
      FM.device = e.target.value;
      FM.sel.remote.clear();
      $("#fm-remote-name").textContent = deviceName(FM.device) || "-";
      FM.paths.remote = "/";
      await loadPane("remote");
    };
    $("#fm-refresh").onclick = () => { loadPane("local"); loadPane("remote"); };
    $("#fm-hidden").onchange = (e) => { FM.hidden = e.target.checked; loadPane("local"); loadPane("remote"); };
    $("#fm-mkdir").onclick = () => fmMkdir("remote");
    $("#fm-upload").onclick = fmUploadBrowser;
    $("#fm-to-device").onclick = () => fmTransfer(true);
    $("#fm-to-host").onclick = () => fmTransfer(false);
    $("#fm-jobs-toggle").onclick = () => {
      const box = $("#fm-jobs");
      box.style.display = box.style.display === "none" ? "block" : "none";
      if (box.style.display === "block") pollJobs();
    };
    document.querySelectorAll("[data-up]").forEach(b =>
      b.onclick = () => goUpOnce(b.dataset.up));
    document.querySelectorAll("[data-newdir]").forEach(b =>
      b.onclick = () => fmMkdir(b.dataset.newdir));
    document.querySelectorAll("[data-ren]").forEach(b =>
      b.onclick = () => fmRename(b.dataset.ren));
    document.querySelectorAll("[data-del]").forEach(b =>
      b.onclick = () => fmDel(b.dataset.del));
    document.querySelectorAll("[data-chmod]").forEach(b =>
      b.onclick = fmChmodRemote);

    syncTransferBtns();
  }

  R.pages = R.pages || {};
  R.pages.files = {
    render: renderFilesPage,
    bind,
  };
})();
