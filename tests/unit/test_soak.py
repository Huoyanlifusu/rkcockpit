#!/usr/bin/env python3
"""Socket-free Stage 3 soak collector and analyzer regressions."""
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from tools import analyze_stage3_soak as analyzer
from tools import bench_stage3_soak as soak


def ids():
    return ["node-%02d" % index for index in range(30)]


class FakeClock(object):
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds
        return False


class FakeLoad(object):
    instances = []

    def __init__(self, target, device_ids, token, source, timeout, clock):
        self.clock = clock
        self.closed = False
        self.resets = 0
        self.__class__.instances.append(self)

    def start(self):
        pass

    def reset_window(self):
        self.resets += 1

    def snapshot(self):
        return {"mixed": {"requests": 10, "p95_ms": 2,
                          "p99_ms": 3, "unexpected": 0},
                "sse": {"active": 16, "connected_total": 16,
                        "disconnected_total": 0,
                        "connect_failed_total": 0,
                        "monitor_max_gap_s": 1}}

    def summary(self):
        return {"requests": 20, "p95_ms": 2, "p99_ms": 3,
                "unexpected": 0, "latency_samples": 20,
                "latency_samples_capped": False}

    def close(self):
        self.closed = True
        return 0


def fake_fetch(_target, path, _token, _timeout):
    if path == "/api/metrics":
        return 200, 2.0, {"audit": {"queue_depth": 0,
                                    "degraded": False},
                            "sse": {"active": 16}}
    return 200, 1.0, {"ok": True}


def fake_resource():
    return {"cpu_cores": .1, "rss_kb": 1024, "threads": 4,
            "fd": 8, "children": 0, "zombies": 0, "available": True}


class CollectorUnitTest(unittest.TestCase):
    def setUp(self):
        FakeLoad.instances[:] = []

    def test_fake_clock_fixed_schema_bounds_and_privacy(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as td:
            output = os.path.join(td, "report.json")
            report = soak.run_soak(
                "https://secret-host.example", ids(), output,
                token="secret-token", log_source="/secret/source.log",
                warmup_s=0, sample_s=10, repeat=1, topology="PC_SIM",
                clock=clock, wait_fn=clock.wait,
                resource_fn=fake_resource, fetch_fn=fake_fetch,
                load_factory=FakeLoad, install_signals=False)
            self.assertTrue(report["complete"])
            self.assertEqual(report["termination"], "complete")
            self.assertEqual(len(report["samples"]), 2)
            self.assertLessEqual(len(report["samples"]), soak.MAX_SAMPLES)
            self.assertTrue(FakeLoad.instances[0].closed)
            with open(output, encoding="utf-8") as fh:
                persisted = json.load(fh)
            self.assertEqual(persisted, report)
            rendered = json.dumps(report)
            for secret in ("secret-host.example", "secret-token",
                           "/secret/source.log", ids()[0]):
                self.assertNotIn(secret, rendered)

    def test_partial_interruption_is_atomic_and_cleans(self):
        clock = FakeClock()

        def interrupted_wait(seconds):
            clock.now += seconds
            return True

        with tempfile.TemporaryDirectory() as td:
            output = os.path.join(td, "partial.json")
            report = soak.run_soak(
                "http://127.0.0.1:8080", ids(), output, warmup_s=1,
                sample_s=10, topology="PC_SIM", clock=clock,
                wait_fn=interrupted_wait, resource_fn=fake_resource,
                fetch_fn=fake_fetch, load_factory=FakeLoad,
                install_signals=False)
            self.assertFalse(report["complete"])
            self.assertEqual(report["termination"], "signal")
            self.assertEqual(report["samples"], [])
            self.assertTrue(FakeLoad.instances[-1].closed)
            self.assertFalse(any(name.startswith(".soak-")
                                 for name in os.listdir(td)))

    def test_input_limits_and_topology_claims(self):
        with self.assertRaisesRegex(ValueError, "7200"):
            soak.validate_run(60, 3000, 3, 5, "PC_SIM", 0)
        with self.assertRaisesRegex(ValueError, "0..30"):
            soak.validate_run(0, 1, 1, 1, "SSH_HITL", 31)
        self.assertFalse(soak.topology_summary("PC_SIM", 30)[
            "topology_valid"])
        self.assertFalse(soak.topology_summary("RK_SIM", 30)[
            "topology_valid"])
        hitl = soak.topology_summary("SSH_HITL", 30)
        self.assertTrue(hitl["topology_candidate"])
        self.assertFalse(hitl["topology_valid"])
        load = soak.Load(SimpleNamespace(scheme="http", hostname="127.0.0.1",
                                         port=8080), ids(), "", "/dev/null",
                         1, FakeClock())
        self.assertEqual(len(load.short), 32)
        self.assertEqual(len(load.paths), 16)

    def test_proc_sampler_fake_proc_cpu_children_and_zombie(self):
        status = "VmRSS:\t2048 kB\nThreads:\t7\n"
        # fields after ')' start with state; indexes 11/12 are utime/stime.
        fields = ["S"] + ["0"] * 10 + ["100", "50"] + ["0"] * 8
        parent_stat = "1 (portal) " + " ".join(fields)
        child_stat = "2 (child) Z 0 0 0"

        def fake_open(path, *args, **kwargs):
            from io import StringIO
            if path.endswith("/status"):
                return StringIO(status)
            if path.endswith("/children"):
                return StringIO("2")
            if path == "/proc/1/stat":
                return StringIO(parent_stat)
            if path == "/proc/2/stat":
                return StringIO(child_stat)
            raise FileNotFoundError(path)

        clock = FakeClock()
        with mock.patch("builtins.open", side_effect=fake_open), \
                mock.patch.object(soak.os, "listdir", return_value=["1"]), \
                mock.patch.object(soak.os, "sysconf", return_value=100):
            sampler = soak.ProcSampler(1, clock)
            first = sampler()
            clock.now = 1
            second = sampler()
        self.assertEqual(first["rss_kb"], 2048)
        self.assertIsNone(first["cpu_cores"])
        self.assertEqual(second["cpu_cores"], 0.0)
        self.assertEqual(second["threads"], 7)
        self.assertEqual(second["fd"], 1)
        self.assertEqual(second["children"], 1)
        self.assertEqual(second["zombies"], 1)

    def test_metric_subset_maps_fixed_runtime_scheduler_schema(self):
        subset = soak.metric_subset({
            "audit": {"pending": 0, "degraded": False},
            "sse": {"active": 16},
            "ssh": {"scheduler": {
                "active": 3, "peak_device_active": 2,
                "peak_background_device": 1, "queue_full_total": 0,
                "wait_timeout_total": 0,
                "wait": {"foreground": {"p95_ms": 4, "max_ms": 7},
                         "background": {"p95_ms": 5, "max_ms": 9}},
            }},
        })
        self.assertEqual(subset["audit_pending"], 0)
        self.assertEqual(subset["scheduler"]["global_active"], 3)
        self.assertEqual(
            subset["scheduler"]["foreground_admission_p95_ms"], 4)
        self.assertEqual(subset["scheduler"]["max_starvation_s"], .009)


def passing_report(topology="SSH_HITL", complete=True):
    scheduler = {"global_active": 32, "device_active_max": 8,
                 "background_active_max": 6, "queue_full_total": 0,
                 "wait_timeout_total": 0,
                 "foreground_admission_p95_ms": 500,
                 "max_starvation_s": 5}
    samples = []
    for elapsed in (300, 1800):
        samples.append({
            "elapsed_s": elapsed,
            "process": {"cpu_cores": .5, "rss_kb": 100000,
                        "threads": 50, "fd": 100, "zombies": 0,
                        "available": True},
            "sentinel": {"health_status": 200, "health_ms": 10,
                         "metrics_status": 200, "metrics_ms": 15},
            "sse": {"active": 16, "monitor_max_gap_s": 2},
            "metrics": {"audit_pending": 0, "audit_degraded": False,
                        "scheduler": scheduler}})
    return {"schema_version": 1, "scenario": "stage3-soak-readonly",
            "complete": complete, "termination": "complete",
            "topology": {"kind": topology, "unique_profiles": 30,
                         "topology_candidate": topology == "SSH_HITL",
                         "topology_valid": False,
                         "verification":
                         "operator_supplied_requires_manual_hitl_signoff"},
            "config": {"device_count": 30, "http_clients": 32,
                       "monitor_sse": 8, "logcenter_sse": 8,
                       "warmup_s": 60, "sample_s": 1800, "repeat": 1,
                       "restart_count": 0},
            "samples": samples,
            "summary": {"mixed": {"p95_ms": 100, "p99_ms": 200,
                                    "unexpected": 0, "requests": 100},
                        "cleanup_threads_alive": 0, "cleanup_s": 0}}


class AnalyzerUnitTest(unittest.TestCase):
    def test_cpu_mean_ignores_initial_sample_without_delta(self):
        report = passing_report()
        report["samples"][0]["process"]["cpu_cores"] = None
        report["samples"][1]["process"]["cpu_cores"] = 1.2
        result = analyzer.analyze(report)
        cpu = next(item for item in result["checks"]
                   if item["name"] == "cpu_mean_cores")
        self.assertEqual(cpu["observed"], 1.2)
        self.assertEqual(cpu["status"], "FAIL")

    def test_numeric_pass_is_still_gap_until_manual_hitl_signoff(self):
        result = analyzer.analyze(passing_report())
        self.assertEqual(result["result"], "GAP")
        self.assertEqual(next(item for item in result["checks"]
                              if item["name"] == "ssh_hitl_topology")[
                                  "status"], "GAP")
        self.assertEqual(analyzer.analyze(passing_report("PC_SIM"))["result"],
                         "GAP")
        self.assertEqual(analyzer.analyze(passing_report("RK_SIM"))["result"],
                         "GAP")
        partial = passing_report(complete=False)
        self.assertEqual(analyzer.analyze(partial)["result"], "GAP")

    def test_gap_uses_distinct_nonzero_exit_status(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "report.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(passing_report(), fh)
            with mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(analyzer.main([path]), 2)

    def test_threshold_failures_override_evidence_gap(self):
        report = passing_report("PC_SIM")
        report["summary"]["mixed"]["p95_ms"] = 500.001
        self.assertEqual(analyzer.analyze(report)["result"], "FAIL")
        report = passing_report()
        report["samples"][-1]["process"]["zombies"] = 1
        self.assertEqual(analyzer.analyze(report)["result"], "FAIL")

    def test_missing_evidence_is_gap_and_reader_is_bounded_nofollow(self):
        report = passing_report()
        report["samples"][-1]["metrics"].pop("scheduler")
        self.assertEqual(analyzer.analyze(report)["result"], "GAP")
        with tempfile.TemporaryDirectory() as td:
            real = os.path.join(td, "real")
            link = os.path.join(td, "link")
            with open(real, "w", encoding="utf-8") as fh:
                json.dump(report, fh)
            os.symlink(real, link)
            with self.assertRaises(OSError):
                analyzer.read_report(link)


if __name__ == "__main__":
    unittest.main()
