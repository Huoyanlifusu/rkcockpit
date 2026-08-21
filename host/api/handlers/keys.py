"""Utilities for host.api.handlers.keys."""
import sys

from host.api.router import register
from host.core.http import read_json_body, send_json
from host.transport import TransportError, make_transport


def _shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _keys(host):
    return host.store.keys


def _audit(host, handler, action, ok, target, detail=None, err=""):
    """Handle audit."""
    try:
        ip = handler.client_address[0]
    except Exception:
        ip = ""
    try:
        if ok:
            host.audit.record_ok(action, target, detail or {}, ip=ip)
        else:
            host.audit.record_fail(action, target, detail or {},
                                   error=err or "", ip=ip)
    except Exception as exc:
        sys.stderr.write("[keys] audit 失败: %r\n" % exc)


def keys_generate(handler, host, match, query):
    body = read_json_body(handler)
    if not isinstance(body, dict):
        return send_json(handler, 400,
                         {"ok": False, "error": "body 必须是 JSON 对象"})
    try:
        key = _keys(host).generate(
            body.get("name"), body.get("type") or "ed25519",
            body.get("comment") or "")
    except ValueError as exc:
        _audit(host, handler, "keys.generate", False,
               {"kind": "key"}, {"name": body.get("name")}, str(exc))
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    _audit(host, handler, "keys.generate", True,
           {"kind": "key", "id": key["id"]},
           {"name": key["name"], "type": key["type"]})
    return send_json(handler, 201, {"ok": True, "key": key})


def keys_list(handler, host, match, query):
    return send_json(handler, 200, {"ok": True, "keys": _keys(host).list()})


def keys_delete(handler, host, match, query):
    kid = match.group(1)
    refs = {}
    try:
        for d in host.store.list():
            if d.get("key_ref"):
                refs[d["key_ref"]] = d["name"]
    except Exception:
        refs = {}
    try:
        _keys(host).delete(kid, refs=refs)
    except KeyError:
        return send_json(handler, 404,
                         {"ok": False, "error": "密钥不存在: %s" % kid})
    except ValueError as exc:
        _audit(host, handler, "keys.delete", False,
               {"kind": "key", "id": kid}, {}, str(exc))
        return send_json(handler, 409, {"ok": False, "error": str(exc)})
    _audit(host, handler, "keys.delete", True, {"kind": "key", "id": kid}, {})
    return send_json(handler, 200, {"ok": True, "deleted": kid})


def keys_install(handler, host, match, query):
    kid = match.group(1)
    body = read_json_body(handler)
    if not isinstance(body, dict):
        return send_json(handler, 400,
                         {"ok": False, "error": "body 必须是 JSON 对象"})
    device_id = str(body.get("device_id") or "").strip()
    target_user = str(body.get("target_user") or "").strip() or None
    if not device_id:
        return send_json(handler, 400, {"ok": False, "error": "device_id 必填"})
    try:
        key = _keys(host).get(kid)
    except KeyError:
        return send_json(handler, 404,
                         {"ok": False, "error": "密钥不存在: %s" % kid})
    try:
        dev = host.store.get(device_id)
    except KeyError:
        return send_json(handler, 404,
                         {"ok": False, "error": "设备不存在: %s" % device_id})
    if dev["type"] != "ssh":
        msg = "%s 设备不支持 SSH 密钥安装（仅 ssh 设备）" % dev["type"]
        _audit(host, handler, "keys.install", False,
               {"kind": "device", "id": device_id},
               {"key_id": kid, "device_type": dev["type"]}, msg)
        return send_json(handler, 200, {"ok": False, "error": msg})

    tdev = dict(dev)
    if target_user:
        tdev["user"] = target_user
    try:
        t = make_transport(
            tdev, control_dir=getattr(host, "ssh_control_dir", None),
            scheduler=getattr(host, "ssh_scheduler", None),
            device_id=device_id, workload="foreground")
    except TransportError as exc:
        _audit(host, handler, "keys.install", False,
               {"kind": "device", "id": device_id},
               {"key_id": kid}, str(exc))
        return send_json(handler, 400, {"ok": False, "error": str(exc)})

    pub = key["public"]
    cmd = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
           "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
           "grep -qxF %s ~/.ssh/authorized_keys || echo %s >> ~/.ssh/authorized_keys"
           % (_shq(pub), _shq(pub)))
    target = {"kind": "device", "id": device_id}
    detail = {"key_id": kid, "target_user": target_user or dev["user"]}
    try:
        rc, out, err = t.exec(cmd, timeout=30)
    except Exception as exc:
        _audit(host, handler, "keys.install", False, target, detail, str(exc))
        return send_json(handler, 400, {"ok": False, "error": str(exc)})
    if rc != 0:
        msg = ((err or out).strip() or "写入 authorized_keys 失败")[:200]
        _audit(host, handler, "keys.install", False, target, detail, msg)
        return send_json(handler, 400, {"ok": False, "error": msg})
    _audit(host, handler, "keys.install", True, target, detail)
    return send_json(handler, 200, {"ok": True, "installed": True,
                                    "target_user": detail["target_user"]})


register("POST", r"^/api/keys/generate$", keys_generate, "keys.generate")
register("GET", r"^/api/keys$", keys_list, "keys.list")
register("DELETE", r"^/api/keys/([^/]+)$", keys_delete, "keys.delete")
register("POST", r"^/api/keys/([^/]+)/install$", keys_install, "keys.install")
