#!/usr/bin/env python3

import hashlib
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
CONF_DIR = tempfile.mkdtemp(prefix="rkss-host-test-")


os.environ["HOME"] = tempfile.mkdtemp(prefix="rkss-test-home-")
HOME = os.path.expanduser("~")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100


PORTAL = _free_port()


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


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def wait_job_done(job_id, timeout=15):
    """Test helper."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        st, r = http_get("http://127.0.0.1:%d/api/jobs" % PORTAL)
        for j in r["jobs"]:
            if j["id"] == job_id:
                last = j
                if j["status"] in ("done", "error", "cancelled"):
                    return j
        time.sleep(0.3)
    return last


class HostApiHardwareTest(unittest.TestCase):
    portal = None
    demo_dir = None
    dev_id = None

    @classmethod
    def setUpClass(cls):
        cls.portal = Proc([PY, "-m", "portal.portal", "--port", str(PORTAL),
                           "--bind", "127.0.0.1", "--sim",
                           "--conf-dir", CONF_DIR])
        assert wait_port(PORTAL), "portal not up"
        st, r = http_post("http://127.0.0.1:%d/api/devices" % PORTAL, {
            "name": "test-local", "type": "local",
            "local_root": os.path.join(CONF_DIR, "test-remote")})
        assert st == 201, r
        cls.dev_id = r["device"]["id"]
        cls.demo_dir = os.path.join(CONF_DIR, "demo-remote")

    @classmethod
    def tearDownClass(cls):
        cls.portal.stop()
        shutil.rmtree(CONF_DIR, ignore_errors=True)



    def test_01_device_crud(self):
        base = "http://127.0.0.1:%d/api/devices" % PORTAL
        st, r = http_post(base, {"name": "x", "type": "ssh",
                                 "host": "192.0.2.11", "port": 22,
                                 "user": "root", "auth": "key",
                                 "password": "secret123"})
        self.assertEqual(st, 201)
        d = r["device"]
        self.assertIn("id", d)
        self.assertTrue(d["has_password"])
        self.assertNotIn("secret123", json.dumps(d))

        st, r = http_send("PUT", base + "/" + d["id"], {"name": "y"})
        self.assertEqual(st, 200)
        self.assertEqual(r["device"]["name"], "y")

        st, r = http_get(base)
        self.assertEqual(st, 200)
        self.assertGreaterEqual(len(r["devices"]), 2)

        st, r = http_send("DELETE", base + "/" + d["id"])
        self.assertEqual(st, 200)
        try:
            http_send("DELETE", base + "/" + d["id"])
            self.fail("expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_02_device_validation(self):
        base = "http://127.0.0.1:%d/api/devices" % PORTAL
        for bad in ({"name": "x"}, {"type": "ftp"},
                    {"type": "ssh", "name": "x"}, {"type": "adb", "name": "x"}):
            try:
                http_post(base, bad)
                self.fail("expected 400 for %r" % bad)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)

    def test_03_check_and_sysinfo(self):
        url = "http://127.0.0.1:%d/api/devices/%s" % (PORTAL, self.dev_id)
        st, r = http_post(url + "/check")
        self.assertEqual(st, 200)
        self.assertEqual(r["state"], "online")
        self.assertEqual(r["info"]["hostname"], socket.gethostname())

        st, r = http_post(url + "/sysinfo")
        self.assertEqual(st, 200)
        d = r["data"]
        for key in ("uptime_s", "os", "kernel", "cpu_usage", "mem_total_mb",
                    "mem_used_mb", "temp_c", "disks", "load"):
            self.assertIn(key, d)

        st, r = http_get("http://127.0.0.1:%d/api/devices" % PORTAL)
        dev = next(x for x in r["devices"] if x["id"] == self.dev_id)
        self.assertEqual(dev["state"], "online")



    def test_04_fs_lifecycle(self):
        base = "http://127.0.0.1:%d/api/fs/%s" % (PORTAL, self.dev_id)
        st, r = http_post(base + "/mkdir", {"path": "/d1/d2"})
        self.assertEqual(st, 200)
        st, r = http_get(base + "/list?path=/d1")
        self.assertTrue(any(e["name"] == "d2" and e["is_dir"] for e in r["entries"]))

        st, r = http_post(base + "/rename", {"path": "/d1/d2", "new_name": "d3"})
        self.assertEqual(st, 200)
        st, r = http_get(base + "/list?path=/d1")
        self.assertTrue(any(e["name"] == "d3" for e in r["entries"]))

        st, r = http_post(base + "/chmod", {"path": "/d1/d3", "mode": "0750"})
        self.assertEqual(st, 200)

        st, r = http_post(base + "/mv", {"path": "/d1/d3", "dest": "/d3"})
        self.assertEqual(st, 200)

        st, r = http_post(base + "/rm", {"path": "/d1", "recursive": True})
        self.assertEqual(st, 200)
        st, r = http_get(base + "/list?path=/")
        self.assertTrue(any(e["name"] == "d3" for e in r["entries"]))
        http_post(base + "/rm", {"path": "/d3", "recursive": True})

    def test_05_copy_roundtrip(self):
        base = "http://127.0.0.1:%d/api/fs/%s" % (PORTAL, self.dev_id)
        src = os.path.join(CONF_DIR, "src.bin")
        with open(src, "wb") as fh:
            fh.write(os.urandom(300000))
        back = os.path.join(HOME, ".rkss-test-back.bin")
        try:
            st, r = http_post(base + "/copy", {"src": src, "dest": "/src.bin"})
            self.assertEqual(st, 200)
            up = wait_job_done(r["job"])
            self.assertEqual(up["status"], "done", up.get("error"))
            st, r = http_post(base + "/copyfrom",
                              {"src": "/src.bin", "dest": back})
            self.assertEqual(st, 200)
            down = wait_job_done(r["job"])
            self.assertEqual(down["status"], "done", down.get("error"))
            self.assertEqual(down["bytes_total"], os.path.getsize(src))
            self.assertEqual(md5(src), md5(back))
            http_post(base + "/rm", {"path": "/src.bin"})
        finally:
            try:
                os.remove(back)
            except OSError:
                pass

    def test_06_raw_upload_download(self):
        base = "http://127.0.0.1:%d/api/fs/%s" % (PORTAL, self.dev_id)
        payload = os.urandom(70000)
        st, r = http_post(base + "/upload?path=/&name=raw.bin", raw=payload)
        self.assertEqual(st, 200)
        self.assertEqual(r["size"], len(payload))
        job = wait_job_done(r["job"])
        self.assertEqual(job["status"], "done", job.get("error"))
        with urllib.request.urlopen(base + "/download?path=/raw.bin",
                                    timeout=8) as resp:
            data = resp.read()
        self.assertEqual(hashlib.md5(data).hexdigest(),
                         hashlib.md5(payload).hexdigest())
        http_post(base + "/rm", {"path": "/raw.bin"})

    def test_07_local_fs_and_danger(self):
        base = "http://127.0.0.1:%d/api/fs/local" % PORTAL
        st, r = http_get(base + "/list?path=~")
        self.assertEqual(st, 200)
        self.assertIsInstance(r["entries"], list)
        try:
            http_post(base + "/rm", {"path": "/", "recursive": True})
            self.fail("expected rejection")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_08_copyfrom_outside_home_rejected(self):
        base = "http://127.0.0.1:%d/api/fs/%s" % (PORTAL, self.dev_id)
        http_post(base + "/mkdir", {"path": "/t"})
        try:
            http_post(base + "/copyfrom", {"src": "/t", "dest": "/etc/rkss-x"})
            self.fail("expected rejection")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        http_post(base + "/rm", {"path": "/t"})

    # ---- 6/7. exec ----

    def test_09_exec_run_poll(self):
        url = "http://127.0.0.1:%d/api/exec/%s" % (PORTAL, self.dev_id)
        st, r = http_post(url + "/run", {"cmd": "echo hi-v2; uname -m"})
        self.assertEqual(st, 200)
        jid = r["job_id"]
        deadline = time.time() + 10
        out = ""
        while time.time() < deadline:
            st, p = http_get(url + "/poll?job_id=" + jid)
            out = p["output"]
            if not p["running"]:
                break
            time.sleep(0.3)
        self.assertIn("hi-v2", out)
        self.assertEqual(p["exit_code"], 0)

    def test_10_exec_kill(self):
        url = "http://127.0.0.1:%d/api/exec/%s" % (PORTAL, self.dev_id)
        st, r = http_post(url + "/run", {"cmd": "sleep 60"})
        jid = r["job_id"]
        time.sleep(0.4)
        st, r = http_post(url + "/kill", {"job_id": jid})
        self.assertEqual(st, 200)
        deadline = time.time() + 10
        while time.time() < deadline:
            st, p = http_get(url + "/poll?job_id=" + jid)
            if not p["running"]:
                break
            time.sleep(0.3)
        self.assertFalse(p["running"])

    def test_11_exec_concurrency_limit(self):
        url = "http://127.0.0.1:%d/api/exec/%s" % (PORTAL, self.dev_id)
        ids = []
        for _ in range(8):
            st, r = http_post(url + "/run", {"cmd": "sleep 20"})
            self.assertEqual(st, 200)
            ids.append(r["job_id"])
        try:
            http_post(url + "/run", {"cmd": "echo over"})
            self.fail("expected 429")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 429)
        for jid in ids:
            http_post(url + "/kill", {"job_id": jid})

    def test_12_demo_device_present(self):
        st, r = http_get("http://127.0.0.1:%d/api/devices" % PORTAL)
        self.assertTrue(any(d["id"] == "demo" for d in r["devices"]))
        st, r = http_get("http://127.0.0.1:%d/api/fs/demo/list?path=/" % PORTAL)
        self.assertEqual(st, 200)

    def test_13_host_env(self):
        st, r = http_get("http://127.0.0.1:%d/api/host" % PORTAL)
        self.assertEqual(st, 200)
        for key in ("name", "python", "has_sshpass", "has_adb",
                    "conf_dir", "version"):
            self.assertIn(key, r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
