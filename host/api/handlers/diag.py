"""Utilities for host.api.handlers.diag."""
import sys

from host.api.router import register
from host.core.http import read_json_body, send_json
from host.service import diag as diag_svc


def _transport(host, did, handler):
    """Handle transport."""
    try:
        return host._transport(did)
    except KeyError:
        send_json(handler, 404, {"ok": False, "error": "设备不存在: %s" % did})
    except Exception as exc:
        send_json(handler, 400, {"ok": False, "error": str(exc)})
    return None


def _q(query, key, default=None):
    v = query.get(key)
    return v[0] if v else default


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
        sys.stderr.write("[diag] audit 失败: %r\n" % exc)


def diag_video(handler, host, match, query):
    t = _transport(host, match.group(1), handler)
    if t is None:
        return True
    try:
        data = diag_svc.video(t, device_id=match.group(1))
    except diag_svc.DiagError as exc:
        return send_json(handler, 200, {"ok": False, "error": str(exc)})
    return send_json(handler, 200, data)


def diag_usb(handler, host, match, query):
    t = _transport(host, match.group(1), handler)
    if t is None:
        return True
    try:
        data = diag_svc.usb(t, device_id=match.group(1))
    except diag_svc.DiagError as exc:
        return send_json(handler, 200, {"ok": False, "error": str(exc)})
    return send_json(handler, 200, data)


def diag_dmesg(handler, host, match, query):
    t = _transport(host, match.group(1), handler)
    if t is None:
        return True
    try:
        data = diag_svc.dmesg(t, lines=_q(query, "lines", "200"),
                              filter=_q(query, "filter"),
                              device_id=match.group(1))
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    except diag_svc.DiagError as exc:
        return send_json(handler, 200, {"ok": False, "error": str(exc)})
    return send_json(handler, 200, data)


def stream_test(handler, host, match, query):
    """Handle stream test."""
    t = _transport(host, match.group(1), handler)
    if t is None:
        return True
    body = read_json_body(handler)
    if not isinstance(body, dict):
        return send_json(handler, 400,
                         {"ok": False, "error": "body 必须是 JSON 对象"})
    did = match.group(1)
    video = body.get("video")
    target = {"kind": "diag", "id": did, "path": video}
    detail = {"video": video}
    try:
        data = diag_svc.stream_test(
            t, device_id=did, video=video,
            width=body.get("width"), height=body.get("height"),
            pixelformat=body.get("pixelformat"))
    except diag_svc.StreamBusy as exc:
        _audit(host, handler, "diag.stream_test", False, target, detail,
               str(exc))
        return send_json(handler, 429, {"ok": False, "error": str(exc)})
    except ValueError as exc:
        _audit(host, handler, "diag.stream_test", False, target, detail,
               str(exc))
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    except diag_svc.DiagError as exc:
        _audit(host, handler, "diag.stream_test", False, target, detail,
               str(exc))
        payload = {"ok": False, "error": str(exc)}
        status = getattr(exc, "status", None)
        if status:
            payload["status"] = status
        return send_json(handler, 200, payload)
    _audit(host, handler, "diag.stream_test", True, target,
           dict(detail, status=data.get("status")))
    return send_json(handler, 200, data)


register("GET", r"^/api/diag/([^/]+)/video$", diag_video, "diag.video")
register("GET", r"^/api/diag/([^/]+)/usb$", diag_usb, "diag.usb")
register("GET", r"^/api/diag/([^/]+)/dmesg$", diag_dmesg, "diag.dmesg")
register("POST", r"^/api/diag/([^/]+)/stream-test$", stream_test,
         "diag.stream_test")
