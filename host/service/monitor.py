"""Utilities for host.service.monitor."""
import threading
import time

from host.task.sampler import DeviceSampler

VALID_WINDOWS = (60, 180, 300, 600)
METRIC_FIELDS = {
    "cpu": ("cpu",),
    "mem": ("mem",),
    "temp": ("temp",),
    "load": ("load",),
    "net": ("net",),
}
_NOW_FRESH_MS = 2000


class MonitorService:

    def __init__(self):
        self._lock = threading.Lock()
        self._samplers = {}
        self._closed = False

    def get_or_start(self, transport_factory, device_id):
        """Return get or start."""
        with self._lock:
            if self._closed:
                raise RuntimeError("monitor service is closed")
            sampler = self._samplers.get(device_id)
            if sampler is None:
                sampler = DeviceSampler(device_id, transport_factory)
                self._samplers[device_id] = sampler
            sampler.start()
            return sampler

    def enable(self, transport_factory, device_id):
        return self.get_or_start(transport_factory, device_id)

    def disable(self, device_id):
        with self._lock:
            sampler = self._samplers.get(device_id)
            if sampler is not None:
                sampler.stop()
        return True

    def remove_device(self, device_id):
        """Stop and forget one sampler when its device is deleted."""
        with self._lock:
            sampler = self._samplers.pop(device_id, None)
            if sampler is not None:
                sampler.stop()
        return True

    def close(self):
        """Idempotently stop every sampler and reject future starts."""
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            samplers = list(self._samplers.values())
            self._samplers.clear()
        for sampler in samplers:
            sampler.stop()
        return True

    def series(self, device_id, metric=None, window=None):
        """Handle series."""
        with self._lock:
            sampler = self._samplers.get(device_id)
        samples = sampler.snapshot(window) if sampler is not None else []
        if metric and samples:
            keep = ("ts", "device_id", "gap") + METRIC_FIELDS[metric]
            samples = [{k: s.get(k) for k in keep} for s in samples]
        return samples

    def latest(self, device_id):
        """Handle latest."""
        with self._lock:
            sampler = self._samplers.get(device_id)
        return sampler.latest() if sampler is not None else None

    def now(self, device_id):
        """Handle now."""
        with self._lock:
            sampler = self._samplers.get(device_id)
        if sampler is None:
            return None
        latest = sampler.latest()
        if latest and not latest.get("gap") and\
                time.time() * 1000 - latest["ts"] < _NOW_FRESH_MS:
            return latest
        return sampler.collect_now()
