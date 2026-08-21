#!/usr/bin/env python3
"""Test module."""
import csv
import datetime
import io
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


class AuditUnitTest(unittest.TestCase):
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



    def test_01_empty_audit(self):
        r = audit_get({})
        self.assertTrue(r["ok"])
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["events"], [])

        st, r = http_get(BASE_URL + "/api/audit/stats?days=7")
        self.assertEqual(st, 200)
        data = r["data"]
        self.assertEqual(data["by_action"], {})
        self.assertEqual(data["total"], 0)
        self.assertEqual(len(data["by_day"]), 7)



    def test_04_device_crud_audited(self):
        st, r = http_post(BASE_URL + "/api/devices", {
            "name": "aud-crud", "type": "ssh", "host": "192.0.2.21"})
        self.assertEqual(st, 201)
        did = r["device"]["id"]

        adds = audit_get({"action": "dev.add"})["events"]
        self.assertEqual(adds[0]["target"]["id"], did)
        self.assertEqual(adds[0]["target"]["kind"], "device")
        self.assertEqual(adds[0]["result"], "ok")

        st, r = http_send("PUT", BASE_URL + "/api/devices/" + did,
                          {"name": "aud-crud-2"})
        self.assertEqual(st, 200)
        ups = audit_get({"action": "dev.update", "device": did})["events"]
        self.assertEqual(ups[0]["result"], "ok")
        self.assertEqual(ups[0]["detail"]["name"], "aud-crud-2")

        st, r = http_send("DELETE", BASE_URL + "/api/devices/" + did)
        self.assertEqual(st, 200)
        dels = audit_get({"action": "dev.delete", "device": did})["events"]
        self.assertEqual(dels[0]["result"], "ok")
        self.assertEqual(dels[0]["target"]["id"], did)


        try:
            http_send("PUT", BASE_URL + "/api/devices/no-such", {"name": "x"})
            self.fail("expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)
        fails = audit_get({"action": "dev.update", "device": "no-such"})["events"]
        self.assertEqual(fails[0]["result"], "fail")
        self.assertIn("设备不存在", fails[0]["err"])

    # ---- 7. export CSV ----

    def test_07_export_csv(self):
        req = urllib.request.Request(BASE_URL + "/api/audit/export")
        with urllib.request.urlopen(req, timeout=8) as resp:
            self.assertEqual(resp.status, 200)
            ctype = resp.headers.get("Content-Type") or ""
            self.assertTrue(ctype.startswith("text/csv"), ctype)
            cd = resp.headers.get("Content-Disposition") or ""
            self.assertIn("attachment", cd)
            self.assertIn("audit.csv", cd)
            body = resp.read().decode("utf-8")
        self.assertTrue(body.startswith("\ufeff"), "应带 UTF-8 BOM")
        rows = list(csv.reader(io.StringIO(body.lstrip("\ufeff"))))
        self.assertEqual(rows[0][:5], ["id", "ts", "actor", "ip", "action"])
        self.assertEqual(len(rows[0]), 11)
        self.assertGreater(len(rows), 1)
        self.assertEqual(len(rows[1]), 11)
        self.assertIn(rows[1][4],
                      ("exec.run", "exec.kill", "fs.mkdir", "fs.rm",
                       "dev.add", "dev.update", "dev.delete"))
        json.loads(rows[1][10])



    def test_08_cross_day_files(self):
        today = datetime.date.today()
        db = today - datetime.timedelta(days=2)
        y = today - datetime.timedelta(days=1)
        db_str, y_str = db.isoformat(), y.isoformat()

        def noon_ms(d):
            return int(datetime.datetime(d.year, d.month, d.day,
                                         12, 0, 0).timestamp() * 1000)

        def day_start_ms(d):
            return int(datetime.datetime(d.year, d.month, d.day)
                       .timestamp() * 1000)

        def day_end_ms(d):
            return int(datetime.datetime(d.year, d.month, d.day,
                                         23, 59, 59, 999000)
                       .timestamp() * 1000)

        os.makedirs(AUDIT_DIR, exist_ok=True)
        crafted = [
            (db_str, {"id": "craft-db-1", "ts": noon_ms(db),
                      "actor": "tester", "ip": "192.0.2.31",
                      "action": "fs.mkdir",
                      "target": {"kind": "file", "id": "local",
                                 "path": "/craft-db"},
                      "detail": {}, "result": "ok", "err": ""}),
            (y_str, {"id": "craft-y-1", "ts": noon_ms(y),
                     "actor": "tester", "ip": "192.0.2.32",
                     "action": "fs.rm",
                     "target": {"kind": "file", "id": "local",
                                "path": "/craft-y"},
                     "detail": {}, "result": "ok", "err": ""}),
        ]
        for date_s, ev in crafted:
            with open(os.path.join(AUDIT_DIR, date_s + ".jsonl"),
                      "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev) + "\n")


        r = audit_get({"from": str(day_start_ms(db)),
                       "to": str(day_end_ms(y))})
        self.assertGreaterEqual(r["total"], 2)
        actions = {e["action"] for e in r["events"]}
        self.assertTrue({"fs.mkdir", "fs.rm"} <= actions)


        r = audit_get({"from": str(day_start_ms(db)),
                       "to": str(day_end_ms(y)),
                       "action": "fs.mkdir", "device": "local"})
        self.assertEqual(len(r["events"]), 1)
        self.assertEqual(r["events"][0]["id"], "craft-db-1")
        r = audit_get({"from": str(day_start_ms(db)),
                       "to": str(day_end_ms(y)),
                       "action": "fs.rm", "device": "local"})
        self.assertEqual(len(r["events"]), 1)
        self.assertEqual(r["events"][0]["id"], "craft-y-1")


        r = audit_get({"from": str(day_start_ms(y)),
                       "to": str(day_end_ms(y))})
        self.assertTrue(all(e["ts"] >= day_start_ms(y) and
                            e["ts"] <= day_end_ms(y) for e in r["events"]))


        st, r = http_get(BASE_URL + "/api/audit/stats?days=7")
        counts = {e["date"]: e["count"] for e in r["data"]["by_day"]}
        self.assertGreaterEqual(counts[db_str], 1)
        self.assertGreaterEqual(counts[y_str], 1)


        today_file = os.path.join(AUDIT_DIR, today.isoformat() + ".jsonl")
        self.assertTrue(os.path.isfile(today_file))
        self.assertEqual(os.stat(today_file).st_mode & 0o777, 0o600)



    def test_09_recorder_perf_and_ring(self):
        from host.audit.recorder import AuditRecorder
        d1 = os.path.join(CONF_DIR, "unit-ring")
        os.makedirs(d1, exist_ok=True)
        rec = AuditRecorder(d1)
        t0 = time.time()
        for i in range(2000):
            rec.record_ok("fs.mkdir",
                          {"kind": "file", "id": "local", "path": "/p%d" % i})
        dur = time.time() - t0
        self.assertLess(dur, 2.0, "2000 条同步 append 平均应 <1ms，实际 %.1fms/条"
                        % (dur * 1000 / 2000))

        t0 = time.time()
        res = rec.query({"action": "fs.mkdir", "device": "local"})
        dur_ms = (time.time() - t0) * 1000
        self.assertEqual(len(res), 2000)
        self.assertTrue(all(e["result"] == "ok" for e in res))
        self.assertTrue(all(e["action"] == "fs.mkdir" for e in res))
        self.assertLess(dur_ms, 300, "2000 条查询耗时 %.1fms" % dur_ms)
        self.assertTrue(rec.close(timeout=5))

        rec2 = AuditRecorder(d1, max_ring=100)
        for i in range(150):
            rec2.record_ok("dev.add", {"kind": "device", "id": "x%d" % i})
        self.assertEqual(len(rec2._ring), 100)
        self.assertEqual(rec2._ring[0]["detail"], {})
        self.assertEqual(rec2._ring[-1]["target"]["id"], "x149")
        self.assertTrue(rec2.close(timeout=5))


        rec3 = AuditRecorder(d1)
        restored = rec3.query({"action": "dev.add"})
        self.assertEqual(len(restored), 150)
        self.assertTrue(rec3.close(timeout=5))



    def test_10_error_semantics(self):
        cases = [
            (BASE_URL + "/api/audit?from=2000&to=1000", None),
            (BASE_URL + "/api/audit?action=foo.bar", None),
            (BASE_URL + "/api/audit?result=maybe", None),
            (BASE_URL + "/api/audit?limit=abc", None),
            (BASE_URL + "/api/audit/export?from=2000&to=1000", None),
            (BASE_URL + "/api/audit/stats?days=abc", None),
            (BASE_URL + "/api/audit/stats?days=999", None),
        ]
        for url, _ in cases:
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    self.fail("expected 400 for %s -> %d" % (url, r.status))
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400, url)
                err = json.loads(exc.read().decode("utf-8"))
                self.assertFalse(err["ok"])
                self.assertTrue(err["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
