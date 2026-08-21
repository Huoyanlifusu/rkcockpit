(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const $ = R.dom.$;
  const el = R.dom.el;
  const api = R.api.fetch;
  const hostError = R.ui.hostError;
  const loadDevices = R.ui.loadDevices;
  const deviceOptions = R.ui.deviceOptions;
  const fmtTime = R.dom.fmtTime;

  let keys = [];
  let installKeyId = null;

  function ensureRoot() {
    let root = $("#tab-keys");
    if (!root) {
      root = el("section", "pane");
      root.id = "tab-keys";
      const main = document.querySelector("main");
      if (main) main.append(root);
    }
    if (!root.querySelector("#key-generate")) {
    root.innerHTML =
      '<div class="toolbar">' +
      '<label>密钥名称 <input id="key-f-name" placeholder="如 rv1126b-01"></label>' +
      '<label>类型 <select id="key-f-type">' +
      '<option value="ed25519">ed25519</option>' +
      '<option value="rsa">rsa</option></select></label>' +
      '<label>备注 <input id="key-f-comment" placeholder="可选"></label>' +
      '<button id="key-generate" class="btn">生成密钥</button>' +
      '<button id="key-refresh" class="btn">刷新</button>' +
      "</div>" +
      '<div id="key-error"></div>' +
      '<div class="panel">' +
      '<table class="fm-table" id="key-table">' +
      "<thead><tr><th>名称</th><th>类型</th><th>指纹</th><th>创建时间</th><th>操作</th></tr></thead>" +
      "<tbody></tbody></table></div>" +
      '<div id="key-modal" class="modal-mask" style="display:none"><div class="modal">' +
      "<h3>安装到设备</h3>" +
      '<div class="row"><label>设备 *</label><select id="key-ms-device"></select></div>' +
      '<div class="row"><label>目标用户</label>' +
      '<input id="key-ms-user" placeholder="留空 = 设备配置的用户"></div>' +
      '<div class="toolbar">' +
      '<button id="key-ms-ok" class="btn">安装</button>' +
      '<button id="key-ms-cancel" class="btn">取消</button>' +
      "</div></div></div>";
    }
    return root;
  }

  async function render() {
    ensureRoot();
    const errBox = $("#key-error");
    errBox.innerHTML = "";
    try {
      const r = await api("/api/keys");
      keys = r.keys || [];
    } catch (e) {
      hostError(errBox, e.message);
    }
    const tb = $("#key-table tbody");
    tb.innerHTML = "";
    for (const k of keys) {
      const tr = el("tr");
      tr.append(el("td", "", k.name));
      tr.append(el("td", "", k.type));
      tr.append(el("td", "muted", k.fingerprint || "-"));
      tr.append(el("td", "muted", fmtTime(k.created_at)));
      const ops = el("td", "ops");
      const bInst = el("button", "", "安装到设备");
      bInst.onclick = () => openInstall(k);
      const bDel = el("button", "del", "删除");
      bDel.onclick = () => keyDelete(k);
      ops.append(bInst, bDel);
      tr.append(ops);
      tb.append(tr);
    }
    if (!keys.length) {
      const tr = el("tr");
      const td = el("td", "empty", "暂无密钥。填写上方表单点击「生成密钥」创建。");
      td.colSpan = 5;
      tr.append(td);
      tb.append(tr);
    }
  }

  async function keyGenerate() {
    const name = $("#key-f-name").value.trim();
    if (!name) {
      hostError($("#key-error"), "密钥名称必填");
      return;
    }
    const body = { name, type: $("#key-f-type").value };
    const comment = $("#key-f-comment").value.trim();
    if (comment) body.comment = comment;
    try {
      const r = await api("/api/keys/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(r.error || "生成失败");
      $("#key-f-name").value = "";
      $("#key-f-comment").value = "";
      await render();
    } catch (e) {
      hostError($("#key-error"), e.message);
    }
  }

  async function openInstall(k) {
    installKeyId = k.id;
    try {
      await loadDevices();
      deviceOptions($("#key-ms-device"), false, true);
    } catch (e) {
      hostError($("#key-error"), e.message);
      return;
    }
    $("#key-ms-user").value = "";
    $("#key-modal").style.display = "flex";
  }

  async function keyInstall() {
    const body = { device_id: $("#key-ms-device").value };
    const user = $("#key-ms-user").value.trim();
    if (user) body.target_user = user;
    try {
      const r = await api("/api/keys/" + installKeyId + "/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(r.error || "安装失败");
      $("#key-modal").style.display = "none";
      const box = $("#key-error");
      box.innerHTML = "";
      const ok = el("div", "error-bar", "已安装到设备 " + body.device_id);
      ok.className = "state-ok";
      box.append(ok);
    } catch (e) {
      hostError($("#key-error"), e.message);
    }
  }

  async function keyDelete(k) {
    if (!confirm("确认删除密钥「" + k.name + "」？")) return;
    try {
      const r = await api("/api/keys/" + k.id, { method: "DELETE" });
      if (!r.ok) throw new Error(r.error || "删除失败");
      await render();
    } catch (e) {
      hostError($("#key-error"), e.message);
    }
  }

  function bind() {
    ensureRoot();
    $("#key-generate").onclick = keyGenerate;
    $("#key-refresh").onclick = render;
    $("#key-ms-ok").onclick = keyInstall;
    $("#key-ms-cancel").onclick = () => { $("#key-modal").style.display = "none"; };
  }

  R.pages = R.pages || {};
  R.pages.keys = { render, bind };
})();
