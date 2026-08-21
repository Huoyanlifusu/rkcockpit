"""Utilities for host.api.handlers.agent."""
import json
import os
import secrets
import sys
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from host.agent import tools as agent_tools
from host.api.router import register
from host.core.http import read_json_body, send_json
from host.service.llm import (LLMAuth, LLMClient, LLMNotConfigured,
                              LLMRateLimit, LLMTimeout, LLMTokenLimit,
                              LLMUnreachable, LLM_CONCURRENCY_SEM,
                              estimate_tokens)



_SESSIONS = OrderedDict()          # id -> session dict（LRU，move_to_end）
_SESS_LOCK = threading.Lock()
MAX_SESSIONS = 32
MAX_MESSAGES = 200
TTL_SECONDS = 24 * 3600
MAX_TOOL_ITER = 8
DEFAULT_HISTORY_BUDGET = 12000


_LLM_CODES = {
    LLMNotConfigured: "not_configured",
    LLMUnreachable: "unreachable",
    LLMTimeout: "timeout",
    LLMAuth: "auth",
    LLMRateLimit: "rate_limit",
    LLMTokenLimit: "token_limit",
}


def _audit(host, handler, action, ok, target, detail=None, err=""):
    """Handle audit."""
    audit = getattr(host, "audit", None)
    if audit is None:
        return
    try:
        ip = handler.client_address[0]
    except Exception:
        ip = ""
    try:
        if ok:
            audit.record_ok(action, target, detail or {}, ip=ip)
        else:
            audit.record_fail(action, target, detail or {},
                              error=err or "", ip=ip)
    except Exception as exc:
        sys.stderr.write("[agent] audit 失败: %r\n" % exc)



_WRITE_ACTION = {
    "exec_run": "agent.exec.run",
    "exec_kill": "agent.exec.kill",
    "fs_write": "agent.fs.write",
    "fs_act": "agent.fs.act",
    "deploy_start": "agent.deploy.start",
    "process_signal": "agent.signal",
}


_AUDIT_PARAM_CAP = 200


def _audit_tool(host, handler, sess, tc, entry, result):
    """Handle audit tool."""
    if entry is None or entry.get("tier") != "write":
        return
    action = _WRITE_ACTION.get(tc["name"])
    if not action:
        return
    try:
        args = json.loads(tc.get("arguments") or "{}")
    except (ValueError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    params = dict(args)
    content = params.get("content")
    if isinstance(content, str) and len(content) > _AUDIT_PARAM_CAP:
        params["content"] = content[:_AUDIT_PARAM_CAP] + "…(截断)"
    ok = bool(result.get("ok", False)) if isinstance(result, dict) else False
    detail = {
        "session_id": sess["id"],
        "device_id": params.get("device_id") or sess.get("device_id") or "",
        "params": params,
        "result": "ok" if ok else "fail",
    }
    err = ""
    if not ok:
        if isinstance(result, dict):
            err = str(result.get("error") or "")[:300]
        else:
            err = str(result)[:300]
    target = {"kind": "agent", "id": sess["id"], "tool": tc["name"]}
    _audit(host, handler, action, ok, target, detail, err)




def _read_llm_conf(host):
    """Handle read llm conf."""
    try:
        conf_dir = host.store.conf_dir
    except AttributeError:
        return {}
    try:
        with open(os.path.join(conf_dir, "llm.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_llm_conf(host, conf):
    """Handle save llm conf."""
    conf_dir = host.store.conf_dir
    os.makedirs(conf_dir, exist_ok=True)
    path = os.path.join(conf_dir, "llm.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(conf, ensure_ascii=False, indent=2)
                 .encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_llm_conf(host):
    """Handle load llm conf."""
    conf = _read_llm_conf(host)
    try:
        timeout = int(conf.get("timeout") or 30)
    except (TypeError, ValueError):
        timeout = 30
    try:
        max_tokens = int(conf.get("max_tokens") or 1024)
    except (TypeError, ValueError):
        max_tokens = 1024
    client = LLMClient(base_url=conf.get("base_url") or "",
                       api_key=conf.get("api_key") or "",
                       model=conf.get("model") or "",
                       timeout=timeout, max_tokens=max_tokens)
    client.history_budget = conf.get("history_budget") or\
        DEFAULT_HISTORY_BUDGET
    try:

        client.history_budget = int(client.history_budget)
    except (TypeError, ValueError):
        client.history_budget = DEFAULT_HISTORY_BUDGET
    return client


def _mask_base_url(url):
    """Handle mask base url."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[1]
        out = "%s://%s%s" % (parts.scheme, netloc, parts.path or "")
    except Exception:
        out = url
    if len(out) > 48:
        return out[:24] + "…" + out[-16:]
    return out




def _public_session(sess):
    return {"id": sess["id"], "device_id": sess.get("device_id") or "",
            "created_ms": sess.get("created_ms"),
            "updated_ms": sess.get("updated_ms"),
            "message_count": sess.get("message_count", 0)}


def _touch(sess):
    sess["updated_ms"] = int(time.time() * 1000)
    sess["message_count"] = len(sess["messages"])
    with _SESS_LOCK:


        if sess["id"] in _SESSIONS:
            _SESSIONS.move_to_end(sess["id"])


def _sweep_sessions(now_ms=None):
    """Handle sweep sessions."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    with _SESS_LOCK:
        expired = [sid for sid, s in _SESSIONS.items()
                   if now_ms - s.get("updated_ms", 0) > TTL_SECONDS * 1000]
        for sid in expired:
            del _SESSIONS[sid]


def _drop_oldest_round(msgs):
    """Handle drop oldest round."""
    first_user = None
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            first_user = i
            break
    if first_user is None:
        return msgs[1:]
    end = len(msgs)
    for j in range(first_user + 1, len(msgs)):
        if msgs[j].get("role") == "user":
            end = j
            break
    return msgs[end:]


def _trim_session(sess):
    """Handle trim session."""
    msgs = sess["messages"]
    while len(msgs) > MAX_MESSAGES:
        dropped = _drop_oldest_round(msgs)
        if not dropped or len(dropped) >= len(msgs):
            break
        msgs[:] = dropped


def _trim_history(messages, budget):
    """Handle trim history."""
    msgs = list(messages)
    head = []
    while msgs and msgs[0].get("role") == "system":
        head.append(msgs.pop(0))
    while msgs and estimate_tokens(head + msgs) > budget:
        dropped = _drop_oldest_round(msgs)
        if not dropped or len(dropped) >= len(msgs):
            break
        msgs = dropped
    return head + msgs




def _build_system(host, device_id):
    """Handle build system."""
    parts = [
        "你是 RK 设备运维智能助手（rkss-web）。你可以使用工具查询设备清单、"
        "连通性、系统信息、诊断、实时监控与文件列表，也可以执行运维写操作"
        "（执行命令、写文件、文件操作、部署、进程信号）；所有写操作都会被"
        "审计留痕。回答使用中文，简洁准确，必要时引用工具返回的数据。",
        "安全规则：破坏性操作（删除文件、杀进程、重启、覆盖关键文件等）"
        "必须由用户明确要求才执行；不要自行扩大操作范围；不确定先询问。",
        "可用工具: %s。" % "、".join(sorted(agent_tools.TOOLS)),
    ]
    devs = []
    try:
        r = host.devices_list()
        if isinstance(r, dict):
            devs = r.get("devices") or []
    except Exception:
        devs = []
    summary = [{"id": d.get("id"), "name": d.get("name"),
                "type": d.get("type"), "state": d.get("state") or "unknown",
                "ping_ms": d.get("ping_ms"), "remark": d.get("remark")}
               for d in devs[:20]]
    parts.append("当前设备清单（前 20 台）: %s"
                 % json.dumps(summary, ensure_ascii=False))
    if device_id:
        try:
            r = host.device_sysinfo(device_id)
            if isinstance(r, tuple):
                r = r[0]
            data = r.get("data") if isinstance(r, dict) else None
            if isinstance(data, dict):
                data = {k: v for k, v in data.items() if v is not None}
                text = json.dumps(data, ensure_ascii=False)[:4000]
                parts.append("绑定设备 %s 系统信息: %s" % (device_id, text))
        except Exception:
            pass
    return "\n".join(parts)




def _sessions_create(handler, host, match, query):
    body = read_json_body(handler)
    if not isinstance(body, dict):
        body = {}
    device_id = (body.get("device_id") or "").strip()
    _sweep_sessions()
    with _SESS_LOCK:
        now = int(time.time() * 1000)
        sess = {"id": secrets.token_hex(8),
                "device_id": device_id or "local",
                "messages": [], "created_ms": now, "updated_ms": now,
                "message_count": 0}
        _SESSIONS[sess["id"]] = sess
        while len(_SESSIONS) > MAX_SESSIONS:
            _SESSIONS.popitem(last=False)
    return send_json(handler, 201, {"ok": True, "session": _public_session(sess)})


def _sessions_list(handler, host, match, query):
    _sweep_sessions()
    with _SESS_LOCK:
        items = [_public_session(s) for s in _SESSIONS.values()]
    items.sort(key=lambda s: s["updated_ms"] or 0, reverse=True)
    return send_json(handler, 200, {"ok": True, "sessions": items})


def _sessions_delete(handler, host, match, query):
    sid = match.group(1)
    with _SESS_LOCK:
        if sid not in _SESSIONS:
            return send_json(handler, 404,
                             {"ok": False, "error": "会话不存在: %s" % sid})
        del _SESSIONS[sid]
    return send_json(handler, 200, {"ok": True, "deleted": sid})


def _messages(handler, host, match, query):
    sid = match.group(1)
    with _SESS_LOCK:
        sess = _SESSIONS.get(sid)
        if sess is None:
            return send_json(handler, 404,
                             {"ok": False, "error": "会话不存在: %s" % sid})
        _SESSIONS.move_to_end(sid)
        msgs = [dict(m) for m in sess["messages"]]
    return send_json(handler, 200, {"ok": True, "messages": msgs})


def _tools(handler, host, match, query):
    return send_json(handler, 200,
                     {"ok": True, "tools": agent_tools.to_openai_tools("read")})


def _config(handler, host, match, query):
    conf = _read_llm_conf(host)
    client = LLMClient(base_url=conf.get("base_url") or "",
                       api_key=conf.get("api_key") or "",
                       model=conf.get("model") or "")

    return send_json(handler, 200, {
        "ok": True, "configured": client.configured,
        "model": client.model,
        "base_url_masked": _mask_base_url(client.base_url)})


def _chat(handler, host, match, query):
    sid = match.group(1)
    body = read_json_body(handler)
    if not isinstance(body, dict):
        return send_json(handler, 400,
                         {"ok": False, "error": "body 必须是 JSON 对象"})

    message = body.get("message")
    if not isinstance(message, str):
        return send_json(handler, 400,
                         {"ok": False, "error": "message 必须是字符串"})
    message = message.strip()
    if not message:
        return send_json(handler, 400, {"ok": False, "error": "message 必填"})
    with _SESS_LOCK:
        sess = _SESSIONS.get(sid)
        if sess is None:
            return send_json(handler, 404,
                             {"ok": False, "error": "会话不存在: %s" % sid})
        sess.setdefault("_lock", threading.Lock())
        sess_lock = sess["_lock"]


    sess_lock.acquire()
    try:
        return _chat_locked(handler, host, sess, message, body)
    finally:
        sess_lock.release()


def _chat_locked(handler, host, sess, message, body):
    """Handle chat locked."""
    _sweep_sessions()


    with _SESS_LOCK:
        if sess["id"] not in _SESSIONS:
            return send_json(handler, 404,
                             {"ok": False,
                              "error": "会话不存在: %s" % sess["id"]})

    did = body.get("device_id")
    if isinstance(did, str) and did.strip():
        sess["device_id"] = did.strip() or sess.get("device_id")
    sess["messages"].append({"role": "user", "content": message})
    _trim_session(sess)
    _touch(sess)

    try:
        llm = _load_llm_conf(host)
    except Exception as exc:
        return send_json(handler, 200,
                         {"ok": False, "code": "unreachable",
                          "error": "LLM 配置读取失败: %s" % str(exc)[:200]})
    tools = agent_tools.to_openai_tools() or None
    budget = getattr(llm, "history_budget", DEFAULT_HISTORY_BUDGET) or\
        DEFAULT_HISTORY_BUDGET
    tool_results = []

    for _ in range(MAX_TOOL_ITER):
        wire = [{"role": "system",
                 "content": _build_system(host, sess["device_id"])}]\
            + list(sess["messages"])
        wire = _trim_history(wire, budget)

        if not LLM_CONCURRENCY_SEM.acquire(blocking=False):
            return send_json(handler, 200,
                             {"ok": False, "code": "agent_busy",
                              "error": "LLM 并发已达上限（2），请稍后重试"})
        try:
            resp = llm.complete(wire, tools=tools)
        except tuple(_LLM_CODES) as exc:
            LLM_CONCURRENCY_SEM.release()
            return send_json(handler, 200,
                             {"ok": False, "code": _LLM_CODES[type(exc)],
                              "error": str(exc)})
        except Exception as exc:
            LLM_CONCURRENCY_SEM.release()
            return send_json(handler, 200,
                             {"ok": False, "code": "unreachable",
                              "error": "LLM 调用失败: %s" % str(exc)[:200]})
        LLM_CONCURRENCY_SEM.release()

        if resp["tool_calls"]:
            openai_tcs = [{
                "id": tc["id"], "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in resp["tool_calls"]]
            sess["messages"].append(
                {"role": "assistant", "content": resp["content"],
                 "tool_calls": openai_tcs})
            for tc in resp["tool_calls"]:
                entry = agent_tools.TOOLS.get(tc["name"])
                result = agent_tools.run_tool_call(host, tc)
                tool_results.append({"name": tc["name"],
                                     "arguments": tc["arguments"],
                                     "result": result})

                _audit_tool(host, handler, sess, tc, entry, result)
                sess["messages"].append(
                    {"role": "tool", "tool_call_id": tc["id"],
                     "content": json.dumps(result, ensure_ascii=False,
                                           default=str)})
            _trim_session(sess)
            _touch(sess)
            continue

        sess["messages"].append({"role": "assistant",
                                 "content": resp["content"]})
        _trim_session(sess)
        _touch(sess)
        return send_json(handler, 200, {
            "ok": True, "reply": resp["content"],
            "tool_calls": tool_results, "session_id": sess["id"],
            "message_count": len(sess["messages"])})


    reply = ("已达 %d 轮工具调用上限仍无法给出最终回答；"
             "请简化问题或补充信息。" % MAX_TOOL_ITER)
    sess["messages"].append({"role": "assistant", "content": reply})
    _trim_session(sess)
    _touch(sess)
    return send_json(handler, 200, {
        "ok": True, "reply": reply, "tool_calls": tool_results,
        "session_id": sess["id"], "message_count": len(sess["messages"])})


register("POST", r"^/api/agent/sessions$", _sessions_create, "agent")
register("GET", r"^/api/agent/sessions$", _sessions_list, "agent")
register("DELETE", r"^/api/agent/sessions/([^/]+)$", _sessions_delete, "agent")
register("POST", r"^/api/agent/sessions/([^/]+)/chat$", _chat, "agent")
register("GET", r"^/api/agent/sessions/([^/]+)/messages$", _messages, "agent")
register("GET", r"^/api/agent/tools$", _tools, "agent")
register("GET", r"^/api/agent/config$", _config, "agent")
