"""Utilities for host.service.diag."""
import os
import re
import threading
import time

MAX_DMESG_BYTES = 256 * 1024
_CACHE_TTL = 10.0
_CACHE = {}
_CACHE_LOCK = threading.Lock()


class DiagError(Exception):
    """Error raised for diag error."""


def _cached(key, fn):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    data = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), data)
    return data


def _kind(transport):
    return getattr(transport, "kind", "?")


# ---- video ----

_VIDEO_SCRIPT = (
    "echo __DEVICES__; ls -1 /dev/video* 2>/dev/null;"
    " echo __SYSFS__; for n in /sys/class/video4linux/video*; do"
    " [ -d \"$n\" ] || continue; echo \"${n##*/}:$(cat \"$n/name\" 2>/dev/null)\"; done;"
    " echo __V4L2__; if command -v v4l2-ctl >/dev/null 2>&1; then"
    " for v in /dev/video*; do [ -e \"$v\" ] || continue; echo \"==$v\";"
    " v4l2-ctl -d \"$v\" --list-formats-ext 2>/dev/null | head -60; done;"
    " else echo NOV4L2; fi"
)


def video(transport, device_id=None, timeout=15):
    """Handle video."""
    key = (device_id or _kind(transport), "video")

    def run():
        try:
            rc, out, err = transport.exec(_VIDEO_SCRIPT, timeout)
        except Exception as exc:
            raise DiagError("video 采集失败: %s" % (str(exc)[:200]))
        if rc != 0 and not out:
            raise DiagError("video 采集失败: %s" % ((err or "").strip()[:200]))
        return _parse_video(out)

    return _cached(key, run)


def _parse_video(out):
    dev_paths = []
    sysfs_names = {}
    v4l2_blocks = {}
    section = None
    cur_dev = None
    for ln in out.splitlines():
        if ln == "__DEVICES__":
            section = "devices"
            continue
        if ln == "__SYSFS__":
            section = "sysfs"
            continue
        if ln == "__V4L2__":
            section = "v4l2"
            continue
        if section == "devices" and ln.strip():
            dev_paths.append(ln.strip())
        elif section == "sysfs" and ":" in ln:
            node, name = ln.split(":", 1)
            sysfs_names[node.strip()] = name.strip()
        elif section == "v4l2":
            if ln.startswith("=="):
                cur_dev = ln[2:].strip()
                v4l2_blocks[cur_dev] = []
            elif cur_dev is not None:
                v4l2_blocks[cur_dev].append(ln)
    devices = []
    for path in dev_paths:
        node = os.path.basename(path.rstrip("/"))
        devices.append({
            "path": path,
            "name": sysfs_names.get(node),
            "formats": _parse_v4l2_block(v4l2_blocks.get(path, [])),
            "status": None,
        })
    return {"ok": True, "devices": devices}


def _parse_v4l2_block(lines):
    fmts = []
    cur = None
    for ln in lines:
        m = re.search(r"Pixel Format:\s*['\"]([^'\"]+)['\"]", ln)
        if m:
            cur = m.group(1)
            if not fmts or fmts[-1][0] != cur:
                fmts.append([cur, []])
            continue
        m = re.search(r"Size:\s*(?:Discrete|Stepwise)?\s*(\d+x\d+)", ln)
        if m and cur and m.group(1) not in fmts[-1][1]:
            fmts[-1][1].append(m.group(1))
    if not fmts:
        return None
    return "; ".join("%s %s" % (f, " ".join(sizes)) for f, sizes in fmts)


# ---- usb ----

_RE_LSUSB = re.compile(
    r"Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):"
    r"([0-9a-fA-F]{4})\s*(.*)")
_USB_SYSFS_SCRIPT = (
    "for d in /sys/bus/usb/devices/*; do [ -e \"$d\" ] || continue;"
    " echo \"$(basename \"$d\")|$(cat \"$d/idVendor\" 2>/dev/null)|"
    "$(cat \"$d/idProduct\" 2>/dev/null)|$(cat \"$d/product\" 2>/dev/null)\"; done"
)


def usb(transport, device_id=None, timeout=15):
    """Handle usb."""
    key = (device_id or _kind(transport), "usb")

    def run():
        try:
            rc, out, err = transport.exec("lsusb 2>&1", timeout)
        except Exception as exc:
            rc, out, err = 1, "", str(exc)
        if rc == 0 and out.strip():
            return {"ok": True, "devices": _parse_lsusb(out)}
        try:
            rc2, out2, err2 = transport.exec(_USB_SYSFS_SCRIPT, timeout)
        except Exception as exc:
            rc2, out2, err2 = 1, "", str(exc)
        if rc2 == 0 and out2.strip():
            return {"ok": True, "devices": _parse_usb_sysfs(out2)}
        raise DiagError("USB 采集失败：无 lsusb 且 sysfs 不可读（%s）"
                        % ((err2 or err).strip()[:200]))

    return _cached(key, run)


def _parse_lsusb(out):
    devices = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = _RE_LSUSB.match(s)
        if m:
            devices.append({"bus": m.group(1), "dev": m.group(2),
                            "vid": m.group(3).lower(),
                            "pid": m.group(4).lower(),
                            "desc": m.group(5).strip()})
        else:
            devices.append({"raw": s})
    return devices


def _parse_usb_sysfs(out):
    devices = []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        parts = ln.split("|")
        if len(parts) < 4:
            continue
        node, vid, pid, product = parts[0], parts[1], parts[2], parts[3]
        devices.append({"bus": node.split("-")[0], "dev": node,
                        "vid": vid or None, "pid": pid or None,
                        "desc": product or None})
    return devices


# ---- dmesg ----


def dmesg(transport, lines=200, filter=None, device_id=None, timeout=15):
    """Handle dmesg."""
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        raise ValueError("lines 参数非法: %r" % (lines,))
    lines = max(1, min(lines, 2000))


    window = lines if not filter else max(lines, 2000)
    raw = _dmesg_raw(transport, timeout, tail=window)
    all_lines = raw.splitlines()
    if filter:
        f = filter.lower()
        all_lines = [ln for ln in all_lines if f in ln.lower()]
    return {"ok": True, "lines": all_lines[-lines:], "truncated": False}


def _dmesg_raw(transport, timeout, tail=None):

    cmd = ("dmesg | tail -n %d" % tail) if tail else "dmesg 2>&1"
    try:
        rc, out, err = transport.exec(cmd, timeout)
    except Exception as exc:
        rc, out, err = 1, "", str(exc)
    if rc == 0 and out.strip():
        return out
    dmesg_err = (out or err or "").strip()[:100]
    try:
        rc2, out2, err2 = transport.exec(
            "head -c 1048576 /dev/kmsg 2>&1", timeout)
    except Exception as exc:
        rc2, out2, err2 = 1, "", str(exc)
    if rc2 == 0 and out2.strip():
        return out2
    kmsg_err = (out2 or err2 or "").strip()[:100]
    raise DiagError(
        "无法读取内核日志：dmesg 无权限（%s）且 /dev/kmsg 不可读（%s），"
        "请在设备上以 root 运行或放宽 dmesg_restrict"
        % (dmesg_err, kmsg_err))



#
# DECISION（worklog D1~D9）：









_STREAM_SEM = threading.Semaphore(2)
_STREAM_TIMEOUT = 20
_RE_VIDEO_DEV = re.compile(r"^/dev/video\d+$")
_RE_PIXFMT_ARG = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_RE_CAPTURED = re.compile(r"Captured\s+(\d+)\s+frames", re.IGNORECASE)



_RE_PIXFMT = re.compile(
    r"(?:Pixel Format:\s*|^\s*\[\d+\]:\s*)['\"]([^'\"]+)['\"]")
_RE_SIZE = re.compile(r"Size:\s*(?:Discrete|Stepwise)?\s*(\d+x\d+)")

_TOOL_PROBE_SCRIPT = (
    "if command -v v4l2-ctl >/dev/null 2>&1; then echo HAS_V4L2_CTL; fi;"
    " if command -v rk_sensor_sync >/dev/null 2>&1;"
    " then echo HAS_RK_SENSOR_SYNC; fi"
)
_FMT_SCRIPT = (
    "if [ -e %(video)s ]; then v4l2-ctl -d %(video)s --list-formats-ext 2>&1;"
    " else echo DEV_NOT_FOUND; fi"
)


class StreamBusy(Exception):
    """Manage stream busy."""


def _occupied_default(exec_fn):
    """Handle occupied default."""
    try:
        rc, _out, _err = exec_fn("systemctl is-active rkss-capture.service",
                                 timeout=10)
    except Exception:
        return False
    return rc == 0


def stream_test(transport, device_id, video, width=None, height=None,
                pixelformat=None, _exec=None, _occupied=None):
    """Handle stream test."""
    if not video or not _RE_VIDEO_DEV.match(str(video)):
        raise ValueError("video 参数必须形如 /dev/videoN")
    if (width is None) != (height is None):
        raise ValueError("width 与 height 必须同时指定")
    if width is not None:
        try:
            width = int(width)
            height = int(height)
        except (TypeError, ValueError):
            raise ValueError("width/height 必须是整数")
        if width < 1 or height < 1:
            raise ValueError("width/height 必须为正整数")
    if pixelformat is not None and not _RE_PIXFMT_ARG.match(str(pixelformat)):
        raise ValueError("pixelformat 仅允许字母/数字/下划线")

    exec_fn = _exec or transport.exec
    occupied = _occupied or _occupied_default
    if not _STREAM_SEM.acquire(blocking=False):
        raise StreamBusy("出流测试并发已满（上限 2 路），请稍后重试")
    t0 = time.time()
    try:
        if occupied(exec_fn):
            raise DiagError("rkss-capture 服务正在占用相机，禁止并发出流测试")
        has_v4l2, has_rk = _probe_tools(exec_fn)
        if not has_v4l2 and not has_rk:
            err = DiagError("设备上未找到 v4l2-ctl 或 rk_sensor_sync，"
                            "无法进行出流测试")
            err.status = "NOV4L2"
            raise err
        if has_v4l2:
            return _stream_v4l2(exec_fn, video, width, height, pixelformat, t0)
        return _stream_rk(exec_fn, video, t0)
    finally:
        _STREAM_SEM.release()


def _probe_tools(exec_fn):
    """Handle probe tools."""
    try:
        rc, out, err = exec_fn(_TOOL_PROBE_SCRIPT, timeout=10)
    except Exception as exc:
        raise DiagError("工具探测失败: %s" % (str(exc)[:200]))
    out = out or ""
    return "HAS_V4L2_CTL" in out, "HAS_RK_SENSOR_SYNC" in out


def _list_formats(lines):
    """Handle list formats."""
    fmts = []
    cur = None
    for ln in lines:
        m = _RE_PIXFMT.search(ln)
        if m:
            cur = m.group(1)
            if not fmts or fmts[-1][0] != cur:
                fmts.append([cur, []])
            continue
        m = _RE_SIZE.search(ln)
        if m and cur and m.group(1) not in fmts[-1][1]:
            fmts[-1][1].append(m.group(1))
    return fmts


def _raw_path(video):
    """Handle raw path."""
    return "/tmp/rkss_stream_test_%s_%d.raw" % (
        os.path.basename(video), int(time.time() * 1000))


def _parse_captured(out):
    """Handle parse captured."""
    out = out or ""
    m = _RE_CAPTURED.search(out)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None

    n = out.count("<")
    return n if n > 0 else None


def _stat_size(exec_fn, rawfile):
    try:
        rc, out, err = exec_fn("stat -c %s %s 2>&1" % ("%s", rawfile),
                               timeout=10)
    except Exception:
        return None
    s = (out or "").strip()
    if rc == 0 and s.isdigit():
        return int(s)
    return None


def _stream_v4l2(exec_fn, video, width, height, pixelformat, t0):
    """Handle stream v4l2."""
    script = _FMT_SCRIPT % {"video": video}
    try:
        rc, out, err = exec_fn(script, timeout=15)
    except Exception as exc:
        raise DiagError("获取视频格式失败: %s" % (str(exc)[:200]))
    out = out or ""
    if "DEV_NOT_FOUND" in out:
        raise DiagError("视频设备不存在: %s" % video)
    if width is None or height is None or pixelformat is None:
        fmts = _list_formats(out.splitlines())
        if not fmts:
            raise DiagError("无法解析 %s 的 v4l2 格式：%s"
                            % (video, ((err or out).strip()[-150:])))
        fmt, sizes = fmts[0]
        if not sizes:
            raise DiagError("格式 %s 无离散分辨率，请显式指定 width/height"
                            % fmt)
        w, h = sizes[0].split("x")
        if width is None:
            width = int(w)
        if height is None:
            height = int(h)
        if pixelformat is None:
            pixelformat = fmt
    rawfile = _raw_path(video)
    cmd = ("timeout -s KILL 15 v4l2-ctl -d %s "
           "--set-fmt-video=width=%s,height=%s,pixelformat=%s "
           "--stream-mmap --stream-count=30 --stream-to=%s 2>&1"
           % (video, width, height, pixelformat, rawfile))
    try:
        rc, out, err = exec_fn(cmd, timeout=_STREAM_TIMEOUT)
    except Exception as exc:
        raise DiagError("出流测试执行失败: %s" % (str(exc)[:200]))
    out = out or ""
    frames = _parse_captured(out)
    base = {"ok": True, "tool": "v4l2-ctl", "video": video,
            "width": width, "height": height, "pixelformat": pixelformat,
            "frames": frames, "file": rawfile,
            "file_size": _stat_size(exec_fn, rawfile),
            "duration_ms": int((time.time() - t0) * 1000)}
    if rc in (124, 137):
        base["status"] = "TIMEOUT"
        base["error"] = "出流测试超时（15s），已自动终止进程"
        return base
    if rc == 0 and frames == 30:
        base["status"] = "STREAMOK"
        return base
    base["status"] = "STREAM_FAIL"
    if frames is not None:
        base["error"] = "预期捕获 30 帧，实际捕获 %s 帧" % frames
    else:
        base["error"] = ((err or out).strip()[-200:]) or "出流测试失败（rc=%s）" % rc
    return base


def _stream_rk(exec_fn, video, t0):
    """Handle stream rk."""
    cmd = "timeout -s KILL 15 rk_sensor_sync -d %s 2>&1" % video
    try:
        rc, out, err = exec_fn(cmd, timeout=_STREAM_TIMEOUT)
    except Exception as exc:
        raise DiagError("出流测试执行失败: %s" % (str(exc)[:200]))
    base = {"ok": True, "tool": "rk_sensor_sync", "video": video,
            "width": None, "height": None, "pixelformat": None,
            "frames": None, "file": None, "file_size": None,
            "duration_ms": int((time.time() - t0) * 1000)}
    if rc in (124, 137):
        base["status"] = "TIMEOUT"
        base["error"] = "出流测试超时（15s），已自动终止进程"
        return base
    if rc == 0:
        base["status"] = "STREAMOK"
        return base
    base["status"] = "STREAM_FAIL"
    base["error"] = ((err or out or "").strip()[-200:]) or "rk_sensor_sync 失败（rc=%s）" % rc
    return base
