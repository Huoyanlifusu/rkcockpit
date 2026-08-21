#!/usr/bin/env python3

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


class SseHardwareTest(unittest.TestCase):
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

    def test_01_stream_headers_and_frames(self):
        s = sse_open(MON + "/demo/stream")
        try:
            self.assertEqual(s.status, 200, "head=%r" % s._head_debug())
            self.assertIn("text/event-stream",
                          s.headers.get("content-type", ""),
                          s._head_debug())
            self.assertEqual(s.headers.get("cache-control"), "no-store")
            for _ in range(2):
                line = s.read_frame(8)
                self.assertIsNotNone(line, "等待数据帧超时")
                self.assertTrue(line.startswith(b"data:"), line)
                self.assertIn(b"\"t\":", line)
                self.assertLess(len(line), 120)
        finally:
            s.close()

    def test_02_keep_alive(self):
        """Test helper."""
        s = sse_open(MON + "/demo/stream")
        try:
            n = 0
            for _ in range(3):
                line = s.read_frame(6)
                self.assertIsNotNone(line, "第 %d 帧未在限时内到达" % (n + 1))
                self.assertTrue(line.startswith(b"data:"), line)
                n += 1
            self.assertEqual(n, 3)
        finally:
            s.close()

    def test_03_connection_limit(self):
        """Test helper."""
        time.sleep(0.6)
        conns = []
        try:
            s1 = sse_open(MON + "/demo/stream")
            self.assertEqual(s1.status, 200)
            line = s1.read_frame(8)
            self.assertTrue(line and line.startswith(b"data:"))
            conns.append(s1)
            for i in range(7):
                s = sse_open(MON + "/demo/stream")
                self.assertEqual(s.status, 200,
                                 "第 %d 个连接应为 200" % (i + 2))
                conns.append(s)
            self.assertEqual(len(conns), 8)
            s17 = sse_open(MON + "/demo/stream")
            try:
                self.assertEqual(s17.status, 503)
                body = json.loads(s17.read_all(2).decode("utf-8"))
                self.assertFalse(body["ok"])
                self.assertIn("monitor SSE 连接数已达上限(8)", body["error"])
            finally:
                s17.close()
        finally:
            for s in conns:
                s.close()
            time.sleep(1.2)

    def test_04_series_works_while_sse(self):
        """Test helper."""
        s = sse_open(MON + "/demo/stream")
        try:
            line = s.read_frame(8)
            self.assertTrue(line and line.startswith(b"data:"))
            st, body = http_get(MON + "/demo/series?window=60")
            self.assertEqual(st, 200)
            self.assertTrue(body["ok"])
            self.assertGreaterEqual(len(body["samples"]), 1)
            st, body = http_get(MON + "/demo/now")
            self.assertEqual(st, 200)
            self.assertTrue(body["ok"])
        finally:
            s.close()
        try:
            http_get(MON + "/no-such-device/stream")
            self.fail("expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_05_disable_stops_stream(self):
        """Test helper."""
        http_post(MON + "/demo/enable")
        time.sleep(1.2)
        s = sse_open(MON + "/demo/stream")
        try:
            line = s.read_frame(8)
            self.assertTrue(line and line.startswith(b"data:"))
            st, body = http_post(MON + "/demo/disable")
            self.assertEqual(st, 200)
            self.assertTrue(body["ok"])
            for _ in range(4):
                if s.read_frame(1.0) is None:
                    break
            line = s.read_frame(2.0)
            self.assertFalse(line, "disable 后仍推帧: %r" % line)
            http_post(MON + "/demo/enable")
            line = s.read_frame(8)
            self.assertTrue(line and line.startswith(b"data:"))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
