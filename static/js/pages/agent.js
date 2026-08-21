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

  let current = null;
  let sessions = [];
  let busy = false;
  let streamMode = true;

  function ensureRoot() {
    return $("#tab-agent") !== null;
  }

  function setErr(msg) {
    hostError($("#agent-error"), msg);
  }

  function autoScroll() {
    const view = $("#agent-messages");
    if (view) view.scrollTop = view.scrollHeight;
  }



  async function loadSessions() {
    try {
      const r = await api("/api/agent/sessions");
      sessions = r.sessions || [];
    } catch (e) {
      setErr("会话列表: " + e.message);
      sessions = [];
    }
    renderSessions();
    if (current && !sessions.some(s => s.id === current)) current = null;
    const title = $("#agent-title");
    if (title) title.textContent = current ? "会话 " + current : "未选择会话";
  }

  function renderSessions() {
    const box = $("#agent-sessions");
    if (!box) return;
    box.innerHTML = "";
    const count = $("#agent-sess-count");
    if (count) count.textContent = sessions.length ? "（" + sessions.length + "）" : "";
    if (!sessions.length) {
      box.append(el("div", "empty", "暂无会话，点「新建会话」开始"));
      return;
    }
    for (const s of sessions) {
      const item = el("div");
      item.style.cssText = "padding:8px 10px;border-bottom:1px solid var(--border);" +
        "cursor:pointer;" +
        (s.id === current ? "background:var(--bg3);" : "");
      const head = el("div");
      head.style.cssText = "display:flex;justify-content:space-between;gap:8px;";
      head.append(el("b", "", s.device_id || "-"));
      head.append(el("span", "muted",
        s.message_count + " 条 · " + fmtTime(s.updated_ms)));
      const del = el("button", "btn", "删除");
      del.type = "button";
      del.style.cssText = "padding:2px 8px;font-size:12px;";
      del.onclick = (e) => { e.stopPropagation(); delSession(s.id); };
      item.append(head, del);
      item.onclick = () => selectSession(s.id);
      box.append(item);
    }
  }

  async function newSession() {
    const did = $("#agent-device").value || "local";
    try {
      const r = await api("/api/agent/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: did }),
      });
      if (!r.ok) throw new Error(r.error || "新建会话失败");
      current = r.session.id;
      await Promise.all([loadSessions(), loadMessages()]);
    } catch (e) {
      setErr("新建会话: " + e.message);
    }
  }

  async function delSession(sid) {
    if (!confirm("确认删除会话？消息将丢失。")) return;
    try {
      const r = await api("/api/agent/sessions/" + sid, { method: "DELETE" });
      if (!r.ok) throw new Error(r.error || "删除失败");
      if (current === sid) { current = null; $("#agent-messages").innerHTML = ""; }
      await loadSessions();
    } catch (e) {
      setErr("删除会话: " + e.message);
    }
  }

  async function selectSession(sid) {
    current = sid;
    await Promise.all([loadSessions(), loadMessages()]);
  }



  function bubbleDiv(cls, cssText) {
    const b = el("div", cls);
    b.style.cssText = "max-width:78%;padding:8px 12px;border-radius:10px;" +
      "margin:6px 0;white-space:pre-wrap;word-break:break-all;font-size:13px;" +
      "line-height:1.5;" + (cssText || "");
    return b;
  }

  async function loadMessages() {
    const view = $("#agent-messages");
    view.innerHTML = "";
    if (!current) return;
    try {
      const r = await api("/api/agent/sessions/" + current + "/messages");
      if (!r.ok) throw new Error(r.error || "读取消息失败");
      for (const m of r.messages || []) appendMessage(view, m);
      view.scrollTop = view.scrollHeight;
    } catch (e) {
      setErr("读取消息: " + e.message);
    }
  }

  function appendMessage(view, m) {
    const role = m.role || "";
    if (role === "user") {
      const b = bubbleDiv("", "margin-left:auto;background:var(--accent);" +
        "color:#fff;");
      b.textContent = m.content;
      view.append(b);
      return;
    }
    if (role === "assistant") {
      const b = bubbleDiv("", "background:var(--bg3);");
      b.append(el("div", "", m.content || ""));
      const tcs = m.tool_calls || [];
      if (tcs.length) {
        const det = el("details");
        det.style.cssText = "margin-top:6px;";
        det.append(el("summary", "muted", "工具调用 " + tcs.length + " 次"));
        for (const tc of tcs) {
          const fn = tc.function || {};
          const block = el("div");
          block.style.cssText = "margin:4px 0;font-size:12px;";
          block.append(el("div", "", "◆ " + (fn.name || "?")));
          block.append(el("div", "muted", "参数: " + (fn.arguments || "{}")));
          det.append(block);
        }
        b.append(det);
      }
      view.append(b);
      return;
    }
    if (role === "tool") {
      const b = bubbleDiv("", "background:var(--bg3);opacity:.85;");
      b.append(el("div", "muted", "工具结果 #" + (m.tool_call_id || "?")));
      b.append(el("div", "", m.content || ""));
      view.append(b);
      return;
    }
    view.append(bubbleDiv("", "background:var(--bg3);", JSON.stringify(m)));
  }



  async function sendChatNonStream(text) {
    const r = await api("/api/agent/sessions/" + current + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        device_id: $("#agent-device").value || "local",
      }),
    });
    if (!r.ok) throw new Error(r.error || "chat 失败");
    const view = $("#agent-messages");
    const ab = bubbleDiv("", "background:var(--bg3);");
    ab.append(el("div", "", r.reply || ""));
    const tcs = r.tool_calls || [];
    if (tcs.length) {
      const det = el("details");
      det.style.cssText = "margin-top:6px;";
      det.append(el("summary", "muted",
        "本轮工具调用 " + tcs.length + " 次（点开查看）"));
      for (const tc of tcs) {
        const block = el("div");
        block.style.cssText = "margin:4px 0;font-size:12px;";
        block.append(el("div", "", "◆ " + tc.name));
        block.append(el("div", "muted", "参数: " + (tc.arguments || "{}")));
        const res = tc.result || {};
        const resText = res.truncated
          ? (res.preview || "") + "…（截断）"
          : JSON.stringify(res, null, 1);
        block.append(el("div", "", "结果: " + resText));
        det.append(block);
      }
      ab.append(det);
    }
    view.append(ab);
    autoScroll();
  }




  function makeStepCard(tc) {
    const det = el("details");
    det.style.cssText = "margin:6px 0;border:1px solid var(--border);" +
      "border-radius:6px;background:var(--bg1);";
    const sum = el("summary");
    sum.style.cssText = "padding:6px 10px;cursor:pointer;font-size:12px;";
    sum.textContent = "🔧 " + (tc.name || "?") +
      " — 参数: " + (tc.arguments || "{}");
    const body = el("div");
    body.style.cssText = "padding:6px 10px;font-size:12px;";
    body.append(el("div", "muted", "执行中…"));
    det.append(sum, body);
    return { det, body };
  }

  function fillStepResult(step, result) {
    step.body.innerHTML = "";
    const res = result || {};
    let text = "";
    if (res.truncated) {
      text = (res.preview || "") + "…（截断）";
    } else {
      try { text = JSON.stringify(res, null, 1); }
      catch (e) { text = String(res); }
    }
    step.body.append(el("div", "", "结果: " + text));
  }


  function ensureStreamBubble(view, ctx) {
    if (ctx.bubble) return;
    const b = bubbleDiv("", "background:var(--bg3);");
    const textEl = el("div", "", "");
    const stepsBox = el("div", "", "");
    b.append(textEl, stepsBox);
    ctx.bubble = b;
    ctx.textEl = textEl;
    ctx.stepsBox = stepsBox;
    view.append(b);
  }


  function handleStreamFrame(frame, view, ctx) {
    let event = "message";
    let data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) return true;
    let payload;
    try { payload = JSON.parse(data); } catch (e) { return true; }

    if (event === "token") {
      ensureStreamBubble(view, ctx);
      ctx.textEl.textContent += payload.text || "";
      autoScroll();
      return true;
    }
    if (event === "tool_call") {
      ensureStreamBubble(view, ctx);
      const step = makeStepCard(payload);
      ctx.steps.push({ id: payload.id, card: step });
      ctx.stepsBox.append(step.det);
      autoScroll();
      return true;
    }
    if (event === "tool_result") {
      const step = ctx.steps.find(s => s.id === payload.id);
      if (step) fillStepResult(step, payload.result);
      autoScroll();
      return true;
    }
    if (event === "done") {
      ctx.done = true;
      if (ctx.textEl) ctx.textEl.textContent = payload.reply || ctx.textEl.textContent;
      return true;
    }
    if (event === "error") {
      ctx.errorMsg = payload.error || ("code=" + (payload.code || "?"));
      ctx.errorCode = payload.code || "";
      return false;
    }
    return true;
  }


  async function sendChatStream(text) {
    const view = $("#agent-messages");
    const ctx = { bubble: null, textEl: null, stepsBox: null, steps: [],
                  done: false, errorMsg: null, errorCode: "" };


    function cleanupEmptyBubble() {
      if (ctx.bubble && !(ctx.textEl && ctx.textEl.textContent) &&
          !(ctx.stepsBox && ctx.stepsBox.children.length)) {
        ctx.bubble.remove();
      }
    }

    let resp;
    try {
      resp = await fetch("/api/agent/sessions/" + current + "/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          device_id: $("#agent-device").value || "local",
        }),
      });
    } catch (e) {
      throw new Error("网络错误: " + e.message);
    }
    if (!resp.ok) {
      if (resp.status === 404) return true;
      let data = null;
      try { data = await resp.json(); } catch (e) { }
      if (resp.status === 401 && R.auth) {
        R.auth.requireLogin((data && data.error) || "需要管理员登录");
      }
      throw new Error((data && data.error) || ("HTTP " + resp.status));
    }
    if (!resp.body) throw new Error("浏览器不支持流式读取");
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (!handleStreamFrame(frame, view, ctx)) {
            await reader.cancel().catch(() => {});
            cleanupEmptyBubble();
            throw new Error(ctx.errorMsg || "流式错误");
          }
        }
      }
      if (buf.trim()) handleStreamFrame(buf, view, ctx);
    } finally {
      reader.releaseLock();
    }
    if (ctx.errorMsg) {
      cleanupEmptyBubble();
      throw new Error(ctx.errorMsg);
    }
    return false;
  }

  async function sendChat() {
    if (busy) return;
    if (!current) {
      setErr("请先新建/选择会话");
      return;
    }
    const input = $("#agent-msg");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    const view = $("#agent-messages");
    const ub = bubbleDiv("", "margin-left:auto;background:var(--accent);color:#fff;");
    ub.textContent = text;
    view.append(ub);
    autoScroll();
    busy = true;
    const sendBtn = $("#agent-send");
    if (sendBtn) sendBtn.disabled = true;
    try {
      if (streamMode) {
        const fellBack = await sendChatStream(text);
        if (fellBack) await sendChatNonStream(text);
      } else {
        await sendChatNonStream(text);
      }
      await loadSessions();
    } catch (e) {

      setErr("chat: " + e.message);
    } finally {
      busy = false;
      if (sendBtn) sendBtn.disabled = false;
    }
  }




  function ensureStreamToggle() {
    if (document.getElementById("agent-stream-toggle")) return;
    const tb = document.querySelector("#tab-agent .toolbar");
    if (!tb) return;
    const lab = el("label", "", "");
    lab.style.cssText = "display:inline-flex;align-items:center;gap:4px;" +
      "margin-left:10px;font-size:12px;cursor:pointer;";
    const cb = el("input");
    cb.type = "checkbox";
    cb.id = "agent-stream-toggle";
    cb.checked = true;
    lab.append(cb, el("span", "", "流式"));
    tb.append(lab);
  }

  async function render() {
    if (!ensureRoot()) return;
    const errBox = $("#agent-error");
    errBox.innerHTML = "";
    try {
      await loadDevices();
      deviceOptions($("#agent-device"), true, true);
      ensureStreamToggle();
      const tgl = $("#agent-stream-toggle");
      if (tgl) {
        streamMode = tgl.checked;
        tgl.onchange = () => { streamMode = tgl.checked; };
      }
      const cfg = await api("/api/agent/config");
      const banner = $("#agent-banner");
      if (cfg && cfg.configured === false) {
        banner.style.display = "block";
        banner.textContent = "未配置 LLM：请在配置目录 llm.json 填写 base_url 与 model（api_key 可选）";
      } else if (banner) {
        banner.style.display = "none";
      }
      const toolsBox = $("#agent-tools");
      if (toolsBox) {
        try {
          const t = await api("/api/agent/tools");
          toolsBox.textContent = "只读工具: " +
            (t.tools || []).map(x => (x.function || {}).name).join(" / ");
        } catch (e) {
          toolsBox.textContent = "";
        }
      }
    } catch (e) {
      hostError(errBox, e.message);
    }
    await loadSessions();
  }

  function bind() {
    if (!ensureRoot()) return;
    $("#agent-new").onclick = newSession;
    $("#agent-refresh").onclick = render;
    $("#agent-send").onclick = sendChat;
    $("#agent-clear").onclick = () => { $("#agent-messages").innerHTML = ""; };
    $("#agent-msg").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });
  }

  function deactivate() {

  }

  R.pages = R.pages || {};
  R.pages.agent = { render, bind, deactivate };
})();
