"""Utilities for host.device.store."""
import base64
import hashlib
import json
import os
import tempfile
import threading
import time

from host.device.keys import KeyStore

VALID_TYPES = ("ssh", "adb", "local")
SCHEMA_VERSION = 1


def gen_id():
    return hashlib.sha1(os.urandom(16)).hexdigest()[:8]


class DeviceStore:
    def __init__(self, conf_dir):
        self.conf_dir = os.path.abspath(conf_dir)
        os.makedirs(self.conf_dir, exist_ok=True)
        self.path = os.path.join(self.conf_dir, "devices.json")
        self.tmp_dir = os.path.join(self.conf_dir, "tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._devices = {}
        self._state = {}
        self.keys = KeyStore(conf_dir)
        self._load()



    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = []
        if not isinstance(raw, list):
            raw = []
        for d in raw:
            if not isinstance(d, dict) or not d.get("id"):
                continue
            d.setdefault("schema_version", SCHEMA_VERSION)
            d["_password"] = base64.b64decode(d.pop("_password_b64"))\
                if d.get("_password_b64") else None
            d["_key_path"] = self._resolve_key_path(d.get("key_ref"))
            self._devices[d["id"]] = d

    def _resolve_key_path(self, key_ref):
        """Handle resolve key path."""
        if not key_ref:
            return None
        try:
            return self.keys.get_path(key_ref)
        except KeyError:
            return None

    def save(self):
        tmp = self.path + ".tmp"
        data = []
        for d in self._devices.values():
            copy = dict(d)
            pw = copy.pop("_password", None)
            copy.pop("_key_path", None)
            copy["_password_b64"] = base64.b64encode(pw.encode("utf-8"))\
                .decode("ascii") if pw else ""
            copy["has_password"] = bool(pw)
            copy["schema_version"] = SCHEMA_VERSION
            data.append(copy)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)



    def _validate(self, data, partial=False):
        errs = []
        typ = (data.get("type") or "").lower()
        if typ not in VALID_TYPES:
            errs.append("type 必须是 ssh|adb|local")
        if not data.get("name"):
            errs.append("name 必填")
        if typ == "ssh" and not data.get("host"):
            errs.append("ssh 设备 host 必填")
        if typ == "adb" and not data.get("host"):
            errs.append("adb 设备 host(serial) 必填")
        if errs:
            raise ValueError("; ".join(errs))
        return typ

    def add(self, data):
        with self._lock:
            typ = self._validate(data)
            d = {
                "id": str(data.get("id") or gen_id()),
                "name": str(data["name"]).strip(),
                "type": typ,
                "host": str(data.get("host") or "").strip(),
                "port": int(data.get("port") or 22),
                "user": str(data.get("user") or "root").strip(),
                "auth": data.get("auth") or "key",
                "remark": str(data.get("remark") or "").strip(),
                "created_at": int(time.time() * 1000),
                "updated_at": int(time.time() * 1000),
            }
            if typ == "local":
                d["local_root"] = os.path.abspath(os.path.expanduser(
                    data.get("local_root") or
                    os.path.join(self.conf_dir, "demo-remote")))
                os.makedirs(d["local_root"], exist_ok=True)
            d["_password"] = data.get("password") or None
            d["has_password"] = bool(d["_password"])
            ref = data.get("key_ref") or None
            d["key_ref"] = ref
            d["_key_path"] = self._resolve_key_path(ref)
            if ref and not d["_key_path"]:
                raise ValueError("key_ref 不存在: %s" % ref)
            d["schema_version"] = SCHEMA_VERSION
            self._devices[d["id"]] = d
            self.save()
            return self.public(d)

    def update(self, did, data):
        with self._lock:
            d = self._devices.get(did)
            if not d:
                raise KeyError(did)
            if "name" in data:
                d["name"] = str(data["name"]).strip()
            if "type" in data:
                d["type"] = (data["type"] or "").lower()
            if "host" in data:
                d["host"] = str(data["host"] or "").strip()
            if "port" in data:
                d["port"] = int(data.get("port") or 22)
            if "user" in data:
                d["user"] = str(data.get("user") or "root").strip()
            if "auth" in data:
                d["auth"] = data.get("auth") or "key"
            if "remark" in data:
                d["remark"] = str(data.get("remark") or "").strip()
            if data.get("local_root") and d["type"] == "local":
                d["local_root"] = os.path.abspath(
                    os.path.expanduser(data["local_root"]))
                os.makedirs(d["local_root"], exist_ok=True)
            if "password" in data:
                d["_password"] = data["password"] or None
                d["has_password"] = bool(d["_password"])
            if "key_ref" in data:
                ref = data["key_ref"] or None
                if ref and not self._resolve_key_path(ref):
                    raise ValueError("key_ref 不存在: %s" % ref)
                d["key_ref"] = ref
                d["_key_path"] = self._resolve_key_path(ref)
            self._validate(d, partial=True)
            d["updated_at"] = int(time.time() * 1000)
            self.save()
            return self.public(d)

    def delete(self, did):
        with self._lock:
            if did not in self._devices:
                raise KeyError(did)
            del self._devices[did]
            self._state.pop(did, None)
            self.save()

    def get(self, did):
        d = self._devices.get(did)
        if not d:
            raise KeyError(did)
        return d

    def list(self):
        with self._lock:
            return [self.public(d) for d in self._devices.values()]

    def public(self, d):
        out = {k: v for k, v in d.items()
               if k not in ("_password", "_password_b64", "_key_path")}
        out["has_password"] = bool(d.get("_password"))
        out.setdefault("schema_version", SCHEMA_VERSION)
        state = self._state.get(d["id"])
        if state and time.time() - state["checked_ms"] < 5:
            out["state"] = state["state"]
            out["ping_ms"] = state.get("ping_ms")
            out["checked_ms"] = state["checked_ms"]
            if state.get("info"):
                out["info"] = state["info"]
        else:

            out["state"] = "unknown"
            out["ping_ms"] = None
            out["checked_ms"] = 0
        return out



    def set_state(self, did, state, ping_ms=None, info=None):
        self._state[did] = {"state": state, "ping_ms": ping_ms,
                            "checked_ms": int(time.time() * 1000),
                            "info": info}

    def temp_file(self, suffix=""):
        fd, path = tempfile.mkstemp(
            prefix="rkss-", suffix=str(suffix or ""), dir=self.tmp_dir)
        os.close(fd)
        return path
