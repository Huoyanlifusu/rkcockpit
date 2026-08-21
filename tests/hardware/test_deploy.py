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
    fd = os.open(path, os.O_RDONLY)
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


class DeployHardwareTest(unittest.TestCase):
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



    def test_03_full_chain_upload_chmod(self):
        did, root = add_local_device("v3")
        a = os.path.join(CONF_DIR, "v3-a.bin")
        b = os.path.join(CONF_DIR, "v3-b.sh")
        with open(a, "wb") as fh:
            fh.write("hello-rkss-α-部署".encode("utf-8") * 64)
        with open(b, "wb") as fh:
            fh.write(b"#!/bin/sh\necho b\n")
        os.makedirs(os.path.join(root, "usr/bin/rkss-x"))
        os.makedirs(os.path.join(root, "opt/app"))
        st, r = http_post("%s/api/deploy/%s/plan" % (BASE_URL, did), {
            "files": [
                {"src": a, "dest": "/usr/bin/rkss-x/", "mode": "0755"},
                {"src": b, "dest": "/opt/app/b.sh", "mode": "0644"},
            ],
            "timeout": 60,
        })
        self.assertEqual(st, 200)
        plan_id = r["plan_id"]
        st, r = http_post("%s/api/deploy/%s/%s/start" % (BASE_URL, did,
                                                         plan_id))
        self.assertEqual(st, 200)
        job = r["job"]
        self.assertTrue(job["id"].startswith("p"))
        self.assertEqual(job["type"], "deploy")
        self.assertEqual(job["device_id"], did)
        self.assertEqual(job["state"], "running")

        job = wait_job(did, plan_id)
        self.assertEqual(job["state"], "done", job)
        self.assertIsNone(job["error"])
        self.assertEqual(job["result"]["exit_code"], None)

        total = os.path.getsize(a) + os.path.getsize(b)
        self.assertEqual(job["progress"]["bytes_total"], total)
        self.assertEqual(job["progress"]["bytes_done"], total)

        self.assertEqual([s["state"] for s in job["stages"]],
                         ["done", "done", "done", "done"])
        self.assertEqual(job["stages"][0]["file"], "v3-a.bin")
        self.assertEqual(job["stages"][1]["file"], "v3-a.bin")

        for k in ("created_ms", "started_ms", "ended_ms", "updated_ms"):
            self.assertGreater(job[k], 0, k)


        for p, expect in ((os.path.join(root, "usr/bin/rkss-x/v3-a.bin"),
                           open(a, "rb").read()),
                          (os.path.join(root, "opt/app/b.sh"),
                           open(b, "rb").read())):
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), expect)

        self.assertEqual(os.stat(os.path.join(
            root, "usr/bin/rkss-x/v3-a.bin")).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(os.path.join(
            root, "opt/app/b.sh")).st_mode & 0o777, 0o644)



    def test_04_cmd_exec_and_nonzero(self):
        did, root = add_local_device("v4")
        marker = "DEPLOY_EXEC_9f21"
        file_marker = "DEPLOY_FILE_3c7e"
        out_file = "/tmp/rkss-deploy-out-%d.txt" % int(time.time() * 1e6)
        script = ("#!/bin/sh\n"
                  "echo %s\n"
                  "echo %s > %s\n"
                  "cat %s\n") % (marker, file_marker, out_file, out_file)
        script_path = os.path.join(CONF_DIR, "v4-start.sh")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)
        os.makedirs(os.path.join(root, "usr/bin/rkss-x"))
        st, r = http_post("%s/api/deploy/%s/plan" % (BASE_URL, did), {
            "files": [{"src": script_path, "dest": "/usr/bin/rkss-x/start.sh",
                       "mode": "0755"}],
            "cmd": "sh %s/usr/bin/rkss-x/start.sh" % root,
            "timeout": 30,
        })
        self.assertEqual(st, 200)
        plan_id = r["plan_id"]
        http_post("%s/api/deploy/%s/%s/start" % (BASE_URL, did, plan_id))
        job = wait_job(did, plan_id)
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["result"]["exit_code"], 0)
        self.assertIn(marker, job["result"]["output_tail"])
        self.assertFalse(job["result"]["truncated"])
        with open(out_file, encoding="utf-8") as fh:
            self.assertIn(file_marker, fh.read())
        os.remove(out_file)


        src = os.path.join(CONF_DIR, "v4-tiny.bin")
        with open(src, "wb") as fh:
            fh.write(b"t")
        st, r = http_post("%s/api/deploy/%s/plan" % (BASE_URL, did), {
            "files": [{"src": src, "dest": "/usr/bin/rkss-x/t.bin",
                       "mode": "0644"}],
            "cmd": "exit 7",
            "timeout": 30,
        })
        plan_id = r["plan_id"]
        http_post("%s/api/deploy/%s/%s/start" % (BASE_URL, did, plan_id))
        job = wait_job(did, plan_id)
        self.assertEqual(job["state"], "error", job)
        self.assertEqual(job["result"]["exit_code"], 7)
        self.assertIn("exec 阶段失败", job["error"])
        self.assertIn("exit code 7", job["error"])
        exec_st = [s for s in job["stages"] if s["name"] == "exec"][0]
        self.assertEqual(exec_st["state"], "failed")
        self.assertEqual(exec_st["detail"], "exit 7")



    def test_05_failure_chain(self):
        did, root = add_local_device("v5")
        f1 = os.path.join(CONF_DIR, "v5-f1.bin")
        f2 = os.path.join(CONF_DIR, "v5-f2.bin")
        with open(f1, "wb") as fh:
            fh.write(b"A" * 1024)
        with open(f2, "wb") as fh:
            fh.write(b"B" * 1024)
        os.makedirs(os.path.join(root, "usr/bin/rkss-x"))


        st, r = http_post("%s/api/deploy/%s/plan" % (BASE_URL, did), {
            "files": [
                {"src": f1, "dest": "/usr/bin/rkss-x/f1", "mode": "0644"},
                {"src": f2, "dest": "/usr/bin/rkss-x/f2", "mode": "0644"},
            ]})
        self.assertEqual(st, 200)
        plan_id = r["plan_id"]
        os.remove(f2)
        code, r = http_error("POST",
                             "%s/api/deploy/%s/%s/start" % (BASE_URL, did,
                                                            plan_id))
        self.assertEqual(code, 400)
        self.assertIn("已被删除", r["error"])


        f1 = os.path.join(CONF_DIR, "v5b-f1.bin")
        f2 = os.path.join(CONF_DIR, "v5b-f2.bin")
        with open(f1, "wb") as fh:
            fh.write(b"Z" * 1024)
        with open(f2, "wb") as fh:
            fh.write(b"Y" * 1024)
        fifo = os.path.join(root, "usr/bin/rkss-x/fifo1")
        os.mkfifo(fifo)
        st, r = http_post("%s/api/deploy/%s/plan" % (BASE_URL, did), {
            "files": [
                {"src": f1, "dest": "/usr/bin/rkss-x/fifo1", "mode": "0644"},
                {"src": f2, "dest": "/usr/bin/rkss-x/f2", "mode": "0644"},
            ]})
        plan_id = r["plan_id"]
        http_post("%s/api/deploy/%s/%s/start" % (BASE_URL, did, plan_id))

        os.remove(f2)
        time.sleep(0.3)
        got = drain_fifo(fifo)
        self.assertEqual(got, b"Z" * 1024)
        job = wait_job(did, plan_id)
        self.assertEqual(job["state"], "error", job)
        self.assertIn("upload 阶段失败", job["error"])
        self.assertIn(f2, job["error"])

        states = [(s["name"], s["file"], s["state"]) for s in job["stages"]]
        self.assertEqual(states, [
            ("upload", "fifo1", "done"),
            ("chmod", "fifo1", "done"),
            ("upload", "f2", "failed"),
            ("chmod", "f2", "failed"),
        ])
        self.assertEqual(job["stages"][3]["detail"],
                         "因前一阶段失败未执行")

        self.assertTrue(os.path.exists(
            os.path.join(root, "usr/bin/rkss-x/fifo1")))



    def test_06_cancel_mid_upload(self):
        did, root = add_local_device("v6")
        src = os.path.join(CONF_DIR, "v6-src.bin")
        with open(src, "wb") as fh:
            fh.write(b"C" * 2048)
        os.makedirs(os.path.join(root, "tmp"))
        fifo = os.path.join(root, "tmp/block.fifo")
        os.mkfifo(fifo)
        st, r = http_post("%s/api/deploy/%s/plan" % (BASE_URL, did), {
            "files": [{"src": src, "dest": "/tmp/block.fifo",
                       "mode": "0644"}]})
        self.assertEqual(st, 200)
        plan_id = r["plan_id"]
        http_post("%s/api/deploy/%s/%s/start" % (BASE_URL, did, plan_id))
        time.sleep(0.3)
        st, r = http_post("%s/api/deploy/%s/%s/cancel" % (BASE_URL, did,
                                                          plan_id))
        self.assertEqual(st, 200)
        self.assertEqual(r, {"ok": True, "cancelled": True})
        self.assertEqual(drain_fifo(fifo), b"")
        job = wait_job(did, plan_id)
        self.assertEqual(job["state"], "cancelled", job)
        self.assertEqual(job["error"], "已取消")

        self.assertEqual(job["stages"][0]["state"], "failed")
        self.assertEqual(job["stages"][0]["detail"], "任务已取消")
        for s in job["stages"][1:]:
            self.assertEqual(s["state"], "skipped", s)
            self.assertEqual(s["detail"], "已取消")


        st, r = http_post("%s/api/deploy/%s/%s/cancel" % (BASE_URL, did,
                                                          plan_id))
        self.assertEqual(st, 200)
        self.assertEqual(r, {"ok": True, "cancelled": False})



    def test_07_audit_records(self):
        did, root = add_local_device("v7")
        src = os.path.join(CONF_DIR, "v7-a.bin")
        with open(src, "wb") as fh:
            fh.write(b"aud" * 32)
        os.makedirs(os.path.join(root, "usr/bin/rkss-x"))
        base = "%s/api/deploy/%s" % (BASE_URL, did)


        st, r = http_post(base + "/plan", {
            "files": [{"src": src, "dest": "/usr/bin/rkss-x/a.bin",
                       "mode": "0755"}],
            "cmd": "exit 5"})
        fail_plan = r["plan_id"]
        http_post(base + "/%s/start" % fail_plan)
        job = wait_job(did, fail_plan)
        self.assertEqual(job["state"], "error")


        st, r = http_post(base + "/plan", {
            "files": [{"src": src, "dest": "/usr/bin/rkss-x/b.bin",
                       "mode": "0644"}]})
        ok_plan = r["plan_id"]
        http_post(base + "/%s/start" % ok_plan)
        job = wait_job(did, ok_plan)
        self.assertEqual(job["state"], "done")


        def audit(action, device=None):
            q = "action=%s" % action
            if device:
                q += "&device=%s" % device
            st, r = http_get("%s/api/audit?%s" % (BASE_URL, q))
            self.assertEqual(st, 200)
            return r["events"]

        starts = audit("deploy.start", did)
        self.assertGreaterEqual(len(starts), 2)
        for ev in starts:
            self.assertEqual(ev["action"], "deploy.start")
            self.assertEqual(ev["target"]["kind"], "deploy")
            self.assertEqual(ev["target"]["id"], did)
            self.assertIn("plan_id", ev["detail"])
            self.assertIn("files", ev["detail"])
        ok_ev = next(e for e in starts if e["detail"]["plan_id"] == ok_plan)
        self.assertEqual(ok_ev["result"], "ok")
        self.assertEqual(ok_ev["detail"]["files"], 1)
        fail_ev = next(e for e in starts
                       if e["detail"]["plan_id"] == fail_plan)
        self.assertEqual(fail_ev["result"], "fail")
        self.assertIn("exec 阶段失败", fail_ev["err"])


        stages = audit("deploy.stage", did)
        self.assertGreaterEqual(len(stages), 5)
        up_ok = next(e for e in stages
                     if e["detail"]["stage"] == "upload" and
                     e["detail"]["plan_id"] == ok_plan)
        self.assertEqual(up_ok["result"], "ok")
        self.assertEqual(up_ok["detail"]["file"], "b.bin")
        self.assertEqual(up_ok["detail"]["rc"], None)
        exec_fail = next(e for e in stages
                         if e["detail"]["stage"] == "exec" and
                         e["detail"]["plan_id"] == fail_plan)
        self.assertEqual(exec_fail["result"], "fail")
        self.assertEqual(exec_fail["detail"]["rc"], 5)
        self.assertIn("exit 5", exec_fail["err"])



    def test_09_restart_stub_and_running_guard(self):
        did, root = add_local_device("v9")
        src = os.path.join(CONF_DIR, "v9-a.bin")
        with open(src, "wb") as fh:
            fh.write(b"R" * 512)
        os.makedirs(os.path.join(root, "usr/bin/rkss-x"))
        base = "%s/api/deploy/%s" % (BASE_URL, did)
        st, r = http_post(base + "/plan", {
            "files": [{"src": src, "dest": "/usr/bin/rkss-x/a.bin",
                       "mode": "0755"}],
            "restart": True})
        self.assertEqual(st, 200)
        plan_id = r["plan_id"]
        http_post(base + "/%s/start" % plan_id)
        job = wait_job(did, plan_id)
        self.assertEqual(job["state"], "done", job)
        rest = [s for s in job["stages"] if s["name"] == "restart"]
        self.assertEqual(len(rest), 1)
        self.assertEqual(rest[0]["state"], "skipped")
        self.assertIn("stub", rest[0]["detail"])


        st, r = http_post(base + "/%s/start" % plan_id)
        self.assertEqual(st, 200)
        job = wait_job(did, plan_id)
        self.assertEqual(job["state"], "done")


        os.makedirs(os.path.join(root, "tmp"))
        fifo = os.path.join(root, "tmp/guard.fifo")
        os.mkfifo(fifo)
        st, r = http_post(base + "/plan", {
            "files": [{"src": src, "dest": "/tmp/guard.fifo",
                       "mode": "0644"}]})
        plan_id = r["plan_id"]
        http_post(base + "/%s/start" % plan_id)
        time.sleep(0.3)
        code, r = http_error("POST", base + "/%s/start" % plan_id)
        self.assertEqual(code, 400)
        self.assertIn("正在执行", r["error"])
        http_post(base + "/%s/cancel" % plan_id)
        self.assertEqual(drain_fifo(fifo), b"")
        job = wait_job(did, plan_id)
        self.assertEqual(job["state"], "cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
