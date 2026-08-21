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

PY = sys.executable
CONF_DIR = tempfile.mkdtemp(prefix="rkss-keys-test-")
KEYS_DIR = os.path.join(CONF_DIR, "keys")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100


PORTAL = _free_port()
BASE_URL = "http://127.0.0.1:%d" % PORTAL


def http_get(url, timeout=8, text=False):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return r.status, (raw if text else json.loads(raw))


def http_send(method, url, body=None, timeout=8):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_post(url, body=None, timeout=8):
    return http_send("POST", url, body, timeout)


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


class P1KeysUnitTest(unittest.TestCase):
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

    def _gen(self, name="k1", typ="ed25519", comment="unit"):
        st, r = http_post(BASE_URL + "/api/keys/generate",
                          {"name": name, "type": typ, "comment": comment})
        self.assertEqual(st, 201, r)
        return r["key"]

    def _add_dev(self, name="key-dev", typ="ssh"):
        st, r = http_post(BASE_URL + "/api/devices",
                          {"name": name, "type": typ,
                           "host": "192.0.2.41" if typ != "local" else ""})
        self.assertEqual(st, 201, r)
        return r["device"]["id"]



    def test_01_generate_ed25519_list_and_no_private_key(self):
        key = self._gen("k-ed", "ed25519", "board-a")
        self.assertTrue(key["id"])
        self.assertEqual(key["type"], "ed25519")
        self.assertTrue(key["fingerprint"].startswith("SHA256:"),
                        key["fingerprint"])
        self.assertIn("ssh-ed25519", key["public"])

        st, r = http_get(BASE_URL + "/api/keys")
        self.assertEqual(st, 200)
        ids = [k["id"] for k in r["keys"]]
        self.assertIn(key["id"], ids)
        hit = next(k for k in r["keys"] if k["id"] == key["id"])
        self.assertEqual(hit["name"], "k-ed")
        self.assertEqual(hit["fingerprint"], key["fingerprint"])


        st, text = http_get(BASE_URL + "/api/keys", text=True)
        self.assertEqual(st, 200)
        self.assertNotIn("PRIVATE KEY", text)



    def test_02_key_file_permissions(self):
        key = self._gen("k-perm", "ed25519")
        priv = os.path.join(KEYS_DIR, key["id"], "id_ed25519")
        pub = priv + ".pub"
        self.assertTrue(os.path.isfile(priv))
        self.assertTrue(os.path.isfile(pub))
        self.assertEqual(os.stat(priv).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(pub).st_mode & 0o777, 0o644)

        self.assertEqual(os.stat(os.path.join(KEYS_DIR, "keys.json"))
                         .st_mode & 0o777, 0o600)



    def test_03_rsa_and_invalid_type(self):
        key = self._gen("k-rsa", "rsa", "rsa-key")
        self.assertEqual(key["type"], "rsa")
        self.assertTrue(key["fingerprint"].startswith("SHA256:"), key["fingerprint"])
        self.assertIn("ssh-rsa", key["public"])
        self.assertTrue(os.path.isfile(os.path.join(
            KEYS_DIR, key["id"], "id_rsa")))

        try:
            http_post(BASE_URL + "/api/keys/generate",
                      {"name": "bad", "type": "dsa"})
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            err = json.loads(exc.read().decode("utf-8"))
            self.assertFalse(err["ok"])
            self.assertIn("ed25519|rsa", err["error"])



    def test_04_delete_free_and_referenced(self):
        free_key = self._gen("k-free", "ed25519")
        st, r = http_send("DELETE", BASE_URL + "/api/keys/" + free_key["id"])
        self.assertEqual(st, 200)
        self.assertEqual(r["deleted"], free_key["id"])
        st, r = http_get(BASE_URL + "/api/keys")
        self.assertNotIn(free_key["id"], [k["id"] for k in r["keys"]])
        self.assertFalse(os.path.isdir(os.path.join(KEYS_DIR, free_key["id"])))

        ref_key = self._gen("k-ref", "ed25519")
        dev = self._add_dev("ref-dev")
        st, r = http_send("PUT", BASE_URL + "/api/devices/" + dev,
                          {"key_ref": ref_key["id"]})
        self.assertEqual(st, 200, r)
        try:
            http_send("DELETE", BASE_URL + "/api/keys/" + ref_key["id"])
            self.fail("expected 409")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409)
            err = json.loads(exc.read().decode("utf-8"))
            self.assertFalse(err["ok"])
            self.assertIn("引用", err["error"])
            self.assertIn("ref-dev", err["error"])

        st, r = http_send("PUT", BASE_URL + "/api/devices/" + dev,
                          {"key_ref": ""})
        self.assertEqual(st, 200)
        st, r = http_send("DELETE", BASE_URL + "/api/keys/" + ref_key["id"])
        self.assertEqual(st, 200)



    def test_05_device_key_ref_validation(self):
        key = self._gen("k-devref", "ed25519")
        dev = self._add_dev("devref-dev")
        st, r = http_send("PUT", BASE_URL + "/api/devices/" + dev,
                          {"key_ref": key["id"]})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["device"]["key_ref"], key["id"])

        st, r = http_get(BASE_URL + "/api/devices")
        hit = next(d for d in r["devices"] if d["id"] == dev)
        self.assertEqual(hit["key_ref"], key["id"])

        st, r = http_post(BASE_URL + "/api/devices",
                          {"name": "devref-add", "type": "ssh",
                           "host": "192.0.2.42", "key_ref": key["id"]})
        self.assertEqual(st, 201, r)
        self.assertEqual(r["device"]["key_ref"], key["id"])

        try:
            http_send("PUT", BASE_URL + "/api/devices/" + dev,
                      {"key_ref": "no-such-key"})
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            err = json.loads(exc.read().decode("utf-8"))
            self.assertFalse(err["ok"])
            self.assertIn("no-such-key", err["error"])



    def test_06_install_local_fail_and_missing_device(self):
        key = self._gen("k-inst", "ed25519")
        loc = self._add_dev("loc-dev", "local")

        st, r = http_post(BASE_URL + "/api/keys/" + key["id"] + "/install",
                          {"device_id": loc})
        self.assertEqual(st, 200, r)
        self.assertFalse(r["ok"])
        self.assertIn("local", r["error"])

        adb = self._add_dev("adb-dev", "adb")
        st, r = http_post(BASE_URL + "/api/keys/" + key["id"] + "/install",
                          {"device_id": adb})
        self.assertEqual(st, 200, r)
        self.assertFalse(r["ok"])
        self.assertIn("adb", r["error"])

        try:
            http_post(BASE_URL + "/api/keys/" + key["id"] + "/install",
                      {"device_id": "no-such-dev"})
            self.fail("expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

        try:
            http_post(BASE_URL + "/api/keys/no-such-key/install",
                      {"device_id": "demo"})
            self.fail("expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

        try:
            http_post(BASE_URL + "/api/keys/" + key["id"] + "/install", {})
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)



    def test_07_keys_audited(self):
        key = self._gen("k-audit", "ed25519", "audit-key")
        loc = self._add_dev("aud-loc", "local")
        http_post(BASE_URL + "/api/keys/" + key["id"] + "/install",
                  {"device_id": loc})
        http_send("DELETE", BASE_URL + "/api/keys/" + key["id"])

        st, r = http_get(BASE_URL + "/api/audit?action=keys.generate")
        self.assertEqual(st, 200)
        gens = r["events"]
        self.assertGreaterEqual(len(gens), 1)
        hit = next(e for e in gens if e["target"].get("id") == key["id"])
        self.assertEqual(hit["result"], "ok")
        self.assertEqual(hit["target"]["kind"], "key")
        self.assertEqual(hit["detail"]["name"], "k-audit")
        self.assertEqual(hit["ip"], "127.0.0.1")

        st, r = http_get(BASE_URL + "/api/audit?action=keys.delete")
        self.assertEqual(st, 200)
        dels = [e for e in r["events"] if e["target"].get("id") == key["id"]]
        self.assertGreaterEqual(len(dels), 1)
        self.assertEqual(dels[0]["result"], "ok")

        st, r = http_get(BASE_URL + "/api/audit?action=keys.install")
        self.assertEqual(st, 200)
        insts = [e for e in r["events"] if e["detail"].get("key_id") == key["id"]]
        self.assertGreaterEqual(len(insts), 1)
        self.assertEqual(insts[0]["target"]["id"], loc)
        self.assertEqual(insts[0]["target"]["kind"], "device")


        st, r = http_get(BASE_URL + "/api/audit/stats?days=1")
        self.assertIn("keys.generate", r["data"]["by_action"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
