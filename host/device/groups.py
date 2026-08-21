"""Utilities for host.device.groups."""
import json
import os
import threading
import time

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def get_group_store(conf_dir):
    """Return get group store."""
    key = os.path.abspath(conf_dir)
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = GroupStore(key)
        return _CACHE[key]


class GroupStore:
    def __init__(self, conf_dir):
        self.conf_dir = os.path.abspath(conf_dir)
        os.makedirs(self.conf_dir, exist_ok=True)
        self.path = os.path.join(self.conf_dir, "groups.json")
        self._lock = threading.Lock()
        self._groups = {}
        self._load()



    @staticmethod
    def _clean_ids(ids):
        out = []
        if not isinstance(ids, (list, tuple)):
            return out
        for x in ids:
            s = str(x).strip()
            if s and s not in out:
                out.append(s)
        return out

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        for name, g in raw.items():
            if not isinstance(g, dict):
                continue
            self._groups[str(name)] = {
                "name": str(name),
                "device_ids": self._clean_ids(g.get("device_ids")),
                "created_at": int(g.get("created_at") or 0),
            }

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._groups, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)



    def list(self):
        with self._lock:
            out = [dict(g) for g in self._groups.values()]
        return sorted(out, key=lambda g: (g["created_at"], g["name"]))

    def get(self, name):
        with self._lock:
            g = self._groups.get(name)
            if not g:
                raise KeyError(name)
            return dict(g)

    def create(self, name, device_ids=None):
        name = str(name or "").strip()
        if not name:
            raise ValueError("组名必填")
        ids = self._clean_ids(device_ids)
        with self._lock:
            if name in self._groups:
                raise ValueError("组已存在: %s" % name)
            g = {"name": name, "device_ids": ids,
                 "created_at": int(time.time() * 1000)}
            self._groups[name] = g
            self._save()
            return dict(g)

    def update(self, name, device_ids):
        ids = self._clean_ids(device_ids)
        with self._lock:
            g = self._groups.get(name)
            if not g:
                raise KeyError(name)
            g["device_ids"] = ids
            self._save()
            return dict(g)

    def delete(self, name):
        with self._lock:
            if name not in self._groups:
                raise KeyError(name)
            del self._groups[name]
            self._save()

    def remove_device(self, did):
        """Remove or stop remove device."""
        changed = False
        with self._lock:
            for g in self._groups.values():
                if did in g["device_ids"]:
                    g["device_ids"] = [x for x in g["device_ids"] if x != did]
                    changed = True
        if changed:
            self._save()
