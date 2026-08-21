"""Utilities for host.task.sampler."""
import threading
import time
from collections import deque

from host.service.sysinfo import collect as collect_sysinfo

_FAIL_BACKOFF = 5.0
_STOP_AFTER = 60.0
_COLLECT_TIMEOUT = 25

_EXTRA_SCRIPT = (
    "cat /proc/stat | grep '^cpu';"
    " echo __SEP__; cat /proc/net/dev;"
    " echo __SEP__; sleep 0.2; cat /proc/stat | grep '^cpu';"
    " echo __SEP__; cat /proc/net/dev;"
    " echo __SEP__; grep -E '^procs_running' /proc/stat 2>/dev/null;"
    " echo __SEP__; ls -d /proc/[0-9]* 2>/dev/null | wc -l"
)


def _cpu_delta(line1, line2):
    """Handle cpu delta."""
    try:
        f1 = line1.split()[1:5]
        f2 = line2.split()[1:5]
        a = sum(int(x) for x in f1)
        b = sum(int(x) for x in f2)
        dt = b - a
        if dt <= 0:
            return None
        idle1, idle2 = int(f1[3]), int(f2[3])
        return round(100.0 * (1 - (idle2 - idle1) / dt), 1)
    except Exception:
        return None


def _per_core(stat1, stat2):
    """Handle per core."""
    lines1 = [ln for ln in stat1.splitlines()
              if ln.startswith("cpu") and not ln.startswith("cpu ")]
    lines2 = [ln for ln in stat2.splitlines()
              if ln.startswith("cpu") and not ln.startswith("cpu ")]
    if not lines1 or len(lines1) != len(lines2):
        return None, None
    return [_cpu_delta(a, b) for a, b in zip(lines1, lines2)], len(lines1)


def _net_counters(out):
    """Handle net counters."""
    rx = tx = 0
    for ln in out.splitlines():
        if ":" not in ln or not ln.strip():
            continue
        iface, rest = ln.split(":", 1)
        if iface.strip().startswith(("Inter", "face")):
            continue
        cols = rest.split()
        if len(cols) < 9:
            continue
        try:
            rx += int(cols[0])
            tx += int(cols[8])
        except ValueError:
            continue
    return rx, tx


class DeviceSampler:
    """Manage device sampler."""

    def __init__(self, device_id, transport_factory, interval=1.0,
                 max_points=600):
        self.device_id = device_id
        self._factory = transport_factory
        self.interval = float(interval)
        self.max_points = int(max_points)
        self.state = "idle"          # idle | running | stopped
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._buf = deque(maxlen=self.max_points)
        self._latest = None
        self._started = 0.0
        self._last_ok = 0.0
        self._fail_streak = 0
        self._prev_net = None        # (ts_ms, rx_bytes, tx_bytes) → pps



    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            self._stop.clear()
            self.state = "running"
            self._started = time.time()
            self._thread = threading.Thread(
                target=self._run, name="sampler-%s" % self.device_id,
                daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout=5)
        with self._lock:
            self.state = "stopped"
        return self



    def snapshot(self, window_seconds=None):
        with self._lock:
            buf = list(self._buf)
        if window_seconds:
            cutoff = (time.time() - window_seconds) * 1000
            buf = [s for s in buf if s["ts"] >= cutoff]
        return buf

    def latest(self):
        with self._lock:
            s = self._latest
        return dict(s) if s is not None else None



    def _run(self):
        while not self._stop.is_set():
            t0 = time.time()
            ok = self._collect_once()
            now = time.time()
            if ok:
                self._last_ok = now
                self._fail_streak = 0
            else:
                self._fail_streak += 1
                ref = self._last_ok or self._started
                if now - ref >= _STOP_AFTER:
                    break
            if not ok and self._fail_streak >= 3:
                wait = _FAIL_BACKOFF
            else:
                wait = self.interval
            self._stop.wait(max(0.05, wait - (now - t0)))
        self.state = "stopped"

    def _collect_once(self):
        sample = self.collect_now()
        with self._lock:
            self._buf.append(sample)
            self._latest = sample
        return not sample.get("gap")



    def collect_now(self, ts=None):
        ts = int(ts if ts is not None else time.time() * 1000)
        try:
            transport = self._factory()
            base = collect_sysinfo(transport, timeout=_COLLECT_TIMEOUT)
            rc, out, err = transport.exec(_EXTRA_SCRIPT, timeout=_COLLECT_TIMEOUT)
            if rc != 0 and not out:
                raise IOError((err or "补充采集失败").strip()[:200])
        except Exception:
            return self._gap_sample(ts)

        parts = [p.strip() for p in out.split("__SEP__")]

        def part(i):
            return parts[i] if i < len(parts) else ""

        per_core, cores = _per_core(part(0), part(2))
        rx, tx = _net_counters(part(3))
        proc_total = None
        try:
            proc_total = int(part(5).strip())
        except (TypeError, ValueError):
            pass
        proc_running = None
        try:
            proc_running = int(part(4).split(":", 1)[1].strip())
        except (IndexError, TypeError, ValueError):
            pass

        mem_total = base.get("mem_total_mb")
        mem_used = base.get("mem_used_mb")
        mem_avail = max(0, mem_total - mem_used)\
            if mem_total is not None and mem_used is not None else None
        sample = {
            "ts": ts,
            "device_id": self.device_id,
            "gap": False,
            "cpu": {
                "usage": base.get("cpu_usage"),
                "freq_mhz": base.get("cpu_freq_mhz"),
                "cores": cores,
                "per_core": per_core,
            },
            "mem": {"total_mb": mem_total, "used_mb": mem_used,
                    "avail_mb": mem_avail},
            "temp": base.get("temp_c") or {},
            "load": base.get("load"),
            "disks": base.get("disks") or [],
            "net": {"rx_bytes": rx, "tx_bytes": tx, "rx_pps": 0, "tx_pps": 0},
            "proc": {"total": proc_total, "running": proc_running},
        }
        self._update_pps(sample, ts)
        return sample

    def _update_pps(self, sample, ts):
        """Handle update pps."""
        with self._lock:
            prev = self._prev_net
            self._prev_net = (ts, sample["net"]["rx_bytes"],
                              sample["net"]["tx_bytes"])
            if prev is None:
                return
            p_ts, p_rx, p_tx = prev
            dt = (ts - p_ts) / 1000.0
            if dt <= 0:
                return
            sample["net"]["rx_pps"] = round(max(0, sample["net"]["rx_bytes"]
                                                - p_rx) / dt)
            sample["net"]["tx_pps"] = round(max(0, sample["net"]["tx_bytes"]
                                                - p_tx) / dt)

    def _gap_sample(self, ts):
        with self._lock:
            self._prev_net = None
        return {"ts": ts, "device_id": self.device_id, "gap": True,
                "cpu": None, "mem": None, "temp": None, "load": None,
                "disks": None, "net": None, "proc": None}
