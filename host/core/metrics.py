"""Fixed-cardinality runtime metrics for the portal process.

Metric names are deliberately closed: callers cannot introduce labels or
request-derived keys.  ``snapshot`` copies state under the lock and performs
process inspection only after releasing it.
"""
import os
import threading
import time


_COUNTERS = (
    "http_requests", "http_rejected", "http_status_2xx",
    "http_status_3xx", "http_status_4xx", "http_status_5xx",
    "sse_accepted", "sse_rejected", "sse_write_timeout",
    "ssh_mux_eligible", "ssh_reused_hint", "ssh_password", "ssh_fail",
    "poll_legacy", "poll_delta", "poll_wire_bytes_saved",
    "audit_enqueued", "audit_fallback", "audit_write_failure",
)
_GAUGES = (
    "http_active", "http_max_workers", "sse_active",
    "audit_queue_depth", "audit_degraded",
)


class RuntimeMetrics:
    """Small thread-safe registry with no dynamic metric names."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._started = self._clock()
        self._lock = threading.Lock()
        self._counters = {name: 0 for name in _COUNTERS}
        self._gauges = {name: 0 for name in _GAUGES}

    def increment(self, name, amount=1):
        if name not in self._counters:
            raise KeyError("unknown counter: %s" % name)
        amount = int(amount)
        if amount < 0:
            raise ValueError("counter increments must be non-negative")
        with self._lock:
            self._counters[name] += amount

    def gauge_add(self, name, amount):
        if name not in self._gauges:
            raise KeyError("unknown gauge: %s" % name)
        with self._lock:
            self._gauges[name] += int(amount)

    def gauge_set(self, name, value):
        if name not in self._gauges:
            raise KeyError("unknown gauge: %s" % name)
        with self._lock:
            self._gauges[name] = int(value)

    def observe_http_status(self, status):
        self.increment("http_requests")
        try:
            group = int(status) // 100
        except (TypeError, ValueError):
            return
        if group in (2, 3, 4, 5):
            self.increment("http_status_%dxx" % group)

    @staticmethod
    def _process_snapshot():
        rss_kb = threads = fd_count = 0
        try:
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                    elif line.startswith("Threads:"):
                        threads = int(line.split()[1])
            fd_count = len(os.listdir("/proc/self/fd"))
        except (OSError, ValueError, IndexError):
            pass
        return {"rss_kb": rss_kb, "threads": threads, "fd_count": fd_count}

    def snapshot(self):
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            uptime_s = max(0.0, self._clock() - self._started)
        # /proc reads and response JSON serialization happen outside _lock.
        process = self._process_snapshot()
        return {
            "ok": True,
            "schema_version": 1,
            "uptime_s": round(uptime_s, 3),
            "process": process,
            "http": {
                "active": gauges["http_active"],
                "max_workers": gauges["http_max_workers"],
                "requests_total": counters["http_requests"],
                "rejected_total": counters["http_rejected"],
                "status": {
                    "2xx": counters["http_status_2xx"],
                    "3xx": counters["http_status_3xx"],
                    "4xx": counters["http_status_4xx"],
                    "5xx": counters["http_status_5xx"],
                },
            },
            "sse": {
                "active": gauges["sse_active"],
                "accepted_total": counters["sse_accepted"],
                "rejected_total": counters["sse_rejected"],
                "write_timeout_total": counters["sse_write_timeout"],
            },
            "ssh": {
                "mux_eligible_total": counters["ssh_mux_eligible"],
                "reused_hint_total": counters["ssh_reused_hint"],
                "password_total": counters["ssh_password"],
                "fail_total": counters["ssh_fail"],
            },
            "poll": {
                "legacy_total": counters["poll_legacy"],
                "delta_total": counters["poll_delta"],
                "wire_bytes_saved_total": counters["poll_wire_bytes_saved"],
            },
            "audit": {
                "queue_depth": gauges["audit_queue_depth"],
                "enqueued_total": counters["audit_enqueued"],
                "fallback_total": counters["audit_fallback"],
                "write_failure_total": counters["audit_write_failure"],
                "degraded": bool(gauges["audit_degraded"]),
            },
        }


METRICS = RuntimeMetrics()

__all__ = ["METRICS", "RuntimeMetrics"]
