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


class GroupsLogcenterUnitTest(unittest.TestCase):
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



    def test_01_groups_crud(self):
        st, r = http_post(BASE_URL + "/api/groups",
                          {"name": "t1-cam", "device_ids": ["demo"]})
        self.assertEqual(st, 201)
        self.assertTrue(r["ok"])
        self.assertEqual(r["group"]["device_ids"], ["demo"])
        self.assertIn("created_at", r["group"])

        st, r = http_get(BASE_URL + "/api/groups")
        self.assertEqual(st, 200)
        self.assertTrue(any(g["name"] == "t1-cam" for g in r["groups"]))

        st, r = http_send("PUT", BASE_URL + "/api/groups/t1-cam",
                          {"device_ids": []})
        self.assertEqual(st, 200)
        self.assertEqual(r["group"]["device_ids"], [])

        st, r = http_send("PUT", BASE_URL + "/api/groups/t1-cam",
                          {"device_ids": ["demo"]})
        self.assertEqual(st, 200)
        self.assertEqual(r["group"]["device_ids"], ["demo"])

        st, r = http_send("DELETE", BASE_URL + "/api/groups/t1-cam")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["deleted"], "t1-cam")
        st, r = http_get(BASE_URL + "/api/groups")
        self.assertFalse(any(g["name"] == "t1-cam" for g in r["groups"]))

    def test_02_duplicate_and_404(self):
        http_post(BASE_URL + "/api/groups", {"name": "t2-g1"})
        try:
            http_post(BASE_URL + "/api/groups", {"name": "t2-g1"})
            self.fail("expected 400 for duplicate name")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

        try:
            http_post(BASE_URL + "/api/groups",
                      {"name": "t2-g2", "device_ids": "demo"})
            self.fail("expected 400 for non-array device_ids")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

        for method, path in (
                ("GET", "/api/groups/t2-nope"),
                ("PUT", "/api/groups/t2-nope"),
                ("DELETE", "/api/groups/t2-nope"),
                ("POST", "/api/groups/t2-nope/exec")):
            try:
                if method == "GET":
                    http_get(BASE_URL + path)
                elif method == "POST":
                    http_post(BASE_URL + path, {"cmd": "echo hi"})
                else:
                    http_send(method, BASE_URL + path, {})
                self.fail("expected 404 for %s %s" % (method, path))
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404, "%s %s" % (method, path))

    def test_03_delete_device_removes_from_group(self):
        did = add_local_device("t3-del-me")
        http_post(BASE_URL + "/api/groups",
                  {"name": "t3-g", "device_ids": [did, "demo"]})
        st, r = http_send("DELETE", BASE_URL + "/api/devices/" + did)
        self.assertEqual(st, 200)
        st, r = http_get(BASE_URL + "/api/groups")
        g = [x for x in r["groups"] if x["name"] == "t3-g"][0]
        self.assertNotIn(did, g["device_ids"])
        self.assertIn("demo", g["device_ids"])

    def test_05_put_filters_unknown_device(self):
        st, r = http_post(BASE_URL + "/api/groups",
                          {"name": "t5-g", "device_ids": ["demo"]})
        self.assertEqual(st, 201)
        st, r = http_send("PUT", BASE_URL + "/api/groups/t5-g",
                          {"device_ids": ["demo", "no-such-xyz"]})
        self.assertEqual(st, 200)
        self.assertEqual(r["group"]["device_ids"], ["demo"])
        self.assertEqual(r["skipped"], ["no-such-xyz"])

        st, r = http_post(BASE_URL + "/api/groups",
                          {"name": "t5-g2", "device_ids": ["no-such-xyz"]})
        self.assertEqual(st, 201)
        self.assertEqual(r["group"]["device_ids"], [])
        self.assertEqual(r["skipped"], ["no-such-xyz"])

    def test_06_devices_filter_by_group(self):
        did = add_local_device("t6-local")
        http_post(BASE_URL + "/api/groups",
                  {"name": "t6-g", "device_ids": [did]})
        st, r = http_get(BASE_URL + "/api/devices?group=t6-g")
        self.assertEqual(st, 200)
        ids = {d["id"] for d in r["devices"]}
        self.assertEqual(ids, {did})

        st, r = http_get(BASE_URL + "/api/devices?group=t6-nope")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["devices"], [])

        st, all_r = http_get(BASE_URL + "/api/devices")
        st, all_r2 = http_get(BASE_URL + "/api/devices")
        self.assertEqual(st, 200)
        self.assertTrue(all_r["ok"])
        self.assertEqual(all_r, all_r2, "无参调用结果必须一致")
        all_ids = {d["id"] for d in all_r["devices"]}
        self.assertIn("demo", all_ids)
        self.assertIn(did, all_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
