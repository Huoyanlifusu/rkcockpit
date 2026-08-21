(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const el = R.dom.el;
  const api = R.api.fetch;
  const hostError = R.ui.hostError;

  let groups = [];
  let devices = [];
  let execTimer = null;

  function section() {
    let sec = document.getElementById("tab-groups");
    if (!sec) {
      sec = el("section", "pane");
      sec.id = "tab-groups";
      const main = document.querySelector("main") || document.body;
      main.append(sec);
    }
    if (!sec.querySelector("#grp-add")) {
      sec.innerHTML =
      '<div class="toolbar">' +
      '<button id="grp-add" class="btn">新建分组</button>' +
      '<button id="grp-refresh" class="btn">刷新</button>' +
      '<span id="grp-total" class="muted"></span>' +
      "</div>" +
      '<div id="grp-error"></div>' +
      '<div class="panel">' +
      '<table class="fm-table" id="grp-table"><thead><tr>' +
      "<th>组名</th><th>成员数</th><th>创建时间</th><th>成员</th><th>操作</th>" +
      "</tr></thead><tbody></tbody></table>" +
      "</div>" +
      '<div id="grp-exec" class="panel" style="display:none"></div>' +
      '<div id="grp-modal" class="modal-mask" style="display:none">' +
      '<div class="modal"><h3 id="grp-modal-title">新建分组</h3>' +
      '<div class="row"><label>组名 *</label><input id="grp-f-name"></div>' +
      '<div class="row"><label>成员设备</label><div id="grp-f-devices"></div></div>' +
      '<div class="toolbar">' +
      '<button id="grp-save" class="btn">保存</button>' +
      '<button id="grp-cancel" class="btn">取消</button>' +
      "</div></div></div>";
    }
    return sec;
  }

  function fmtTime(ms) {
    if (!ms) return "-";
    const d = new Date(ms);
    const p = n => (n < 10 ? "0" : "") + n;
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function memberNames(ids) {
    const names = [];
    for (const id of ids) {
      const d = R.store.deviceMap[id];
      names.push(d ? d.name : id + "(已删除)");
    }
    return names.length ? names.join("、") : "-";
  }

  async function render() {
    const errBox = document.getElementById("grp-error");
    if (!errBox) return;
    try {
      await R.ui.loadDevices();
      devices = R.store.devices || [];
      const r = await api("/api/groups");
      groups = r.groups || [];
      const tb = document.querySelector("#grp-table tbody");
      tb.innerHTML = "";
      document.getElementById("grp-total").textContent =
        "共 " + groups.length + " 组";
      if (!groups.length) {
        const tr = el("tr");
        const td = el("td", "empty", "暂无分组，点右上角新建");
        td.colSpan = 5;
        tr.append(td);
        tb.append(tr);
        return;
      }
      for (const g of groups) {
        const tr = el("tr");
        tr.append(el("td", "", g.name));
        tr.append(el("td", "", String(g.device_ids.length)));
        tr.append(el("td", "", fmtTime(g.created_at)));
        tr.append(el("td", "muted", memberNames(g.device_ids)));
        const op = el("td");
        const editBtn = el("button", "btn chip", "编辑");
        editBtn.onclick = () => openModal(g.name);
        const execBtn = el("button", "btn chip", "执行");
        execBtn.onclick = () => openExec(g.name);
        const delBtn = el("button", "btn chip danger", "删除");
        delBtn.onclick = () => delGroup(g.name);
        op.append(editBtn, execBtn, delBtn);
        tr.append(op);
        tb.append(tr);
      }
      errBox.innerHTML = "";
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  function openModal(name) {
    section();
    document.getElementById("grp-modal-title").textContent =
      name ? "编辑分组: " + name : "新建分组";
    const nameInput = document.getElementById("grp-f-name");
    nameInput.value = name || "";
    nameInput.disabled = !!name;
    const cur = name ?
      ((groups.find(g => g.name === name) || {}).device_ids || []) : [];
    const box = document.getElementById("grp-f-devices");
    box.innerHTML = "";
    if (!devices.length) {
      box.append(el("div", "muted", "暂无设备，请先在设备管理页添加"));
    }
    for (const d of devices) {
      const label = el("label", "grp-dev");
      const cb = el("input");
      cb.type = "checkbox";
      cb.value = d.id;
      cb.checked = cur.indexOf(d.id) >= 0;
      label.append(cb);
      label.append(document.createTextNode(" " + d.name + " (" + d.id + ")"));
      box.append(label);
    }
    document.getElementById("grp-modal").style.display = "block";
  }

  function closeModal() {
    document.getElementById("grp-modal").style.display = "none";
  }

  async function saveGroup() {
    const errBox = document.getElementById("grp-error");
    const nameInput = document.getElementById("grp-f-name");
    const name = nameInput.value.trim();
    if (!name) {
      hostError(errBox, "组名必填");
      return;
    }
    const ids = [];
    document.querySelectorAll("#grp-f-devices input:checked").forEach(cb =>
      ids.push(cb.value));
    try {
      if (nameInput.disabled) {
        const r = await api("/api/groups/" + encodeURIComponent(name), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ device_ids: ids }),
        });
        if (r.skipped && r.skipped.length) {
          hostError(errBox, "已忽略不存在的设备: " + r.skipped.join(", "));
        } else {
          errBox.innerHTML = "";
        }
      } else {
        await api("/api/groups", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name, device_ids: ids }),
        });
      }
      closeModal();
      render();
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  async function delGroup(name) {
    if (!window.confirm("删除分组 \"" + name + "\"？（设备保留）")) return;
    try {
      await api("/api/groups/" + encodeURIComponent(name), { method: "DELETE" });
      render();
    } catch (e) {
      hostError(document.getElementById("grp-error"), e.message);
    }
  }

  function openExec(name) {
    const box = document.getElementById("grp-exec");
    box.style.display = "block";
    box.innerHTML = "";
    const h = el("h3", "", "批量执行: " + name);
    const row = el("div", "toolbar");
    const cmd = el("input");
    cmd.id = "grp-exec-cmd";
    cmd.placeholder = "如 uname -a";
    const tm = el("input");
    tm.type = "number";
    tm.value = "120";
    tm.min = "1";
    tm.max = "3600";
    tm.style.width = "80px";
    const btn = el("button", "btn", "执行");
    row.append(el("label", "", "命令"), cmd,
      el("label", "", "超时(s)"), tm, btn);
    box.append(h, row);
    const out = el("div", "panel");
    box.append(out);
    btn.onclick = () => runExec(name, cmd.value.trim(), tm.value, out);
  }

  async function runExec(name, cmd, timeout, out) {
    const errBox = document.getElementById("grp-error");
    if (!cmd) {
      hostError(errBox, "命令不能为空");
      return;
    }
    if (execTimer) {
      clearInterval(execTimer);
      execTimer = null;
    }
    out.innerHTML = "";
    try {
      const r = await api("/api/groups/" + encodeURIComponent(name) + "/exec", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cmd: cmd, timeout: parseInt(timeout || 120, 10) }),
      });
      const jobs = r.jobs || [];
      const box = el("pre", "log-view");
      box.textContent = "已提交 " + jobs.length + " 个 job，正在轮询输出…\n";
      out.append(box);
      const rows = {};
      for (const j of jobs) rows[j.device_id + "/" + j.job_id] = true;
      const tick = async () => {
        let text = "";
        for (const key of Object.keys(rows)) {
          if (rows[key] === null) continue;
          const did = key.split("/")[0];
          const jobId = key.split("/")[1];
          try {
            const p = await api("/api/exec/" + did + "/poll?job_id=" + jobId);
            if (!p.running) {
              rows[key] = null;
              text += "── " + key + " ── exit " +
                (p.exit_code === null ? "?" : p.exit_code) + "\n" +
                (p.output || "") + "\n";
            }
          } catch (e) {
            rows[key] = null;
            text += "── " + key + " ── " + e.message + "\n";
          }
        }
        if (text) box.textContent += text;
        if (Object.values(rows).every(v => v === null)) {
          clearInterval(execTimer);
          execTimer = null;
        }
      };
      tick();
      execTimer = setInterval(tick, 1000);
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  function bind() {
    section();
    document.getElementById("grp-add").onclick = () => openModal(null);
    document.getElementById("grp-refresh").onclick = render;
    document.getElementById("grp-save").onclick = saveGroup;
    document.getElementById("grp-cancel").onclick = closeModal;
  }

  function deactivate() {
    if (execTimer) {
      clearInterval(execTimer);
      execTimer = null;
    }
  }

  R.pages = R.pages || {};
  R.pages.groups = { render: render, bind: bind, deactivate: deactivate };
})();
