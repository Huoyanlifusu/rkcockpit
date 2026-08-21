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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))
PY = sys.executable
CONF_DIR = tempfile.mkdtemp(prefix="rkss-p1-groups-")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100


PORTAL = _free_port()
BASE_URL = "http://127.0.0.1:%d" % PORTAL


def http_get(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_post(url, body=None, timeout=8):
    req = urllib.request.Request(
        url, data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_send(method, url, body=None, timeout=8):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
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


def add_local_device(name):
    st, r = http_post(BASE_URL + "/api/devices",
                      {"name": name, "type": "local"})
    assert st == 201, "add device %s failed: %s" % (name, r)
    return r["device"]["id"]


def wait_job(did, jid, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, r = http_get(BASE_URL + "/api/exec/%s/poll?job_id=%s" % (did, jid))
        if not r["running"]:
            return r
        time.sleep(0.3)
    raise AssertionError("job %s/%s 未在 %ds 内结束" % (did, jid, timeout))


class GroupsLogcenterHardwareTest(unittest.TestCase):
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



    def test_04_group_exec_jobs_and_poll(self):
        did = add_local_device("t4-local")
        http_post(BASE_URL + "/api/groups",
                  {"name": "t4-g", "device_ids": [did, "demo"]})
        st, r = http_post(BASE_URL + "/api/groups/t4-g/exec",
                          {"cmd": "uname -a", "timeout": 30})
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        jobs = r["jobs"]
        self.assertEqual(len(jobs), 2)
        by_dev = {j["device_id"]: j["job_id"] for j in jobs}
        self.assertIn(did, by_dev)
        self.assertIn("demo", by_dev)
        for dev, jid in by_dev.items():
            poll = wait_job(dev, jid)
            self.assertEqual(poll["exit_code"], 0)
            self.assertIn("Linux", poll["output"])

        try:
            http_post(BASE_URL + "/api/groups/t4-g/exec", {"cmd": "  "})
            self.fail("expected 400 for empty cmd")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)



    def test_07_sources_shape(self):
        st, r = http_get(BASE_URL + "/api/logcenter/demo/sources")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertIsInstance(r["sources"], list)
        self.assertGreaterEqual(len(r["sources"]), 1)
        for s in r["sources"]:
            self.assertIn("name", s)
            self.assertIn("path", s)
            self.assertIn("accessible", s)
            self.assertIsInstance(s["accessible"], bool)

    def test_08_tail_returns_lines(self):
        st, r = http_get(BASE_URL + "/api/logcenter/demo/tail"
                             "?source=/etc/hostname&lines=10")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "/etc/hostname")
        self.assertIsInstance(r["lines"], list)
        self.assertGreaterEqual(len(r["lines"]), 1)
        self.assertTrue(all(isinstance(x, str) for x in r["lines"]))

        st, r = http_get(BASE_URL + "/api/logcenter/demo/tail"
                             "?source=/no-such-rkss-file-xyz")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["lines"], [])

    def test_09_tail_filter_and_bad_params(self):
        st, r = http_get(BASE_URL + "/api/logcenter/demo/tail"
                             "?source=/etc/hostname&filter=^[a-z]")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(len(r["lines"]), 1)
        st, r = http_get(BASE_URL + "/api/logcenter/demo/tail"
                             "?source=/etc/hostname&filter=zzz-no-match")
        self.assertEqual(st, 200)
        self.assertEqual(r["lines"], [])

        for qs in ("lines=abc", "lines=12x"):
            try:
                http_get(BASE_URL + "/api/logcenter/demo/tail"
                             "?source=/etc/hostname&" + qs)
                self.fail("expected 400 for " + qs)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400, qs)

        for path in ("/api/logcenter/no-such-dev/sources",
                     "/api/logcenter/no-such-dev/tail?source=/etc/hostname"):
            try:
                http_get(BASE_URL + path)
                self.fail("expected 404 for " + path)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404, path)
        try:
            http_post(BASE_URL + "/api/logcenter/no-such-dev/follow",
                      {"source": "/etc/hostname"})
            self.fail("expected 404 for follow on unknown device")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_10_follow_unfollow_lifecycle(self):
        path = os.path.join(CONF_DIR, "follow.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("line1\nline2\n")
        st, r = http_post(BASE_URL + "/api/logcenter/demo/follow",
                          {"source": path})
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["follow"]["source"], path)
        self.assertTrue(r["follow"]["alive"])

        with open(path, "a", encoding="utf-8") as fh:
            fh.write("line3\n")

        deadline = time.time() + 8
        lines = 0
        while time.time() < deadline:
            st, r = http_get(BASE_URL + "/api/logcenter/running")
            mine = [f for f in r["running"] if f["device_id"] == "demo"]
            self.assertEqual(len(mine), 1, "follow 后 running 应有 1 条")
            if mine[0]["lines"] >= 3:
                lines = mine[0]["lines"]
                break
            time.sleep(0.5)
        self.assertGreaterEqual(lines, 3, "环形缓冲应捕获新增行")

        st, r = http_post(BASE_URL + "/api/logcenter/demo/unfollow")
        self.assertEqual(st, 200)
        deadline = time.time() + 5
        while time.time() < deadline:
            st, r = http_get(BASE_URL + "/api/logcenter/running")
            mine = [f for f in r["running"] if f["device_id"] == "demo"]
            if not mine:
                break
            time.sleep(0.3)
        self.assertEqual(mine, [], "unfollow 后 running 应消失")

        st, r = http_get(BASE_URL + "/api/logcenter/demo/tail?source=" + path)
        self.assertEqual(st, 200)
        self.assertIn("line3", r["lines"])

    def test_11_stream_sse(self):
        """Test helper."""
        path = os.path.join(CONF_DIR, "stream.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("sline1\nsline2\n")
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", PORTAL, timeout=20)
        try:
            c.request("GET", "/api/logcenter/demo/stream?source=" +
                      urllib.parse.quote(path) + "&filter=.")
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertIn("text/event-stream", r.getheader("Content-Type"))
            deadline = time.time() + 12
            body = ""
            while time.time() < deadline:
                ln = r.readline()
                if not ln:
                    break
                body += ln.decode("utf-8", "replace")
                if "sline1" in body:
                    break
            self.assertIn("event: reconnect", body)
            self.assertIn("event: line", body)
            self.assertIn("sline1", body)
            r.close()
        finally:
            c.close()
        http_post(BASE_URL + "/api/logcenter/demo/unfollow")

    def test_12_audit_groups_and_logcenter(self):
        st, r = http_post(BASE_URL + "/api/groups",
                          {"name": "t12-g", "device_ids": ["demo"]})
        self.assertEqual(st, 201)
        st, r = http_post(BASE_URL + "/api/groups/t12-g/exec",
                          {"cmd": "uname -a"})
        self.assertEqual(st, 200)
        http_get(BASE_URL + "/api/logcenter/demo/tail?source=/etc/hostname")

        st, r = http_get(BASE_URL + "/api/audit?action=groups.create")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(r["total"], 1)
        self.assertTrue(any(ev.get("target", {}).get("id") == "t12-g"
                            for ev in r["events"]))
        st, r = http_get(BASE_URL + "/api/audit?action=groups.exec")
        self.assertGreaterEqual(r["total"], 1)
        st, r = http_get(BASE_URL + "/api/audit?action=logcenter.tail")
        self.assertGreaterEqual(r["total"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
