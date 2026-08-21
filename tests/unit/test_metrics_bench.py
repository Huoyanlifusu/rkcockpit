#!/usr/bin/env python3
"""Stage 2 fixed-cardinality metrics and benchmark safety tests."""
import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent
while not (ROOT / ".git").exists() and ROOT != ROOT.parent:
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))


from host.core.auth import TokenAuth
from host.core.metrics import RuntimeMetrics
from portal.portal import Handler
from portal.server import BoundedThreadingHTTPServer
from tools import bench_stage1


class _NullDiscover:
    def get(self, force=False):
        return {"ok": True, "adb": [], "ssh": []}


class _NullRules:
    def get(self):
        return {}


class _Portal:
    def __init__(self, auth):
        self.server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), Handler, max_workers=4, idle_timeout=2)
        self.server.index_html = ""
        self.server.auth = auth
        self.server.host = object()
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
        self.thread.join(timeout=2)


def _get(port, path, token=""):
    headers = {"Authorization": "Bearer " + token} if token else {}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()


class RuntimeMetricsUnitTest(unittest.TestCase):
    def test_registry_is_closed_and_snapshot_does_io_without_lock(self):
        metrics = RuntimeMetrics(clock=lambda: 10.0)
        with self.assertRaises(KeyError):
            metrics.increment("device_192.0.2.1")
        lock_was_free = []

        def process_probe():
            acquired = metrics._lock.acquire(blocking=False)
            lock_was_free.append(acquired)
            if acquired:
                metrics._lock.release()
            return {"rss_kb": 1, "threads": 2, "fd_count": 3}

        metrics._process_snapshot = process_probe
        snap = metrics.snapshot()
        self.assertEqual(lock_was_free, [True])
        self.assertEqual(snap["schema_version"], 1)
        self.assertEqual(set(snap), {
            "ok", "schema_version", "uptime_s", "process", "http", "sse",
            "ssh", "poll", "audit",
        })
        self.assertNotIn("192.0.2.1", json.dumps(snap))

    def test_metrics_is_authenticated_and_health_shape_is_unchanged(self):
        token = "stage2-secret-token-0123456789abcdef"
        portal = _Portal(TokenAuth(token))
        try:
            status, health = _get(portal.port, "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(health, {"ok": True, "service": "rkss-portal",
                                      "version": "0.2.0"})
            status, body = _get(portal.port, "/api/metrics")
            self.assertEqual((status, body["code"]), (401, "unauthorized"))
            status, body = _get(portal.port, "/api/metrics", token)
            self.assertEqual((status, body["ok"], body["schema_version"]),
                             (200, True, 1))
            rendered = json.dumps(body)
            self.assertNotIn(token, rendered)
            self.assertNotIn("127.0.0.1", rendered)
        finally:
            portal.close()

    def test_metrics_overlay_is_cached_for_one_second(self):
        token = "stage2-cache-token-0123456789abcdef"
        portal = _Portal(TokenAuth(token))
        payload = {"ok": True, "schema_version": 1}
        try:
            with mock.patch("portal.portal.runtime_metrics_snapshot",
                            return_value=payload) as snapshot:
                self.assertEqual(_get(portal.port, "/api/metrics", token)[0],
                                 200)
                self.assertEqual(_get(portal.port, "/api/metrics", token)[0],
                                 200)
                self.assertEqual(snapshot.call_count, 1)
        finally:
            portal.close()


class _SmallHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BenchmarkSafetyUnitTest(unittest.TestCase):
    def test_target_scheme_allowlist_and_plaintext_token_gate(self):
        self.assertEqual(
            bench_stage1.validate_target("https://example.test", "/api/metrics").scheme,
            "https")
        with self.assertRaisesRegex(ValueError, "allowlist"):
            bench_stage1.validate_target("http://127.0.0.1:8080", "/api/delete")
        with self.assertRaisesRegex(ValueError, "明文 HTTP"):
            bench_stage1.validate_target(
                "http://192.0.2.10:8080", "/api/metrics", token=True)
        warning = io.StringIO()
        with contextlib.redirect_stderr(warning):
            bench_stage1.validate_target(
                "http://192.0.2.10:8080", "/api/metrics", token=True,
                allow_insecure_token=True)
        self.assertIn("WARNING", warning.getvalue())

    def test_token_file_requires_regular_single_link_mode_0600(self):
        with tempfile.TemporaryDirectory() as td:
            token_path = os.path.join(td, "token")
            with open(token_path, "w", encoding="utf-8") as fh:
                fh.write("t" * 32 + "\n")
            os.chmod(token_path, 0o600)
            self.assertEqual(bench_stage1.read_token_file(token_path), "t" * 32)
            os.chmod(token_path, 0o640)
            with self.assertRaisesRegex(ValueError, "0600"):
                bench_stage1.read_token_file(token_path)
            os.chmod(token_path, 0o600)
            link = os.path.join(td, "link")
            os.symlink(token_path, link)
            with self.assertRaisesRegex(ValueError, "regular"):
                bench_stage1.read_token_file(link)

    def test_limits_apply_below_cli_and_latency_storage_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "1..128"):
            bench_stage1.run_benchmark(
                "http://127.0.0.1", "/api/health", 129, 1)
        with self.assertRaisesRegex(ValueError, "3600"):
            bench_stage1.run_benchmark(
                "http://127.0.0.1", "/api/health", 1, 3601)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _SmallHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(bench_stage1, "MAX_LATENCY_SAMPLES", 2):
                result = bench_stage1.run_benchmark(
                    "http://127.0.0.1:%d" % server.server_address[1],
                    "/api/health", 1, 0.05)
            self.assertLessEqual(result["latency_samples"], 2)
            self.assertGreater(result["requests"], result["latency_samples"])
            self.assertTrue(result["latency_samples_capped"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_response_body_is_capped_at_four_mib(self):
        target = bench_stage1.validate_target(
            "http://127.0.0.1", "/api/health")
        response = mock.Mock()
        response.getheader.return_value = str(bench_stage1.MAX_RESPONSE_BYTES + 1)
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(bench_stage1.http.client, "HTTPConnection",
                               return_value=connection):
            with self.assertRaisesRegex(ValueError, "4 MiB"):
                bench_stage1.request_once(target, "/api/health", "", 1)
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
