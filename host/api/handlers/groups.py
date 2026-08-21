"""Utilities for host.api.handlers.groups."""
from urllib.parse import unquote

from host.api.router import register
from host.core.http import send_json, read_json_body
from host.device.groups import get_group_store
from host.task.execjob import ExecQueueFull


def _store(host):
    return get_group_store(host.store.conf_dir)


def _audit(host, handler, action, target, ok, detail=None, err=""):
    try:
        host.audit.record({
            "action": action, "target": target or {},
            "detail": dict(detail or {}), "result": "ok" if ok else "fail",
            "err": err or "", "ip": handler.client_address[0] or "",
        })
    except Exception:
        pass


def _existing_ids(host, ids):
    """Handle existing ids."""
    good, bad = [], []
    for did in ids:
        try:
            host.store.get(did)
            good.append(did)
        except KeyError:
            bad.append(did)
    return good, bad


def _groups_list(handler, host, match, query):
    return send_json(handler, 200, {"ok": True, "groups": _store(host).list()})


def _groups_create(handler, host, match, query):
    data = read_json_body(handler)
    name = data.get("name") or ""
    device_ids = data.get("device_ids")
    if device_ids is not None and not isinstance(device_ids, list):
        return send_json(handler, 400,
                         {"ok": False, "error": "device_ids 必须是数组"})
    good, bad = _existing_ids(host, device_ids or [])
    try:
        group = _store(host).create(name, good)
    except ValueError as exc:
        _audit(host, handler, "groups.create",
               {"kind": "group", "id": str(name).strip()}, False,
               {"device_ids": good}, str(exc))
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    _audit(host, handler, "groups.create",
           {"kind": "group", "id": group["name"]}, True,
           {"device_ids": group["device_ids"]})
    payload = {"ok": True, "group": group}
    if bad:
        payload["skipped"] = bad
    return send_json(handler, 201, payload)


def _groups_update(handler, host, match, query):
    name = unquote(match.group(1))
    try:
        _store(host).get(name)
    except KeyError:
        _audit(host, handler, "groups.update",
               {"kind": "group", "id": name}, False, None,
               "组不存在: %s" % name)
        return send_json(handler, 404,
                         {"ok": False, "error": "组不存在: %s" % name})
    data = read_json_body(handler)
    device_ids = data.get("device_ids")
    if not isinstance(device_ids, list):
        return send_json(handler, 400,
                         {"ok": False, "error": "device_ids 必须是数组"})
    good, bad = _existing_ids(host, device_ids)
    try:
        group = _store(host).update(name, good)
    except KeyError:
        _audit(host, handler, "groups.update",
               {"kind": "group", "id": name}, False,
               {"device_ids": good}, "组不存在: %s" % name)
        return send_json(handler, 404,
                         {"ok": False, "error": "组不存在: %s" % name})
    _audit(host, handler, "groups.update",
           {"kind": "group", "id": name}, True,
           {"device_ids": group["device_ids"], "skipped": bad})
    return send_json(handler, 200,
                     {"ok": True, "group": group, "skipped": bad})


def _groups_delete(handler, host, match, query):
    name = unquote(match.group(1))
    try:
        _store(host).delete(name)
    except KeyError:
        _audit(host, handler, "groups.delete",
               {"kind": "group", "id": name}, False, None,
               "组不存在: %s" % name)
        return send_json(handler, 404,
                         {"ok": False, "error": "组不存在: %s" % name})
    _audit(host, handler, "groups.delete", {"kind": "group", "id": name}, True)
    return send_json(handler, 200, {"ok": True, "deleted": name})


def _groups_exec(handler, host, match, query):
    name = unquote(match.group(1))
    try:
        group = _store(host).get(name)
    except KeyError:
        return send_json(handler, 404,
                         {"ok": False, "error": "组不存在: %s" % name})
    data = read_json_body(handler)
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return send_json(handler, 400, {"ok": False, "error": "cmd 必填"})
    try:
        timeout = int(data.get("timeout") or 120)
    except (TypeError, ValueError):
        return send_json(handler, 400,
                         {"ok": False, "error": "timeout 必须是整数"})
    jobs, skipped = [], []
    for did in group["device_ids"]:
        try:
            host.store.get(did)
        except KeyError:
            skipped.append(did)
            continue
        try:
            jid = host._exec_store(did).run(cmd, timeout)
        except ExecQueueFull as exc:
            _audit(host, handler, "groups.exec",
                   {"kind": "group", "id": name}, False,
                   {"cmd": cmd, "timeout": timeout, "device_id": did},
                   str(exc))
            return send_json(handler, 429, {
                "ok": False,
                "error": "%s，已启动 %d 个 job，请分两批执行"
                         % (str(exc), len(jobs)),
                "jobs": jobs})
        jobs.append({"device_id": did, "job_id": jid})
    _audit(host, handler, "groups.exec", {"kind": "group", "id": name}, True,
           {"cmd": cmd, "timeout": timeout, "jobs": len(jobs),
            "skipped": skipped})
    return send_json(handler, 200,
                     {"ok": True, "jobs": jobs, "skipped": skipped})


register("GET", r"^/api/groups$", _groups_list, "groups")
register("POST", r"^/api/groups$", _groups_create, "groups")
register("PUT", r"^/api/groups/([^/]+)$", _groups_update, "groups")
register("DELETE", r"^/api/groups/([^/]+)$", _groups_delete, "groups")
register("POST", r"^/api/groups/([^/]+)/exec$", _groups_exec, "groups")
