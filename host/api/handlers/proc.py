"""Utilities for host.api.handlers.proc."""
from host.api.router import register
from host.core.http import read_json_body, send_json
from host.service import proc as proc_svc


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


def _maybe_audit(host, action, did, pid, detail):
    """Handle maybe audit."""
    audit = getattr(host, "audit", None)
    record = getattr(audit, "record", None)
    if record is None:
        return
    try:
        record({"actor": "web", "action": action,
                "target": {"kind": "proc", "id": did, "pid": pid},
                "detail": {"sig": detail.get("sig"), "rc": detail.get("rc")},
                "result": "ok"})
    except Exception:
        pass


def proc_list(handler, host, match, query):
    t = _transport(host, match.group(1), handler)
    if t is None:
        return True
    try:
        data = proc_svc.list_processes(
            t, pattern=_q(query, "pattern"),
            sort=_q(query, "sort", "cpu"),
            order=_q(query, "order", "desc"),
            limit=_q(query, "limit", "200"),
            offset=_q(query, "offset", "0"))
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    except proc_svc.ProcError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    return send_json(handler, 200, data)


def proc_detail(handler, host, match, query):
    t = _transport(host, match.group(1), handler)
    if t is None:
        return True
    pid = int(match.group(2))
    try:
        data = proc_svc.process_detail(t, pid)
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    except proc_svc.ProcError as exc:
        return send_json(handler, 404, {"ok": False, "error": str(exc)})
    return send_json(handler, 200, data)


def proc_signal(handler, host, match, query):
    t = _transport(host, match.group(1), handler)
    if t is None:
        return True
    pid = int(match.group(2))
    body = read_json_body(handler)
    if not isinstance(body, dict):
        return send_json(handler, 400, {"ok": False,
                                        "error": "body 必须是 JSON 对象"})
    try:
        data = proc_svc.signal(t, pid, body.get("sig"))
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    except proc_svc.SignalBlockedError as exc:
        return send_json(handler, 200, {"ok": False, "error": str(exc)})
    except proc_svc.ProcError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    _maybe_audit(host, "proc.signal", match.group(1), pid, data)
    return send_json(handler, 200, data)


register("GET", r"^/api/proc/([^/]+)/list$", proc_list, "proc.list")
register("GET", r"^/api/proc/([^/]+)/(\d+)$", proc_detail, "proc.detail")
register("POST", r"^/api/proc/([^/]+)/(\d+)/signal$", proc_signal,
         "proc.signal")
