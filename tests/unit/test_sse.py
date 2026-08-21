#!/usr/bin/env python3
"""Test module."""
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
CONF_DIR = tempfile.mkdtemp(prefix="rkss-p1-sse-")


def _free_port():
    """Test helper."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100 + os.getpid() % 3000


PORTAL = _free_port()
BASE_URL = "http://127.0.0.1:%d" % PORTAL
MON = BASE_URL + "/api/monitor"


def http_get(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def http_post(url, body=None, timeout=8):
    req = urllib.request.Request(
        url, data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
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


def sse_open(path, timeout=8):
    """Test helper."""
    sock = socket.create_connection(("127.0.0.1", PORTAL), timeout=timeout)
    return SseStream(sock, path)


class SseStream:
    """Test class."""

    def __init__(self, sock, path):
        self.sock = sock
        self.buf = b""
        self.status = None
        self.headers = {}
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
                           % path).encode("utf-8"))
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        if lines and lines[0].split():
            try:
                self.status = int(lines[0].split()[1])
            except (IndexError, ValueError):
                self.status = None
        for ln in lines[1:]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                self.headers[k.strip().lower().decode("utf-8")] = \
                    v.strip().decode("utf-8")

    def read_line(self, seconds):
        """Test helper."""
        self.sock.settimeout(seconds)
        try:
            while True:
                if b"\n" in self.buf:
                    line, _, self.buf = self.buf.partition(b"\n")
                    return line + b"\n"
                chunk = self.sock.recv(4096)
                if not chunk:
                    if self.buf:
                        line, _, self.buf = self.buf.partition(b"\n")
                        return line
                    return b""
                self.buf += chunk
        except (socket.timeout, OSError):
            return None

    def read_frame(self, seconds):
        """Test helper."""
        while True:
            line = self.read_line(seconds)
            if line is None or line == b"":
                return line
            if line.strip():
                return line

    def read_all(self, seconds):
        """Test helper."""
        parts = []
        self.sock.settimeout(seconds)
        try:
            while True:
                if self.buf:
                    parts.append(self.buf)
                    self.buf = b""
                chunk = self.sock.recv(4096)
                if not chunk:
                    return b"".join(parts)
                parts.append(chunk)
        except (socket.timeout, OSError):
            return b"".join(parts)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _head_debug(self):
        return "status=%r headers=%r buf=%r" % (
            self.status, self.headers, self.buf[:120])


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


class SseUnitTest(unittest.TestCase):
    portal = None

    @classmethod
    def setUpClass(cls):


        for _ in range(3):
            cls.portal = Proc([PY, "-m", "portal.portal",
                               "--port", str(PORTAL),
                               "--bind", "127.0.0.1", "--sim",
                               "--conf-dir", CONF_DIR])
            if wait_port(PORTAL) and cls.portal.p.poll() is None:
                return
            cls.portal.stop()
            time.sleep(0.5)
        raise RuntimeError("portal failed to start on port %d" % PORTAL)

    @classmethod
    def tearDownClass(cls):
        cls.portal.stop()
        shutil.rmtree(CONF_DIR, ignore_errors=True)

    def test_06_offline_gap_events(self):
        """Test helper."""
        st, r = http_post(BASE_URL + "/api/devices", {
            "name": "sse-bad", "type": "ssh", "host": "127.0.0.1",
            "port": 1, "user": "root", "auth": "key"})
        self.assertEqual(st, 201)
        did = r["device"]["id"]
        s = sse_open(MON + "/" + did + "/stream")
        try:
            self.assertEqual(s.status, 200)
            lines = []
            for _ in range(6):
                line = s.read_frame(8)
                if not line:
                    break
                lines.append(line)
            joined = b"".join(lines)
            self.assertIn(b"event: gap\n", joined, lines)
            self.assertIn(b"data: {\"t\":", joined)
        finally:
            s.close()

    def test_07_sse_unit(self):
        """Test helper."""
        from portal import sse
        buf = io.BytesIO()
        self.assertTrue(sse.SseConn(buf).write({"t": 1, "c": 2.0}))
        self.assertEqual(buf.getvalue(), b'data: {"t":1,"c":2.0}\n\n')
        buf2 = io.BytesIO()
        self.assertTrue(sse.SseConn(buf2).write({"t": 9}, event="gap"))
        self.assertEqual(buf2.getvalue(), b'event: gap\ndata: {"t":9}\n\n')
        buf3 = io.BytesIO()
        self.assertTrue(sse.SseConn(buf3).ping())
        self.assertEqual(buf3.getvalue(), b":ping\n\n")
        bad = sse.SseConn(io.BytesIO())
        bad.wfile.close()
        self.assertFalse(bad.write({}))
        self.assertFalse(bad.ping())
        hub = sse.SseHub(max_connections=16)
        conns = [sse.SseConn(io.BytesIO()) for _ in range(17)]
        self.assertEqual([hub.add(c) for c in conns],
                         [True] * 16 + [False])
        self.assertEqual(hub.count(), 16)
        hub.remove(conns[0])
        self.assertEqual(hub.count(), 15)
        self.assertTrue(hub.add(conns[16]))
        self.assertEqual(hub.count(), 16)
        hub2 = sse.SseHub(max_connections=4)
        stale = sse.SseConn(io.BytesIO())
        hub2.add(stale)
        stale.last_write = time.time() - sse.HEARTBEAT_S - 1
        self.assertEqual(hub2.heartbeat(), 0)
        self.assertIn(b":ping\n\n", stale.wfile.getvalue())
        hub3 = sse.SseHub(max_connections=4)
        okc = sse.SseConn(io.BytesIO())
        deadc = sse.SseConn(io.BytesIO())
        deadc.wfile.close()
        hub3.add(okc)
        hub3.add(deadc)
        hub3.broadcast({"t": 1})
        self.assertEqual(hub3.count(), 1)
        self.assertIn(b'data: {"t":1}', okc.wfile.getvalue())



        fair = sse.SseHub(max_connections=16)
        monitors = [sse.SseConn(io.BytesIO()) for _ in range(9)]
        agents = [sse.SseConn(io.BytesIO()) for _ in range(5)]
        logs = [sse.SseConn(io.BytesIO()) for _ in range(4)]
        self.assertEqual([fair.add(c, "monitor") for c in monitors],
                         [True] * 8 + [False])
        self.assertEqual([fair.add(c, "agent") for c in agents],
                         [True] * 4 + [False])
        self.assertEqual([fair.add(c, "logcenter") for c in logs],
                         [True] * 4)
        self.assertEqual(fair.count(), 16)
        self.assertEqual(fair.count("monitor"), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
