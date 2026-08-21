#!/usr/bin/env python3

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


class MonitorHardwareTest(unittest.TestCase):
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

    def test_01_enable_then_now(self):
        st, r = http_post(MON + "/demo/enable")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        time.sleep(2.5)
        st, r = http_get(MON + "/demo/now")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        s = r["sample"]
        self.assertIsNotNone(s)
        self.assertFalse(s["gap"])
        self.assertIsInstance(s["cpu"]["usage"], (int, float))
        self.assertGreaterEqual(s["cpu"]["usage"], 0)
        self.assertLessEqual(s["cpu"]["usage"], 100)
        self.assertIn("per_core", s["cpu"])
        self.assertIn("mem", s)
        self.assertIn("net", s)

        st, r = http_get(MON + "/local/now")
        self.assertEqual(st, 200)
        self.assertFalse(r["sample"]["gap"])

    def test_02_series_window_60(self):
        http_post(MON + "/demo/enable")
        time.sleep(4.2)
        st, r = http_get(MON + "/demo/series?window=60")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        samples = r["samples"]
        self.assertGreaterEqual(len(samples), 2)
        ts = [s["ts"] for s in samples]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(not s["gap"] for s in samples))
        self.assertGreater(ts[-1] - ts[0], 0)

    def test_03_metric_filter(self):
        http_post(MON + "/demo/enable")
        time.sleep(1.2)
        st, r = http_get(MON + "/demo/series?metric=cpu&window=60")
        self.assertEqual(st, 200)
        s = r["samples"][0]
        self.assertEqual(set(s.keys()), {"ts", "device_id", "gap", "cpu"})
        st, r = http_get(MON + "/demo/series?metric=net&window=60")
        s = r["samples"][0]
        self.assertEqual(set(s.keys()), {"ts", "device_id", "gap", "net"})
        self.assertIn("rx_pps", s["net"])

    def test_07_stream_sse(self):
        """Test helper."""
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", PORTAL, timeout=8)
        try:
            c.request("GET", MON + "/demo/stream")
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertIn("text/event-stream", r.getheader("Content-Type"))
            line = r.readline()
            self.assertTrue(line.startswith(b"data:"), line)
            r.close()
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
