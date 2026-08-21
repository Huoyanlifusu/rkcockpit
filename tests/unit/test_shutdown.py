#!/usr/bin/env python3
"""Stage2 SIGTERM shutdown drains accepted audit events."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(url, method="GET", body=None, timeout=2):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


class SigtermDrainUnitTest(unittest.TestCase):
    def test_real_portal_sigterm_drains_audit_queue(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage2-term-") as conf:
            port = _free_port()
            base_url = "http://127.0.0.1:%d" % port
            proc = subprocess.Popen(
                [sys.executable, "-m", "portal.portal", "--bind", "127.0.0.1",
                 "--port", str(port), "--conf-dir", conf],
                cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            try:
                deadline = time.time() + 8
                while time.time() < deadline:
                    if proc.poll() is not None:
                        self.fail("portal exited before becoming ready")
                    try:
                        if _request(base_url + "/api/health", timeout=0.3)[0] == 200:
                            break
                    except Exception:
                        time.sleep(0.05)
                else:
                    self.fail("portal did not become ready")

                expected = 40
                for index in range(expected):
                    status, result = _request(
                        base_url + "/api/devices", "POST",
                        {"name": "shutdown-%d" % index, "type": "local",
                         "local_root": os.path.join(conf, "d-%d" % index)})
                    self.assertEqual(status, 201)
                    self.assertTrue(result["ok"])

                os.kill(proc.pid, signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=12), 0)
                proc = None

                audit_dir = os.path.join(conf, "audit")
                rows = []
                for filename in os.listdir(audit_dir):
                    if not filename.endswith(".jsonl"):
                        continue
                    with open(os.path.join(audit_dir, filename),
                              encoding="utf-8") as stream:
                        rows.extend(json.loads(line) for line in stream if line.strip())
                added = [row for row in rows if row.get("action") == "dev.add"]
                self.assertEqual(len(added), expected)
            finally:
                if proc is not None and proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
