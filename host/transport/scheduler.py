"""Bounded fair admission for SSH subprocess channels.

The scheduler owns only its condition lock.  Callers receive a lease after
admission and perform all transport work after that lock has been released.
"""
import collections
import threading
import time

from host.transport.base import TransportError


GLOBAL_LIMIT = 32
DEVICE_LIMIT = 8
BACKGROUND_DEVICE_LIMIT = 6
GLOBAL_WAIT_LIMIT = 128
DEVICE_WAIT_LIMIT = 16
DEFAULT_WAIT_TIMEOUT = 5.0
FOREGROUND_BURST = 8

FOREGROUND = "foreground"
BACKGROUND = "background"
_WORKLOADS = (FOREGROUND, BACKGROUND)

ERR_GLOBAL_QUEUE_FULL = "ssh_busy: global wait queue full"
ERR_DEVICE_QUEUE_FULL = "ssh_busy: device wait queue full"
ERR_WAIT_TIMEOUT = "ssh_busy: scheduler wait timeout"
ERR_DEVICE_REMOVED = "ssh_busy: device removed"


class _Waiter:
    def __init__(self, device_id, workload, started):
        self.device_id = device_id
        self.workload = workload
        self.started = started
        self.granted = False
        self.error = None


class Lease:
    """Exactly-once release token returned by :class:`TransportScheduler`."""

    def __init__(self, scheduler, device_id, workload):
        self._scheduler = scheduler
        self.device_id = device_id
        self.workload = workload
        self._lock = threading.Lock()
        self._released = False

    @property
    def released(self):
        with self._lock:
            return self._released

    def release(self):
        with self._lock:
            if self._released:
                return False
            self._released = True
        self._scheduler._release(self.device_id, self.workload)
        return True

    def __enter__(self):
        return self

    def __exit__(self, _typ, _value, _traceback):
        self.release()


class TransportScheduler:
    """Foreground-first, per-class device round-robin SSH scheduler."""

    def __init__(self, global_limit=GLOBAL_LIMIT, device_limit=DEVICE_LIMIT,
                 background_device_limit=BACKGROUND_DEVICE_LIMIT,
                 global_wait_limit=GLOBAL_WAIT_LIMIT,
                 device_wait_limit=DEVICE_WAIT_LIMIT,
                 wait_timeout=DEFAULT_WAIT_TIMEOUT, clock=None):
        self.global_limit = int(global_limit)
        self.device_limit = int(device_limit)
        self.background_device_limit = int(background_device_limit)
        self.global_wait_limit = int(global_wait_limit)
        self.device_wait_limit = int(device_wait_limit)
        self.wait_timeout = float(wait_timeout)
        if self.global_limit < 1 or self.device_limit < 1 or\
                not 0 <= self.background_device_limit <= self.device_limit or\
                self.global_wait_limit < 1 or self.device_wait_limit < 1 or\
                self.wait_timeout <= 0:
            raise ValueError("invalid SSH scheduler limits")
        self._clock = clock or time.monotonic
        self._cond = threading.Condition()
        self._active = 0
        self._active_by_device = {}
        self._background_by_device = {}
        self._waiting = 0
        self._waiting_by_device = {}
        self._queues = {kind: {} for kind in _WORKLOADS}
        self._round_robin = {kind: collections.deque()
                             for kind in _WORKLOADS}
        self._foreground_streak = 0
        self._peak_active = 0
        self._peak_waiting = 0
        self._peak_device_active = 0
        self._peak_device_waiting = 0
        self._peak_background_device = 0
        self._queue_full_total = 0
        self._wait_timeout_total = 0
        self._wait_count = {kind: 0 for kind in _WORKLOADS}
        self._wait_total_ms = {kind: 0.0 for kind in _WORKLOADS}
        self._wait_max_ms = {kind: 0.0 for kind in _WORKLOADS}
        self._wait_samples = {kind: collections.deque(maxlen=4096)
                              for kind in _WORKLOADS}

    @property
    def active(self):
        with self._cond:
            return self._active

    @property
    def waiting(self):
        with self._cond:
            return self._waiting

    def active_for(self, device_id):
        with self._cond:
            return self._active_by_device.get(str(device_id), 0)

    def waiting_for(self, device_id):
        with self._cond:
            return self._waiting_by_device.get(str(device_id), 0)

    def _can_grant_locked(self, device_id, workload):
        if self._active >= self.global_limit or\
                self._active_by_device.get(device_id, 0) >= self.device_limit:
            return False
        if workload == BACKGROUND and\
                self._background_by_device.get(device_id, 0) >=\
                self.background_device_limit:
            return False
        return True

    def _pop_eligible_locked(self, workload):
        rr = self._round_robin[workload]
        queues = self._queues[workload]
        for _ in range(len(rr)):
            device_id = rr.popleft()
            queue = queues.get(device_id)
            if not queue:
                queues.pop(device_id, None)
                continue
            if not self._can_grant_locked(device_id, workload):
                rr.append(device_id)
                continue
            waiter = queue.popleft()
            if queue:
                rr.append(device_id)
            else:
                del queues[device_id]
            return waiter
        return None

    def _grant_locked(self):
        changed = False
        while self._active < self.global_limit:
            waiter = None
            if self._foreground_streak >= FOREGROUND_BURST:
                waiter = self._pop_eligible_locked(BACKGROUND)
                if waiter is not None:
                    self._foreground_streak = 0
            if waiter is None:
                waiter = self._pop_eligible_locked(FOREGROUND)
                if waiter is not None:
                    self._foreground_streak += 1
            if waiter is None:
                waiter = self._pop_eligible_locked(BACKGROUND)
                if waiter is not None:
                    self._foreground_streak = 0
            if waiter is None:
                break
            device_id = waiter.device_id
            self._waiting -= 1
            remaining = self._waiting_by_device[device_id] - 1
            if remaining:
                self._waiting_by_device[device_id] = remaining
            else:
                del self._waiting_by_device[device_id]
            self._active += 1
            self._active_by_device[device_id] =\
                self._active_by_device.get(device_id, 0) + 1
            if waiter.workload == BACKGROUND:
                self._background_by_device[device_id] =\
                    self._background_by_device.get(device_id, 0) + 1
            self._peak_active = max(self._peak_active, self._active)
            self._peak_device_active = max(
                self._peak_device_active, self._active_by_device[device_id])
            self._peak_background_device = max(
                self._peak_background_device,
                self._background_by_device.get(device_id, 0))
            waited_ms = max(0.0, (self._clock() - waiter.started) * 1000.0)
            self._wait_count[waiter.workload] += 1
            self._wait_total_ms[waiter.workload] += waited_ms
            self._wait_max_ms[waiter.workload] = max(
                self._wait_max_ms[waiter.workload], waited_ms)
            self._wait_samples[waiter.workload].append(waited_ms)
            waiter.granted = True
            changed = True
        if changed:
            self._cond.notify_all()

    def _remove_waiter_locked(self, waiter):
        queues = self._queues[waiter.workload]
        queue = queues.get(waiter.device_id)
        if queue is None:
            return False
        try:
            queue.remove(waiter)
        except ValueError:
            return False
        if not queue:
            del queues[waiter.device_id]
            rr = self._round_robin[waiter.workload]
            self._round_robin[waiter.workload] = collections.deque(
                item for item in rr if item != waiter.device_id)
        self._waiting -= 1
        remaining = self._waiting_by_device[waiter.device_id] - 1
        if remaining:
            self._waiting_by_device[waiter.device_id] = remaining
        else:
            del self._waiting_by_device[waiter.device_id]
        return True

    def acquire(self, device_id, workload=FOREGROUND, timeout=None):
        device_id = str(device_id or "<unknown>")
        if workload not in _WORKLOADS:
            raise ValueError("workload must be foreground or background")
        timeout = self.wait_timeout if timeout is None else float(timeout)
        if timeout <= 0:
            raise ValueError("scheduler timeout must be > 0")
        deadline = self._clock() + timeout
        with self._cond:
            if self._waiting >= self.global_wait_limit:
                self._queue_full_total += 1
                raise TransportError(ERR_GLOBAL_QUEUE_FULL)
            if self._waiting_by_device.get(device_id, 0) >=\
                    self.device_wait_limit:
                self._queue_full_total += 1
                raise TransportError(ERR_DEVICE_QUEUE_FULL)
            waiter = _Waiter(device_id, workload, self._clock())
            queue = self._queues[workload].setdefault(
                device_id, collections.deque())
            if not queue:
                self._round_robin[workload].append(device_id)
            queue.append(waiter)
            self._waiting += 1
            self._waiting_by_device[device_id] =\
                self._waiting_by_device.get(device_id, 0) + 1
            self._peak_waiting = max(self._peak_waiting, self._waiting)
            self._peak_device_waiting = max(
                self._peak_device_waiting,
                self._waiting_by_device[device_id])
            self._grant_locked()
            while not waiter.granted and waiter.error is None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._remove_waiter_locked(waiter)
                    self._grant_locked()
                    self._wait_timeout_total += 1
                    raise TransportError(ERR_WAIT_TIMEOUT)
                self._cond.wait(remaining)
            if waiter.error is not None:
                raise TransportError(waiter.error)
            return Lease(self, device_id, workload)

    def _release(self, device_id, workload):
        with self._cond:
            current = self._active_by_device.get(device_id, 0)
            if self._active <= 0 or current <= 0:
                raise RuntimeError("SSH scheduler lease accounting underflow")
            self._active -= 1
            if current == 1:
                del self._active_by_device[device_id]
            else:
                self._active_by_device[device_id] = current - 1
            if workload == BACKGROUND:
                background = self._background_by_device.get(device_id, 0)
                if background <= 0:
                    raise RuntimeError("SSH scheduler background accounting underflow")
                if background == 1:
                    del self._background_by_device[device_id]
                else:
                    self._background_by_device[device_id] = background - 1
            self._grant_locked()
            self._cond.notify_all()

    def stats(self):
        """Fixed-cardinality aggregate snapshot; never exposes device IDs."""
        with self._cond:
            waits = {}
            for workload in _WORKLOADS:
                count = self._wait_count[workload]
                samples = sorted(self._wait_samples[workload])
                if samples:
                    rank = max(0, min(len(samples) - 1,
                                      int((len(samples) - 1) * .95 + .999999)))
                    p95 = samples[rank]
                else:
                    p95 = 0.0
                waits[workload] = {
                    "count": count,
                    "mean_ms": round(
                        self._wait_total_ms[workload] / count, 3)
                    if count else 0.0,
                    "max_ms": round(self._wait_max_ms[workload], 3),
                    "p95_ms": round(p95, 3),
                    "sample_count": len(samples),
                    "samples_capped": count > len(samples),
                }
            return {
                "active": self._active,
                "waiting": self._waiting,
                "background_active": sum(
                    self._background_by_device.values()),
                "peak_active": self._peak_active,
                "peak_waiting": self._peak_waiting,
                "peak_device_active": self._peak_device_active,
                "peak_device_waiting": self._peak_device_waiting,
                "peak_background_device": self._peak_background_device,
                "queue_full_total": self._queue_full_total,
                "wait_timeout_total": self._wait_timeout_total,
                "wait": waits,
                "limits": {
                    "global": self.global_limit,
                    "device": self.device_limit,
                    "background_device": self.background_device_limit,
                    "global_wait": self.global_wait_limit,
                    "device_wait": self.device_wait_limit,
                },
            }

    def remove_device(self, device_id):
        """Cancel queued work; active leases disappear through normal release."""
        device_id = str(device_id or "<unknown>")
        with self._cond:
            for workload in _WORKLOADS:
                queue = self._queues[workload].pop(device_id, None)
                if not queue:
                    continue
                self._round_robin[workload] = collections.deque(
                    item for item in self._round_robin[workload]
                    if item != device_id)
                for waiter in queue:
                    waiter.error = ERR_DEVICE_REMOVED
                    self._waiting -= 1
                self._waiting_by_device.pop(device_id, None)
            self._grant_locked()
            self._cond.notify_all()


__all__ = [
    "TransportScheduler", "Lease", "GLOBAL_LIMIT", "DEVICE_LIMIT",
    "BACKGROUND_DEVICE_LIMIT", "GLOBAL_WAIT_LIMIT", "DEVICE_WAIT_LIMIT",
    "FOREGROUND_BURST", "FOREGROUND", "BACKGROUND",
]
