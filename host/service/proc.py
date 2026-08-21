"""Utilities for host.service.proc."""
import re

_FULL_PS = "ps -eo pid,ppid,stat,pcpu,pmem,rss,comm"
_NO_RSS_PS = "ps -eo pid,ppid,stat,pcpu,pmem,comm"
_WIDE_PS = "ps w"
_PS_CANDIDATES = ((_FULL_PS, "full"), (_NO_RSS_PS, "no_rss"), (_WIDE_PS, "wide"))

_SIGNALS = ("TERM", "KILL", "STOP", "CONT", "HUP")
_MAX_PAGE = 5000


class ProcError(Exception):
    """Error raised for proc error."""


class SignalBlockedError(ProcError):
    """Error raised for signal blocked error."""


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_rows(out, fmt):
    """Handle parse rows."""
    rows = []
    for raw in out.splitlines():
        s = raw.strip()
        if not s:
            continue
        toks = s.split()
        if not toks[0].isdigit():
            continue
        pid = _int(toks[0])
        if fmt == "full":
            if len(toks) < 7:
                continue
            rows.append({"pid": pid, "ppid": _int(toks[1]), "stat": toks[2],
                         "pcpu": _float(toks[3]), "pmem": _float(toks[4]),
                         "rss_kb": _int(toks[5]), "comm": " ".join(toks[6:])})
        elif fmt == "no_rss":
            if len(toks) < 6:
                continue
            rows.append({"pid": pid, "ppid": _int(toks[1]), "stat": toks[2],
                         "pcpu": _float(toks[3]), "pmem": _float(toks[4]),
                         "rss_kb": None, "comm": " ".join(toks[5:])})
        else:
            rows.append({"pid": pid, "ppid": None, "stat": None,
                         "pcpu": None, "pmem": None, "rss_kb": None,
                         "comm": " ".join(toks[1:])})
    return rows


def list_processes(transport, pattern=None, sort="cpu", order="desc",
                   limit=200, offset=0, timeout=15):
    """Return list processes."""
    if sort not in ("cpu", "mem", "pid"):
        raise ValueError("sort 参数非法（可选 cpu/mem/pid）: %r" % sort)
    if order not in ("asc", "desc"):
        raise ValueError("order 参数非法（可选 asc/desc）: %r" % order)
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError):
        raise ValueError("limit/offset 必须为整数")
    if limit < 0 or offset < 0:
        raise ValueError("limit/offset 不能为负")
    limit = min(limit, _MAX_PAGE)

    out = None
    fmt = None
    last_err = ""
    for cmd, f in _PS_CANDIDATES:
        try:
            rc, o, e = transport.exec(cmd, timeout)
        except Exception as exc:
            last_err = str(exc)
            continue
        if rc == 0 and o.strip():
            out, fmt = o, f
            break
        last_err = (e or o).strip()[:200]
    if out is None:
        raise ProcError("ps 执行失败: %s" % (last_err or "无输出"))

    rows = [r for r in _parse_rows(out, fmt) if r["pid"] is not None]
    if pattern:
        pat = pattern.lower()
        rows = [r for r in rows if pat in (r["comm"] or "").lower()]
    total = len(rows)
    if sort == "cpu":
        rows.sort(key=lambda r: r["pcpu"] if r["pcpu"] is not None else -1.0,
                  reverse=order == "desc")
    elif sort == "mem":
        rows.sort(key=lambda r: r["pmem"] if r["pmem"] is not None else -1.0,
                  reverse=order == "desc")
    else:
        rows.sort(key=lambda r: r["pid"] or 0, reverse=order == "desc")
    return {"ok": True, "total": total,
            "processes": rows[offset:offset + limit]}


def process_detail(transport, pid, timeout=10):
    """Handle process detail."""
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("非法 pid: %r" % pid)
    script = (
        "cat /proc/%d/cmdline 2>/dev/null | tr '\\0' ' '; echo __SEP__;"
        " ls /proc/%d/task 2>/dev/null | wc -l; echo __SEP__;"
        " ls /proc/%d/fd 2>/dev/null | wc -l; echo __SEP__;"
        " cat /proc/%d/oom_score 2>/dev/null; echo __SEP__;"
        " awk '{print $19}' /proc/%d/stat 2>/dev/null; echo __SEP__;"
        " awk '{print $22}' /proc/%d/stat 2>/dev/null; echo __SEP__;"
        " grep -m1 btime /proc/stat 2>/dev/null"
    ) % (pid, pid, pid, pid, pid, pid)
    try:
        rc, out, err = transport.exec(script, timeout)
    except Exception as exc:
        raise ProcError("读取进程 %d 详情失败: %s" % (pid, exc))
    parts = [p.strip() for p in out.split("__SEP__")]
    if not parts[0] and not parts[5]:
        raise ProcError("进程不存在: %d" % pid)

    start_ms = None
    ticks = _int(parts[5])
    btime = None
    for ln in parts[6].splitlines():
        if ln.startswith("btime"):
            btime = _int(ln.split()[-1] if ln.split() else "")
            break
    if ticks is not None and btime is not None:
        start_ms = int((btime + ticks / 100.0) * 1000)

    proc = {"pid": pid,
            "cmdline": parts[0] or None,
            "threads": _int(parts[1]),
            "fd_count": _int(parts[2]),
            "oom_score": _int(parts[3]),
            "nice": _int(parts[4]),
            "start_ms": start_ms}
    return {"ok": True, "pid": pid, "process": proc}


def signal(transport, pid, sig, timeout=10):
    """Handle signal."""
    if sig not in _SIGNALS:
        raise ValueError("非法信号: %r（可选 %s）" % (sig, "/".join(_SIGNALS)))
    if pid <= 1:
        raise SignalBlockedError(
            "拒绝操作受保护进程 PID=%d（PID<=1）" % pid)
    try:
        rc, out, err = transport.exec("kill -s %s %d" % (sig, pid), timeout)
    except Exception as exc:
        raise ProcError("kill %d 执行失败: %s" % (pid, exc))
    return {"ok": True, "rc": rc, "sig": sig, "pid": pid}
