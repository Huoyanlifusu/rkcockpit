#!/usr/bin/env python3
"""Bounded Stage 3 soak collector (30 devices, 16 SSE, 32 GET clients)."""
import argparse
import collections
import http.client
import json
import math
import os
import select
import signal
import sys
import tempfile
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from tools import bench_stage1
from tools import bench_stage2_mixed as mixed


SCHEMA_VERSION = 1
SAMPLE_INTERVAL_S = 5.0
MAX_SAMPLES = 4096
MAX_REPORT_BYTES = 16 << 20
TOPOLOGIES = ("PC_SIM", "RK_SIM", "SSH_HITL")


def validate_run(warmup_s, sample_s, repeat, timeout, topology,
                 unique_profiles):
    values = (float(warmup_s), float(sample_s), float(timeout))
    if not all(math.isfinite(item) for item in values):
        raise ValueError("durations must be finite")
    warmup_s, sample_s, timeout = values
    repeat, unique_profiles = int(repeat), int(unique_profiles)
    if warmup_s < 0 or sample_s <= 0 or not 1 <= repeat <= 3:
        raise ValueError("warmup/sample/repeat are out of range")
    if not 0 < timeout <= bench_stage1.MAX_TIMEOUT_S:
        raise ValueError("timeout must be in (0, 60]")
    if repeat * (warmup_s + sample_s + timeout + 10) > 7200:
        raise ValueError("configured soak exceeds 7200 seconds")
    if math.ceil(sample_s / SAMPLE_INTERVAL_S) * repeat > MAX_SAMPLES:
        raise ValueError("configured soak exceeds sample bound")
    if topology not in TOPOLOGIES:
        raise ValueError("topology must be PC_SIM, RK_SIM, or SSH_HITL")
    if not 0 <= unique_profiles <= mixed.DEVICE_COUNT:
        raise ValueError("unique-profiles must be in 0..30")
    return warmup_s, sample_s, repeat, timeout, unique_profiles


def topology_summary(name, unique_profiles):
    return {
        "kind": name,
        "unique_profiles": int(unique_profiles),
        "topology_candidate": name == "SSH_HITL" and unique_profiles == 30,
        # CLI values are operator-supplied, not an attestation.  This collector
        # can produce a candidate artifact but can never self-certify HITL.
        "topology_valid": False,
        "verification": "operator_supplied_requires_manual_hitl_signoff",
    }


def _connection(target, timeout):
    cls = http.client.HTTPSConnection if target.scheme == "https" else \
        http.client.HTTPConnection
    return cls(target.hostname, target.port, timeout=timeout)


def fetch_json(target, path, token, timeout):
    conn = _connection(target, timeout)
    headers = {"Connection": "close"}
    if token:
        headers["Authorization"] = "Bearer " + token
    started = time.perf_counter()
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read(bench_stage1.MAX_RESPONSE_BYTES + 1)
        if len(body) > bench_stage1.MAX_RESPONSE_BYTES:
            raise ValueError("sentinel response exceeds 4 MiB")
        elapsed = (time.perf_counter() - started) * 1000.0
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            payload = {}
        return response.status, elapsed, payload
    finally:
        conn.close()


class ProcSampler(object):
    def __init__(self, pid, clock=None):
        self.pid = int(pid) if pid else None
        self.clock = clock or time.monotonic
        self.previous = None
        self.ticks = float(os.sysconf("SC_CLK_TCK"))

    def __call__(self):
        empty = {"cpu_cores": 0.0, "rss_kb": 0, "threads": 0,
                 "fd": 0, "children": 0, "zombies": 0,
                 "available": False}
        if not self.pid:
            return empty
        try:
            status = {}
            with open("/proc/%d/status" % self.pid, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith(("VmRSS:", "Threads:")):
                        status[line.split(":", 1)[0]] = int(line.split()[1])
            with open("/proc/%d/stat" % self.pid, encoding="utf-8") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            cpu_ticks = int(fields[11]) + int(fields[12])
            now = self.clock()
            cpu = None
            if self.previous is not None:
                old_time, old_ticks = self.previous
                elapsed = now - old_time
                if elapsed > 0:
                    cpu = (cpu_ticks - old_ticks) / self.ticks / elapsed
            self.previous = (now, cpu_ticks)
            children = []
            child_file = "/proc/%d/task/%d/children" % (self.pid, self.pid)
            try:
                with open(child_file, encoding="ascii") as fh:
                    children = [int(item) for item in fh.read().split()]
            except OSError:
                pass
            zombies = 0
            for child in children[:1024]:
                try:
                    with open("/proc/%d/stat" % child, encoding="ascii") as fh:
                        if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                            zombies += 1
                except OSError:
                    pass
            return {"cpu_cores": None if cpu is None else
                    round(max(0.0, cpu), 4),
                    "rss_kb": status.get("VmRSS", 0),
                    "threads": status.get("Threads", 0),
                    "fd": len(os.listdir("/proc/%d/fd" % self.pid)),
                    "children": len(children), "zombies": zombies,
                    "available": True}
        except (OSError, ValueError, IndexError):
            return empty


class Load(object):
    """The fixed read-only mixed workload and bounded aggregate counters."""
    def __init__(self, target, ids, token, source, timeout, clock=None):
        monitors, logs, short = mixed.build_paths(ids, source)
        self.target, self.token, self.timeout = target, token, timeout
        self.paths, self.monitor_count = monitors + logs, len(monitors)
        self.short = short
        self.clock = clock or time.monotonic
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.threads = []
        self.connections = mixed._ConnectionRegistry()
        self.http = collections.Counter()
        self.window = collections.Counter()
        self.latencies, self.window_latencies = [], []
        self.active = self.connected = self.disconnected = self.failed = 0
        self.monitor_last = {}
        self.monitor_max_gap = 0.0

    def start(self):
        for index, path in enumerate(self.short):
            self.threads.append(threading.Thread(
                target=self._http, args=(path,), daemon=True,
                name="soak-http-%d" % index))
        for index, path in enumerate(self.paths):
            self.threads.append(threading.Thread(
                target=self._sse, args=(index, path), daemon=True,
                name="soak-sse-%d" % index))
        for thread in self.threads:
            thread.start()

    def _http(self, path):
        while not self.stop.is_set():
            try:
                status, size, latency = bench_stage1.request_once(
                    self.target, path, self.token, self.timeout)
            except Exception:
                status, size, latency = 0, 0, self.timeout * 1000.0
            with self.lock:
                self.http["requests"] += 1
                self.http["status_%d" % status] += 1
                self.window["requests"] += 1
                self.window["status_%d" % status] += 1
                if len(self.latencies) < bench_stage1.MAX_LATENCY_SAMPLES:
                    self.latencies.append(latency)
                if len(self.window_latencies) < 10000:
                    self.window_latencies.append(latency)

    def _sse(self, index, path):
        while not self.stop.is_set():
            conn = _connection(self.target, self.timeout)
            self.connections.add(conn)
            opened = False
            response = None
            try:
                headers = {"Accept": "text/event-stream"}
                if self.token:
                    headers["Authorization"] = "Bearer " + self.token
                conn.request("GET", path, headers=headers)
                response = conn.getresponse()
                self.connections.add(response)
                if response.status != 200 or "text/event-stream" not in \
                        (response.getheader("Content-Type") or ""):
                    raise ValueError("SSE rejected")
                opened = True
                with self.lock:
                    self.active += 1
                    self.connected += 1
                    if index < self.monitor_count:
                        self.monitor_last[index] = self.clock()
                pending = b""
                while not self.stop.is_set():
                    stream = mixed._response_stream(conn, response)
                    if stream is None:
                        break
                    ready, _, _ = select.select([stream], [], [], 0.5)
                    if not ready:
                        continue
                    chunk = response.read1(mixed.MAX_SSE_LINE_BYTES + 1)
                    if not chunk:
                        break
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if len(line) + 1 > mixed.MAX_SSE_LINE_BYTES:
                            raise ValueError("SSE line exceeds 64 KiB")
                        if index < self.monitor_count and \
                                line.startswith(b"data:"):
                            now = self.clock()
                            with self.lock:
                                previous = self.monitor_last.get(index, now)
                                self.monitor_max_gap = max(
                                    self.monitor_max_gap, now - previous)
                                self.monitor_last[index] = now
                    if len(pending) > mixed.MAX_SSE_LINE_BYTES:
                        raise ValueError("SSE line exceeds 64 KiB")
            except Exception:
                with self.lock:
                    self.failed += 1
            finally:
                if opened:
                    with self.lock:
                        self.active = max(0, self.active - 1)
                        if not self.stop.is_set():
                            self.disconnected += 1
                try:
                    if response is not None:
                        response.close()
                except Exception:
                    pass
                if response is not None:
                    self.connections.remove(response)
                conn.close()
                self.connections.remove(conn)
            self.stop.wait(0.1)

    def reset_window(self):
        with self.lock:
            self.window.clear()
            self.window_latencies[:] = []

    def snapshot(self):
        now = self.clock()
        with self.lock:
            gaps = [max(0.0, now - value)
                    for value in self.monitor_last.values()]
            statuses = {key[7:]: value for key, value in self.window.items()
                        if key.startswith("status_")}
            unexpected = sum(value for key, value in statuses.items()
                             if key == "0" or not key.startswith(("2", "3")))
            return {
                "mixed": {"requests": self.window["requests"],
                          "p95_ms": round(bench_stage1.percentile(
                              self.window_latencies, 95), 3),
                          "p99_ms": round(bench_stage1.percentile(
                              self.window_latencies, 99), 3),
                          "unexpected": unexpected},
                "sse": {"active": self.active,
                        "connected_total": self.connected,
                        "disconnected_total": self.disconnected,
                        "connect_failed_total": self.failed,
                        "monitor_max_gap_s": round(max(
                            [self.monitor_max_gap] + gaps), 3)},
            }

    def summary(self):
        with self.lock:
            statuses = {key[7:]: value for key, value in self.http.items()
                        if key.startswith("status_")}
            unexpected = sum(value for key, value in statuses.items()
                             if key == "0" or not key.startswith(("2", "3")))
            return {"requests": self.http["requests"],
                    "p95_ms": round(bench_stage1.percentile(
                        self.latencies, 95), 3),
                    "p99_ms": round(bench_stage1.percentile(
                        self.latencies, 99), 3),
                    "unexpected": unexpected,
                    "latency_samples": len(self.latencies),
                    "latency_samples_capped":
                        self.http["requests"] > len(self.latencies)}

    def close(self):
        self.stop.set()
        self.connections.close_all()
        deadline = time.monotonic() + self.timeout + 2
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return sum(thread.is_alive() for thread in self.threads)


def metric_subset(payload):
    if not isinstance(payload, dict):
        return {}
    audit = payload.get("audit") if isinstance(payload.get("audit"), dict) \
        else {}
    ssh = payload.get("ssh") if isinstance(payload.get("ssh"), dict) else {}
    scheduler = ssh.get("scheduler") \
        if isinstance(ssh.get("scheduler"), dict) else {}
    sse = payload.get("sse") if isinstance(payload.get("sse"), dict) else {}
    wait = scheduler.get("wait") if isinstance(scheduler.get("wait"), dict) \
        else {}
    foreground = wait.get("foreground") \
        if isinstance(wait.get("foreground"), dict) else {}
    wait_maxes = [item.get("max_ms") for item in wait.values()
                  if isinstance(item, dict) and
                  isinstance(item.get("max_ms"), (int, float))]
    normalized = {}
    if scheduler:
        normalized = {
            "global_active": scheduler.get("active"),
            "device_active_max": scheduler.get("peak_device_active"),
            "background_active_max": scheduler.get(
                "peak_background_device"),
            "queue_full_total": scheduler.get("queue_full_total"),
            "wait_timeout_total": scheduler.get("wait_timeout_total"),
            "foreground_admission_p95_ms": foreground.get("p95_ms"),
            "max_starvation_s": max(wait_maxes) / 1000.0
            if wait_maxes else None,
        }
    return {"audit_pending": audit.get("pending"),
            "audit_degraded": audit.get("degraded"),
            "sse_active": sse.get("active"),
            "scheduler": {key: normalized.get(key) for key in (
                "global_active", "device_active_max", "background_active_max",
                "queue_full_total", "wait_timeout_total",
                "foreground_admission_p95_ms", "max_starvation_s")
                if normalized.get(key) is not None}}


def atomic_write(path, report):
    rendered = (json.dumps(report, sort_keys=True, separators=(",", ":")) +
                "\n").encode("utf-8")
    if len(rendered) > MAX_REPORT_BYTES:
        raise ValueError("report exceeds 16 MiB")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temporary = tempfile.mkstemp(prefix=".soak-", dir=directory)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(rendered)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def summarize(samples, load_summary, cleanup_alive, cleanup_s):
    return {"mixed": load_summary,
            "cleanup_threads_alive": cleanup_alive,
            "cleanup_s": round(cleanup_s, 3),
            "sample_count": len(samples)}


def run_soak(base_url, device_ids, output, token="", log_source="/dev/null",
             warmup_s=60, sample_s=1800, repeat=1, portal_pid=None,
             timeout=5, allow_insecure_token=False, topology="PC_SIM",
             unique_profiles=0, restart_count=None, clock=None,
             wait_fn=None, resource_fn=None, fetch_fn=None,
             load_factory=None, install_signals=True):
    warmup_s, sample_s, repeat, timeout, unique_profiles = validate_run(
        warmup_s, sample_s, repeat, timeout, topology, unique_profiles)
    ids = mixed.validate_device_ids(device_ids)
    source = mixed.validate_log_source(log_source)
    target = bench_stage1.validate_target(base_url, "/api/health", bool(token),
                                          allow_insecure_token)
    bench_stage1.validate_target(base_url, "/api/metrics", bool(token),
                                 allow_insecure_token)
    clock = clock or time.monotonic
    stopped = threading.Event()
    wait_fn = wait_fn or stopped.wait
    resource_fn = resource_fn or ProcSampler(portal_pid, clock)
    fetch_fn = fetch_fn or fetch_json
    load_factory = load_factory or Load
    report = {"schema_version": SCHEMA_VERSION,
              "scenario": "stage3-soak-readonly", "complete": False,
              "termination": "running",
              "topology": topology_summary(topology, unique_profiles),
              "config": {"device_count": 30, "http_clients": 32,
                         "monitor_sse": 8, "logcenter_sse": 8,
                         "warmup_s": warmup_s, "sample_s": sample_s,
                         "repeat": repeat, "interval_s": SAMPLE_INTERVAL_S,
                         "restart_count": restart_count},
              "samples": [], "summary": {}}
    old_handlers = {}

    def interrupted(signum, _frame):
        report["termination"] = "signal"
        stopped.set()

    if install_signals and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, interrupted)
    load = None
    cleanup_alive = 0
    try:
        load = load_factory(target, ids, token, source, timeout, clock)
        load.start()
        if wait_fn(warmup_s):
            report["termination"] = "signal"
            stopped.set()
        load.reset_window()
        started = clock()
        for round_index in range(1, repeat + 1):
            round_started = clock()
            while not stopped.is_set() and clock() - round_started < sample_s:
                remaining = sample_s - (clock() - round_started)
                if wait_fn(min(SAMPLE_INTERVAL_S, max(0.0, remaining))):
                    break
                health_status, health_ms, _ = fetch_fn(
                    target, "/api/health", token, timeout)
                metrics_status, metrics_ms, metrics = fetch_fn(
                    target, "/api/metrics", token, timeout)
                snapshot = load.snapshot()
                snapshot.update({
                    "index": len(report["samples"]), "round": round_index,
                    "elapsed_s": round(clock() - started, 3),
                    "process": resource_fn(),
                    "sentinel": {"health_status": health_status,
                                 "health_ms": round(health_ms, 3),
                                 "metrics_status": metrics_status,
                                 "metrics_ms": round(metrics_ms, 3)},
                    "metrics": metric_subset(metrics),
                })
                report["samples"].append(snapshot)
                if len(report["samples"]) > MAX_SAMPLES:
                    raise RuntimeError("sample bound reached")
                load.reset_window()
        if not stopped.is_set():
            report["complete"] = True
            report["termination"] = "complete"
    except BaseException:
        report["termination"] = "error"
        raise
    finally:
        if load is not None:
            cleanup_started = time.monotonic()
            cleanup_alive = load.close()
            cleanup_s = time.monotonic() - cleanup_started
            report["summary"] = summarize(
                report["samples"], load.summary(), cleanup_alive, cleanup_s)
        atomic_write(output, report)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--device-ids-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--token-file")
    parser.add_argument("--allow-insecure-token", action="store_true")
    parser.add_argument("--log-source", default="/dev/null")
    parser.add_argument("--warmup", type=float, default=60)
    parser.add_argument("--sample", type=float, default=1800)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--portal-pid", type=int)
    parser.add_argument("--topology", choices=TOPOLOGIES, default="PC_SIM")
    parser.add_argument("--unique-profiles", type=int, default=0)
    parser.add_argument("--restart-count", type=int)
    args = parser.parse_args(argv)
    try:
        ids = mixed.load_device_ids(args.device_ids_file)
        token = bench_stage1.read_token_file(args.token_file) \
            if args.token_file else ""
        run_soak(args.base_url, ids, args.output, token, args.log_source,
                 args.warmup, args.sample, args.repeat, args.portal_pid,
                 args.timeout, args.allow_insecure_token, args.topology,
                 args.unique_profiles, args.restart_count)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
