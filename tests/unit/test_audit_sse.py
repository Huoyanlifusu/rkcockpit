#!/usr/bin/env python3
"""Stage2 audit queue and SSE fairness/slow-client regression tests."""
import io
import json
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent
while not (ROOT / ".git").exists() and ROOT != ROOT.parent:
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from host.audit.recorder import AuditRecorder  # noqa: E402
from portal.sse import SseConn, SseHub, WRITE_TIMEOUT_S  # noqa: E402


def _event(index):
    return {
        "action": "fs.mkdir",
        "target": {"kind": "file", "id": "local",
                   "path": "/stage2-%d" % index},
    }


class AuditQueueUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rkss-stage2-audit-")

    def tearDown(self):
        self.tmp.cleanup()

    def test_async_record_is_immediately_queryable_and_close_drains(self):
        rec = AuditRecorder(self.tmp.name)
        for i in range(200):
            self.assertTrue(rec.record(_event(i)))
        # Ring visibility does not depend on writer scheduling.
        self.assertEqual(len(rec.query({"action": "fs.mkdir"})), 200)
        self.assertTrue(rec.close(timeout=5))
        runtime = rec.stats(include_runtime=True)["runtime"]
        self.assertEqual(runtime["pending"], 0)
        self.assertEqual(runtime["written"], 200)
        self.assertFalse(runtime["accepting"])
        path = os.path.join(rec.audit_dir,
                            time.strftime("%Y-%m-%d") + ".jsonl")
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual(len(rows), 200)

    def test_full_queue_uses_synchronous_fallback(self):
        rec = AuditRecorder(self.tmp.name, queue_size=1)
        with mock.patch.object(rec._queue, "put_nowait",
                               side_effect=queue.Full):
            self.assertTrue(rec.record(_event(1)))
        runtime = rec.stats(include_runtime=True)["runtime"]
        self.assertEqual(runtime["fallback"], 1)
        self.assertEqual(runtime["written"], 1)
        self.assertEqual(runtime["pending"], 0)
        self.assertTrue(rec.close())

    def test_full_queue_disk_failure_returns_without_blocking(self):
        rec = AuditRecorder(self.tmp.name, queue_size=1)
        started = time.monotonic()
        with mock.patch.object(rec._queue, "put_nowait",
                               side_effect=queue.Full), \
                mock.patch.object(rec, "_append",
                                  side_effect=OSError("disk full")):
            self.assertFalse(rec.record(_event(9)))
        self.assertLess(time.monotonic() - started, 0.2)
        runtime = rec.stats(include_runtime=True)["runtime"]
        self.assertEqual(runtime["fallback"], 1)
        self.assertEqual(runtime["unpersisted"], 1)
        self.assertEqual(runtime["pending"], 0)
        self.assertTrue(runtime["degraded"])
        # Queue is drained, but close must not claim a clean persistence state.
        self.assertFalse(rec.close())

    def test_short_write_loops_until_complete(self):
        output = bytearray()

        def short_write(_fd, data):
            part = bytes(data[:3])
            output.extend(part)
            return len(part)

        with mock.patch("host.audit.recorder.os.write",
                        side_effect=short_write) as write:
            AuditRecorder._write_all(123, b"abcdefghij")
        self.assertEqual(bytes(output), b"abcdefghij")
        self.assertGreater(write.call_count, 1)

    def test_writer_retries_and_reports_degraded_state(self):
        rec = AuditRecorder(self.tmp.name)
        original = rec._append
        attempts = {"n": 0}

        def transient(ev):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("temporary disk failure")
            return original(ev)

        with mock.patch.object(rec, "_append", side_effect=transient):
            self.assertTrue(rec.record(_event(2)))
            self.assertTrue(rec.close(timeout=5))
        runtime = rec.stats(include_runtime=True)["runtime"]
        self.assertGreaterEqual(runtime["write_failure"], 1)
        self.assertEqual(runtime["written"], 1)
        self.assertFalse(runtime["degraded"])

    def test_invalid_event_cannot_block_following_valid_event(self):
        rec = AuditRecorder(self.tmp.name)
        bad = _event(1)
        bad["detail"] = {"value": object()}
        self.assertFalse(rec.record(bad))
        good = _event(2)
        self.assertTrue(rec.record(good))
        # Mutating the caller-owned object after record cannot change disk.
        good["target"]["path"] = "/mutated-after-record"
        self.assertTrue(rec.close(timeout=5))
        runtime = rec.runtime_stats()
        self.assertEqual(runtime["invalid"], 1)
        self.assertEqual(runtime["written"], 1)
        path = os.path.join(rec.audit_dir,
                            time.strftime("%Y-%m-%d") + ".jsonl")
        with open(path, encoding="utf-8") as stream:
            row = json.loads(stream.readline())
        self.assertEqual(row["target"]["path"], "/stage2-2")

    def test_out_of_range_timestamp_cannot_block_writer_queue(self):
        with tempfile.TemporaryDirectory() as conf:
            recorder = AuditRecorder(conf)
            self.assertFalse(recorder.record({
                "action": "exec.run", "ts": 10 ** 100,
            }))
            self.assertTrue(recorder.record({"action": "exec.run"}))
            self.assertTrue(recorder.close(timeout=2.0))
            runtime = recorder.runtime_stats()
            self.assertEqual(runtime["invalid"], 1)
            self.assertEqual(runtime["written"], 1)
            self.assertEqual(runtime["pending"], 0)

    def test_close_timeout_does_not_claim_drain_or_drop_pending(self):
        rec = AuditRecorder(self.tmp.name)
        original = rec._append
        with mock.patch.object(rec, "_append",
                               side_effect=OSError("disk offline")):
            self.assertTrue(rec.record(_event(3)))
            self.assertFalse(rec.close(timeout=0.05))
            runtime = rec.stats(include_runtime=True)["runtime"]
            self.assertEqual(runtime["pending"], 1)
            self.assertTrue(runtime["degraded"])
        # Once storage recovers the same event is retried and drained.
        rec._append = original
        self.assertTrue(rec.close(timeout=5))
        self.assertEqual(rec.stats(include_runtime=True)["runtime"]["written"], 1)


class _FakeSocket:
    def __init__(self, timeout=30.0):
        self.timeout = timeout
        self.seen = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value
        self.seen.append(value)


class _TimeoutWriter:
    def __init__(self, sock):
        self.sock = sock

    def write(self, _data):
        if self.sock.timeout == WRITE_TIMEOUT_S:
            raise socket.timeout("slow client")

    def flush(self):
        pass


class SseFairnessTest(unittest.TestCase):
    def test_kind_caps_and_total_cap(self):
        hub = SseHub(max_connections=16)
        monitor = [SseConn(io.BytesIO()) for _ in range(9)]
        agent = [SseConn(io.BytesIO()) for _ in range(5)]
        logs = [SseConn(io.BytesIO()) for _ in range(4)]
        self.assertEqual([hub.add(c, "monitor") for c in monitor],
                         [True] * 8 + [False])
        self.assertEqual([hub.add(c, "agent") for c in agent],
                         [True] * 4 + [False])
        self.assertEqual([hub.add(c, "logcenter") for c in logs],
                         [True] * 4)
        self.assertEqual(hub.count(), 16)
        stats = hub.stats()
        self.assertEqual(stats["active_by_kind"],
                         {"monitor": 8, "logcenter": 4,
                          "agent": 4, "generic": 0})
        self.assertEqual(stats["rejected_by_kind"],
                         {"monitor": 1, "logcenter": 0,
                          "agent": 1, "generic": 0})

    def test_monitor_and_logcenter_can_fill_fixed_mixed_load(self):
        hub = SseHub(max_connections=16)
        monitors = [SseConn(io.BytesIO()) for _ in range(8)]
        logs = [SseConn(io.BytesIO()) for _ in range(8)]
        self.assertTrue(all(hub.add(conn, "monitor") for conn in monitors))
        self.assertTrue(all(hub.add(conn, "logcenter") for conn in logs))
        self.assertEqual(hub.count(), 16)

    def test_unknown_kind_maps_to_fixed_generic_bucket(self):
        hub = SseHub(max_connections=2)
        conn = SseConn(io.BytesIO())
        self.assertTrue(hub.add(conn, "device-192.0.2.123"))
        stats = hub.stats()
        self.assertEqual(set(stats["active_by_kind"]),
                         {"monitor", "logcenter", "agent", "generic"})
        self.assertEqual(stats["active_by_kind"]["generic"], 1)
        self.assertNotIn("device-192.0.2.123", repr(stats))

    def test_legacy_generic_add_keeps_total_limit(self):
        hub = SseHub(max_connections=2)
        conns = [SseConn(io.BytesIO()) for _ in range(3)]
        self.assertEqual([hub.add(c) for c in conns], [True, True, False])
        hub.remove(conns[0])
        hub.remove(conns[0])       # exactly-once release
        self.assertEqual(hub.count(), 1)

    def test_write_deadline_restored_and_timeout_counted_once(self):
        sock = _FakeSocket()
        conn = SseConn(_TimeoutWriter(sock), sock)
        hub = SseHub(max_connections=2)
        self.assertTrue(hub.add(conn, "monitor"))
        self.assertFalse(conn.write({"t": 1}))
        self.assertTrue(conn.write_timed_out)
        self.assertEqual(sock.timeout, 30.0)
        self.assertIn(WRITE_TIMEOUT_S, sock.seen)
        hub.remove(conn)
        hub.remove(conn)
        self.assertEqual(hub.stats()["write_timeout"], 1)

    def test_hub_never_holds_registry_lock_during_network_write(self):
        hub = SseHub(max_connections=2)

        class Probe(io.BytesIO):
            def write(self, data):
                hub.stats()        # would deadlock if broadcast held hub._lock
                return super().write(data)

        conn = SseConn(Probe())
        hub.add(conn)
        thread = threading.Thread(target=hub.broadcast, args=({"t": 1},))
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive(), "network write held HUB lock")


if __name__ == "__main__":
    unittest.main(verbosity=2)
