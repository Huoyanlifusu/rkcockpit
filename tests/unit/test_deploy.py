#!/usr/bin/env python3
"""Test module."""
import json
import os
import select
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
from host.transport import TransportError
PY = sys.executable
CONF_DIR = tempfile.mkdtemp(prefix="rkss-p0-deploy-")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100


PORTAL = _free_port()
BASE_URL = "http://127.0.0.1:%d" % PORTAL


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_post(url, body=None, timeout=8):
    req = urllib.request.Request(
        url, data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_error(method, url, body=None, timeout=8):
    """Test helper."""
    req = urllib.request.Request(
        url, data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raise AssertionError("expected HTTP error, got %d" % r.status)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


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
    def __init__(self, args, logfile=None):
        if logfile:
            self._log = open(logfile, "w")
        else:
            self._log = subprocess.DEVNULL
        self.p = subprocess.Popen(args, cwd=BASE,
                                  stdout=self._log,
                                  stderr=subprocess.STDOUT,
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
    root = os.path.join(CONF_DIR, "remote-" + name)
    st, r = http_post(BASE_URL + "/api/devices", {
        "name": name, "type": "local", "local_root": root})
    assert st == 201, r
    return r["device"]["id"], root


def wait_job(did, plan_id, timeout=30):
    url = "%s/api/deploy/%s/%s" % (BASE_URL, did, plan_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, r = http_get(url)
        assert st == 200, r
        job = r["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.1)
    raise AssertionError("deploy job 超时（>%ss）: %s" % (timeout, plan_id))


def drain_fifo(path):
    """Test helper."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    ready, _, _ = select.select([fd], [], [], 10)
    if not ready:
        raise AssertionError("FIFO writer never arrived")
    buf = b""
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
    finally:
        os.close(fd)
    return buf


class DeployUnitTest(unittest.TestCase):
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



    def test_01_plan_validation(self):
        did, root = add_local_device("v1")
        src = os.path.join(CONF_DIR, "v1-ok.bin")
        with open(src, "wb") as fh:
            fh.write(b"x" * 16)
        url = "%s/api/deploy/%s/plan" % (BASE_URL, did)
        ok_files = [{"src": src, "dest": "/usr/bin/rkss-x/a.bin",
                     "mode": "0755"}]


        code, r = http_error("POST", "%s/api/deploy/no-such/plan"
                             % BASE_URL, ok_files)
        self.assertEqual(code, 404)
        self.assertIn("设备不存在", r["error"])


        bad = [dict(ok_files[0], src="/no/such/src-file.bin")]
        code, r = http_error("POST", url, {"files": bad})
        self.assertEqual(code, 400)
        self.assertIn("src 不存在", r["error"])


        for m in ("abc", "999", "0755x", "12345", ""):
            bad = [dict(ok_files[0], mode=m)]
            code, r = http_error("POST", url, {"files": bad})
            self.assertEqual(code, 400, "mode=%r" % m)
            self.assertIn("mode 非法", r["error"])


        for d in ("/", "/etc/x", "/etc", "/usr/x", "/usr/local",
                  "/bin/x", "/sbin/x", "/lib/x", "/var/x", "rel/path"):
            bad = [dict(ok_files[0], dest=d)]
            code, r = http_error("POST", url, {"files": bad})
            self.assertEqual(code, 400, "dest=%r" % d)
            self.assertIn("危险 dest", r["error"])


        for d in ("/usr/bin/rkss-x/", "/usr/local/bin/x",
                  "/opt", "/home/u", "/data/d", "/userdata/d",
                  "/tmp/d", "/root/d"):
            st, r = http_post(url, {"files": [dict(ok_files[0], dest=d)]})
            self.assertEqual(st, 200, "dest=%r -> %r" % (d, r))


        for t in (0, -1, 3601, "abc"):
            code, r = http_error("POST", url,
                                 {"files": ok_files, "timeout": t})
            self.assertEqual(code, 400, "timeout=%r" % t)
        code, r = http_error("POST", url, {"files": []})
        self.assertEqual(code, 400)
        code, r = http_error("POST", url, {})
        self.assertEqual(code, 400)


        st, r = http_post(url, {"files": ok_files, "timeout": 60})
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertTrue(r["plan_id"].startswith("p"))



    def test_02_missing_plan_404(self):
        did, root = add_local_device("v2")
        base = "%s/api/deploy/%s" % (BASE_URL, did)
        for path, method, body in (
                (base + "/p999/start", "POST", {}),
                (base + "/p999", "GET", None),
                (base + "/p999/cancel", "POST", {})):
            if method == "GET":
                req = urllib.request.Request(path, method="GET")
            else:
                req = urllib.request.Request(
                    path, data=json.dumps(body or {}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method=method)
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    self.fail("expected 404 for %s" % path)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404, path)
                self.assertFalse(json.loads(exc.read())[
                    "ok"])

        code, r = http_error("POST", "%s/api/deploy/no-such/p999/start"
                             % BASE_URL)
        self.assertEqual(code, 404)



    def test_08_adb_chmod_skipped(self):
        from host.audit.recorder import AuditRecorder
        from host.task.deployjob import DeployJobStore

        class FakeAdbTransport:
            kind = "adb"

            def __init__(self):
                self.uploads = []

            def upload(self, fh, remote, size_hint=0, job=None):
                data = fh.read(1 << 16)
                self.uploads.append((remote, len(data)))
                return len(data)

            def chmod(self, path, mode):
                raise TransportError("adb 不支持 chmod")

        class FakeHost:
            def __init__(self, dev, transport, audit):
                self._dev = dev
                self._t = transport
                self.audit = audit

            def _device(self, did):
                if did != self._dev["id"]:
                    raise KeyError(did)
                return self._dev

            def _transport(self, did):
                return self._t

            def _exec_store(self, did):
                raise AssertionError("不应执行 exec")

        dev = {"id": "adb01", "name": "fake-adb", "type": "adb",
               "host": "127.0.0.1"}
        t = FakeAdbTransport()
        audit_dir = os.path.join(CONF_DIR, "unit-adb")
        rec = AuditRecorder(audit_dir)
        store = DeployJobStore(FakeHost(dev, t, rec))
        src = os.path.join(CONF_DIR, "v8-src.bin")
        with open(src, "wb") as fh:
            fh.write(b"adb" * 100)
        r = store.plan("adb01", [{"src": src, "dest": "/data/rkss/x.bin",
                                  "mode": "0755"}])
        plan_id = r["plan_id"]
        job = store.start(plan_id)
        deadline = time.time() + 15
        while time.time() < deadline:
            job = store.get(plan_id)
            if job["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(t.uploads, [("/data/rkss/x.bin", 300)])
        chmod_st = [s for s in job["stages"] if s["name"] == "chmod"][0]
        self.assertEqual(chmod_st["state"], "skipped")
        self.assertIn("不支持", chmod_st["detail"])

        evs = rec.query({"action": "deploy.stage"})
        self.assertGreaterEqual(len(evs), 2)
        self.assertTrue(all(e["result"] == "ok" for e in evs))
        self.assertEqual(evs[0]["target"]["kind"], "deploy")

if __name__ == "__main__":
    unittest.main(verbosity=2)
