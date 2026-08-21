#!/usr/bin/env python3

import json
import os
import shutil
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
sys.path.insert(0, str(BASE))
PY = sys.executable
CONF_DIR = tempfile.mkdtemp(prefix="rkss-p0-test-")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100


PORTAL = _free_port()


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_post(url, body=None, timeout=8):
    req = urllib.request.Request(
        url, data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def wait_port(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_get("http://127.0.0.1:%d/api/health" % port, 0.5)
            return True
        except Exception:
            time.sleep(0.2)
    return False


class Proc:
    def __init__(self, args):
        self.p = subprocess.Popen(args, cwd=BASE,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  start_new_session=True)

    def stop(self):
        try:
            os.killpg(os.getpgid(self.p.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            self.p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(self.p.pid), signal.SIGKILL)
            except Exception:
                pass


class DiagHardwareTest(unittest.TestCase):
    portal = None

    @classmethod
    def setUpClass(cls):
        cls.portal = Proc([PY, "-m", "portal.portal", "--port", str(PORTAL),
                           "--bind", "127.0.0.1", "--sim",
                           "--conf-dir", CONF_DIR])
        assert wait_port(PORTAL), "portal not up"

    @classmethod
    def tearDownClass(cls):
        cls.portal.stop()
        shutil.rmtree(CONF_DIR, ignore_errors=True)

    def proc_url(self, path):
        return "http://127.0.0.1:%d/api/proc/%s" % (PORTAL, path)

    def diag_url(self, path):
        return "http://127.0.0.1:%d/api/diag/%s" % (PORTAL, path)

    # ---- diag ----

    def test_10_diag_usb_shape(self):
        st, r = http_get(self.diag_url("local/usb"))
        self.assertEqual(st, 200)
        self.assertIsInstance(r["devices"], list)
        for d in r["devices"]:
            if "raw" in d:
                continue
            for key in ("bus", "dev", "vid", "pid", "desc"):
                self.assertIn(key, d)

    def test_12_diag_cache_hit(self):
        import host.service.diag as diag_mod
        from host.transport.local import LocalTransport

        r1 = diag_mod.usb(LocalTransport(), device_id="cacheprobe")
        r2 = diag_mod.usb(LocalTransport(), device_id="cacheprobe")
        self.assertIs(r1, r2, "第二次调用应命中缓存（同一对象）")
        key = ("cacheprobe", "usb")
        self.assertIn(key, diag_mod._CACHE)
        self.assertLess(time.time() - diag_mod._CACHE[key][0], 10)

        st1, hr1 = http_get(self.diag_url("local/usb"))
        st2, hr2 = http_get(self.diag_url("local/usb"))
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual(hr1, hr2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
