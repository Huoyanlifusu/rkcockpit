(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});
  const $ = R.dom.$;
  const TERM = R.state.term;
  const api = R.api.fetch;
  const loadDevices = R.ui.loadDevices;
  const deviceOptions = R.ui.deviceOptions;

  function termAppend(text) {
    const view = $("#term-view");
    view.textContent += text;
    if (TERM.follow) view.scrollTop = view.scrollHeight;
  }

  function stopTermPolling() {
    TERM.pollToken += 1;
    if (TERM.pollTimer) clearTimeout(TERM.pollTimer);
    TERM.pollTimer = null;
  }

  async function renderTermPage() {
    await loadDevices();
    deviceOptions($("#term-device"), true, true);
    TERM.device = $("#term-device").value || "local";
    try {
      const r = await api("/api/exec/" + TERM.device + "/running");
      const jobs = r.jobs || [];
      if (jobs.length) {
        $("#term-status").textContent = "进行中: " + jobs.map(j => j.cmd).join("; ");
      }
    } catch (e) {
      /* ignore */
    }
  }

  async function termRun(cmd) {
    if (!cmd) return;
    stopTermPolling();
    termAppend("$ " + cmd + "\n");
    TERM.offset = 0;
    const device = TERM.device;
    try {
      const r = await api("/api/exec/" + device + "/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cmd, timeout: 120 }),
      });
      if (!r.ok) throw new Error(r.error);
      TERM.jobId = r.job_id;
      TERM.jobDevice = device;
      $("#term-status").textContent = "运行中 " + r.job_id;
      termPollLoop(r.job_id, device);
    } catch (e) {
      $("#term-status").textContent = "错误: " + e.message;
    }
  }

  function termPollLoop(jobId, device) {
    stopTermPolling();
    const token = TERM.pollToken;

    async function pollOnce() {
      if (token !== TERM.pollToken || TERM.jobId !== jobId) return;
      TERM.pollInFlight = true;
      try {
        const r = await api("/api/exec/" + device + "/poll?job_id=" +
          encodeURIComponent(jobId) + "&offset=" + encodeURIComponent(TERM.offset));
        if (token !== TERM.pollToken || TERM.jobId !== jobId) return;
        if (!r.ok) throw new Error(r.error);
        if (r.reset) {
          $("#term-view").textContent = "";
          termAppend("[输出已截断，显示当前可用尾部]\n");
        }
        if (r.output) termAppend(r.output);
        TERM.offset = r.offset;
        if (!r.running) {
          TERM.pollTimer = null;
          $("#term-status").textContent =
            `完成 job ${r.job_id} · exit ${r.exit_code}${r.truncated ? " · 输出已截断(512KB)" : ""}`;
          TERM.jobId = null;
          TERM.jobDevice = null;
          return;
        }
      } catch (e) {
        TERM.pollTimer = null;
        if (token === TERM.pollToken) {
          $("#term-status").textContent = "错误: " + e.message;
        }
        return;
      } finally {
        if (token === TERM.pollToken) TERM.pollInFlight = false;
      }
      if (token === TERM.pollToken && TERM.jobId === jobId) {
        TERM.pollTimer = setTimeout(pollOnce, 500);
      }
    }

    pollOnce();
  }

  async function termKill() {
    if (!TERM.jobId) { $("#term-status").textContent = "当前无运行任务"; return; }
    try {
      const device = TERM.jobDevice || TERM.device;
      const r = await api("/api/exec/" + device + "/kill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: TERM.jobId }),
      });
      if (!r.ok) throw new Error(r.error);
      $("#term-status").textContent = "已发送终止 " + r.killed;
    } catch (e) {
      $("#term-status").textContent = "错误: " + e.message;
    }
  }

  function bind() {
    $("#term-device").onchange = (e) => { TERM.device = e.target.value; };
    $("#term-run").onclick = () => {
      const input = $("#term-cmd");
      const cmd = input.value.trim();
      if (!cmd) return;
      TERM.history.unshift(cmd);
      TERM.histIdx = -1;
      termRun(cmd);
    };
    $("#term-cmd").onkeydown = (e) => {
      if (e.key === "Enter") { $("#term-run").onclick(); return; }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        if (TERM.histIdx < TERM.history.length - 1) {
          TERM.histIdx++;
          $("#term-cmd").value = TERM.history[TERM.histIdx];
        }
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        if (TERM.histIdx > 0) {
          TERM.histIdx--;
          $("#term-cmd").value = TERM.history[TERM.histIdx];
        } else {
          TERM.histIdx = -1;
          $("#term-cmd").value = "";
        }
      }
    };
    $("#term-kill").onclick = termKill;
    $("#term-clear").onclick = () => { $("#term-view").textContent = ""; };
    document.querySelectorAll(".chip[data-cmd]").forEach(b =>
      b.onclick = () => {
        $("#term-cmd").value = b.dataset.cmd;
        termRun(b.dataset.cmd);
      });
    $("#term-view").onscroll = () => {
      const view = $("#term-view");
      TERM.follow = view.scrollTop + view.clientHeight >= view.scrollHeight - 10;
    };
  }

  R.pages = R.pages || {};
  R.pages.term = {
    render: renderTermPage,
    bind,
  };
})();
