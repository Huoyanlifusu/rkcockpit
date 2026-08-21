#!/usr/bin/env python3
"""Offline PASS/FAIL/GAP analysis for a Stage 3 soak JSON report."""
import argparse
import json
import os
import stat
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from tools import bench_stage1
from tools.bench_stage3_soak import MAX_REPORT_BYTES, SCHEMA_VERSION


def read_report(path):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | \
        getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.path.abspath(os.path.expanduser(path)), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("report must be a regular file")
        raw = os.read(fd, MAX_REPORT_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("report exceeds 16 MiB")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid report JSON") from exc


def _pct(values, pct):
    return bench_stage1.percentile([float(item) for item in values], pct)


def _slope(samples):
    points = [(float(item.get("elapsed_s", 0)),
               float(item.get("process", {}).get("rss_kb", 0)))
              for item in samples if float(item.get("elapsed_s", 0)) >= 300]
    if len(points) < 2:
        return None
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    denom = sum((x - mean_x) ** 2 for x, _ in points)
    if denom <= 0:
        return None
    kb_s = sum((x - mean_x) * (y - mean_y) for x, y in points) / denom
    return kb_s * 60.0 / 1024.0


def analyze(report):
    checks = []

    def add(name, status, observed=None, limit=None):
        item = {"name": name, "status": status}
        if observed is not None:
            item["observed"] = observed
        if limit is not None:
            item["limit"] = limit
        checks.append(item)

    if not isinstance(report, dict) or report.get("schema_version") != \
            SCHEMA_VERSION or report.get("scenario") != "stage3-soak-readonly":
        add("schema", "FAIL")
        return {"schema_version": 1, "result": "FAIL", "checks": checks}
    samples = report.get("samples") if isinstance(report.get("samples"), list) \
        else []
    config = report.get("config") if isinstance(report.get("config"), dict) \
        else {}
    topology = report.get("topology") \
        if isinstance(report.get("topology"), dict) else {}
    add("complete", "PASS" if report.get("complete") else "GAP")
    formal = config.get("warmup_s", 0) >= 60 and \
        config.get("sample_s", 0) >= 1800 and config.get("repeat", 0) >= 1
    add("duration", "PASS" if formal else "GAP")
    fixed = tuple(config.get(key) for key in
                  ("device_count", "http_clients", "monitor_sse",
                   "logcenter_sse")) == (30, 32, 8, 8)
    add("fixed_load", "PASS" if fixed else "FAIL")
    topology_candidate = topology.get("kind") == "SSH_HITL" and \
        topology.get("unique_profiles") == 30 and \
        topology.get("topology_candidate") is True
    # A local JSON report cannot attest physical topology.  Keep the gate open
    # for manual inventory/mux/HITL sign-off even when every numeric SLO passes.
    add("ssh_hitl_topology", "GAP",
        "candidate_requires_manual_signoff" if topology_candidate
        else "not_a_30_profile_candidate")
    if not samples:
        add("samples", "GAP")
    summary = report.get("summary") if isinstance(report.get("summary"), dict) \
        else {}
    mixed_summary = summary.get("mixed") \
        if isinstance(summary.get("mixed"), dict) else {}
    for key, limit in (("p95_ms", 500), ("p99_ms", 1500)):
        value = mixed_summary.get(key)
        add("mixed_" + key, "GAP" if value is None else
            ("PASS" if value <= limit else "FAIL"), value, limit)
    unexpected = mixed_summary.get("unexpected")
    add("mixed_unexpected", "GAP" if unexpected is None else
        ("PASS" if unexpected == 0 else "FAIL"), unexpected, 0)
    requests = mixed_summary.get("requests")
    add("mixed_requests", "GAP" if requests is None else
        ("PASS" if requests > 0 else "FAIL"), requests, ">0")
    health = [s.get("sentinel", {}).get("health_ms") for s in samples]
    metrics = [s.get("sentinel", {}).get("metrics_ms") for s in samples]
    sentinel = [v for v in health + metrics if isinstance(v, (int, float))]
    for pct, limit in ((95, 100), (99, 250)):
        value = _pct(sentinel, pct) if sentinel else None
        add("sentinel_p%d_ms" % pct, "GAP" if value is None else
            ("PASS" if value <= limit else "FAIL"), value, limit)
    bad_sentinel = sum(1 for s in samples for key in
                       ("health_status", "metrics_status")
                       if s.get("sentinel", {}).get(key) != 200)
    add("sentinel_status", "GAP" if not samples else
        ("PASS" if bad_sentinel == 0 else "FAIL"), bad_sentinel, 0)
    active = [s.get("sse", {}).get("active") for s in samples
              if isinstance(s.get("sse", {}).get("active"), (int, float))]
    availability = sum(active) / (16.0 * len(active)) if active else None
    add("sse_availability", "GAP" if availability is None else
        ("PASS" if availability >= .995 else "FAIL"), availability, .995)
    gaps = [s.get("sse", {}).get("monitor_max_gap_s") for s in samples
            if isinstance(s.get("sse", {}).get("monitor_max_gap_s"),
                          (int, float))]
    max_gap = max(gaps) if gaps else None
    add("monitor_max_gap_s", "GAP" if max_gap is None else
        ("PASS" if max_gap <= 5 else "FAIL"), max_gap, 5)
    resource_samples = [s for s in samples
                        if s.get("process", {}).get("available") is True]
    cpu = [s.get("process", {}).get("cpu_cores")
           for s in resource_samples]
    cpu = [v for v in cpu if isinstance(v, (int, float))]
    for name, value, limit in (
            ("cpu_mean_cores", sum(cpu) / len(cpu) if cpu else None, 1),
            ("cpu_p95_cores", _pct(cpu, 95) if cpu else None, 2)):
        add(name, "GAP" if value is None else
            ("PASS" if value <= limit else "FAIL"), value, limit)
    for key, limit in (("rss_kb", 256 * 1024), ("threads", 128),
                       ("fd", 256), ("zombies", 0)):
        values = [s.get("process", {}).get(key) for s in resource_samples]
        values = [v for v in values if isinstance(v, (int, float))]
        value = max(values) if values else None
        add(key + "_max", "GAP" if value is None else
            ("PASS" if value <= limit else "FAIL"), value, limit)
    slope = _slope(resource_samples)
    add("rss_slope_mib_min", "GAP" if slope is None else
        ("PASS" if slope <= .5 else "FAIL"), slope, .5)
    restart = config.get("restart_count")
    add("restart_count", "GAP" if restart is None else
        ("PASS" if restart == 0 else "FAIL"), restart, 0)
    last_metrics = samples[-1].get("metrics", {}) if samples else {}
    pending, degraded = (last_metrics.get("audit_pending"),
                         last_metrics.get("audit_degraded"))
    add("audit_steady", "GAP" if pending is None or degraded is None else
        ("PASS" if pending == 0 and degraded is False else "FAIL"))
    scheduler = last_metrics.get("scheduler", {}) \
        if isinstance(last_metrics.get("scheduler"), dict) else {}
    scheduler_limits = {"global_active": 32, "device_active_max": 8,
                        "background_active_max": 6, "queue_full_total": 0,
                        "wait_timeout_total": 0,
                        "foreground_admission_p95_ms": 500,
                        "max_starvation_s": 5}
    for key, limit in scheduler_limits.items():
        value = scheduler.get(key)
        add("scheduler_" + key, "GAP" if value is None else
            ("PASS" if value <= limit else "FAIL"), value, limit)
    cleanup = summary.get("cleanup_threads_alive")
    add("client_cleanup", "GAP" if cleanup is None else
        ("PASS" if cleanup == 0 else "FAIL"), cleanup, 0)
    cleanup_s = summary.get("cleanup_s")
    add("client_cleanup_s", "GAP" if cleanup_s is None else
        ("PASS" if cleanup_s <= 5 else "FAIL"), cleanup_s, 5)
    statuses = {item["status"] for item in checks}
    result = "FAIL" if "FAIL" in statuses else ("GAP" if "GAP" in statuses
                                                 else "PASS")
    return {"schema_version": 1, "result": result,
            "evidence_class": topology.get("kind", "UNKNOWN"),
            "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        result = analyze(read_report(args.report))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(rendered)
    else:
        sys.stdout.write(rendered)
    # Incomplete evidence must not look successful to status-only automation.
    return {"PASS": 0, "FAIL": 1, "GAP": 2}[result["result"]]


if __name__ == "__main__":
    raise SystemExit(main())
