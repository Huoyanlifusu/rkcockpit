(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});

  const COLORS = ["#58a6ff", "#2ea043", "#d29922", "#f85149", "#bc8cff", "#39c5cf"];
  const GRID = "#2a3442";
  const MUTED = "#8b98a9";

  function line(canvas, series, opts) {
    opts = opts || {};
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(60, canvas.clientWidth || canvas.parentNode.clientWidth || 300);
    const H = Math.max(40, canvas.clientHeight || 120);
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const padL = 44, padR = 10, padT = 16, padB = 20;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    let lo = Infinity, hi = -Infinity;
    let tMax = -Infinity;
    let any = false;
    for (const s of series) {
      for (const p of s.points || []) {
        if (p.ts > tMax) tMax = p.ts;
        if (p.value === null || p.value === undefined) continue;
        if (p.value < lo) lo = p.value;
        if (p.value > hi) hi = p.value;
        any = true;
      }
    }
    if (!any) { lo = 0; hi = 1; }
    if (opts.min !== undefined) lo = opts.min;
    if (opts.max !== undefined) hi = opts.max;
    if (lo === hi) { lo -= 1; hi += 1; }
    if (lo >= 0 && lo - (hi - lo) * 0.05 < 0) lo = 0;
    const span = hi - lo;
    lo -= span * 0.05; hi += span * 0.05;

    const win = opts.windowMs || 60000;
    if (tMax === -Infinity) tMax = Date.now();
    const tMin = tMax - win;

    const yAt = v => padT + plotH * (1 - (v - lo) / (hi - lo));
    const xAt = t => padL + (t - tMin) / win * plotW;
    const fmtNum = v => {
      if (opts.yFmt) return opts.yFmt(v);
      if (opts.decimals !== undefined) return v.toFixed(opts.decimals);
      if (v >= 1000) return String(Math.round(v));
      if (v >= 100) return v.toFixed(0);
      return v.toFixed(1);
    };

    ctx.font = "10px monospace";
    const steps = 4;
    for (let i = 0; i <= steps; i++) {
      const y = padT + plotH * i / steps;
      ctx.strokeStyle = GRID;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
      ctx.fillStyle = MUTED;
      ctx.textAlign = "right";
      ctx.fillText(fmtNum(hi - (hi - lo) * i / steps), padL - 5, y + 3);
    }
    ctx.fillStyle = MUTED;
    ctx.textAlign = "center";
    for (let i = 0; i <= 2; i++) {
      const d = new Date(tMin + win * i / 2);
      const p = n => (n < 10 ? "0" : "") + n;
      ctx.fillText(p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()),
                   padL + plotW * i / 2, H - 6);
    }
    if (opts.yLabel) {
      ctx.textAlign = "left";
      ctx.fillText(opts.yLabel, padL + 2, 8);
    }

    series.forEach((s, idx) => {
      const color = s.color || COLORS[idx % COLORS.length];
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      let pen = false;
      for (const p of s.points || []) {
        if (p.ts < tMin) continue;
        if (p.value === null || p.value === undefined) { pen = false; continue; }
        const x = xAt(p.ts), y = yAt(p.value);
        if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        pen = true;
      }
      ctx.stroke();
    });

    if (series.length > 1) {
      let ly = padT;
      for (let i = 0; i < series.length; i++) {
        const s = series[i];
        const lbl = s.name || "";
        ctx.fillStyle = s.color || COLORS[i % COLORS.length];
        ctx.fillRect(W - padR - 8 - ctx.measureText(lbl).width, ly, 6, 6);
        ctx.fillStyle = MUTED;
        ctx.textAlign = "right";
        ctx.fillText(lbl, W - padR - 2, ly + 6);
        ly += 12;
      }
    }
  }

  R.charts = R.charts || {};
  R.charts.line = line;
})();
