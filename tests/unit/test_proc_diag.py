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


class ProcDiagUnitTest(unittest.TestCase):
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

    # ---- proc list ----

    def test_01_proc_list_shape(self):
        st, r = http_get(self.proc_url("local/list"))
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertIn("total", r)
        self.assertIsInstance(r["processes"], list)
        self.assertGreater(r["total"], 0)
        self.assertLessEqual(len(r["processes"]), 200)
        for p in r["processes"]:
            for key in ("pid", "ppid", "stat", "pcpu", "pmem", "rss_kb",
                        "comm"):
                self.assertIn(key, p, key)
            self.assertTrue(p["comm"], "comm 不应为空")

    def test_02_proc_sort_cpu_desc(self):
        st, r = http_get(self.proc_url("local/list?sort=cpu&order=desc&limit=100"))
        self.assertEqual(st, 200)
        nums = [p["pcpu"] for p in r["processes"] if p["pcpu"] is not None]
        self.assertEqual(nums, sorted(nums, reverse=True))

    def test_03_proc_pattern_filter(self):
        st, r = http_get(self.proc_url("local/list?pattern=PYTHON"))
        self.assertEqual(st, 200)
        self.assertGreaterEqual(r["total"], 1)
        for p in r["processes"]:
            self.assertIn("python", (p["comm"] or "").lower())
        self.assertEqual(r["total"], len(r["processes"]))

    def test_04_proc_pagination(self):
        url = self.proc_url("local/list?sort=pid&order=asc")
        st, r1 = http_get(url + "&limit=5&offset=0")
        st, r2 = http_get(url + "&limit=5&offset=5")
        self.assertEqual(st, 200)
        self.assertEqual(len(r1["processes"]), 5)
        self.assertEqual(len(r2["processes"]), 5)
        self.assertEqual(r1["total"], r2["total"])
        pids1 = {p["pid"] for p in r1["processes"]}
        pids2 = {p["pid"] for p in r2["processes"]}
        self.assertFalse(pids1 & pids2)
        asc = [p["pid"] for p in r1["processes"] + r2["processes"]]
        self.assertEqual(asc, sorted(asc))
        try:
            http_get(url + "&limit=-1")
            self.fail("expected 400 for negative limit")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_05_proc_detail_unknown_pid_404(self):
        try:
            http_get(self.proc_url("local/99999999"))
            self.fail("expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)
            self.assertFalse(json.loads(exc.read()).get("ok"))

    def test_06_signal_pid1_rejected(self):
        st, r = http_post(self.proc_url("local/1/signal"), {"sig": "KILL"})
        self.assertEqual(st, 200)
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_07_signal_invalid_sig_400(self):
        try:
            http_post(self.proc_url("local/1/signal"), {"sig": "FOO"})
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_08_signal_kill_sleep(self):
        exec_url = "http://127.0.0.1:%d/api/exec/local" % PORTAL
        st, r = http_post(exec_url + "/run", {"cmd": "sleep 60"})
        self.assertEqual(st, 200)
        jid = r["job_id"]
        pid = None
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                st, r = http_get(self.proc_url(
                    "local/list?pattern=sleep&sort=pid&order=desc"))
                if r["processes"]:
                    pid = r["processes"][0]["pid"]
                    break
                time.sleep(0.2)
            self.assertIsNotNone(pid, "未找到自起的 sleep 进程")
            st, r = http_post(self.proc_url("local/%d/signal" % pid),
                              {"sig": "KILL"})
            self.assertEqual(st, 200)
            self.assertTrue(r["ok"])
            self.assertIn("rc", r)
            deadline = time.time() + 5
            gone = False
            while time.time() < deadline:
                st, r = http_get(self.proc_url("local/list?pattern=sleep"))
                if not any(p["pid"] == pid for p in r["processes"]):
                    gone = True
                    break
                time.sleep(0.2)
            self.assertTrue(gone, "KILL 后 sleep 进程仍存在")
        finally:
            http_post(exec_url + "/kill", {"job_id": jid})

    # ---- diag ----

    def test_09_diag_video_shape(self):
        st, r = http_get(self.diag_url("local/video"))
        self.assertEqual(st, 200)
        self.assertIn("devices", r)
        self.assertIsInstance(r["devices"], list)
        for d in r["devices"]:
            for key in ("path", "name", "formats", "status"):
                self.assertIn(key, d)
            self.assertTrue(d["path"].startswith("/dev/video"))

    def test_11_diag_dmesg(self):
        st, r = http_get(self.diag_url("local/dmesg?lines=50"))
        self.assertEqual(st, 200)
        if r.get("ok"):
            self.assertIn("truncated", r)
            self.assertIsInstance(r["lines"], list)
            self.assertLessEqual(len(r["lines"]), 50)
            for ln in r["lines"]:
                self.assertIsInstance(ln, str)
            st, rf = http_get(self.diag_url("local/dmesg?lines=50&filter=linux"))
            if rf["lines"]:
                for ln in rf["lines"]:
                    self.assertIn("linux", ln.lower())
        else:
            self.assertIn("error", r)

    def test_13_unknown_device_404(self):
        for url in (self.proc_url("ghost/list"),
                    self.proc_url("ghost/1/signal"),
                    self.diag_url("ghost/video"),
                    self.diag_url("ghost/dmesg")):
            try:
                if url.endswith("/signal"):
                    http_post(url, {"sig": "TERM"})
                else:
                    http_get(url)
                self.fail("expected 404 for %s" % url)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404, url)

if __name__ == "__main__":
    unittest.main(verbosity=2)
