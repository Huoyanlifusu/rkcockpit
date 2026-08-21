#!/usr/bin/env python3
"""Test module."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))
PY = sys.executable
CONF_DIR = tempfile.mkdtemp(prefix="rkss-p0-monitor-")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100


PORTAL = _free_port()
BASE_URL = "http://127.0.0.1:%d" % PORTAL
MON = BASE_URL + "/api/monitor"


def http_get(url, timeout=8):
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
            http_get(BASE_URL + "/api/health", 0.5)
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


class MonitorUnitTest(unittest.TestCase):
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

    def test_04_bad_params_and_404(self):
        for w in ("120", "abc", "0", "9999"):
            try:
                http_get(MON + "/demo/series?window=" + w)
                self.fail("expected 400 for window=%s" % w)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400, "window=%s" % w)
        try:
            http_get(MON + "/demo/series?metric=bogus&window=60")
            self.fail("expected 400 for unknown metric")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        for path in (MON + "/no-such-device/now",
                     MON + "/no-such-device/series?window=60",
                     MON + "/no-such-device/enable"):
            try:
                if path.endswith("/enable"):
                    http_post(path)
                else:
                    http_get(path)
                self.fail("expected 404 for %s" % path)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404, path)

    def test_05_disable_stops_sampling(self):
        http_post(MON + "/demo/enable")
        time.sleep(0.2)
        n1 = len(http_get(MON + "/demo/series?window=60")[1]["samples"])
        st, r = http_post(MON + "/demo/disable")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        time.sleep(0.5)
        n2 = len(http_get(MON + "/demo/series?window=60")[1]["samples"])
        self.assertEqual(n1, n2)
        st, r = http_post(MON + "/demo/enable")
        self.assertTrue(r["ok"])

    def test_06_ssh_gap_sample(self):
        st, r = http_post(BASE_URL + "/api/devices", {
            "name": "bad-ssh", "type": "ssh", "host": "127.0.0.1",
            "port": 1, "user": "root", "auth": "key"})
        self.assertEqual(st, 201)
        did = r["device"]["id"]
        st, r = http_post(MON + "/" + did + "/enable")
        self.assertEqual(st, 200)
        time.sleep(0.5)
        st, r = http_get(MON + "/" + did + "/now")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        s = r["sample"]
        self.assertTrue(s["gap"])
        self.assertIsNone(s["cpu"])
        self.assertIsNone(s["net"])
        st, r = http_get(MON + "/" + did + "/series?window=60")
        self.assertEqual(st, 200)
        self.assertTrue(all(x["gap"] for x in r["samples"]))

    def test_08_sampler_thread_no_leak(self):
        from host.task.sampler import DeviceSampler
        from host.transport import LocalTransport
        before = {t.name for t in threading.enumerate()}
        s = DeviceSampler("leak-check", lambda: LocalTransport())
        s.start()
        time.sleep(2.0)
        self.assertGreaterEqual(len(s.snapshot(60)), 1)
        self.assertIn("sampler-leak-check",
                      {t.name for t in threading.enumerate()})
        s.stop()
        time.sleep(0.6)
        alive = [t.name for t in threading.enumerate()
                 if t.name == "sampler-leak-check"]
        self.assertEqual(alive, [], "采样线程未退出")
        now = {t.name for t in threading.enumerate()}
        self.assertEqual(now - before - {"sampler-leak-check"}, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
