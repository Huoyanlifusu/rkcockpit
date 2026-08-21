#!/usr/bin/env python3
"""Stage2 device churn, service close, and live metrics overlay tests."""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent
while not (ROOT / ".git").exists() and ROOT != ROOT.parent:
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from host.api.handlers.legacy import HostApi  # noqa: E402
from host.service.logcenter import LogCenter  # noqa: E402
from host.service.monitor import MonitorService  # noqa: E402
from portal.portal import runtime_metrics_snapshot  # noqa: E402


class _FakeSampler:
    made = []

    def __init__(self, device_id, factory):
        self.device_id = device_id
        self.factory = factory
        self.started = 0
        self.stopped = 0
        self.__class__.made.append(self)

    def start(self):
        self.started += 1
        return self

    def stop(self):
        self.stopped += 1
        return self

    def snapshot(self, _window=None):
        return []

    def latest(self):
        return None

    def collect_now(self):
        return None


class _FakeFollower:
    made = []

    def __init__(self, device_id, transport, source, pattern):
        self.device_id = device_id
        self.transport = transport
        self.source = source
        self.pattern = pattern
        self.started = 0
        self.stopped = 0
        self.__class__.made.append(self)

    def start(self):
        self.started += 1
        return self

    def stop(self):
        self.stopped += 1

    def info(self):
        return {"device_id": self.device_id}


class ServiceLifecycleUnitTest(unittest.TestCase):
    def setUp(self):
        _FakeSampler.made = []
        _FakeFollower.made = []

    def test_monitor_create_remove_and_close_are_thread_safe(self):
        with mock.patch("host.service.monitor.DeviceSampler", _FakeSampler):
            svc = MonitorService()
            threads = [threading.Thread(
                target=svc.get_or_start, args=(lambda: object(), "dev1"))
                for _ in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(_FakeSampler.made), 1)
            sampler = _FakeSampler.made[0]
            self.assertTrue(svc.remove_device("dev1"))
            self.assertEqual(sampler.stopped, 1)
            svc.get_or_start(lambda: object(), "dev2")
            sampler2 = _FakeSampler.made[-1]
            self.assertTrue(svc.close())
            self.assertEqual(sampler2.stopped, 1)
            self.assertTrue(svc.close())
            with self.assertRaises(RuntimeError):
                svc.get_or_start(lambda: object(), "dev3")

    def test_logcenter_remove_and_close_stop_followers(self):
        with mock.patch("host.service.logcenter._Follower", _FakeFollower):
            svc = LogCenter()
            first = svc.follow("dev1", object(), "/var/log/messages")
            self.assertTrue(svc.remove_device("dev1"))
            self.assertEqual(first.stopped, 1)
            second = svc.follow("dev2", object(), "/var/log/messages")
            self.assertTrue(svc.close())
            self.assertEqual(second.stopped, 1)
            self.assertTrue(svc.close())
            with self.assertRaises(RuntimeError):
                svc.follow("dev3", object(), "/var/log/messages")

    def test_hostapi_device_delete_and_close_dispatch_callbacks(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage2-life-") as tmp:
            host = HostApi(tmp, sim=True)
            removed = []
            closed = []
            host.register_cleanup(removed.append,
                                  lambda: closed.append(True) or True)
            result = host.devices_delete("demo")
            if isinstance(result, tuple):
                result = result[0]
            self.assertTrue(result["ok"])
            self.assertEqual(removed, ["demo"])
            self.assertTrue(host.close())
            self.assertEqual(closed, [True])
            self.assertTrue(host.close())
            self.assertEqual(closed, [True])

    def test_handler_services_are_isolated_per_hostapi(self):
        from host.api.handlers import logcenter as log_handler
        from host.api.handlers import monitor as monitor_handler

        with tempfile.TemporaryDirectory(prefix="rkss-stage2-host1-") as d1, \
                tempfile.TemporaryDirectory(prefix="rkss-stage2-host2-") as d2:
            host1 = HostApi(d1)
            host2 = HostApi(d2)
            mon1, mon2 = monitor_handler._svc(host1), \
                monitor_handler._svc(host2)
            log1, log2 = log_handler._svc(host1), log_handler._svc(host2)
            self.assertIsNot(mon1, mon2)
            self.assertIsNot(log1, log2)
            self.assertTrue(host1.close())
            self.assertTrue(mon1._closed)
            self.assertTrue(log1._closed)
            self.assertFalse(mon2._closed)
            self.assertFalse(log2._closed)
            # Closing the old HostApi never closes the newer instance.
            self.assertIs(monitor_handler._svc(host2), mon2)
            self.assertIs(log_handler._svc(host2), log2)
            self.assertTrue(host2.close())


class MetricsOverlayTest(unittest.TestCase):
    def test_fixed_schema_uses_runtime_audit_only(self):
        class Audit:
            def runtime_stats(self):
                return {
                    "queue": 3, "queue_capacity": 4096, "pending": 4,
                    "enqueued": 9, "fallback": 2, "written": 7,
                    "write_failure": 1, "unpersisted": 1,
                    "invalid": 2,
                    "degraded": True, "accepting": True,
                }

            def stats(self, *_args, **_kwargs):
                raise AssertionError("metrics must not scan audit history")

        host = type("Host", (), {"audit": Audit()})()
        snapshot = runtime_metrics_snapshot(host)
        self.assertEqual(snapshot["audit"]["queue_depth"], 3)
        self.assertEqual(snapshot["audit"]["unpersisted_total"], 1)
        self.assertEqual(snapshot["audit"]["invalid_total"], 2)
        self.assertTrue(snapshot["audit"]["degraded"])
        self.assertEqual(set(snapshot["sse"]["active_by_kind"]),
                         {"monitor", "logcenter", "agent", "generic"})
        self.assertEqual(set(snapshot["sse"]["rejected_by_kind"]),
                         {"monitor", "logcenter", "agent", "generic"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
