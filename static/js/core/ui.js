(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const $ = R.dom.$;
  const el = R.dom.el;

  function hostError(container, msg) {
    container.innerHTML = "";
    if (!msg) return;
    container.append(el("div", "error-bar", "错误: " + msg));
  }

  function deviceOptions(sel, includeLocal, keep) {
    const cur = keep ? sel.value : "";
    sel.innerHTML = "";
    const opts = [];
    if (includeLocal) opts.push({ id: "local", name: "上位机本地（local）" });
    for (const d of R.store.devices) opts.push({ id: d.id, name: d.name + (d.type === "adb" ? " (adb)" : "") });
    for (const o of opts) {
      const opt = el("option", "", o.name);
      opt.value = o.id;
      sel.append(opt);
    }
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  }

  async function loadDevices() {
    try {
      const r = await R.api.fetch("/api/devices");
      R.store.setDevices(r.devices || []);
    } catch (e) {
      hostError($("#dev-error"), e.message);
    }
  }

  function deviceName(id) {
    const d = R.store.deviceMap[id];
    return d ? d.name : null;
  }

  R.ui = R.ui || {};
  R.ui.errorBar = hostError;
  R.ui.hostError = hostError;
  R.ui.deviceOptions = deviceOptions;
  R.ui.loadDevices = loadDevices;
  R.ui.deviceName = deviceName;
})();
