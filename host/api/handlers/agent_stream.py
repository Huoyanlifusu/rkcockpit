"""Utilities for host.api.handlers.agent_stream."""
import json
import select
import socket
import threading
import time

from host.agent import tools as agent_tools
from host.api.handlers import agent as agent_handler
from host.api.router import register
from host.core.http import read_json_body, send_json
from host.service.llm import LLM_CONCURRENCY_SEM, MAX_LLM_CONCURRENCY
from portal.sse import HUB, IDLE_TIMEOUT_S, SseConn


def _stream(handler, host, match, query):
    """Handle stream."""
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
    with agent_handler._SESS_LOCK:
        sess = agent_handler._SESSIONS.get(sid)
        if sess is None:
            return send_json(handler, 404,
                             {"ok": False, "error": "会话不存在: %s" % sid})
        sess.setdefault("_lock", threading.Lock())
        sess_lock = sess["_lock"]


    sess_lock.acquire()
    try:
        return _stream_locked(handler, host, sess, message, body)
    finally:
        sess_lock.release()


def _stream_locked(handler, host, sess, message, body):
    """Handle stream locked."""
    agent_handler._sweep_sessions()


    with agent_handler._SESS_LOCK:
        if sess["id"] not in agent_handler._SESSIONS:
            return send_json(handler, 404,
                             {"ok": False,
                              "error": "会话不存在: %s" % sess["id"]})

    did = body.get("device_id")
    if isinstance(did, str) and did.strip():
        sess["device_id"] = did.strip() or sess.get("device_id")
    sess["messages"].append({"role": "user", "content": message})
    agent_handler._trim_session(sess)
    agent_handler._touch(sess)

    conn = SseConn(handler.wfile, getattr(handler, "connection", None))
    if not HUB.add(conn, kind="agent"):
        return send_json(handler, 503, {
            "ok": False, "error": "agent SSE 连接数已达上限(4)，请稍后再试"})
    try:
        handler.protocol_version = "HTTP/1.1"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        handler.wfile.flush()
    except Exception:
        HUB.remove(conn)
        return True

    done = threading.Event()
    worker = threading.Thread(
        target=_worker, args=(conn, done, handler, host, sess),
        name="agent-stream-%s" % sess["id"], daemon=True)
    worker.start()
    try:
        _pump(conn, getattr(handler, "connection", None), done)
    finally:
        done.set()
        worker.join(timeout=10)
        HUB.remove(conn)
    return True


def _pump(conn, sock, done):
    """Handle pump."""
    while not done.is_set():
        if time.time() - conn.created >= IDLE_TIMEOUT_S:
            break
        try:
            r, _, _ = select.select([sock], [], [], 0.2)
            if r:
                try:
                    if sock.recv(1, socket.MSG_PEEK) == b"":
                        break
                except OSError:
                    break
        except (TypeError, ValueError, OSError):
            time.sleep(0.2)
        if not conn.ping_if_stale():
            break


def _worker(conn, done, handler, host, sess):
    """Handle worker."""
    try:
        _run_loop(conn, handler, host, sess)
    except Exception as exc:
        try:
            conn.write({"code": "unreachable",
                        "error": "流式处理异常: %s" % str(exc)[:200]},
                       event="error")
        except Exception:
            pass
    finally:
        done.set()


def _run_loop(conn, handler, host, sess):
    """Handle run loop."""
    if not conn.write({
            "session_id": sess["id"],
            "device_id": sess.get("device_id") or "",
            "message_count": len(sess["messages"])}, event="session"):
        return
    try:
        llm = agent_handler._load_llm_conf(host)
    except Exception as exc:
        conn.write({"code": "unreachable",
                    "error": "LLM 配置读取失败: %s" % str(exc)[:200]},
                   event="error")
        return
    tools = agent_tools.to_openai_tools() or None
    budget = getattr(llm, "history_budget",
                     agent_handler.DEFAULT_HISTORY_BUDGET) or\
        agent_handler.DEFAULT_HISTORY_BUDGET
    tool_results = []

    for _ in range(agent_handler.MAX_TOOL_ITER):
        wire = [{"role": "system",
                 "content": agent_handler._build_system(
                     host, sess["device_id"])}] + list(sess["messages"])
        wire = agent_handler._trim_history(wire, budget)
        content, tool_calls = _llm_stream(conn, llm, wire, tools)
        if content is None:
            return

        if tool_calls:
            openai_tcs = [{
                "id": tc["id"], "type": "function",
                "function": {"name": tc["name"],
                             "arguments": tc["arguments"]}}
                for tc in tool_calls]
            sess["messages"].append(
                {"role": "assistant", "content": content,
                 "tool_calls": openai_tcs})
            for tc in tool_calls:
                if not conn.write({"id": tc["id"], "name": tc["name"],
                                   "arguments": tc["arguments"]},
                                  event="tool_call"):
                    return
                entry = agent_tools.TOOLS.get(tc["name"])
                result = agent_tools.run_tool_call(host, tc)
                tool_results.append({"name": tc["name"],
                                     "arguments": tc["arguments"],
                                     "result": result})

                agent_handler._audit_tool(host, handler, sess, tc, entry,
                                          result)
                if not conn.write({"id": tc["id"], "name": tc["name"],
                                   "result": result}, event="tool_result"):
                    return
                sess["messages"].append(
                    {"role": "tool", "tool_call_id": tc["id"],
                     "content": json.dumps(result, ensure_ascii=False,
                                           default=str)})
            agent_handler._trim_session(sess)
            agent_handler._touch(sess)
            continue

        sess["messages"].append({"role": "assistant", "content": content})
        agent_handler._trim_session(sess)
        agent_handler._touch(sess)
        conn.write({"reply": content, "session_id": sess["id"],
                    "message_count": len(sess["messages"])}, event="done")
        return


    reply = ("已达 %d 轮工具调用上限仍无法给出最终回答；"
             "请简化问题或补充信息。" % agent_handler.MAX_TOOL_ITER)
    sess["messages"].append({"role": "assistant", "content": reply})
    agent_handler._trim_session(sess)
    agent_handler._touch(sess)
    conn.write({"reply": reply, "session_id": sess["id"],
                "message_count": len(sess["messages"])}, event="done")


def _llm_stream(conn, llm, wire, tools):
    """Handle llm stream."""
    if not LLM_CONCURRENCY_SEM.acquire(blocking=False):
        conn.write({"code": "agent_busy",
                    "error": "LLM 并发调用已达上限(%d)，请稍后重试"
                             % MAX_LLM_CONCURRENCY}, event="error")
        return None, None
    try:
        content = ""
        tool_calls = []
        try:
            for ev in llm.complete_stream(wire, tools=tools):
                if ev["type"] == "token":
                    content += ev["text"]
                    if not conn.write({"text": ev["text"]}, event="token"):
                        return None, None
                elif ev["type"] == "tool_calls":
                    tool_calls = ev["tool_calls"]

        except tuple(agent_handler._LLM_CODES) as exc:
            conn.write({"code": agent_handler._LLM_CODES[type(exc)],
                        "error": str(exc)}, event="error")
            return None, None
        except Exception as exc:
            conn.write({"code": "unreachable",
                        "error": "LLM 调用失败: %s" % str(exc)[:200]},
                       event="error")
            return None, None
        return content, tool_calls
    finally:
        LLM_CONCURRENCY_SEM.release()


register("POST", r"^/api/agent/sessions/([^/]+)/stream$", _stream, "agent")
