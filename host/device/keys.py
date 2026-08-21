"""Utilities for host.device.keys."""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time

VALID_TYPES = ("ed25519", "rsa")


def gen_id():
    return hashlib.sha1(os.urandom(16)).hexdigest()[:8]


class KeyStore:
    def __init__(self, conf_dir):
        self.conf_dir = os.path.abspath(conf_dir)
        self.keys_dir = os.path.join(self.conf_dir, "keys")
        os.makedirs(self.keys_dir, exist_ok=True)
        self.meta_path = os.path.join(self.keys_dir, "keys.json")
        self._lock = threading.Lock()
        self._keys = {}
        self._load()



    def _load(self):
        try:
            with open(self.meta_path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            for kid, meta in raw.items():
                if isinstance(meta, dict) and meta.get("id"):
                    self._keys[kid] = meta

    def _save(self):
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._keys, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.meta_path)



    def generate(self, name, typ="ed25519", comment=""):
        """Handle generate."""
        name = str(name or "").strip()
        typ = str(typ or "ed25519").strip().lower()
        if not name:
            raise ValueError("name 必填")
        if typ not in VALID_TYPES:
            raise ValueError("type 必须是 ed25519|rsa")
        comment = str(comment or "").replace("\n", " ").replace("\r", "").strip()
        with self._lock:
            kid = gen_id()
            while kid in self._keys:
                kid = gen_id()
            kdir = os.path.join(self.keys_dir, kid)
            os.makedirs(kdir, exist_ok=True)
            priv = os.path.join(kdir, "id_" + typ)
            pub = priv + ".pub"
            argv = ["ssh-keygen", "-t", typ, "-N", "",
                    "-C", comment or "", "-f", priv]
            try:
                proc = subprocess.run(argv, capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError("ssh-keygen 执行失败: %s" % exc)
            if proc.returncode != 0:
                raise ValueError("ssh-keygen 失败: %s" %
                                 proc.stderr.decode("utf-8", "replace").strip())
            os.chmod(priv, 0o600)
            os.chmod(pub, 0o644)
            try:
                with open(pub, encoding="utf-8") as fh:
                    public = fh.read().strip()
            except OSError as exc:
                raise ValueError("读取公钥失败: %s" % exc)
            meta = {
                "id": kid,
                "name": name,
                "type": typ,
                "public": public,
                "fingerprint": self._fingerprint(pub),
                "created_at": int(time.time() * 1000),
            }
            self._keys[kid] = meta
            self._save()
            return dict(meta)

    def _fingerprint(self, pub_path):
        """Handle fingerprint."""
        try:
            proc = subprocess.run(["ssh-keygen", "-lf", pub_path],
                                  capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode != 0:
            return ""
        for part in proc.stdout.decode("utf-8", "replace").split():
            if part.startswith("SHA256:") or part.startswith("MD5:"):
                return part
        return ""



    def list(self):
        """Return list."""
        with self._lock:
            return [dict(m) for m in self._keys.values()]

    def get(self, key_id):
        with self._lock:
            m = self._keys.get(key_id)
        if not m:
            raise KeyError(key_id)
        return dict(m)

    def get_path(self, key_id):
        """Return get path."""
        m = self.get(key_id)
        return os.path.join(self.keys_dir, key_id, "id_" + m["type"])

    def delete(self, key_id, refs=None):
        """Remove or stop delete."""
        with self._lock:
            m = self._keys.get(key_id)
            if not m:
                raise KeyError(key_id)
            if refs and key_id in refs:
                raise ValueError("被设备 %s 引用，请先解除 key_ref" % refs[key_id])
            del self._keys[key_id]
            self._save()
        shutil.rmtree(os.path.join(self.keys_dir, key_id), ignore_errors=True)
        return key_id
