"""Utilities for host.service.sysinfo."""
import time

_SCRIPT = (
    "cat /etc/os-release 2>/dev/null | grep '^PRETTY_NAME=';"
    " echo __SEP__; uname -r;"
    " echo __SEP__; cat /proc/uptime;"
    " echo __SEP__; cat /proc/loadavg;"
    " echo __SEP__; grep -E 'MemTotal|MemAvailable' /proc/meminfo 2>/dev/null;"
    " echo __SEP__; cat /proc/stat | head -1;"
    " echo __SEP__; sleep 0.3; cat /proc/stat | head -1;"
    " echo __SEP__; cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq 2>/dev/null || cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null;"
    " echo __SEP__; for z in /sys/class/thermal/thermal_zone*/; do t=$(cat $z/type 2>/dev/null);"
    " v=$(cat $z/temp 2>/dev/null); echo \"$t:$v\"; done;"
    " echo __SEP__; df -k 2>/dev/null"
)


def _cpu_usage(s1, s2):
    try:
        f1 = s1.split()[1:5]
        f2 = s2.split()[1:5]
        a = sum(int(x) for x in f1)
        b = sum(int(x) for x in f2)
        idle1, idle2 = int(f1[3]), int(f2[3])
        dt = b - a
        if dt <= 0:
            return None
        return round(100.0 * (1 - (idle2 - idle1) / dt), 1)
    except Exception:
        return None


def collect(transport, timeout=25):
    data = {"uptime_s": None, "os": None, "kernel": None,
            "cpu_usage": None, "cpu_freq_mhz": None,
            "mem_total_mb": None, "mem_used_mb": None,
            "temp_c": {}, "disks": [], "load": None}
    try:
        rc, out, err = transport.exec(_SCRIPT, timeout)
        if rc != 0 and not out:
            return data
    except Exception:
        return data

    parts = [p.strip() for p in out.split("__SEP__")]

    def part(i):
        return parts[i] if i < len(parts) else ""

    try:
        p = part(0)
        data["os"] = p.split("=", 1)[1].strip("\"'") if "=" in p else (p or None)
    except Exception:
        pass
    data["kernel"] = part(1).splitlines()[0] if part(1) else None
    try:
        data["uptime_s"] = int(float(part(2).split()[0]))
    except Exception:
        pass
    try:
        data["load"] = [float(x) for x in part(3).split()[:3]]
    except Exception:
        pass
    for ln in part(4).splitlines():
        try:
            k, v = ln.split(":", 1)
            mb = int(v.split()[0]) // 1024
            if "MemTotal" in k:
                data["mem_total_mb"] = mb
            elif "MemAvailable" in k:
                data["mem_used_mb"] = data["mem_total_mb"] - mb\
                    if data["mem_total_mb"] else None
        except Exception:
            pass
    data["cpu_usage"] = _cpu_usage(part(5), part(6))
    try:
        data["cpu_freq_mhz"] = int(part(7).strip()) // 1000
    except Exception:
        pass
    for ln in part(8).splitlines():
        try:
            name, v = ln.split(":", 1)
            data["temp_c"][name.strip()] = round(int(v) / 1000, 1)
        except Exception:
            pass
    lines = [ln for ln in part(9).splitlines() if ln.strip()][1:]
    for ln in lines:
        cols = ln.split()
        if len(cols) < 4:
            continue
        try:
            data["disks"].append({
                "mount": cols[-1],
                "total_mb": int(cols[1]),
                "used_mb": int(cols[2]),
            })
        except Exception:
            pass
    return data
