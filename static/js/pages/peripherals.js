(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const $ = R.dom.$;
  const el = R.dom.el;
  const api = R.api.fetch;
  const rawApi = R.api.api;
  const hostError = R.ui.hostError;
  const loadDevices = R.ui.loadDevices;
  const deviceOptions = R.ui.deviceOptions;

  let device = "local";

  async function streamTest(item, btn, video) {
    btn.disabled = true;
    for (const old of item.querySelectorAll(".periph-result")) old.remove();
    const res = el("div", "diag-sub periph-result");
    res.textContent = "出流测试中…";
    item.append(res);
    try {
      const r = await rawApi("/api/diag/" + encodeURIComponent(device) + "/stream-test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ video: video }),
      });
      if (!r.ok) {
        res.textContent = "出流测试失败: " + (r.error || "未知错误");
      } else if (r.status === "STREAMOK") {
        const info = [
          "帧数 " + (r.frames === null || r.frames === undefined ? "-" : r.frames),
          "文件 " + (r.file || "-"),
          "大小 " + R.dom.fmtSize(r.file_size),
          (r.width || "-") + "x" + (r.height || "-") + " " + (r.pixelformat || "-"),
        ].join("，");
        res.textContent = "出流测试通过（STREAMOK）: " + info;
      } else {
        res.textContent = "出流测试" + (r.status || "失败") + ": " + (r.error || "");
      }
    } catch (e) {
      const msg = (e.message || "").indexOf("429") >= 0
        ? "并发已满（上限 2 路），请稍后重试"
        : (e.message || "请求失败");
      res.textContent = "出流测试失败: " + msg;
    } finally {
      btn.disabled = false;
    }
  }

  async function loadVideo() {
    const box = $("#periph-video");
    const errBox = $("#periph-error");
    try {
      const r = await api("/api/diag/" + encodeURIComponent(device) + "/video");
      if (!r.ok) throw new Error(r.error);
      box.innerHTML = "";
      const devs = r.devices || [];
      if (!devs.length) { box.append(el("div", "empty", "未发现视频设备")); return; }
      for (const d of devs) {
        const item = el("div", "diag-item");
        const head = el("div", "");
        head.append(el("b", "", d.path || "-"));
        if (d.name) head.append(el("span", "muted", "  " + d.name));
        if (d.status) head.append(el("span", "level-warn", "  " + d.status));
        const btn = el("button", "btn", "测试出流");
        btn.type = "button";
        btn.onclick = () => streamTest(item, btn, d.path);
        head.append(btn);
        item.append(head);
        if (d.formats) item.append(el("div", "diag-sub", d.formats));
        box.append(item);
      }
    } catch (e) {
      hostError(errBox, "video: " + e.message);
    }
  }

  async function loadUsb() {
    const box = $("#periph-usb");
    const errBox = $("#periph-error");
    try {
      const r = await api("/api/diag/" + encodeURIComponent(device) + "/usb");
      if (!r.ok) throw new Error(r.error);
      box.innerHTML = "";
      const devs = r.devices || [];
      if (!devs.length) { box.append(el("div", "empty", "未发现 USB 设备")); return; }
      for (const d of devs) {
        const item = el("div", "diag-item");
        if (d.raw) {
          item.append(el("span", "muted", d.raw));
        } else {
          item.append(el("b", "", "Bus " + (d.bus || "-") + " Dev " + (d.dev || "-")));
          item.append(el("span", "", "  " + (d.desc || "-")));
          if (d.vid && d.pid) {
            item.append(el("span", "muted", "  VID:PID " + d.vid + ":" + d.pid));
          }
        }
        box.append(item);
      }
    } catch (e) {
      hostError(errBox, "usb: " + e.message);
    }
  }

  async function loadI2c() {
    const box = $("#periph-i2c");
    const errBox = $("#periph-error");
    try {
      const r = await api("/api/periph/" + encodeURIComponent(device) + "/i2c");
      if (!r.ok) throw new Error(r.error);
      box.innerHTML = "";
      const buses = r.buses || [];
      if (!buses.length) { box.append(el("div", "empty", "未发现 I2C 总线或设备")); return; }
      for (const b of buses) {
        const item = el("div", "diag-item");
        const head = el("div", "");
        head.append(el("b", "", "I2C-" + (b.bus || "-")));
        if (b.name) head.append(el("span", "muted", "  " + b.name));
        item.append(head);
        const devs = b.devices || [];
        if (!devs.length) {
          item.append(el("div", "diag-sub", "（该总线无设备）"));
        } else {
          for (const d of devs) {
            const sub = el("div", "diag-sub");
            sub.append(el("span", "", "0x" + (d.addr || "-") + "  " + (d.name || "-")));
            if (d.driver) {
              sub.append(el("span", "muted", "  已绑定 " + d.driver));
            } else {
              sub.append(el("span", "level-warn", "  未绑定驱动"));
            }
            item.append(sub);
          }
        }
        box.append(item);
      }
    } catch (e) {
      hostError(errBox, "i2c: " + e.message);
    }
  }

  async function loadGpio() {
    const box = $("#periph-gpio");
    const errBox = $("#periph-error");
    try {
      const r = await api("/api/periph/" + encodeURIComponent(device) + "/gpio");
      if (!r.ok) throw new Error(r.error);
      box.innerHTML = "";
      if (r.source) box.append(el("div", "diag-sub", "采集方式: " + r.source));
      const chips = r.chips || [];
      if (!chips.length) { box.append(el("div", "empty", "未发现 GPIO 控制器")); return; }
      const MAX_LINES = 64;
      for (const c of chips) {
        const item = el("div", "diag-item");
        const head = el("div", "");
        head.append(el("b", "", c.name || "-"));
        if (c.ngpio !== null && c.ngpio !== undefined) {
          head.append(el("span", "muted", "  " + c.ngpio + " 线"));
        }
        item.append(head);
        const lines = c.lines || [];
        if (!lines.length) {
          item.append(el("div", "diag-sub", "（无占用信息，仅控制器枚举）"));
        } else {
          const shown = lines.slice(0, MAX_LINES);
          for (const l of shown) {
            const sub = el("div", "diag-sub");
            let txt = "line " + l.line + "  " + (l.label || "-");
            if (l.owner) txt += "  占用 " + l.owner;
            if (l.direction) txt += "  " + l.direction;
            sub.append(el("span", "", txt));
            item.append(sub);
          }
          if (lines.length > MAX_LINES) {
            item.append(el("div", "diag-sub",
              "… 共 " + lines.length + " 行，仅显示前 " + MAX_LINES + " 行"));
          }
        }
        box.append(item);
      }
    } catch (e) {
      hostError(errBox, "gpio: " + e.message);
    }
  }

  function jumpToDmesg(filter) {
    const f = document.getElementById("diag-filter");
    if (f) f.value = filter || "";
    const tab = document.querySelector('.tab[data-tab="diag"]');
    if (tab) tab.click();
  }

  function jumpBtn(filter) {
    const btn = el("button", "btn", "查看内核日志");
    btn.type = "button";
    btn.onclick = () => jumpToDmesg(filter);
    return btn;
  }

  function restrictedBlock(msg, filter) {
    const item = el("div", "diag-item");
    const head = el("div", "");
    head.append(R.dom.badge("UNAUTHORIZED", "受限"));
    item.append(head);
    if (msg) item.append(el("div", "diag-sub", msg));
    const row = el("div", "diag-sub");
    row.append(jumpBtn(filter));
    item.append(row);
    return item;
  }

  function emptyBlock(msg, filter) {
    const item = el("div", "diag-item");
    item.append(el("span", "muted", msg));
    const row = el("div", "diag-sub");
    row.append(jumpBtn(filter));
    item.append(row);
    return item;
  }

  async function loadPeriphPanel(id, url, filter, renderFn) {
    const box = $("#" + id);
    box.innerHTML = "";
    let r;
    try {
      r = await rawApi(url);
    } catch (e) {
      box.append(restrictedBlock("请求失败: " + e.message, filter));
      return;
    }
    if (!r.ok) {
      box.append(restrictedBlock("采集失败: " + (r.error || "未知错误"), filter));
      return;
    }
    try {
      renderFn(box, r, filter);
    } catch (e) {
      box.append(restrictedBlock("渲染失败: " +
        (e && e.message ? e.message : "未知错误"), filter));
    }
  }

  function fmtHz(n) {
    if (n === null || n === undefined) return "-";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + " MHz";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + " kHz";
    return n + " Hz";
  }

  function loadPwm() {
    return loadPeriphPanel("periph-pwm",
      "/api/periph/" + encodeURIComponent(device) + "/pwm", "pwm",
      (box, r, filter) => {
        const chips = r.chips || [];
        if (!chips.length) { box.append(emptyBlock("未发现 PWM 控制器", filter)); return; }
        for (const c of chips) {
          const item = el("div", "diag-item");
          const head = el("div", "");
          head.append(el("b", "", c.name || "-"));
          if (c.label) head.append(el("span", "muted", "  " + c.label));
          if (c.npwm !== null && c.npwm !== undefined) {
            head.append(el("span", "muted", "  " + c.npwm + " 通道"));
          }
          item.append(head);
          const chs = c.channels || [];
          if (!chs.length) {
            item.append(el("div", "diag-sub", "（无已 export 通道）"));
          } else {
            for (const ch of chs) {
              const bits = ["pwm" + ch.index];
              if (ch.period_ns !== null && ch.period_ns !== undefined) {
                bits.push("period " + ch.period_ns + "ns");
              }
              if (ch.duty_ns !== null && ch.duty_ns !== undefined) {
                bits.push("duty " + ch.duty_ns + "ns");
              }
              if (ch.polarity) bits.push(ch.polarity);
              if (ch.enabled !== null && ch.enabled !== undefined) {
                bits.push(ch.enabled ? "enable" : "disable");
              }
              item.append(el("div", "diag-sub", bits.join("  ")));
            }
          }
          box.append(item);
        }
      });
  }

  function loadSpi() {
    return loadPeriphPanel("periph-spi",
      "/api/periph/" + encodeURIComponent(device) + "/spi", "spi",
      (box, r, filter) => {
        const masters = r.masters || [];
        if (!masters.length) { box.append(emptyBlock("未发现 SPI 控制器", filter)); return; }
        for (const m of masters) {
          const item = el("div", "diag-item");
          item.append(el("b", "", m.name || "-"));
          const devs = m.devices || [];
          if (!devs.length) {
            item.append(el("div", "diag-sub", "（无挂载设备）"));
          } else {
            for (const d of devs) {
              const sub = el("div", "diag-sub");
              sub.append(el("span", "", (d.path || "-") + "  " + (d.name || "-")));
              if (d.spidev) sub.append(el("span", "muted", "  /dev/spidev"));
              item.append(sub);
            }
          }
          box.append(item);
        }
      });
  }

  function loadUart() {
    return loadPeriphPanel("periph-uart",
      "/api/periph/" + encodeURIComponent(device) + "/uart", "uart",
      (box, r, filter) => {
        const ports = r.ports || [];
        if (!ports.length) { box.append(emptyBlock("未发现串口节点", filter)); return; }
        for (const p of ports) {
          const item = el("div", "diag-item");
          const bits = [p.name || "-"];
          if (p.type) bits.push(p.type);
          if (p.tx !== null && p.tx !== undefined) bits.push("tx:" + p.tx);
          if (p.rx !== null && p.rx !== undefined) bits.push("rx:" + p.rx);
          item.append(el("span", "", bits.join("  ")));
          box.append(item);
        }
      });
  }

  function loadClk() {
    return loadPeriphPanel("periph-clk",
      "/api/periph/" + encodeURIComponent(device) + "/clk", "clk",
      (box, r, filter) => {
        if (r.restricted) {
          box.append(restrictedBlock(r.reason || "时钟信息不可用", filter));
          return;
        }
        const clocks = r.clocks || [];
        if (!clocks.length) { box.append(emptyBlock("未发现时钟", filter)); return; }
        const MAX_CLOCKS = 200;
        const shown = clocks.slice(0, MAX_CLOCKS);
        for (const c of shown) {
          const item = el("div", "diag-item");
          item.append(el("b", "", c.name || "-"));
          const bits = [];
          if (c.rate !== null && c.rate !== undefined) bits.push(fmtHz(c.rate));
          if (c.enable !== null && c.enable !== undefined) bits.push("enable " + c.enable);
          if (c.prepare !== null && c.prepare !== undefined) bits.push("prepare " + c.prepare);
          if (bits.length) item.append(el("div", "diag-sub", bits.join("  ")));
          box.append(item);
        }
        if (clocks.length > MAX_CLOCKS) {
          box.append(el("div", "diag-sub",
            "… 共 " + clocks.length + " 个时钟，仅显示前 " + MAX_CLOCKS + " 个"));
        }
      });
  }

  function fmtVolt(n) {
    if (n === null || n === undefined) return "-";
    if (n >= 1e6) return (n / 1e6).toFixed(3) + " V";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + " mV";
    return n + " µV";
  }

  function loadWatchdog() {
    return loadPeriphPanel("periph-watchdog",
      "/api/periph/" + encodeURIComponent(device) + "/watchdog", "watchdog",
      (box, r, filter) => {
        const devs = r.devices || [];
        if (!devs.length) { box.append(emptyBlock("未发现看门狗设备", filter)); return; }
        for (const d of devs) {
          const item = el("div", "diag-item");
          const bits = [d.name || "-"];
          if (d.state) bits.push(d.state);
          if (d.timeout !== null && d.timeout !== undefined) {
            bits.push("超时 " + d.timeout + "s");
          }
          if (d.bootstatus !== null && d.bootstatus !== undefined) {
            bits.push("bootstatus " + d.bootstatus);
          }
          item.append(el("span", "", bits.join("  ")));
          box.append(item);
        }
      });
  }

  function loadRegulator() {
    return loadPeriphPanel("periph-regulator",
      "/api/periph/" + encodeURIComponent(device) + "/regulator", "regulator",
      (box, r, filter) => {
        const regs = r.regulators || [];
        if (!regs.length) { box.append(emptyBlock("未发现电源调节器", filter)); return; }
        for (const g of regs) {
          const item = el("div", "diag-item");
          const bits = [g.name || "-"];
          if (g.state) bits.push(g.state);
          if (g.microvolts !== null && g.microvolts !== undefined) {
            bits.push(fmtVolt(g.microvolts));
          }
          item.append(el("span", "", bits.join("  ")));
          box.append(item);
        }
      });
  }

  function loadDma() {
    return loadPeriphPanel("periph-dma",
      "/api/periph/" + encodeURIComponent(device) + "/dma", "dma",
      (box, r, filter) => {
        if (r.restricted) {
          box.append(restrictedBlock(r.reason || "DMA 信息不可用", filter));
          return;
        }
        const ctrls = r.controllers || [];
        if (ctrls.length) {
          for (const ctrl of ctrls) {
            const item = el("div", "diag-item");
            const head = el("div", "");
            head.append(el("b", "", ctrl.name || "-"));
            if (ctrl.addr) head.append(el("span", "muted", "  " + ctrl.addr));
            if (ctrl.nchannels !== null && ctrl.nchannels !== undefined) {
              head.append(el("span", "muted", "  " + ctrl.nchannels + " 通道"));
            }
            item.append(head);
            for (const c of (ctrl.channels || [])) {
              const sub = el("div", "diag-sub");
              const bits = [c.chan || "-"];
              if (c.client) bits.push("→ " + c.client);
              sub.append(el("span", "", bits.join("  ")));
              item.append(sub);
            }
            box.append(item);
          }
          return;
        }
        const chs = r.channels || [];
        if (!chs.length) { box.append(emptyBlock("未发现 DMA 通道", filter)); return; }
        for (const c of chs) {
          const item = el("div", "diag-item");
          const bits = ["chan " + (c.chan === null || c.chan === undefined ? "-" : c.chan)];
          if (c.name) bits.push(c.name);
          item.append(el("span", "", bits.join("  ")));
          box.append(item);
        }
      });
  }

  async function render() {
    const errBox = $("#periph-error");
    errBox.innerHTML = "";
    await loadDevices();
    deviceOptions($("#periph-device"), true, true);
    device = $("#periph-device").value || "local";
    await Promise.all([loadVideo(), loadUsb(), loadI2c(), loadGpio(),
                       loadPwm(), loadSpi(), loadUart(), loadClk(),
                       loadWatchdog(), loadRegulator(), loadDma()]);
  }

  function deactivate() {
    }

  function bind() {
    $("#periph-device").onchange = (e) => {
      device = e.target.value;
      render();
    };
  }

  R.pages = R.pages || {};
  R.pages.peripherals = { render, bind, deactivate };
})();
