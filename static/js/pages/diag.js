(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const $ = R.dom.$;
  const api = R.api.fetch;
  const hostError = R.ui.hostError;
  const loadDevices = R.ui.loadDevices;
  const deviceOptions = R.ui.deviceOptions;

  const FOLLOW_MS = 3000;
  let device = "local";
  let followTimer = null;

  async function loadDmesg() {
    const view = $("#diag-dmesg");
    const errBox = $("#diag-error");
    const qs = [
      "lines=" + ($("#diag-lines").value || "200"),
    ];
    const f = $("#diag-filter").value.trim();
    if (f) qs.push("filter=" + encodeURIComponent(f));
    try {
      const r = await api("/api/diag/" + encodeURIComponent(device) + "/dmesg?" + qs.join("&"));
      if (!r.ok) throw new Error(r.error);
      const lines = r.lines || [];
      view.textContent = lines.join("\n");
      if ($("#diag-follow").checked) view.scrollTop = view.scrollHeight;
    } catch (e) {
      hostError(errBox, "dmesg: " + e.message);
    }
  }

  function startFollow() {
    stopFollow();
    followTimer = setInterval(loadDmesg, FOLLOW_MS);
  }

  function stopFollow() {
    if (followTimer) { clearInterval(followTimer); followTimer = null; }
  }

  async function render() {
    const errBox = $("#diag-error");
    errBox.innerHTML = "";
    await loadDevices();
    deviceOptions($("#diag-device"), true, true);
    device = $("#diag-device").value || "local";
    await loadDmesg();
  }

  function deactivate() {
    stopFollow();
  }

  function bind() {
    $("#diag-device").onchange = (e) => {
      device = e.target.value;
      render();
    };
    $("#diag-refresh").onclick = loadDmesg;
    $("#diag-follow").onchange = (e) => {
      if (e.target.checked) { loadDmesg(); startFollow(); }
      else stopFollow();
    };
  }

  R.pages = R.pages || {};
  R.pages.diag = { render, bind, deactivate };
})();
