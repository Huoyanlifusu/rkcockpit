#!/usr/bin/env python3
"""Repeatable Stage 2 mixed load: 30 devices, 16 SSE, 32 short GETs."""
import argparse
import collections
import http.client
import json
import math
import os
import re
import select
import stat
import statistics
import sys
import threading
import time
from urllib.parse import quote

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from tools import bench_stage1


DEVICE_COUNT = 30
MONITOR_SSE_COUNT = 8
LOG_SSE_COUNT = 8
HTTP_CLIENTS = 32
MAX_DEVICE_IDS = 128
MAX_ID_FILE_BYTES = 64 << 10
MAX_ID_LENGTH = 64
MAX_LOG_SOURCE_LENGTH = 256
MAX_TOTAL_DURATION_S = 3600.0
MAX_REPEAT = 10
MAX_SSE_HEADER_BYTES = 16 << 10
MAX_SSE_LINE_BYTES = 64 << 10
MAX_LATENCY_SAMPLES = 100000
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,%d}\Z" % MAX_ID_LENGTH)


def validate_device_ids(device_ids, allow_more=False):
    ids = list(device_ids)
    maximum = MAX_DEVICE_IDS if allow_more else DEVICE_COUNT
    if len(ids) < DEVICE_COUNT or len(ids) > maximum:
        expected = "30..128" if allow_more else "exactly 30"
        raise ValueError("device ID count must be %s" % expected)
    for item in ids:
        if not isinstance(item, str) or _ID_RE.fullmatch(item) is None:
            raise ValueError("device ID has invalid format or length")
    if len(set(ids)) != len(ids):
        raise ValueError("device IDs must be unique")
    return ids[:DEVICE_COUNT]


def load_device_ids(path):
    """Load a bounded, unique list and select the first 30 deterministically."""
    path = os.path.abspath(os.path.expanduser(path))
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("device IDs file must be a regular file")
            raw = os.read(fd, MAX_ID_FILE_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ValueError("cannot read device IDs file: %s" % exc)
    if len(raw) > MAX_ID_FILE_BYTES:
        raise ValueError("device IDs file exceeds 64 KiB")
    try:
        lines = [line.strip() for line in raw.decode("utf-8").splitlines()]
    except UnicodeError as exc:
        raise ValueError("cannot read device IDs file: %s" % exc)
    ids = [item for item in lines if item]
    return validate_device_ids(ids, allow_more=True)


def validate_log_source(source):
    source = str(source or "").strip()
    if not source or len(source) > MAX_LOG_SOURCE_LENGTH or \
            any(ord(char) < 32 or ord(char) == 127 for char in source):
        raise ValueError("log source has invalid format or length")
    if source != "journalctl" and not source.startswith("/"):
        raise ValueError("log source must be an absolute path or journalctl")
    return source


def build_paths(device_ids, log_source="/dev/null"):
    device_ids = validate_device_ids(device_ids)
    source = quote(validate_log_source(log_source), safe="")
    encoded = [quote(item, safe="") for item in device_ids]
    monitor = ["/api/monitor/%s/stream" % item
               for item in encoded[:MONITOR_SSE_COUNT]]
    logs = ["/api/logcenter/%s/stream?source=%s" % (item, source)
            for item in encoded[MONITOR_SSE_COUNT:
                                MONITOR_SSE_COUNT + LOG_SSE_COUNT]]
    short = ["/api/monitor/%s/now" % item for item in encoded]
    short.extend(("/api/metrics", "/api/health"))
    return monitor, logs, short


def validate_run(warmup_s, sample_s, repeat, timeout):
    warmup_s = float(warmup_s)
    sample_s = float(sample_s)
    repeat = int(repeat)
    timeout = float(timeout)
    if not all(math.isfinite(value) for value in
               (warmup_s, sample_s, timeout)):
        raise ValueError("benchmark durations must be finite")
    if warmup_s < 0 or sample_s <= 0:
        raise ValueError("warmup must be >= 0 and sample must be > 0")
    if not 1 <= repeat <= MAX_REPEAT:
        raise ValueError("repeat must be in 1..10")
    # Include barrier and cleanup budgets so a valid configuration remains
    # bounded even when every connection consumes its full timeout.
    if repeat * (warmup_s + sample_s + timeout + 12) > \
            MAX_TOTAL_DURATION_S:
        raise ValueError("configured run exceeds 3600 seconds")
    if not 0 < timeout <= bench_stage1.MAX_TIMEOUT_S:
        raise ValueError("timeout must be in (0, 60]")
    return warmup_s, sample_s, repeat, timeout


class _ConnectionRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._connections = set()

    def add(self, connection):
        with self._lock:
            self._connections.add(connection)

    def remove(self, connection):
        with self._lock:
            self._connections.discard(connection)

    def close_all(self):
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass


class _SseStats:
    def __init__(self):
        self._lock = threading.Lock()
        self._sampling = False
        self._active = 0
        self._connected = 0
        self._disconnected = 0
        self._failed = 0
        self._peak = 0
        self._active_start = 0
        self._active_end = 0

    def begin_sample(self):
        with self._lock:
            self._sampling = True
            self._connected = self._active
            self._disconnected = 0
            self._failed = 0
            self._peak = self._active
            self._active_start = self._active

    def end_sample(self):
        with self._lock:
            self._active_end = self._active
            self._sampling = False

    def connected(self):
        with self._lock:
            self._active += 1
            if self._sampling:
                self._connected += 1
                self._peak = max(self._peak, self._active)

    def disconnected(self, unexpected):
        with self._lock:
            self._active = max(0, self._active - 1)
            if self._sampling and unexpected:
                self._disconnected += 1

    def failed(self):
        with self._lock:
            if self._sampling:
                self._failed += 1

    def snapshot(self):
        with self._lock:
            return {
                "connected": self._connected,
                "disconnected": self._disconnected,
                "connect_failed": self._failed,
                "active_at_start": self._active_start,
                "active_at_end": self._active_end,
                "peak_active": self._peak,
            }


def _connection(target, timeout):
    cls = http.client.HTTPSConnection if target.scheme == "https" \
        else http.client.HTTPConnection
    return cls(target.hostname, target.port, timeout=timeout)


def _response_stream(connection, response):
    """Return the live stream after http.client detaches SSE from a connection.

    A response without Content-Length is marked ``will_close`` even when the
    server explicitly keeps the SSE socket open.  ``HTTPConnection`` then sets
    ``sock`` to None and transfers ownership to ``HTTPResponse.fp``.
    """
    stream = getattr(response, "fp", None)
    return stream if stream is not None else connection.sock


def _consume_sse(target, path, token, timeout, stop, registry, stats):
    connection = _connection(target, timeout)
    registry.add(connection)
    response = None
    opened = False
    try:
        headers = {"Accept": "text/event-stream"}
        if token:
            headers["Authorization"] = "Bearer " + token
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        registry.add(response)
        header_bytes = sum(len(str(name)) + len(str(value)) + 4
                           for name, value in response.getheaders()) + 64
        if header_bytes > MAX_SSE_HEADER_BYTES:
            raise ValueError("SSE response headers exceed 16 KiB")
        content_type = response.getheader("Content-Type") or ""
        if response.status != 200 or "text/event-stream" not in content_type:
            stats.failed()
            response.read(bench_stage1.MAX_RESPONSE_BYTES + 1)
            return
        opened = True
        stats.connected()
        pending = b""
        while not stop.is_set():
            stream = _response_stream(connection, response)
            if stream is None:
                break
            try:
                readable, _, _ = select.select([stream], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            chunk = response.read1(MAX_SSE_LINE_BYTES + 1)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if len(line) + 1 > MAX_SSE_LINE_BYTES:
                    raise ValueError("SSE line exceeds 64 KiB")
            if len(pending) > MAX_SSE_LINE_BYTES:
                raise ValueError("SSE line exceeds 64 KiB")
    except Exception:
        stats.failed()
    finally:
        if opened:
            stats.disconnected(unexpected=not stop.is_set())
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        if response is not None:
            registry.remove(response)
        connection.close()
        registry.remove(connection)


def _sse_worker(barrier, target, path, token, timeout, stop, registry, stats):
    try:
        barrier.wait()
    except threading.BrokenBarrierError:
        return
    while not stop.is_set():
        _consume_sse(target, path, token, timeout, stop, registry, stats)
        if not stop.is_set():
            stop.wait(0.1)


def _http_worker(barrier, target, path, token, timeout, stop, sampling,
                 result):
    sample_limit = max(1, MAX_LATENCY_SAMPLES // HTTP_CLIENTS)
    try:
        barrier.wait()
    except threading.BrokenBarrierError:
        return
    while not stop.is_set():
        started_during_sample = sampling.is_set()
        try:
            status, size, latency = bench_stage1.request_once(
                target, path, token, timeout)
        except Exception:
            status, size, latency = 0, 0, timeout * 1000
        if started_during_sample and sampling.is_set():
            result["requests"] += 1
            result["bytes"] += size
            result["statuses"][status] += 1
            if len(result["latencies"]) < sample_limit:
                result["latencies"].append(latency)


def run_round(target, device_ids, token, log_source, warmup_s, sample_s,
              portal_pid=None, timeout=5.0, round_index=1):
    monitor_paths, log_paths, short_paths = build_paths(device_ids, log_source)
    stop = threading.Event()
    sampling = threading.Event()
    registry = _ConnectionRegistry()
    sse_stats = _SseStats()
    worker_count = HTTP_CLIENTS + MONITOR_SSE_COUNT + LOG_SSE_COUNT
    barrier = threading.Barrier(worker_count + 1)
    threads = []
    http_results = []

    for index in range(HTTP_CLIENTS):
        result = {"requests": 0, "bytes": 0,
                  "statuses": collections.Counter(), "latencies": []}
        http_results.append(result)
        thread = threading.Thread(
            target=_http_worker,
            args=(barrier, target, short_paths[index], token, timeout, stop,
                  sampling, result), daemon=True,
            name="mixed-http-%d" % index)
        threads.append(thread)
    for index, path in enumerate(monitor_paths + log_paths):
        thread = threading.Thread(
            target=_sse_worker,
            args=(barrier, target, path, token, timeout, stop, registry,
                  sse_stats), daemon=True, name="mixed-sse-%d" % index)
        threads.append(thread)

    rss_peak = threads_peak = 0
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        if warmup_s:
            stop.wait(warmup_s)
        sse_stats.begin_sample()
        sampling.set()
        deadline = time.monotonic() + sample_s
        while time.monotonic() < deadline:
            rss, thread_count = bench_stage1.proc_resources(portal_pid)
            rss_peak = max(rss_peak, rss)
            threads_peak = max(threads_peak, thread_count)
            stop.wait(min(0.1, max(0.0, deadline - time.monotonic())))
        sampling.clear()
        sse_stats.end_sample()
    finally:
        sampling.clear()
        stop.set()
        registry.close_all()
        try:
            barrier.abort()
        except Exception:
            pass
        join_deadline = time.monotonic() + timeout + 2
        for thread in threads:
            thread.join(max(0.0, join_deadline - time.monotonic()))

    alive = sum(1 for thread in threads if thread.is_alive())
    if alive:
        raise RuntimeError("mixed worker cleanup failed: %d threads alive" % alive)
    latencies = []
    statuses = collections.Counter()
    requests = total_bytes = 0
    for result in http_results:
        requests += result["requests"]
        total_bytes += result["bytes"]
        statuses.update(result["statuses"])
        latencies.extend(result["latencies"])
    return {
        "round": int(round_index),
        "duration_s": round(sample_s, 3),
        "requests": requests,
        "latency_samples": len(latencies),
        "latency_samples_capped": requests > len(latencies),
        "p95_ms": round(bench_stage1.percentile(latencies, 95), 3),
        "p99_ms": round(bench_stage1.percentile(latencies, 99), 3),
        "rps": round(requests / sample_s, 3),
        "status_counts": {str(key): value
                          for key, value in sorted(statuses.items())},
        "bytes_rx": total_bytes,
        "rss_peak_kb": rss_peak,
        "threads_peak": threads_peak,
        "sse": sse_stats.snapshot(),
        "cleanup_threads_alive": alive,
    }


def run_mixed(base_url, device_ids, token="", log_source="/dev/null",
              warmup_s=10.0, sample_s=60.0, repeat=3, portal_pid=None,
              timeout=5.0, allow_insecure_token=False):
    warmup_s, sample_s, repeat, timeout = validate_run(
        warmup_s, sample_s, repeat, timeout)
    target = bench_stage1.validate_target(
        base_url, "/api/health", bool(token), allow_insecure_token)
    device_ids = validate_device_ids(device_ids)
    log_source = validate_log_source(log_source)
    rounds = [run_round(target, device_ids, token, log_source, warmup_s,
                        sample_s, portal_pid, timeout, index + 1)
              for index in range(repeat)]
    median_round = dict(sorted(rounds, key=lambda item: item["p95_ms"])[
        len(rounds) // 2])
    median_round["selected_by"] = "p95_ms"
    return {
        "schema_version": 1,
        "scenario": "stage2-mixed-readonly",
        "device_count": DEVICE_COUNT,
        "http_clients": HTTP_CLIENTS,
        "monitor_sse": MONITOR_SSE_COUNT,
        "logcenter_sse": LOG_SSE_COUNT,
        "warmup_s": warmup_s,
        "sample_s": sample_s,
        "repeat": repeat,
        "rounds": rounds,
        "median_round": median_round,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--device-ids-file", required=True)
    parser.add_argument("--token-file")
    parser.add_argument("--allow-insecure-token", action="store_true")
    parser.add_argument("--log-source", default="/dev/null")
    parser.add_argument("--warmup", type=float, default=10.0)
    parser.add_argument("--sample", type=float, default=60.0)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--portal-pid", type=int)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        device_ids = load_device_ids(args.device_ids_file)
        token = bench_stage1.read_token_file(args.token_file) \
            if args.token_file else ""
        report = run_mixed(
            args.base_url, device_ids, token, args.log_source, args.warmup,
            args.sample, args.repeat, args.portal_pid, args.timeout,
            args.allow_insecure_token)
    except ValueError as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
