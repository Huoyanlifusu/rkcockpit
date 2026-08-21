#!/usr/bin/env python3
"""Stage 2 mixed benchmark validation and socket-free core tests."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent
while not (ROOT / ".git").exists() and ROOT != ROOT.parent:
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))


from tools import bench_stage2_mixed as mixed


def device_ids():
    return ["device-%02d" % index for index in range(mixed.DEVICE_COUNT)]


class DeviceInputUnitTest(unittest.TestCase):
    def test_file_requires_bounded_unique_valid_ids_and_selects_first_30(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ids")
            values = ["device-%03d" % index for index in range(35)]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(values) + "\n")
            self.assertEqual(mixed.load_device_ids(path), values[:30])

            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(values[:29]))
            with self.assertRaisesRegex(ValueError, "30..128"):
                mixed.load_device_ids(path)

            bad = values[:30]
            bad[-1] = bad[0]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(bad))
            with self.assertRaisesRegex(ValueError, "unique"):
                mixed.load_device_ids(path)

            bad[-1] = "../escape"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(bad))
            with self.assertRaisesRegex(ValueError, "format"):
                mixed.load_device_ids(path)

    def test_paths_are_fixed_get_only_and_quote_path_and_query_values(self):
        ids = device_ids()
        ids[0] = "adb:serial"
        monitor, logs, short = mixed.build_paths(ids, "/var/log/a b.log")
        self.assertEqual((len(monitor), len(logs), len(short)), (8, 8, 32))
        self.assertIn("adb%3Aserial", monitor[0])
        self.assertIn("source=%2Fvar%2Flog%2Fa%20b.log", logs[0])
        self.assertTrue(all(path.startswith("/api/") for path in
                            monitor + logs + short))
        self.assertTrue(all("?" not in path or path.startswith(
            "/api/logcenter/") for path in monitor + logs + short))
        with self.assertRaisesRegex(ValueError, "absolute"):
            mixed.build_paths(ids, "relative.log")


class MixedRunUnitTest(unittest.TestCase):
    def test_duration_bounds_and_remote_plaintext_token_gate(self):
        with self.assertRaisesRegex(ValueError, "3600"):
            mixed.validate_run(10, 600, 10, 5)
        with self.assertRaisesRegex(ValueError, "1..10"):
            mixed.validate_run(0, 1, 11, 5)
        with self.assertRaisesRegex(ValueError, "明文 HTTP"):
            mixed.run_mixed("http://192.0.2.4:8080", device_ids(),
                            token="secret", warmup_s=0, sample_s=0.01,
                            repeat=1)

    def test_report_selects_coherent_median_round_and_contains_no_secrets(self):
        rounds = [
            {"round": 1, "p95_ms": 30, "p99_ms": 40, "rps": 10,
             "status_counts": {"200": 1}, "rss_peak_kb": 1,
             "threads_peak": 2, "sse": {"connected": 16,
                                           "disconnected": 0}},
            {"round": 2, "p95_ms": 10, "p99_ms": 20, "rps": 30,
             "status_counts": {"200": 3}, "rss_peak_kb": 3,
             "threads_peak": 4, "sse": {"connected": 16,
                                           "disconnected": 1}},
            {"round": 3, "p95_ms": 20, "p99_ms": 25, "rps": 20,
             "status_counts": {"200": 2}, "rss_peak_kb": 2,
             "threads_peak": 3, "sse": {"connected": 16,
                                           "disconnected": 0}},
        ]
        ids = device_ids()
        with mock.patch.object(mixed, "run_round", side_effect=rounds):
            report = mixed.run_mixed(
                "https://portal.example", ids, token="top-secret-token",
                log_source="/secret/device.log", warmup_s=0, sample_s=0.01,
                repeat=3)
        self.assertEqual(report["median_round"]["round"], 3)
        self.assertEqual(report["median_round"]["p99_ms"], 25)
        rendered = json.dumps(report)
        for secret in ("top-secret-token", "portal.example",
                       "/secret/device.log", ids[0]):
            self.assertNotIn(secret, rendered)

    def test_round_uses_barrier_tracks_16_sse_and_cleans_every_thread(self):
        ids = device_ids()
        target = SimpleNamespace(scheme="http", hostname="127.0.0.1", port=80)

        def request_once(_target, _path, _token, _timeout):
            time.sleep(0.0005)
            return 200, 2, 1.25

        def consume(_target, _path, _token, _timeout, stop, _registry, stats):
            stats.connected()
            stop.wait()
            stats.disconnected(unexpected=False)

        with mock.patch.object(mixed.bench_stage1, "request_once",
                               side_effect=request_once), \
                mock.patch.object(mixed, "_consume_sse",
                                  side_effect=consume), \
                mock.patch.object(mixed.bench_stage1, "proc_resources",
                                  return_value=(1234, 56)):
            result = mixed.run_round(
                target, ids, "", "/dev/null", warmup_s=0.03,
                sample_s=0.03, portal_pid=123, timeout=0.2)
        self.assertGreater(result["requests"], 0)
        self.assertEqual(result["status_counts"], {"200": result["requests"]})
        self.assertLessEqual(result["latency_samples"],
                             mixed.MAX_LATENCY_SAMPLES)
        self.assertEqual(result["rss_peak_kb"], 1234)
        self.assertEqual(result["threads_peak"], 56)
        self.assertEqual(result["sse"]["active_at_start"], 16)
        self.assertEqual(result["sse"]["connected"], 16)
        self.assertEqual(result["sse"]["disconnected"], 0)
        self.assertEqual(result["cleanup_threads_alive"], 0)


class _HeaderResponse:
    status = 200

    def __init__(self):
        self.closed = False

    def getheaders(self):
        return [("X-Large", "x" * mixed.MAX_SSE_HEADER_BYTES)]

    def getheader(self, _name):
        return "text/event-stream"

    def close(self):
        self.closed = True


class _HeaderConnection:
    sock = None

    def __init__(self):
        self.response = _HeaderResponse()
        self.closed = False

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class SseBoundUnitTest(unittest.TestCase):
    def test_detached_sse_uses_response_owned_stream(self):
        stream = object()
        response = SimpleNamespace(fp=stream)
        connection = SimpleNamespace(sock=None)
        self.assertIs(mixed._response_stream(connection, response), stream)

    def test_oversized_sse_headers_fail_without_leaking_connection(self):
        registry = mixed._ConnectionRegistry()
        stats = mixed._SseStats()
        stats.begin_sample()
        connection = _HeaderConnection()
        with mock.patch.object(mixed, "_connection",
                               return_value=connection):
            mixed._consume_sse(
                SimpleNamespace(), "/api/monitor/x/stream", "", 1,
                mock.Mock(is_set=mock.Mock(return_value=False)), registry,
                stats)
        stats.end_sample()
        self.assertEqual(stats.snapshot()["connect_failed"], 1)
        self.assertTrue(connection.response.closed)
        self.assertTrue(connection.closed)
        self.assertEqual(len(registry._connections), 0)


if __name__ == "__main__":
    unittest.main()
