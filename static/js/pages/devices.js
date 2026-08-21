(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const $ = R.dom.$;
  const el = R.dom.el;
  const HOST = R.store;
  const hostError = R.ui.hostError;
  const loadDevices = R.ui.loadDevices;
  const badge = R.dom.badge;
  const fill = R.dom.fill;

  let devEditId = null;
  let discoverItems = [];
  let discoverSelection = [];
  let discoverRules = { ips: [], serials: [] };
  let discoverFiltered = null;

  async function renderDevicesPage() {
    const errBox = $("#dev-error");
    errBox.innerHTML = "";
    await loadDevices();
    try {
      HOST.hostEnv = await R.api.fetch("/api/host");
      const env = HOST.hostEnv;
      $("#host-env").textContent =
        `上位机 ${env.name} · Python ${env.python} · sshpass ${env.has_sshpass ? "有" : "无"} · adb ${env.has_adb ? "有" : "无"} · 配置 ${env.conf_dir}`;
    } catch (e) {
      hostError(errBox, "获取环境信息失败: " + e.message);
    }
    const tb = $("#dev-table tbody");
    tb.innerHTML = "";
    for (const d of HOST.devices) {
      const tr = el("tr");
      tr.append(el("td", "", d.name));
      tr.append(el("td", "", d.type));
      tr.append(el("td", "", d.type === "ssh" ? d.user + "@" + d.host + ":" + d.port
        : (d.type === "adb" ? d.host : d.local_root || "-")));
      tr.append(el("td", "", d.type === "ssh" ? (d.has_password ? "密码" : "密钥") : "-"));
      tr.append(el("td", "", d.remark || "-"));
      const stateCell = el("td");
      stateCell.append(badge(d.state || "unknown", d.state || "未知"));
      tr.append(stateCell);
      const ops = el("td", "ops");
      const bCheck = el("button", "", "测试连接");
      bCheck.onclick = () => devCheck(d.id, bCheck);
      const bEdit = el("button", "", "编辑");
      bEdit.onclick = () => openDevModal(d);
      const bDel = el("button", "del", "删除");
      bDel.onclick = () => devDelete(d);
      ops.append(bCheck, bEdit, bDel);
      tr.append(ops);
      tb.append(tr);
    }
    if (!HOST.devices.length) {
      const tr = el("tr");
      tr.append(el("td", "empty", "暂无设备。点击「新增设备」添加，或启动门户时加 --sim 自动注册模拟设备。"));
      tr.children[0].colSpan = 7;
      tb.append(tr);
    }
  }

  async function devCheck(id, btn) {
    const label = btn.textContent;
    btn.textContent = "测试中…";
    btn.disabled = true;
    const box = $("#dev-check-result");
    box.style.display = "block";
    box.innerHTML = "";
    try {
      const r = await R.api.api("/api/devices/" + id + "/check", { method: "POST" });
      if (r.ok && r.info) {
        const info = r.info;
        fill(box, [
          ["连接", r.state],
          ["延迟", r.ping_ms !== undefined ? r.ping_ms + "ms" : "-"],
          ["主机名", info.hostname || "-"],
          ["系统", info.os || "-"],
          ["内核", info.kernel || "-"],
          ["型号", info.model || "-"],
          ["运行时长", info.uptime_s !== undefined && info.uptime_s !== null
            ? Math.floor(info.uptime_s / 3600) + "h" : "-"],
        ]);
      } else {
        box.append(el("div", "empty", "连接失败: " + (r.error || "未知错误")));
      }
      await renderDevicesPage();
      btn.textContent = label;
      btn.disabled = false;
    } catch (e) {
      hostError($("#dev-error"), e.message);
      btn.textContent = label;
      btn.disabled = false;
    }
  }

  async function devDelete(d) {
    if (!confirm("确认删除设备「" + d.name + "」？")) return;
    try {
      await R.api.fetch("/api/devices/" + d.id, { method: "DELETE" });
      await renderDevicesPage();
    } catch (e) {
      hostError($("#dev-error"), e.message);
    }
  }

  function openDevModal(d) {
    devEditId = d ? d.id : null;
    $("#dev-modal-title").textContent = d ? "编辑设备：" + d.name : "新增设备";
    $("#dev-f-name").value = d ? d.name : "";
    $("#dev-f-type").value = d ? d.type : "ssh";
    $("#dev-f-host").value = d ? (d.host || "") : "";
    $("#dev-f-port").value = d ? d.port : 22;
    $("#dev-f-user").value = d ? d.user : "root";
    $("#dev-f-auth").value = d ? d.auth : "key";
    $("#dev-f-password").value = "";
    $("#dev-f-root").value = d && d.local_root ? d.local_root : "";
    $("#dev-f-remark").value = d ? (d.remark || "") : "";
    syncDevFormFields();
    $("#dev-modal").style.display = "flex";
  }

  function syncDevFormFields() {
    const t = $("#dev-f-type").value;
    $("#dev-f-pw-row").style.display = t === "ssh" ? "" : "none";
    $("#dev-f-auth").parentElement.style.display = t === "ssh" ? "" : "none";
    $("#dev-f-root-row").style.display = t === "local" ? "" : "none";
    $("#dev-f-host").placeholder = t === "adb" ? "serial（如 rk3588-demo-001）" : "IP / 域名";
  }

  async function saveDev() {
    const body = {
      name: $("#dev-f-name").value.trim(),
      type: $("#dev-f-type").value,
      host: $("#dev-f-host").value.trim(),
      port: parseInt($("#dev-f-port").value || "22"),
      user: $("#dev-f-user").value.trim(),
      auth: $("#dev-f-auth").value,
      remark: $("#dev-f-remark").value.trim(),
    };
    if ($("#dev-f-password").value) body.password = $("#dev-f-password").value;
    if (body.type === "local") body.local_root = $("#dev-f-root").value.trim();
    try {
      const url = devEditId ? "/api/devices/" + devEditId : "/api/devices";
      const r = await R.api.fetch(url, {
        method: devEditId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(r.error || "保存失败");
      $("#dev-modal").style.display = "none";
      await renderDevicesPage();
    } catch (e) {
      hostError($("#dev-error"), e.message);
    }
  }

  async function devDiscover() {
    const btn = $("#dev-discover");
    btn.textContent = "发现中…";
    btn.disabled = true;
    const box = $("#dev-discover-box");
    box.style.display = "block";
    box.innerHTML = "";
    try {
      const r = await R.api.fetch("/api/discover");
      try {
        const rr = await R.api.fetch("/api/discover/rules");
        discoverRules = (rr && rr.rules) || { ips: [], serials: [] };
      } catch (e) {
        discoverRules = { ips: [], serials: [] };
      }
      discoverFiltered = r.filtered || null;
      renderDiscoverBox(box, r);
    } catch (e) {
      hostError($("#dev-error"), "自动发现失败: " + e.message);
    } finally {
      btn.textContent = "自动发现";
      btn.disabled = false;
    }
  }

  function discoverRow(item) {
    const row = el("div", "row");
    const box = el("input");
    box.type = "checkbox";
    box.checked = item.checked !== false;
    box.onchange = () => {
      if (box.checked) discoverSelection.push(item);
      else discoverSelection = discoverSelection.filter(x => x !== item);
      updateDiscoverButtons();
    };
    row.append(box);
    if (item.type === "adb") {
      const adbState = { device: "ONLINE", unauthorized: "UNAUTHORIZED",
        offline: "OFFLINE" }[item.state] || "UNKNOWN";
      const adbLabel = { device: "已连接", unauthorized: "未授权",
        offline: "离线" }[item.state] || item.state || "unknown";
      row.append(el("span", "", "ADB " + item.serial));
      row.append(badge(adbState, adbLabel));
      row.append(el("span", "muted", item.model || item.product || "-"));
    } else {
      row.append(el("span", "", "SSH " + item.host + ":" + item.port));
      row.append(badge("reachable", "可达"));
      row.append(el("span", "muted", item.banner || ""));
    }
    return row;
  }

  function renderDiscoverBox(container, r) {
    discoverItems = [];
    discoverSelection = [];
    const adb = (r.adb || []).map(d => ({
      type: "adb", host: d.serial, serial: d.serial, state: d.state,
      model: d.model, product: d.product,
      checked: d.state === "device",
    }));
    const ssh = (r.ssh || []).map(h => ({
      type: "ssh", host: h.host, port: h.port || 22, banner: h.banner,
      checked: true,
    }));
    discoverItems = adb.concat(ssh);
    discoverSelection = discoverItems.filter(i => i.checked !== false);
    container.innerHTML = "";
    const h = el("h2", "", "自动发现（SSH / ADB）");
    container.append(h);
    container.append(renderRulesSection());
    if (!discoverItems.length) {
      container.append(el("div", "empty",
        "未发现可通过 SSH/ADB 访问的板卡。请确认板卡已连接、ADB 已授权，或与本机同一局域网。"));
      return;
    }
    const list = el("div", "");
    for (const item of discoverItems) list.append(discoverRow(item));
    container.append(list);
    const actions = el("div", "toolbar");
    if (ssh.length) {
      const u = el("label", "", "SSH 用户");
      const uInput = el("input");
      uInput.id = "disc-ssh-user";
      uInput.value = "root";
      u.append(uInput);
      actions.append(u);
    }
    const selAll = el("button", "btn", "全选");
    selAll.onclick = () => setDiscoverSelection(true);
    const invert = el("button", "btn", "反选");
    invert.onclick = () => invertDiscoverSelection();
    const addSelected = el("button", "btn", "添加所选");
    addSelected.id = "disc-add-selected";
    addSelected.onclick = () => importDiscover(discoverSelection);
    const addAll = el("button", "btn", "全部添加");
    addAll.onclick = () => importDiscover(discoverItems);
    actions.append(selAll, invert, addSelected, addAll);
    container.append(actions);
    updateDiscoverButtons();
  }

  function renderRulesSection() {
    const rules = discoverRules || { ips: [], serials: [] };
    const details = el("details", "discover-rules");
    details.id = "disc-rules-box";
    details.append(el("summary", "", "屏蔽规则（IP / SN）"));
    const body = el("div", "");
    body.append(el("div", "muted",
      "命中规则的 IP/SN 不会出现在发现结果；改动后重新点击「自动发现」生效。"));
    const list = el("div", "");
    list.append(el("div", "sub", "IP 规则"));
    (rules.ips || []).forEach((rule, i) => {
      const row = el("div", "row");
      row.append(el("span", "", rule));
      const del = el("button", "btn", "删除");
      del.onclick = () => saveRules({
        ips: rules.ips.filter((_, j) => j !== i),
        serials: rules.serials,
      });
      row.append(del);
      list.append(row);
    });
    list.append(el("div", "sub", "SN 规则"));
    (rules.serials || []).forEach((rule, i) => {
      const row = el("div", "row");
      row.append(el("span", "", rule));
      const del = el("button", "btn", "删除");
      del.onclick = () => saveRules({
        ips: rules.ips,
        serials: rules.serials.filter((_, j) => j !== i),
      });
      row.append(del);
      list.append(row);
    });
    body.append(list);
    const inputRow = el("div", "toolbar");
    const input = el("input");
    input.id = "disc-rule-input";
    input.placeholder = "IP / CIDR / 通配符，如 10.23.*";
    const addIp = el("button", "btn", "添加 IP 规则");
    addIp.onclick = () => addRule("ips");
    const addSn = el("button", "btn", "添加 SN 规则");
    addSn.onclick = () => addRule("serials");
    inputRow.append(input, addIp, addSn);
    body.append(inputRow);
    if (discoverFiltered &&
        (discoverFiltered.adb || discoverFiltered.ssh)) {
      body.append(el("div", "muted",
        `已按规则过滤 ADB ${discoverFiltered.adb} 台 / SSH ${discoverFiltered.ssh} 台`));
    }
    details.append(body);
    return details;
  }

  async function addRule(kind) {
    const input = $("#disc-rule-input");
    const value = input ? input.value.trim() : "";
    if (!value) {
      hostError($("#dev-error"), "请输入要屏蔽的 IP/CIDR/通配符或 SN");
      return;
    }
    if (discoverRules[kind].includes(value)) {
      hostError($("#dev-error"), "规则已存在");
      return;
    }
    const next = {
      ips: discoverRules.ips.slice(),
      serials: discoverRules.serials.slice(),
    };
    next[kind].push(value);
    if (await saveRules(next)) input.value = "";
  }

  async function saveRules(next) {
    try {
      const r = await R.api.fetch("/api/discover/rules", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rules: next }),
      });
      discoverRules = (r && r.rules) || next;
      const old = $("#disc-rules-box");
      if (old) old.replaceWith(renderRulesSection());
      return true;
    } catch (e) {
      hostError($("#dev-error"), "保存屏蔽规则失败: " + e.message);
      return false;
    }
  }

  function setDiscoverSelection(val) {
    const box = $("#dev-discover-box");
    const boxes = [...box.querySelectorAll('input[type="checkbox"]')];
    discoverSelection = [];
    boxes.forEach((b, i) => {
      b.checked = val;
      if (val && discoverItems[i]) discoverSelection.push(discoverItems[i]);
    });
    updateDiscoverButtons();
  }

  function invertDiscoverSelection() {
    const box = $("#dev-discover-box");
    const boxes = [...box.querySelectorAll('input[type="checkbox"]')];
    discoverSelection = [];
    boxes.forEach((b, i) => {
      b.checked = !b.checked;
      if (b.checked && discoverItems[i]) discoverSelection.push(discoverItems[i]);
    });
    updateDiscoverButtons();
  }

  function updateDiscoverButtons() {
    const btn = $("#disc-add-selected");
    if (btn) btn.disabled = discoverSelection.length === 0;
  }

  async function importDiscover(items) {
    if (!items.length) return;
    const sshUser = $("#disc-ssh-user");
    const user = sshUser ? (sshUser.value.trim() || "root") : "root";
    const box = $("#dev-discover-box");
    box.innerHTML = "";
    box.append(el("div", "muted", "正在导入并检查连接…"));
    try {
      const r = await R.api.fetch("/api/discover/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: items.map(i => ({
          type: i.type, host: i.host, port: i.port,
          user: i.type === "ssh" ? user : undefined,
        })) }),
      });
      box.innerHTML = "";
      box.append(el("h2", "", "导入结果"));
      box.append(el("div", "", `已添加 ${r.devices.length} 台，跳过 ${r.skipped.length} 台`));
      for (const d of r.devices) {
        const c = d.check || {};
        const row = el("div", "row");
        row.append(el("span", "", d.name));
        row.append(badge(c.state || "unknown",
          c.ok ? "连接正常" : (c.error || "未检查")));
        if (!c.ok) {
          const edit = el("button", "btn", "编辑");
          edit.onclick = () => openDevModal(d);
          row.append(edit);
        }
        box.append(row);
      }
      for (const s of r.skipped) {
        box.append(el("div", "muted",
          `${s.type} ${s.host || "-"}: ${s.error || "未知原因"}`));
      }
      await renderDevicesPage();
    } catch (e) {
      hostError($("#dev-error"), "导入失败: " + e.message);
    }
  }

  function bind() {
    $("#dev-add").onclick = () => openDevModal(null);
    $("#dev-discover").onclick = devDiscover;
    $("#dev-refresh").onclick = renderDevicesPage;
    $("#dev-save").onclick = saveDev;
    $("#dev-cancel").onclick = () => { $("#dev-modal").style.display = "none"; };
    $("#dev-f-type").onchange = syncDevFormFields;
  }

  R.pages = R.pages || {};
  R.pages.devices = {
    render: renderDevicesPage,
    openModal: openDevModal,
    saveDev,
    syncDevFormFields,
    bind,
  };
})();
