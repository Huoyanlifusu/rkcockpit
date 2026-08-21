"""Utilities for host.api.handlers.deploy."""
from host.api.router import register
from host.core.http import read_json_body, send_json
from host.task.deployjob import DeployJobStore

_CAP = "deploy"


def _store(host):
    """Handle store."""
    s = getattr(host, "_deploy_store", None)
    if s is None:
        s = DeployJobStore(host)
        host._deploy_store = s
    return s


def _check_device(handler, host, did):
    try:
        host._device(did)
    except KeyError:
        send_json(handler, 404, {"ok": False,
                                 "error": "设备不存在: %s" % did})
        return False
    return True


def _plan(handler, host, match, query):
    did = match.group(1)
    if not _check_device(handler, host, did):
        return True
    body = read_json_body(handler)
    if not isinstance(body, dict):
        return send_json(handler, 400, {"ok": False,
                                        "error": "body 必须是 JSON 对象"})
    try:
        r = _store(host).plan(did, body.get("files"), cmd=body.get("cmd"),
                              timeout=body.get("timeout"),
                              restart=body.get("restart"))
        return send_json(handler, 200, r)
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})


def _start(handler, host, match, query):
    did, plan_id = match.group(1), match.group(2)
    if not _check_device(handler, host, did):
        return True
    try:
        job = _store(host).start(plan_id, did)
    except KeyError:
        return send_json(handler, 404, {"ok": False,
                                        "error": "部署计划不存在: %s" % plan_id})
    except ValueError as exc:
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    return send_json(handler, 200, {"ok": True, "job": job})


def _get(handler, host, match, query):
    did, plan_id = match.group(1), match.group(2)
    if not _check_device(handler, host, did):
        return True
    try:
        job = _store(host).get(plan_id, did)
    except KeyError:
        return send_json(handler, 404, {"ok": False,
                                        "error": "部署计划不存在: %s" % plan_id})
    return send_json(handler, 200, {"ok": True, "job": job})


def _cancel(handler, host, match, query):
    did, plan_id = match.group(1), match.group(2)
    if not _check_device(handler, host, did):
        return True
    try:
        r = _store(host).cancel(plan_id, did)
    except KeyError:
        return send_json(handler, 404, {"ok": False,
                                        "error": "部署计划不存在: %s" % plan_id})
    return send_json(handler, 200, r)


register("POST", r"^/api/deploy/([^/]+)/plan$", _plan, _CAP)
register("POST", r"^/api/deploy/([^/]+)/([^/]+)/start$", _start, _CAP)
register("GET", r"^/api/deploy/([^/]+)/([^/]+)$", _get, _CAP)
register("POST", r"^/api/deploy/([^/]+)/([^/]+)/cancel$", _cancel, _CAP)
