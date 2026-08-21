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

  const POLL_MS = 2000;
  let device = "local";
  let winS = 180;
  let sampling = false;
  let timer = null;
  let es = null;
  let mode = "sse";          // "sse" | "poll"
  let samples = [];
  let latest = null;

  function setBtn() {
    const b = $("#mon-toggle");
    b.textContent = sampling ? "停止采样" : "开始采样";
    b.classList.toggle("danger", sampling);
  }

  function summaryText() {
    if (!latest) return "暂无样本";
    const parts = [];
    if (latest.cpu && latest.cpu.usage !== null && latest.cpu.usage !== undefined) {
      parts.push("cpu " + latest.cpu.usage.toFixed(1) + "%");
      if (latest.cpu.freq_mhz) parts.push(latest.cpu.freq_mhz + "MHz");
    }
    if (latest.mem && latest.mem.used_mb !== null && latest.mem.used_mb !== undefined) {
      parts.push("mem " + latest.mem.used_mb + "MB/" + (latest.mem.total_mb || "?") + "MB");
    }
    const t = latest.temp || {};
    const tk = Object.keys(t)[0];
    if (tk) parts.push("temp " + t[tk].toFixed(1) + "°C");
    if (latest.load && latest.load[0] !== null && latest.load[0] !== undefined) {
      parts.push("load " + latest.load[0].toFixed(2));
    }
    return "最近: " + parts.join(" · ");
  }

  function trimWindow() {
    const winMs = winS * 1000;
    const ref = samples.length ? samples[samples.length - 1].ts : Date.now();
    while (samples.length && samples[0].ts < ref - winMs) samples.shift();
  }

  function appendSample(s) {
    samples.push(s);
    trimWindow();
  }

  function ssePoint(d) {
    const has = v => v !== undefined && v !== null;
    return {
      ts: d.t,
      gap: false,
      cpu: has(d.c) ? { usage: d.c } : null,
      mem: d.m && has(d.m.u) ? { used_mb: d.m.u } : null,
      temp: has(d.T) ? { _: d.T } : null,
      load: has(d.l) ? [d.l] : null,
    };
  }

  async function pollOnce() {
    const errBox = $("#mon-error");
    try {
      const r = await api("/api/monitor/" + encodeURIComponent(device) +
        "/series?window=" + winS);
      if (!r.ok) throw new Error(r.error);
      samples = r.samples || [];
      draw(samples);
      const ss = r.samples || [];
      latest = ss.length ? ss[ss.length - 1] : null;
      $("#mon-summary").textContent = summaryText();
      errBox.innerHTML = "";
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  function draw(samples) {
    const mk = (key, pick) => {
      const pts = [];
      for (const s of samples) {
        const v = s && s.gap ? null : pick(s);
        pts.push({ ts: s.ts, value: v });
      }
      return { name: key, points: pts };
    };
    const winMs = winS * 1000;

    const cpuSer = [];
    cpuSer.push(mk("usage", s => (s.cpu && s.cpu.usage !== null && s.cpu.usage !== undefined)
      ? s.cpu.usage : null));
    const perCore = samples.length ? ((samples[samples.length - 1].cpu || {}).per_core || []) : [];
    perCore.forEach((_, i) =>
      cpuSer.push(mk("core" + i, s => {
        const c = (s.cpu || {}).per_core || [];
        return c[i] !== undefined && c[i] !== null ? c[i] : null;
      })));

    R.charts.line($("#mon-cpu"), cpuSer, {
      yLabel: "CPU %", decimals: 0, min: 0, max: 100, windowMs: winMs,
    });
    R.charts.line($("#mon-mem"), [mk("used", s => (s.mem && s.mem.used_mb !== null)
      ? s.mem.used_mb : null)], {
      yLabel: "MEM MB", decimals: 0, min: 0, windowMs: winMs,
    });
    R.charts.line($("#mon-temp"), [mk("temp", s => {
      const t = s.temp || {};
      const k = Object.keys(t)[0];
      return k ? t[k] : null;
    })], { yLabel: "°C", decimals: 1, min: 0, windowMs: winMs });
    R.charts.line($("#mon-load"), [mk("load1", s => (s.load && s.load[0] !== null
      && s.load[0] !== undefined) ? s.load[0] : null)], {
      yLabel: "load", decimals: 2, min: 0, windowMs: winMs,
    });
  }

  function openStream() {
    closeStream();
    mode = "sse";
    const errBox = $("#mon-error");
    const url = "/api/monitor/" + encodeURIComponent(device) + "/stream";
    es = new EventSource(url);
    es.onopen = () => { errBox.innerHTML = ""; };
    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        appendSample(ssePoint(d));
        latest = samples[samples.length - 1];
        draw(samples);
        $("#mon-summary").textContent = summaryText();
      } catch (e) { }
    };
    es.addEventListener("gap", (ev) => {
      try {
        const d = JSON.parse(ev.data);
        appendSample({ ts: d.t, gap: true, cpu: null, mem: null,
                       temp: null, load: null });
        draw(samples);
      } catch (e) { }
    });
    es.onerror = () => {
      if (mode === "poll") return;
      closeStream();
      mode = "poll";
      startPoll();
    };
  }

  function closeStream() {
    if (es) { es.close(); es = null; }
  }

  function startPoll() {
    stopPoll();
    timer = setInterval(pollOnce, POLL_MS);
    pollOnce();
  }

  function stopPoll() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  function stopAll() {
    stopPoll();
    closeStream();
  }

  async function toggleSampling() {
    const errBox = $("#mon-error");
    const action = sampling ? "disable" : "enable";
    try {
      const r = await api("/api/monitor/" + encodeURIComponent(device) + "/" + action,
        { method: "POST" });
      if (!r.ok) throw new Error(r.error);
      sampling = !sampling;
      setBtn();
    } catch (e) {
      hostError(errBox, e.message);
    }
  }

  async function render() {
    stopAll();
    samples = [];
    const errBox = $("#mon-error");
    errBox.innerHTML = "";
    await loadDevices();
    deviceOptions($("#mon-device"), true, true);
    device = $("#mon-device").value || "local";
    $("#mon-summary").textContent = deviceName(device) || "local";
    try {
      const r = await api("/api/monitor/" + encodeURIComponent(device) + "/now");
      if (r.ok && r.sample) {
        latest = r.sample;
        sampling = true;
        setBtn();
        $("#mon-summary").textContent = summaryText();
      }
    } catch (e) { }
    openStream();
  }

  function deactivate() {
    stopAll();
    samples = [];
  }

  function bind() {
    $("#mon-device").onchange = (e) => {
      device = e.target.value;
      render();
    };
    $("#mon-window").onchange = (e) => {
      winS = parseInt(e.target.value, 10);
      samples = [];
      pollOnce();
    };
    $("#mon-toggle").onclick = toggleSampling;
    window.addEventListener("resize", () => {
      if (mode === "poll" && timer) { pollOnce(); return; }
      if (samples.length) draw(samples);
    });
  }

  R.pages = R.pages || {};
  R.pages.monitor = { render, bind, deactivate };
})();
