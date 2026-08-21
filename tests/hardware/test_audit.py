#!/usr/bin/env python3

import datetime
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
CONF_DIR = tempfile.mkdtemp(prefix="rkss-audit-test-")
AUDIT_DIR = os.path.join(CONF_DIR, "audit")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100


PORTAL = _free_port()
BASE_URL = "http://127.0.0.1:%d" % PORTAL


def http_get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_post(url, body=None, raw=None, timeout=8):
    data = raw if raw is not None else \
        json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/octet-stream"
                 if raw is not None else "application/json"},
        method="POST")
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


def audit_get(params):
    url = BASE_URL + "/api/audit"
    if params:
        url += "?" + "&".join("%s=%s" % (k, v) for k, v in params.items())
    st, r = http_get(url)
    assert st == 200, r
    return r


class AuditHardwareTest(unittest.TestCase):
    portal = None
    dev_id = None

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

    def _ensure_device(self):
        if self.__class__.dev_id is None:
            st, r = http_post(BASE_URL + "/api/devices", {
                "name": "aud-exec", "type": "local",
                "local_root": os.path.join(CONF_DIR, "exec-remote")})
            self.assertEqual(st, 201)
            self.__class__.dev_id = r["device"]["id"]
        return self.__class__.dev_id



    def test_02_exec_run_kill_audited(self):
        dev = self._ensure_device()
        url = BASE_URL + "/api/exec/%s" % dev

        st, r = http_post(url + "/run", {"cmd": "echo audit-ok-123"})
        self.assertEqual(st, 200)
        jid = r["job_id"]

        st, r = http_post(url + "/run", {"cmd": "sleep 60"})
        kill_jid = r["job_id"]
        time.sleep(0.4)
        st, r = http_post(url + "/kill", {"job_id": kill_jid})
        self.assertEqual(st, 200)

        events = audit_get({"action": "exec.run", "device": dev})["events"]
        self.assertGreaterEqual(len(events), 2)
        hit = next(e for e in events if e["detail"].get("job_id") == jid)
        self.assertEqual(hit["result"], "ok")
        self.assertEqual(hit["target"]["kind"], "device")
        self.assertEqual(hit["target"]["id"], dev)
        self.assertEqual(hit["detail"]["cmd"], "echo audit-ok-123")
        self.assertEqual(hit["ip"], "127.0.0.1")
        self.assertIn("id", hit)
        self.assertIn("ts", hit)

        kills = audit_get({"action": "exec.kill", "device": dev})["events"]
        self.assertEqual(kills[0]["result"], "ok")
        self.assertEqual(kills[0]["detail"]["job_id"], kill_jid)
        self.assertEqual(kills[0]["target"]["kind"], "proc")



    def test_03_fs_mkdir_rm_audited(self):
        dev = self._ensure_device()
        base = BASE_URL + "/api/fs/%s" % dev
        st, r = http_post(base + "/mkdir", {"path": "/au-dir"})
        self.assertEqual(st, 200)
        st, r = http_post(base + "/rm", {"path": "/au-dir", "recursive": True})
        self.assertEqual(st, 200)

        evs = audit_get({"action": "fs.mkdir", "device": dev})["events"]
        self.assertEqual(evs[0]["result"], "ok")
        self.assertEqual(evs[0]["target"]["kind"], "file")
        self.assertEqual(evs[0]["target"]["path"], "/au-dir")

        evs = audit_get({"action": "fs.rm", "device": dev})["events"]
        self.assertEqual(evs[0]["result"], "ok")
        self.assertEqual(evs[0]["detail"].get("recursive"), True)
        self.assertEqual(evs[0]["target"]["path"], "/au-dir")



    def test_05_filter_accuracy_and_fail(self):
        dev = self._ensure_device()
        base = BASE_URL + "/api/fs/%s" % dev
        try:
            http_post(base + "/rm", {"path": "/no-such-xyz-42",
                                     "recursive": True})
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


        fails = audit_get({"action": "fs.rm", "device": dev,
                           "result": "fail"})["events"]
        self.assertGreaterEqual(len(fails), 1)
        self.assertTrue(all(e["result"] == "fail" for e in fails))
        self.assertTrue(all(e["err"] for e in fails))
        self.assertEqual(fails[0]["target"]["path"], "/no-such-xyz-42")


        oks = audit_get({"action": "fs.rm", "device": dev,
                         "result": "ok"})["events"]
        self.assertGreaterEqual(len(oks), 1)
        self.assertTrue(all(e["result"] == "ok" for e in oks))


        all_rm = audit_get({"action": "fs.rm", "device": dev})["events"]
        self.assertTrue(all(e["action"] == "fs.rm" for e in all_rm))
        self.assertTrue(all(e["target"]["id"] == dev for e in all_rm))
        self.assertEqual(len(all_rm), len(fails) + len(oks))

        # limit / offset
        r = audit_get({"action": "fs.rm", "device": dev, "limit": "1"})
        self.assertEqual(len(r["events"]), 1)
        self.assertEqual(r["total"], len(all_rm))
        r2 = audit_get({"action": "fs.rm", "device": dev,
                        "limit": "1", "offset": "1"})
        self.assertEqual(r2["events"][0]["id"], all_rm[1]["id"])



    def test_06_stats_shape(self):
        st, r = http_get(BASE_URL + "/api/audit/stats?days=7")
        self.assertEqual(st, 200)
        data = r["data"]
        self.assertEqual(set(data.keys()), {"by_action", "by_day", "total"})
        self.assertEqual(len(data["by_day"]), 7)
        for e in data["by_day"]:
            self.assertEqual(set(e.keys()), {"date", "count"})
            datetime.datetime.strptime(e["date"], "%Y-%m-%d")
        self.assertGreater(data["total"], 0)
        self.assertEqual(sum(e["count"] for e in data["by_day"]),
                         data["total"])
        self.assertEqual(sum(data["by_action"].values()), data["total"])
        for key in ("exec.run", "exec.kill", "fs.mkdir", "fs.rm",
                    "dev.add", "dev.update", "dev.delete"):
            self.assertIn(key, data["by_action"])


        st, r = http_get(BASE_URL + "/api/audit/stats?days=1")
        self.assertEqual(len(r["data"]["by_day"]), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
