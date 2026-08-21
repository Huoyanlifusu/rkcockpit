"""Utilities for host.api.handlers.logcenter."""
import select
import socket
import threading
import time

from host.api.router import register
from host.core.http import send_json, read_json_body
from host.service.logcenter import LogCenter
from portal.sse import IDLE_TIMEOUT_S, SseConn, HUB

_SVC = None
_SVC_LOCK = threading.Lock()


def _svc(host):
    global _SVC
    with _SVC_LOCK:
        svc = getattr(host, "_logcenter_service", None)
        if svc is None:
            svc = LogCenter(host)
            host._logcenter_service = svc
        _SVC = svc
    register_cleanup = getattr(host, "register_cleanup", None)
    if register_cleanup is not None:
        register_cleanup(svc.remove_device, svc.close)
    return svc


def _audit(host, handler, action, target, ok, detail=None, err=""):
    try:
        host.audit.record({
            "action": action, "target": target or {},
            "detail": dict(detail or {}), "result": "ok" if ok else "fail",
            "err": err or "", "ip": handler.client_address[0] or "",
        })
    except Exception:
        pass


def _q(query, key, default=None):
    v = query.get(key)
    return v[0] if v else default


def _check_device(handler, host, did):
    try:
        host._device(did)
        return True
    except KeyError:
        send_json(handler, 404, {"ok": False, "error": "设备不存在: %s" % did})
        return False


def _transport(handler, host, did):
    if not _check_device(handler, host, did):
        return None
    try:
        return host._transport(did)
    except Exception as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return None


def _background_transport(handler, host, did):
    if not _check_device(handler, host, did):
        return None
    try:
        try:
            return host._transport(did, workload="background")
        except TypeError as exc:
            if "unexpected keyword argument 'workload'" not in str(exc):
                raise
            return host._transport(did)
    except Exception as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
        return None


def _sources(handler, host, match, query):
    did = match.group(1)
    t = _transport(handler, host, did)
    if t is None:
        return True
    return send_json(handler, 200,
                     {"ok": True, "sources": _svc(host).sources(t)})


def _tail(handler, host, match, query):
    did = match.group(1)
    t = _transport(handler, host, did)
    if t is None:
        return True
    source = _q(query, "source") or ""
    if not source:
        source = _svc(host).default_source(t) or ""
    try:
        data = _svc(host).tail(t, source, lines=_q(query, "lines", "200"),
                               filter=_q(query, "filter"))
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    _audit(host, handler, "logcenter.tail", {"kind": "device", "id": did},
           True, {"source": source, "lines": len(data["lines"])})
    return send_json(handler, 200, data)


def _follow(handler, host, match, query):
    did = match.group(1)
    t = _background_transport(handler, host, did)
    if t is None:
        return True
    body = read_json_body(handler)
    source = (body.get("source") or "").strip()
    try:
        f = _svc(host).follow(did, t, source, body.get("filter"))
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    _audit(host, handler, "logcenter.follow", {"kind": "device", "id": did},
           True, {"source": f.source, "filter": f.pattern})
    return send_json(handler, 200, {"ok": True, "follow": f.info()})


def _unfollow(handler, host, match, query):
    did = match.group(1)
    if not _check_device(handler, host, did):
        return True
    _svc(host).unfollow(did)
    _audit(host, handler, "logcenter.unfollow", {"kind": "device", "id": did},
           True, None)
    return send_json(handler, 200, {"ok": True})


def _running(handler, host, match, query):
    return send_json(handler, 200,
                     {"ok": True, "running": _svc(host).running()})


def _stream(handler, host, match, query):
    """Handle stream."""
    did = match.group(1)
    if not _check_device(handler, host, did):
        return True
    svc = _svc(host)
    try:
        t = _background_transport(handler, host, did)
        if t is None:
            return True
        f = svc.follow(did, t, _q(query, "source") or "",
                       _q(query, "filter"))
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    except Exception as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    conn = SseConn(handler.wfile, handler.connection)
    if not HUB.add(conn, kind="logcenter"):
        return send_json(handler, 503, {
            "ok": False, "error": "logcenter SSE 连接数已达上限(8)，请稍后再试"})
    try:
        handler.protocol_version = "HTTP/1.1"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        handler.wfile.flush()
        sock = handler.connection
        seq, rows = f.buffer.since(0)
        backlog = rows[-200:]
        if not conn.write({"backlog": len(backlog)}, event="reconnect"):
            return True
        for _i, ts, text in backlog:
            if not conn.write({"t": int(ts), "text": text}, event="line"):
                return True
        ok = True
        while ok and time.time() - conn.created < IDLE_TIMEOUT_S:
            if select.select([sock], [], [], 0)[0]:
                try:
                    if sock.recv(1, socket.MSG_PEEK) == b"":
                        break
                except OSError:
                    break
            seq, rows = f.buffer.since(seq)
            for _i, ts, text in rows:
                if not conn.write({"t": int(ts), "text": text}, event="line"):
                    ok = False
                    break
            if ok and not conn.ping_if_stale():
                ok = False
            if ok:
                time.sleep(0.5)
    finally:
        HUB.remove(conn)
    return True


register("GET", r"^/api/logcenter/running$", _running, "logcenter")
register("GET", r"^/api/logcenter/([^/]+)/sources$", _sources, "logcenter")
register("GET", r"^/api/logcenter/([^/]+)/tail$", _tail, "logcenter")
register("POST", r"^/api/logcenter/([^/]+)/follow$", _follow, "logcenter")
register("POST", r"^/api/logcenter/([^/]+)/unfollow$", _unfollow, "logcenter")
register("GET", r"^/api/logcenter/([^/]+)/stream$", _stream, "logcenter")
