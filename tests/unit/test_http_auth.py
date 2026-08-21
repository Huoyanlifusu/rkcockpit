#!/usr/bin/env python3
"""Stage 1 HTTP admission, request-boundary and token-auth tests."""
import http.client
import json
import os
import tempfile
import threading
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
while not (ROOT / ".git").exists() and ROOT != ROOT.parent:
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from host.core.auth import AuthConfigError, SESSION_SECONDS, TokenAuth
from portal.portal import Handler
from portal.server import BoundedThreadingHTTPServer


class _NullDiscover:
    def get(self, force=False):
        return {"ok": True, "adb": [], "ssh": []}


class _NullRules:
    def get(self):
        return {}


class _NullHost:
    pass


class PortalServer:
    def __init__(self, auth, handler=Handler, workers=8):
        self.server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), handler, max_workers=workers,
            idle_timeout=2)
        self.server.index_html = "<html>login shell</html>"
        self.server.auth = auth
        self.server.host = _NullHost()
        self.server.discover = _NullDiscover()
        self.server.rules = _NullRules()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if not self.server.drain(timeout=2):
            raise AssertionError("HTTP worker pool did not drain")
        self.thread.join(timeout=2)


def request(port, method, path, body=None, headers=None):
    headers = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        parsed = json.loads(raw.decode("utf-8")) \
            if response.getheader("Content-Type", "").startswith("application/json") \
            else raw.decode("utf-8")
        return response.status, dict(response.getheaders()), parsed
    finally:
        conn.close()


class TokenAuthUnitTest(unittest.TestCase):
    def test_token_file_permissions_and_strength(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "token")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("a" * 32 + "\n")
            os.chmod(path, 0o600)
            self.assertTrue(TokenAuth.from_file(path).enabled)
            os.chmod(path, 0o644)
            with self.assertRaises(AuthConfigError):
                TokenAuth.from_file(path)

    def test_token_file_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "target")
            link = os.path.join(td, "token")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("z" * 32)
            os.chmod(target, 0o600)
            before = os.stat(target)
            os.symlink(target, link)
            with self.assertRaises(AuthConfigError):
                TokenAuth.from_file(link)
            after = os.stat(target)
            self.assertEqual((after.st_uid, after.st_mode, after.st_size),
                             (before.st_uid, before.st_mode, before.st_size))

    def test_session_expiry(self):
        now = [1000]
        auth = TokenAuth("x" * 32, clock=lambda: now[0])
        cookie = auth.login_cookie().split(";", 1)[0].split("=", 1)[1]
        self.assertTrue(auth.verify_session(cookie))
        now[0] += SESSION_SECONDS + 1
        self.assertFalse(auth.verify_session(cookie))


class PortalAuthIntegratioUnitTest(unittest.TestCase):
    def setUp(self):
        self.auth = TokenAuth("stage1-secret-token-0123456789abcdef")
        self.portal = PortalServer(self.auth)

    def tearDown(self):
        self.portal.close()

    def test_public_health_and_protected_api(self):
        status, _, body = request(self.portal.port, "GET", "/api/health")
        self.assertEqual((status, body["ok"]), (200, True))
        status, _, body = request(self.portal.port, "GET", "/api/devices")
        self.assertEqual((status, body["code"]), (401, "unauthorized"))

    def test_login_cookie_bearer_and_same_origin(self):
        status, headers, body = request(
            self.portal.port, "POST", "/api/auth/login",
            {"token": "stage1-secret-token-0123456789abcdef"})
        self.assertEqual((status, body["authenticated"]), (200, True))
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        status, _, _ = request(self.portal.port, "GET", "/api/unknown",
                               headers={"Cookie": cookie})
        self.assertEqual(status, 404)
        status, _, body = request(self.portal.port, "POST", "/api/unknown", {},
                                  headers={"Cookie": cookie})
        self.assertEqual((status, body["code"]), (403, "origin_required"))
        origin = "http://127.0.0.1:%d" % self.portal.port
        status, _, _ = request(self.portal.port, "POST", "/api/unknown", {},
                               headers={"Cookie": cookie, "Origin": origin})
        self.assertEqual(status, 404)
        status, _, _ = request(
            self.portal.port, "POST", "/api/unknown", {}, headers={
                "Authorization": "Bearer stage1-secret-token-0123456789abcdef"})
        self.assertEqual(status, 404)

    def test_login_rate_limit(self):
        for _ in range(5):
            status, _, _ = request(self.portal.port, "POST", "/api/auth/login",
                                   {"token": "wrong"})
            self.assertEqual(status, 401)
        status, headers, body = request(
            self.portal.port, "POST", "/api/auth/login", {"token": "wrong"})
        self.assertEqual((status, body["code"]), (429, "login_rate_limited"))
        self.assertEqual(headers["Retry-After"], "300")

    def test_non_json_body_is_still_capped(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.portal.port, timeout=3)
        try:
            conn.putrequest("POST", "/api/auth/login")
            conn.putheader("Content-Type", "text/plain")
            conn.putheader("Content-Length", str((1 << 20) + 1))
            conn.endheaders()
            response = conn.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 413)
            self.assertIn("1 MiB", body["error"])
        finally:
            conn.close()

    def test_raw_upload_rejects_chunked_and_invalid_length_centrally(self):
        headers = {"Authorization":
                   "Bearer stage1-secret-token-0123456789abcdef",
                   "Transfer-Encoding": "chunked"}
        status, _, body = request(
            self.portal.port, "POST", "/api/fs/local/upload?path=/&name=x",
            headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("chunked", body["error"])

        conn = http.client.HTTPConnection("127.0.0.1", self.portal.port, timeout=3)
        try:
            conn.putrequest("POST", "/api/fs/local/upload?path=/&name=x")
            conn.putheader("Authorization",
                           "Bearer stage1-secret-token-0123456789abcdef")
            conn.putheader("Content-Length", "not-a-number")
            conn.endheaders()
            response = conn.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 400)
            self.assertIn("Content-Length", body["error"])
        finally:
            conn.close()

    def test_static_parent_escape_is_rejected(self):
        status, _, _ = request(self.portal.port, "GET", "/static/../README.md")
        self.assertEqual(status, 404)


class _BlockingHandler(Handler):
    entered = threading.Event()
    release = threading.Event()

    def do_GET(self):
        if self.path == "/block":
            self.entered.set()
            self.release.wait(3)
            return self._send(200, {"ok": True})
        return super().do_GET()


class BoundedServerUnitTest(unittest.TestCase):
    def test_close_during_active_request_drains_and_joins_workers(self):
        _BlockingHandler.entered.clear()
        _BlockingHandler.release.clear()
        portal = PortalServer(TokenAuth.disabled(), _BlockingHandler,
                              workers=2)
        first = http.client.HTTPConnection("127.0.0.1", portal.port,
                                           timeout=3)
        first.request("GET", "/block")
        self.assertTrue(_BlockingHandler.entered.wait(1))
        portal.server.shutdown()
        portal.server.server_close()
        _BlockingHandler.release.set()
        first.getresponse().read()
        first.close()
        self.assertTrue(portal.server.drain(timeout=2))
        self.assertFalse(any(worker.is_alive()
                             for worker in portal.server._workers))
        portal.thread.join(timeout=2)

    def test_short_requests_reuse_fixed_worker_pool(self):
        portal = PortalServer(TokenAuth.disabled(), workers=2)
        seen = set()
        original = portal.server.finish_request

        def recording_finish(request_socket, client_address):
            seen.add(threading.get_ident())
            return original(request_socket, client_address)

        portal.server.finish_request = recording_finish
        try:
            for _index in range(20):
                self.assertEqual(request(
                    portal.port, "GET", "/api/health")[0], 200)
            self.assertLessEqual(len(seen), 2)
            self.assertTrue(all(worker.daemon
                                for worker in portal.server._workers))
        finally:
            portal.close()

    def test_second_connection_gets_fast_503_before_thread_creation(self):
        _BlockingHandler.entered.clear()
        _BlockingHandler.release.clear()
        portal = PortalServer(TokenAuth.disabled(), _BlockingHandler, workers=1)
        first = http.client.HTTPConnection("127.0.0.1", portal.port, timeout=3)
        first.request("GET", "/block")
        self.assertTrue(_BlockingHandler.entered.wait(1))
        try:
            status, headers, body = request(portal.port, "GET", "/api/health")
            self.assertEqual((status, body["code"]), (503, "server_busy"))
            self.assertEqual(headers["Retry-After"], "1")
        finally:
            _BlockingHandler.release.set()
            first.getresponse().read()
            first.close()
            portal.close()


if __name__ == "__main__":
    unittest.main()
