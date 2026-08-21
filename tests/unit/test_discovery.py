#!/usr/bin/env python3

# dont reply on real board

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host import discovery

PY = sys.executable


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _adb_run(returncode, stdout):
    return mock.patch(
        "host.discovery.subprocess.run",
        return_value=SimpleNamespace(returncode=returncode, stdout=stdout,
                                     stderr=""))


class AdbParseTest(unittest.TestCase):
    def test_parse_devices(self):
        output = (
            "List of devices attached\n"
            "rk3588-demo-001 device usb:1-1 product:rk3588_evb "
            "model:RK3588_ROBOTIC_EVB device:rk3588_evb transport_id:1\n"
            "rv1126-demo-001 offline usb:3-1.4 transport_id:2\n"
        )
        with _adb_run(0, output):
            devices = discovery.adb_devices()
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]["serial"], "rk3588-demo-001")
        self.assertEqual(devices[0]["state"], "device")
        self.assertEqual(devices[0]["product"], "rk3588_evb")
        self.assertEqual(devices[0]["model"], "RK3588_ROBOTIC_EVB")
        self.assertEqual(devices[0]["device"], "rk3588_evb")
        self.assertEqual(devices[0]["transport_id"], "1")
        self.assertEqual(devices[1]["state"], "offline")
        self.assertEqual(devices[1]["model"], "")

    def test_adb_missing_and_failure(self):
        with mock.patch("host.discovery.subprocess.run",
                        side_effect=FileNotFoundError):
            self.assertEqual(discovery.adb_devices(), [])
        with _adb_run(1, "List of devices attached\n"):
            self.assertEqual(discovery.adb_devices(), [])


class SshCandidatesTest(unittest.TestCase):
    def test_candidates_dedupe_and_filter(self):
        with mock.patch("host.discovery._arp_hosts",
                        return_value=["192.0.2.5", "127.0.0.1"]), \
                mock.patch("host.discovery._ssh_config_hosts",
                           return_value=["board.lan"]), \
                mock.patch("host.discovery._known_hosts",
                           return_value=["198.51.100.9", "198.51.100.10"]):
            hosts = discovery.ssh_candidates([
                {"type": "ssh", "host": "192.0.2.5"},
                {"type": "adb", "host": "ignored-serial"},
                {"type": "ssh", "host": ""},
            ])
        self.assertEqual(hosts, ["192.0.2.5", "198.51.100.10",
                                 "198.51.100.9", "board.lan"])

    def test_config_pattern_skipped(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".ssh"), exist_ok=True)
        with open(os.path.join(home, ".ssh", "config"),
                  "w", encoding="utf-8") as fh:
            fh.write("Host *\n  HostName *.example.com\n"
                     "Host prod\n  HostName 203.0.113.23\n")
        with mock.patch("host.discovery.os.path.expanduser",
                        return_value=home), \
                mock.patch("host.discovery._arp_hosts", return_value=[]), \
                mock.patch("host.discovery._known_hosts", return_value=[]):
            hosts = discovery._ssh_config_hosts()
        self.assertEqual(hosts, ["203.0.113.23"])

    def test_known_hosts_bracket_and_hash(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".ssh"), exist_ok=True)
        with open(os.path.join(home, ".ssh", "known_hosts"),
                  "w", encoding="utf-8") as fh:
            fh.write("198.51.100.9 ssh-rsa AAAAB3...\n"
                     "[198.51.100.10]:2222 ssh-ed25519 AAAAC3...\n"
                     "|1|hashed=entry ssh-rsa AAAAB3...\n"
                     "# comment\n")
        with mock.patch("host.discovery.os.path.expanduser",
                        return_value=home):
            hosts = discovery._known_hosts()
        self.assertEqual(hosts, ["198.51.100.9", "198.51.100.10"])


class _BannerServer:
    def __init__(self, banner, delay=0.0):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.banner = banner
        self.delay = delay
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
            if self.delay:
                time.sleep(self.delay)
            try:
                conn.sendall(self.banner.encode())
            finally:
                conn.close()
        finally:
            self.srv.close()


class SshProbeTest(unittest.TestCase):
    def test_ssh_banner_ok(self):
        server = _BannerServer("SSH-2.0-OpenSSH_8.9p1 Test\r\n")
        result = discovery.probe_ssh("127.0.0.1", server.port, timeout=2)
        self.assertIsNotNone(result)
        self.assertTrue(result["banner"].startswith("SSH-2.0"))
        self.assertEqual(result["port"], server.port)

    def test_non_ssh_banner_rejected(self):
        server = _BannerServer("HTTP/1.1 400 Bad Request\r\n")
        self.assertIsNone(discovery.probe_ssh("127.0.0.1", server.port,
                                              timeout=2))

    def test_closed_port_none(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.assertIsNone(discovery.probe_ssh("127.0.0.1", port, timeout=1))


class DiscoverTest(unittest.TestCase):
    def test_discover_shape(self):
        adb = [{"serial": "abc", "state": "device"}]
        ssh = [{"host": "198.51.100.9", "port": 22, "banner": "SSH-2.0-x"}]
        with mock.patch("host.discovery.adb_devices",
                        return_value=adb), \
                mock.patch("host.discovery.discover_ssh",
                           return_value=ssh):
            result = discovery.discover()
        self.assertEqual(result["adb"], adb)
        self.assertEqual(result["ssh"], ssh)
        self.assertGreater(result["generated_at"], 0)


class DiscoverRulesTest(unittest.TestCase):
    def test_matches_cidr_glob_exact(self):
        self.assertTrue(discovery._matches("10.23.45.67", "10.23.*"))
        self.assertTrue(discovery._matches("abc123", "abc123"))
        self.assertFalse(discovery._matches("10.23.45.67", "10.24.*"))

    def test_filter_ssh_banner(self):
        hosts = [
            {"host": "198.51.100.9", "port": 22,
             "banner": "SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.6"},
            {"host": "198.51.100.10", "port": 22,
             "banner": "SSH-2.0-OpenSSH_for_Windows_9.5"},
            {"host": "192.0.2.5", "port": 22,
             "banner": "SSH-2.0-OpenSSH_9.2p1 Debian"},
        ]

        rules = {"banner_exclude": ["for_windows", "win32"]}
        kept = discovery.filter_ssh_banner(hosts, rules)
        self.assertEqual([h["host"] for h in kept],
                         ["198.51.100.9", "192.0.2.5"])

        self.assertEqual(discovery.filter_ssh_banner(hosts, {}), hosts)

        rules2 = {"banner_exclude": ["FOR_WINDOWS"]}
        kept2 = discovery.filter_ssh_banner(hosts, rules2)
        self.assertEqual([h["host"] for h in kept2],
                         ["198.51.100.9", "192.0.2.5"])


class PortalDiscoveryApiTest(unittest.TestCase):
    PORTAL = _free_port() + 200
    BASE_URL = "http://127.0.0.1:%d" % PORTAL
    OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def setUpClass(cls):
        cls.conf_dir = tempfile.mkdtemp(prefix="rkss-discover-")
        cls.proc = subprocess.Popen(
            [PY, "-m", "portal.portal", "--port", str(cls.PORTAL),
             "--conf-dir", cls.conf_dir],
            cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                cls._get("/api/health", timeout=0.5)
                return
            except Exception:
                if cls.proc.poll() is not None:
                    raise RuntimeError("portal exited early: %r"
                                       % cls.proc.returncode)
                time.sleep(0.2)
        raise RuntimeError("portal did not start")

    @classmethod
    def tearDownClass(cls):
        if cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()

    @staticmethod
    def _get(path, timeout=15):
        with PortalDiscoveryApiTest.OPENER.open(
                PortalDiscoveryApiTest.BASE_URL + path,
                timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _post(path, body, timeout=15):
        req = urllib.request.Request(
            PortalDiscoveryApiTest.BASE_URL + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with PortalDiscoveryApiTest.OPENER.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _put(path, body, timeout=15):
        req = urllib.request.Request(
            PortalDiscoveryApiTest.BASE_URL + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PUT")
        with PortalDiscoveryApiTest.OPENER.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_discover_ok(self):
        status, body = self._get("/api/discover")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["adb"], list)
        self.assertIsInstance(body["ssh"], list)
        self.assertIn("filtered", body)
        self.assertGreater(body["generated_at"], 0)

    def test_rules_invalid(self):
        try:
            self._put("/api/discover/rules",
                      {"rules": {"ips": ["ok", 1], "serials": []}})
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            body = json.loads(exc.read().decode("utf-8"))
            self.assertFalse(body["ok"])
            return
        self.fail("expected HTTP 400")

    def test_import_and_dedupe(self):
        status, body = self._post("/api/discover/import",
                                  {"items": [{"type": "ssh",
                                              "host": "192.0.2.55"}]})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["devices"]), 1)
        self.assertEqual(body["devices"][0]["name"], "ssh-192.0.2.55")
        self.assertEqual(body["skipped"], [])
        status, body = self._post("/api/discover/import",
                                  {"items": [{"type": "ssh",
                                              "host": "192.0.2.55"}]})
        self.assertEqual(len(body["devices"]), 0)
        self.assertEqual(len(body["skipped"]), 1)
        self.assertEqual(body["skipped"][0]["error"], "设备已存在")
        _, devices = self._get("/api/devices")
        self.assertTrue(any(d["type"] == "ssh" and d["host"] == "192.0.2.55"
                            for d in devices["devices"]))

    def test_import_invalid(self):
        status, body = self._post("/api/discover/import",
                                  {"items": [{"type": "ftp",
                                              "host": "x"}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"], [])
        self.assertEqual(body["skipped"][0]["error"], "type/host 无效")


if __name__ == "__main__":
    unittest.main()
