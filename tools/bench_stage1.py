#!/usr/bin/env python3
"""Repeatable stdlib HTTP benchmark for the runtime-foundation stages.

This is deliberately not part of unittest discovery.  Run it against an
already-started portal and save the JSON result for before/after comparison.
Hardware thresholds belong in an RK3588/HITL report, not in CI.
"""
import argparse
import concurrent.futures
import http.client
import ipaddress
import json
import os
import platform
import stat
import statistics
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


MAX_CONCURRENCY = 128
MAX_DURATION_S = 3600.0
MAX_TIMEOUT_S = 60.0
MAX_RESPONSE_BYTES = 4 << 20
MAX_LATENCY_SAMPLES = 100000
READ_ONLY_PATHS = frozenset((
    "/api/health", "/api/metrics", "/api/host", "/api/devices",
    "/api/jobs", "/api/discover", "/api/discover/rules",
))


def percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def git_commit():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def proc_resources(pid):
    rss_kb = threads = 0
    if not pid:
        return rss_kb, threads
    try:
        with open("/proc/%d/status" % pid, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                elif line.startswith("Threads:"):
                    threads = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return rss_kb, threads


def _is_loopback_host(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_target(base_url, path, token=False, allow_insecure_token=False):
    target = urlsplit(base_url)
    if target.scheme not in ("http", "https") or not target.hostname:
        raise ValueError("base-url 必须是 http://host:port 或 https://host:port")
    if target.username or target.password or target.path not in ("", "/") or \
            target.query or target.fragment:
        raise ValueError("base-url 只能包含 scheme、host 和 port")
    try:
        target.port
    except ValueError as exc:
        raise ValueError("base-url port 无效") from exc
    if path not in READ_ONLY_PATHS:
        raise ValueError("path 不在只读 allowlist 中")
    if token and target.scheme == "http" and not _is_loopback_host(target.hostname):
        if not allow_insecure_token:
            raise ValueError("拒绝通过远端明文 HTTP 发送 token；请使用 HTTPS")
        sys.stderr.write(
            "WARNING: sending bearer token over remote plaintext HTTP to %s\n" %
            target.hostname)
    return target


def read_token_file(path):
    path = os.path.abspath(os.path.expanduser(path))
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ValueError("无法读取 token 文件: %s" % exc)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("token 文件必须是单链接 regular file")
    if before.st_uid != os.geteuid():
        raise ValueError("token 文件必须属于当前用户")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise ValueError("token 文件权限必须为 0600")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | \
        getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("token 文件在打开期间发生变化")
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or \
                    opened.st_uid != os.geteuid() or \
                    stat.S_IMODE(opened.st_mode) != 0o600:
                raise ValueError("token 文件属性在打开期间不再安全")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ValueError("无法读取 token 文件: %s" % exc)
    if len(raw) > 4096:
        raise ValueError("token 文件超过 4 KiB")
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeError as exc:
        raise ValueError("token 文件不是 UTF-8") from exc
    if not token or "\n" in token or "\r" in token:
        raise ValueError("token 必须是非空单行文本")
    return token


def request_once(target, path, token, timeout):
    connection_cls = http.client.HTTPSConnection if target.scheme == "https" \
        else http.client.HTTPConnection
    conn = connection_cls(target.hostname, target.port, timeout=timeout)
    headers = {"Connection": "close"}
    if token:
        headers["Authorization"] = "Bearer " + token
    started = time.perf_counter()
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        raw_length = response.getheader("Content-Length")
        try:
            declared = int(raw_length) if raw_length is not None else None
        except ValueError:
            declared = None
        if declared is not None and declared > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds 4 MiB limit")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds 4 MiB limit")
        return response.status, len(body), (time.perf_counter() - started) * 1000
    finally:
        conn.close()


def run_benchmark(base_url, path, concurrency, duration_s, token="",
                  portal_pid=None, timeout=5.0,
                  allow_insecure_token=False):
    concurrency = int(concurrency)
    duration_s = float(duration_s)
    timeout = float(timeout)
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError("concurrency 必须在 1..128")
    if not 0 < duration_s <= MAX_DURATION_S:
        raise ValueError("duration 必须在 (0, 3600]")
    if not 0 < timeout <= MAX_TIMEOUT_S:
        raise ValueError("timeout 必须在 (0, 60]")
    target = validate_target(base_url, path, bool(token),
                             allow_insecure_token)
    stop_at = time.monotonic() + duration_s
    lock = threading.Lock()
    latencies = []
    statuses = {}
    total_bytes = 0
    total_requests = 0
    rss_peak = threads_peak = 0
    sample_limit = max(1, MAX_LATENCY_SAMPLES // concurrency)

    def worker():
        nonlocal total_bytes, total_requests
        local_latencies = []
        local_statuses = {}
        local_bytes = 0
        local_requests = 0
        while time.monotonic() < stop_at:
            try:
                status, size, latency = request_once(target, path, token, timeout)
            except Exception:
                status, size, latency = 0, 0, timeout * 1000
            if len(local_latencies) < sample_limit:
                local_latencies.append(latency)
            local_requests += 1
            local_statuses[status] = local_statuses.get(status, 0) + 1
            local_bytes += size
        with lock:
            latencies.extend(local_latencies)
            total_bytes += local_bytes
            total_requests += local_requests
            for status, count in local_statuses.items():
                statuses[status] = statuses.get(status, 0) + count

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(concurrency)]
        while any(not f.done() for f in futures):
            rss, threads = proc_resources(portal_pid)
            rss_peak = max(rss_peak, rss)
            threads_peak = max(threads_peak, threads)
            time.sleep(0.1)
        for future in futures:
            future.result()
    elapsed = time.monotonic() - started
    requests = total_requests
    ok = sum(count for status, count in statuses.items() if 200 <= status < 400)
    errors_4xx = sum(count for status, count in statuses.items() if 400 <= status < 500)
    errors_5xx = sum(count for status, count in statuses.items()
                     if status == 0 or status >= 500)
    return {
        "commit": git_commit(),
        "host": platform.node(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "scenario": path,
        "concurrency": concurrency,
        "requests": requests,
        "latency_samples": len(latencies),
        "latency_samples_capped": requests > len(latencies),
        "duration_s": round(elapsed, 3),
        "ok": ok,
        "error_4xx": errors_4xx,
        "error_5xx": errors_5xx,
        "status_counts": {str(k): v for k, v in sorted(statuses.items())},
        "p50_ms": round(percentile(latencies, 50), 3),
        "p95_ms": round(percentile(latencies, 95), 3),
        "p99_ms": round(percentile(latencies, 99), 3),
        "mean_ms": round(statistics.fmean(latencies), 3) if latencies else 0.0,
        "rps": round(requests / elapsed, 3) if elapsed else 0.0,
        "bytes_rx": total_bytes,
        "rss_peak_kb": rss_peak,
        "threads_peak": threads_peak,
        "auth": bool(token),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--path", default="/api/health")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--token-file")
    parser.add_argument(
        "--allow-insecure-token", action="store_true",
        help="允许向非 loopback 的明文 HTTP 发送 token（危险，会告警）")
    parser.add_argument("--portal-pid", type=int)
    parser.add_argument("--output", help="JSON 输出文件；默认 stdout")
    args = parser.parse_args(argv)
    try:
        token = read_token_file(args.token_file) if args.token_file else ""
        result = run_benchmark(
            args.base_url, args.path, args.concurrency, args.duration, token,
            args.portal_pid, args.timeout, args.allow_insecure_token)
    except ValueError as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
