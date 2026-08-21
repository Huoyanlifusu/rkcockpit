"""Utilities for host.api.handlers.monitor."""
import select
import socket
import threading
import time

from host.api.router import register
from host.core.http import send_json
from host.service.monitor import (METRIC_FIELDS, MonitorService,
                                  VALID_WINDOWS)
from portal.sse import IDLE_TIMEOUT_S, SseConn, HUB

_CAP = "monitor"
_SVC = MonitorService()
_SVC_LOCK = threading.Lock()


def _svc(host):
    global _SVC
    with _SVC_LOCK:
        svc = getattr(host, "_monitor_service", None)
        if svc is None:
            svc = MonitorService()
            host._monitor_service = svc
        # Compatibility for host.agent.tools, which imports this symbol lazily.
        _SVC = svc
    register_cleanup = getattr(host, "register_cleanup", None)
    if register_cleanup is not None:
        register_cleanup(svc.remove_device, svc.close)
    return svc


def _background_transport(host, did):
    """Use Stage2 scheduler classification, with pre-Stage2 HostApi fallback."""
    try:
        return host._transport(did, workload="background")
    except TypeError as exc:
        if "unexpected keyword argument 'workload'" not in str(exc):
            raise
        return host._transport(did)


def _check_device(handler, host, did):
    try:
        host._device(did)
    except KeyError:
        send_json(handler, 404, {"ok": False, "error": "设备不存在: %s" % did})
        return False
    return True


def _enable(handler, host, match, query):
    did = match.group(1)
    if not _check_device(handler, host, did):
        return True
    try:
        _svc(host).enable(lambda: _background_transport(host, did), did)
        return send_json(handler, 200, {"ok": True})
    except Exception as exc:
        return send_json(handler, 200, {"ok": False, "error": str(exc)})


def _disable(handler, host, match, query):
    did = match.group(1)
    if not _check_device(handler, host, did):
        return True
    try:
        _svc(host).disable(did)
        return send_json(handler, 200, {"ok": True})
    except Exception as exc:
        return send_json(handler, 200, {"ok": False, "error": str(exc)})


def _now(handler, host, match, query):
    did = match.group(1)
    if not _check_device(handler, host, did):
        return True
    try:
        _svc(host).get_or_start(
            lambda: _background_transport(host, did), did)
        sample = _svc(host).now(did)
        return send_json(handler, 200, {"ok": True, "sample": sample})
    except Exception as exc:
        return send_json(handler, 200, {"ok": False, "error": str(exc)})


def _series(handler, host, match, query):
    did = match.group(1)
    if not _check_device(handler, host, did):
        return True
    metric = (query.get("metric") or [None])[0]
    raw_window = (query.get("window") or ["60"])[0]
    if metric is not None and metric not in METRIC_FIELDS:
        return send_json(handler, 400,
                         {"ok": False, "error": "未知 metric: %s" % metric})
    try:
        window = int(raw_window)
    except (TypeError, ValueError):
        return send_json(handler, 400,
                         {"ok": False, "error": "window 非法: %s" % raw_window})
    if window not in VALID_WINDOWS:
        return send_json(handler, 400,
                         {"ok": False,
                          "error": "window 必须是 60/180/300/600"})
    try:
        samples = _svc(host).series(did, metric=metric, window=window)
        return send_json(handler, 200, {"ok": True, "samples": samples})
    except Exception as exc:
        return send_json(handler, 200, {"ok": False, "error": str(exc)})


def _frame(sample):
    """Handle frame."""
    f = {"t": sample["ts"]}
    cpu = sample.get("cpu") or {}
    if cpu.get("usage") is not None:
        f["c"] = cpu["usage"]
    mem = sample.get("mem") or {}
    if mem.get("used_mb") is not None:
        f["m"] = {"u": mem["used_mb"]}
    temp = sample.get("temp") or {}
    if temp:
        f["T"] = next(iter(temp.values()))
    load = sample.get("load") or []
    if load and load[0] is not None:
        f["l"] = load[0]
    return f


def _stream(handler, host, match, query):
    """Handle stream."""
    did = match.group(1)
    if not _check_device(handler, host, did):
        return True
    conn = SseConn(handler.wfile, handler.connection)
    if not HUB.add(conn, kind="monitor"):
        return send_json(handler, 503, {
            "ok": False, "error": "monitor SSE 连接数已达上限(8)，请稍后再试"})
    svc = _svc(host)
    try:
        svc.get_or_start(lambda: _background_transport(host, did), did)
    except Exception as exc:
        HUB.remove(conn)
        return send_json(handler, 200, {"ok": False, "error": str(exc)})
    try:
        handler.protocol_version = "HTTP/1.1"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        handler.wfile.flush()
        sock = handler.connection
        last_ts = 0
        while True:
            if time.time() - conn.created >= IDLE_TIMEOUT_S:
                break

            r, _, _ = select.select([sock], [], [], 0.2)
            if r:
                try:
                    if sock.recv(1, socket.MSG_PEEK) == b"":
                        break
                except OSError:
                    break
            sample = svc.latest(did)
            if sample is not None and sample["ts"] != last_ts:
                last_ts = sample["ts"]
                gap = bool(sample.get("gap"))
                frame = {"t": sample["ts"]} if gap else _frame(sample)
                if not conn.write(frame, event="gap" if gap else None):
                    break
            elif not conn.ping_if_stale():
                break
    finally:
        HUB.remove(conn)
    return True


register("POST", r"^/api/monitor/([^/]+)/enable$", _enable, _CAP)
register("POST", r"^/api/monitor/([^/]+)/disable$", _disable, _CAP)
register("GET", r"^/api/monitor/([^/]+)/now$", _now, _CAP)
register("GET", r"^/api/monitor/([^/]+)/series$", _series, _CAP)
register("GET", r"^/api/monitor/([^/]+)/stream$", _stream, _CAP)
