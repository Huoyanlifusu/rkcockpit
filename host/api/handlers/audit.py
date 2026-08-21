"""Utilities for host.api.handlers.audit."""
from host.core.http import send_json
from host.audit import ACTIONS
from host.service.audit import AuditService


from host.api.router import register


def _svc(host):
    return AuditService(host.audit)


def _q(query, key, default=None):
    v = query.get(key)
    return v[0] if v else default


def _parse_int(value, name):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是整数" % name)


def _audit_query(handler, host, match, query):
    try:
        from_ms = _parse_int(_q(query, "from"), "from")
        to_ms = _parse_int(_q(query, "to"), "to")
        limit = _parse_int(_q(query, "limit"), "limit") or 100
        offset = _parse_int(_q(query, "offset"), "offset") or 0
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    action = _q(query, "action") or ""
    device = _q(query, "device") or ""
    result = _q(query, "result") or ""
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        return send_json(handler, 400,
                         {"ok": False, "error": "from 不能大于 to"})
    if result and result not in ("ok", "fail"):
        return send_json(handler, 400,
                         {"ok": False, "error": "result 只支持 ok|fail"})
    if action and action not in ACTIONS:
        return send_json(handler, 400,
                         {"ok": False, "error": "未知 action: %s" % action})
    data = _svc(host).query(from_ms=from_ms, to_ms=to_ms, action=action,
                            device=device, result=result,
                            limit=limit, offset=offset)
    return send_json(handler, 200,
                     {"ok": True, "total": data["total"],
                      "events": data["events"]})


def _audit_stats(handler, host, match, query):
    try:
        days = int(_q(query, "days", "7") or 7)
    except (TypeError, ValueError):
        return send_json(handler, 400, {"ok": False, "error": "days 必须是整数"})
    if not 1 <= days <= 90:
        return send_json(handler, 400, {"ok": False,
                                        "error": "days 必须在 1..90 之间"})
    return send_json(handler, 200,
                     {"ok": True, "data": _svc(host).stats(days)})


def _audit_export(handler, host, match, query):
    try:
        from_ms = _parse_int(_q(query, "from"), "from")
        to_ms = _parse_int(_q(query, "to"), "to")
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        return send_json(handler, 400,
                         {"ok": False, "error": "from 不能大于 to"})
    body = _svc(host).export_csv(from_ms=from_ms, to_ms=to_ms)
    data = body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition",
                        "attachment; filename=\"audit.csv\"")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)
    return True


register("GET", r"^/api/audit$", _audit_query, "audit")
register("GET", r"^/api/audit/stats$", _audit_stats, "audit")
register("GET", r"^/api/audit/export$", _audit_export, "audit")
