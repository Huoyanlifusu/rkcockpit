#!/usr/bin/env python3
"""Test module."""
import ast
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
HOST_ROOT = os.path.join(BASE, "host")
PY = sys.executable
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_FORBIDDEN_API = ("host.api",)
_FORBIDDEN_UPPER = ("host.device", "host.service", "host.task", "host.api")
_ALLOWED_FOR_TRANSPORT = ("host.core", "host.transport")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100


PORTAL = _free_port()
CONF_DIR = tempfile.mkdtemp(prefix="rkss-refactor-test-")


def _py_files():
    out = []
    for root, dirs, files in os.walk(HOST_ROOT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return sorted(out)


def _package_of(path):
    """Test helper."""
    rel = os.path.relpath(path, os.path.dirname(HOST_ROOT))
    parts = rel.split(os.sep)
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1] + [parts[-1][:-3]])


def _import_targets(path):
    """Test helper."""
    pkg = _package_of(path)
    targets = []
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                targets.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = pkg.split(".")
            if node.level:
                base = base[:len(base) - (node.level - 1)]
            if node.module:
                base = base + node.module.split(".")
            targets.append(".".join(base))
    return pkg, [t for t in targets if t == "host" or t.startswith("host.")]


def http_get(url, timeout=5, json_body=True):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return r.status, (json.loads(raw) if json_body else raw)


def wait_port(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_get("http://127.0.0.1:%d/api/health" % port, 0.5)
            return True
        except Exception:
            time.sleep(0.2)
    return False


class RefactorUnitTest(unittest.TestCase):
    portal = None

    @classmethod
    def setUpClass(cls):
        cls.portal = subprocess.Popen(
            [PY, "-m", "portal.portal", "--port", str(PORTAL),
             "--bind", "127.0.0.1", "--sim", "--conf-dir", CONF_DIR],
            cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        assert wait_port(PORTAL), "portal not up"

    @classmethod
    def tearDownClass(cls):
        if cls.portal:
            try:
                os.killpg(os.getpgid(cls.portal.pid), signal.SIGTERM)
            except Exception:
                pass
            try:
                cls.portal.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(cls.portal.pid), signal.SIGKILL)
                except Exception:
                    pass
        import shutil
        shutil.rmtree(CONF_DIR, ignore_errors=True)



    def test_01_legacy_imports(self):
        from host.api import HostApi, host_api_dispatch  # noqa: F401
        from host.transport import make_transport  # noqa: F401
        from host.core import pathguard  # noqa: F401
        import host.api.handlers.legacy as legacy
        self.assertTrue(callable(host_api_dispatch))
        self.assertTrue(callable(legacy.host_api_dispatch))
        self.assertTrue(hasattr(legacy, "HOST_API_VERSION"))
        api = HostApi(CONF_DIR, sim=True)
        self.assertTrue(hasattr(api, "store"))
        self.assertTrue(callable(make_transport))



    def test_02_lower_layers_do_not_import_api(self):
        violations = []
        for path in _py_files():
            pkg, targets = _import_targets(path)
            layer = pkg.split(".")[1] if pkg.count(".") >= 1 else None
            if layer not in ("service", "task", "transport", "device"):
                continue
            for t in targets:
                if t == "host.api" or t.startswith("host.api."):
                    violations.append("%s imports %s" % (pkg, t))
        self.assertEqual(violations, [],
                         "service/task/transport/device 层禁止 import host.api:\n%s"
                         % "\n".join(violations))

    def test_03_transport_does_not_import_upper_layers(self):
        violations = []
        for path in _py_files():
            pkg, targets = _import_targets(path)
            layer = pkg.split(".")[1] if pkg.count(".") >= 1 else None
            if layer != "transport":
                continue
            for t in targets:
                if any(t == f or t.startswith(f + ".") for f in _FORBIDDEN_UPPER):
                    violations.append("%s imports %s" % (pkg, t))
        self.assertEqual(violations, [],
                         "transport 层禁止 import host.device/service/task/api"
                         "（仅 host.core/host.transport 自身）:\n%s"
                         % "\n".join(violations))

    def test_04_core_layer_exists_and_clean(self):
        self.assertTrue(os.path.isfile(os.path.join(HOST_ROOT, "core", "http.py")))
        self.assertTrue(os.path.isfile(os.path.join(HOST_ROOT, "core", "pathguard.py")))
        for path in _py_files():
            pkg, targets = _import_targets(path)
            layer = pkg.split(".")[1] if pkg.count(".") >= 1 else None
            if layer == "core":
                self.assertEqual(targets, [], "core 层不应依赖 host 内部模块")



    def test_05_portal_routes_200(self):
        base = "http://127.0.0.1:%d" % PORTAL
        st, html = http_get(base + "/", json_body=False)
        self.assertEqual(st, 200)
        self.assertIn("RK 设备运维控制台", html)
        self.assertIn("设备管理", html)
        for path in ("/api/health", "/api/devices", "/api/host"):
            st, body = http_get(base + path)
            self.assertEqual(st, 200, "GET %s -> %d" % (path, st))
        st, r = http_get(base + "/api/health")
        self.assertTrue(r["ok"])
        st, r = http_get(base + "/api/devices")
        self.assertTrue(r["ok"])
        self.assertTrue(any(d["id"] == "demo" for d in r["devices"]))

    def test_06_device_status_appends_badge_node(self):
        path = os.path.join(BASE, "static", "js", "pages", "devices.js")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("stateCell.append(badge(", source)
        self.assertNotIn('el("td", "", badge(', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
