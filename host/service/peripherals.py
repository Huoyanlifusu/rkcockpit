"""Utilities for host.service.peripherals."""
import os
import re
import threading
import time

_CACHE_TTL = 10.0
_CACHE = {}
_CACHE_LOCK = threading.Lock()


_TIMEOUT = 10


class PeriphError(Exception):
    """Error raised for periph error."""


class _ProbeFail(Exception):
    """Manage probe fail."""


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


_PERM_MARKS = ("permission denied", "operation not permitted",
               "not permitted", "denied", "not authorized")


def _perm_hint(msg):
    """Handle perm hint."""
    low = (msg or "").lower()
    if any(m in low for m in _PERM_MARKS):
        return "（疑似权限不足，请以 root 运行门户或放宽内核权限后重试）"
    return ""



#

#   __BUSES__   i2c-N|adapter_name


_I2C_SCRIPT = (
    "echo __BUSES__;"
    " for b in /sys/bus/i2c/devices/i2c-*; do [ -d \"$b\" ] || continue;"
    " n=${b##*/}; echo \"$n|$(cat \"$b/name\" 2>/dev/null)\"; done;"
    " echo __DEVICES__;"
    " for d in /sys/bus/i2c/devices/*; do [ -d \"$d\" ] || continue;"
    " n=${d##*/}; case \"$n\" in i2c-*) continue;; esac;"
    " drv=\"\"; [ -e \"$d/driver\" ] &&"
    " drv=$(readlink -f \"$d/driver\" 2>/dev/null);"
    " echo \"$n|$(cat \"$d/name\" 2>/dev/null)|$drv\"; done"
)


def i2c(transport, device_id=None, timeout=_TIMEOUT):
    """Handle i2c."""
    key = (device_id or _kind(transport), "i2c")

    def run():
        try:
            rc, out, err = transport.exec(_I2C_SCRIPT, timeout)
        except Exception as exc:
            raise PeriphError("I2C 采集失败: %s" % (str(exc)[:200]))
        if rc != 0 and not (out or "").strip():
            msg = ((err or "").strip())[:200] or ("rc=%s" % rc)
            raise PeriphError("I2C 采集失败: %s%s" % (msg, _perm_hint(msg)))
        return _parse_i2c(out or "")

    return _cached(key, run)


def _parse_i2c(out):
    buses = {}
    order = []
    section = None
    for ln in out.splitlines():
        ln = ln.strip()
        if ln == "__BUSES__":
            section = "buses"
            continue
        if ln == "__DEVICES__":
            section = "devices"
            continue
        if not ln or section is None:
            continue
        if section == "buses":
            parts = ln.split("|", 1)
            bus = parts[0].strip()
            if not bus:
                continue
            entry = buses.get(bus)
            if entry is None:
                entry = {"bus": bus, "name": None, "devices": []}
                buses[bus] = entry
                order.append(bus)
            name = parts[1].strip() if len(parts) > 1 else ""
            entry["name"] = name or None
        else:  # devices
            parts = ln.split("|", 2)
            node = parts[0].strip()
            if not node or "-" not in node:
                continue
            bus, addr = node.split("-", 1)


            bus_key = bus if bus.startswith("i2c-") else "i2c-" + bus
            entry = buses.get(bus_key)
            if entry is None:
                entry = {"bus": bus_key, "name": None, "devices": []}
                buses[bus_key] = entry
                order.append(bus_key)
            name = parts[1].strip() if len(parts) > 1 else ""
            drv = parts[2].strip() if len(parts) > 2 else ""
            entry["devices"].append({
                "name": name or None,
                "driver": os.path.basename(drv.rstrip("/")) if drv else None,
                "addr": addr,
            })
    return {"ok": True, "buses": [buses[b] for b in order]}




_GPIOINFO_CMD = "gpioinfo 2>&1; echo __RC__=%s" % "$?"

_GPIO_DEBUGFS_CMD = "cat /sys/kernel/debug/gpio 2>&1"

_GPIO_SYSFS_SCRIPT = (
    "for c in /sys/class/gpio/gpiochip*; do [ -d \"$c\" ] || continue;"
    " echo \"$(basename \"$c\")|$(cat \"$c/ngpio\" 2>/dev/null)|"
    "$(cat \"$c/base\" 2>/dev/null)\"; done"
)





_RE_GPIOINFO_CHIP = re.compile(
    r"gpiochip(\d+)"
    r"(?:\s+\"[^\"]*\"|\s+\[[^\]]*\])?"
    r"\s*(?:-\s*(\d+)\s+lines?|\(\s*(\d+)\s+lines?\))"
)
_RE_GPIOINFO_LINE = re.compile(r"line\s+(\d+):\s*(.*)$")

# debugfs：gpiochipN: GPIOs 0-31, ... / gpio-N (label |consumer) in|out
_RE_DEBUG_CHIP = re.compile(r"gpiochip(\d+):\s*GPIOs\s+(\d+)(?:-(\d+))?")
_RE_DEBUG_LINE = re.compile(r"gpio-(\d+)\s+\((.*?)\)\s*(in|out)?\b")


def gpio(transport, device_id=None, timeout=_TIMEOUT):
    """Handle gpio."""
    key = (device_id or _kind(transport), "gpio")

    def run():
        try:
            reasons = []
            for src, probe in (("gpioinfo", _gpio_gpioinfo),
                               ("debugfs", _gpio_debugfs),
                               ("sysfs", _gpio_sysfs)):
                try:
                    chips = probe(transport, timeout)
                    return {"ok": True, "source": src, "chips": chips}
                except _ProbeFail as exc:
                    reasons.append("%s: %s" % (src, exc))
            joined = "；".join(reasons)
            raise PeriphError("GPIO 采集失败：三级降级均不可用（%s）%s"
                              % (joined, _perm_hint(joined)))
        except PeriphError:
            raise
        except Exception as exc:
            raise PeriphError("GPIO 采集失败: %s" % (str(exc)[:200]))

    return _cached(key, run)


def _gpio_gpioinfo(transport, timeout):
    """Handle gpio gpioinfo."""
    try:
        rc, out, err = transport.exec(_GPIOINFO_CMD, timeout)
    except Exception as exc:
        raise _ProbeFail("执行失败: %s" % (str(exc)[:120]))
    out = ((out or "") + (err or ""))
    m = re.search(r"__RC__=(\d+)", out)
    g_rc = int(m.group(1)) if m else rc
    out = re.sub(r"__RC__=\d+.*$", "", out, flags=re.S).strip()
    if not out:
        raise _ProbeFail("输出为空")
    low = out.lower()
    if "command not found" in low or "no such file" in low\
            or "not found" in low:
        raise _ProbeFail("未安装 libgpiod 工具（gpioinfo）")
    if g_rc != 0:
        raise _ProbeFail((out or err or "").strip()[-120:] or
                         "rc=%s" % g_rc)
    chips = _parse_gpioinfo(out)
    if not chips:
        raise _ProbeFail("输出未解析到 gpiochip（格式不兼容或确无控制器）")
    return chips


def _parse_gpioinfo(out):
    chips = []
    cur = None
    for ln in out.splitlines():
        m = _RE_GPIOINFO_CHIP.search(ln)
        if m:
            ngpio = m.group(2) or m.group(3)   # v1: - M lines / v2: (M lines)
            cur = {"name": "gpiochip%s" % m.group(1),
                   "ngpio": int(ngpio), "base": None, "lines": []}
            chips.append(cur)
            continue
        m = _RE_GPIOINFO_LINE.search(ln)
        if m and cur is not None:
            rest = m.group(2)
            quotes = re.findall(r'"([^"]*)"', rest)
            entry = {"line": int(m.group(1)),
                     "label": quotes[0] if quotes else None,
                     "owner": quotes[1] if len(quotes) > 1 else None,
                     "direction": None}
            dm = re.search(r"\b(input|output)\b", rest)
            if dm:
                entry["direction"] = dm.group(1)
            cur["lines"].append(entry)
    return chips


def _gpio_debugfs(transport, timeout):
    """Handle gpio debugfs."""
    try:
        rc, out, err = transport.exec(_GPIO_DEBUGFS_CMD, timeout)
    except Exception as exc:
        raise _ProbeFail("执行失败: %s" % (str(exc)[:120]))
    out = out or ""
    if rc != 0:
        raise _ProbeFail(((err or out).strip()[:120]) or "rc=%s" % rc)
    if not out.strip():
        raise _ProbeFail("debugfs 输出为空（可能未挂载或不可读）")
    chips = _parse_debugfs_gpio(out)
    if not chips:

        raise _ProbeFail("输出未解析到 gpiochip（格式不兼容或确无控制器）")
    return chips


def _parse_debugfs_gpio(out):
    chips = []
    cur = None
    for ln in out.splitlines():
        m = _RE_DEBUG_CHIP.search(ln)
        if m:
            lo = int(m.group(2))
            hi = int(m.group(3)) if m.group(3) else lo
            cur = {"name": "gpiochip%s" % m.group(1), "base": lo,
                   "ngpio": hi - lo + 1, "lines": []}
            chips.append(cur)
            continue
        m = _RE_DEBUG_LINE.search(ln)
        if m and cur is not None:
            inside = (m.group(2) or "").strip()
            parts = [p.strip() for p in inside.split("|")] if inside else []
            cur["lines"].append({
                "line": int(m.group(1)),
                "label": (parts[0] or None) if parts else None,
                "owner": (parts[1] or None) if len(parts) > 1 else None,
                "direction": m.group(3),
            })
    return chips


def _gpio_sysfs(transport, timeout):
    """Handle gpio sysfs."""
    try:
        rc, out, err = transport.exec(_GPIO_SYSFS_SCRIPT, timeout)
    except Exception as exc:
        raise _ProbeFail("执行失败: %s" % (str(exc)[:120]))
    out = out or ""
    if rc != 0 and not out.strip():
        raise _ProbeFail(((err or "").strip()[:120]) or "rc=%s" % rc)
    chips = []
    for ln in out.splitlines():
        parts = ln.split("|")
        name = parts[0].strip() if parts else ""
        if not name:
            continue
        ngpio = None
        if len(parts) > 1 and parts[1].strip().isdigit():
            ngpio = int(parts[1].strip())
        base = None
        if len(parts) > 2 and parts[2].strip().isdigit():
            base = int(parts[2].strip())
        chips.append({"name": name, "ngpio": ngpio, "base": base,
                      "lines": []})
    if not chips:
        raise _ProbeFail("sysfs 无 gpiochip 或不可读")
    return chips



#


#   __CH__|chip_name|index|period_ns|duty_cycle_ns|polarity|enable



_PWM_SCRIPT = (
    "for c in /sys/class/pwm/pwmchip*; do [ -d \"$c\" ] || continue;"
    " n=${c##*/}; lbl=\"\"; [ -r \"$c/label\" ] &&"
    " lbl=$(cat \"$c/label\" 2>/dev/null);"
    " echo \"__CHIP__|$n|$(cat \"$c/npwm\" 2>/dev/null)|$lbl\";"
    " for p in \"$c\"/pwm*; do [ -d \"$p\" ] || continue;"
    " idx=${p##*/}; case \"$idx\" in pwm[0-9]*) ;; *) continue ;; esac;"
    " echo \"__CH__|$n|${idx#pwm}|$(cat \"$p/period_ns\" 2>/dev/null)|"
    "$(cat \"$p/duty_cycle_ns\" 2>/dev/null)|$(cat \"$p/polarity\" 2>/dev/null)|"
    "$(cat \"$p/enable\" 2>/dev/null)\"; done; done"
)


def pwm(transport, device_id=None, timeout=_TIMEOUT):
    """Handle pwm."""
    key = (device_id or _kind(transport), "pwm")

    def run():
        try:
            rc, out, err = transport.exec(_PWM_SCRIPT, timeout)
        except Exception as exc:
            raise PeriphError("PWM 采集失败: %s" % (str(exc)[:200]))
        if rc != 0 and not (out or "").strip():
            msg = ((err or "").strip())[:200] or ("rc=%s" % rc)
            raise PeriphError("PWM 采集失败: %s%s" % (msg, _perm_hint(msg)))
        return _parse_pwm(out or "")

    return _cached(key, run)


def _parse_pwm(out):
    chips = []
    cur = None
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("__CHIP__|"):
            parts = ln[len("__CHIP__|"):].split("|", 3)
            name = parts[0].strip()
            if not name:
                continue
            npwm = None
            if len(parts) > 1 and parts[1].strip().isdigit():
                npwm = int(parts[1].strip())
            label = parts[2].strip() if len(parts) > 2 else ""
            cur = {"name": name, "label": label or None, "npwm": npwm,
                   "channels": []}
            chips.append(cur)
            continue
        if ln.startswith("__CH__|") and cur is not None:
            parts = ln[len("__CH__|"):].split("|", 6)
            if len(parts) < 2 or parts[0].strip() != cur["name"]:
                continue
            idx = parts[1].strip()
            if not idx.isdigit():
                continue

            def _num(s):
                s = (s or "").strip()
                return int(s) if s.isdigit() else None

            entry = {"index": int(idx),
                     "period_ns": _num(parts[2]) if len(parts) > 2 else None,
                     "duty_ns": _num(parts[3]) if len(parts) > 3 else None,
                     "polarity": (parts[4].strip() or None)
                     if len(parts) > 4 else None,
                     "enabled": None}
            if len(parts) > 5:
                en = parts[5].strip()
                if en == "1":
                    entry["enabled"] = True
                elif en == "0":
                    entry["enabled"] = False
            cur["channels"].append(entry)


    for chip in chips:
        npwm = chip.get("npwm")
        if not npwm or npwm <= 0:
            continue
        exported = {ch["index"] for ch in chip["channels"]}
        for i in range(npwm):
            if i not in exported:
                chip["channels"].append({"index": i})
        chip["channels"].sort(key=lambda ch: ch["index"])
    return {"ok": True, "chips": chips}


# ---- spi（D-PH-P1-1：/sys/class/spi_master + /sys/bus/spi/devices + spidev） ----
#






_SPI_SCRIPT = (
    "echo __MASTERS__;"
    " for m in /sys/class/spi_master/spi*; do [ -d \"$m\" ] || continue;"
    " echo \"$(basename \"$m\")\"; done;"
    " echo __DEVICES__;"
    " for d in /sys/bus/spi/devices/spi*.*; do [ -d \"$d\" ] || continue;"
    " echo \"${d##*/}|$(cat \"$d/name\" 2>/dev/null)|"
    "$(cat \"$d/modalias\" 2>/dev/null)\"; done;"
    " echo __SPIDEV__; ls /dev/spidev* 2>/dev/null"
)


def spi(transport, device_id=None, timeout=_TIMEOUT):
    """Handle spi."""
    key = (device_id or _kind(transport), "spi")

    def run():
        try:
            rc, out, err = transport.exec(_SPI_SCRIPT, timeout)
        except Exception as exc:
            raise PeriphError("SPI 采集失败: %s" % (str(exc)[:200]))
        if rc != 0 and not (out or "").strip():
            msg = ((err or "").strip())[:200] or ("rc=%s" % rc)
            raise PeriphError("SPI 采集失败: %s%s" % (msg, _perm_hint(msg)))
        return _parse_spi(out or "")

    return _cached(key, run)


def _parse_spi(out):
    masters = []
    by_name = {}
    device_order = []
    devices = {}
    spidev = set()    # "/dev/spidevX.Y"
    section = None
    for ln in out.splitlines():
        ln = ln.strip()
        if ln == "__MASTERS__":
            section = "masters"
            continue
        if ln == "__DEVICES__":
            section = "devices"
            continue
        if ln == "__SPIDEV__":
            section = "spidev"
            continue
        if not ln or section is None:
            continue
        if section == "masters":
            name = ln.split()[0] if ln else ""
            if name and name not in by_name:
                by_name[name] = {"name": name, "devices": []}
                masters.append(by_name[name])
        elif section == "devices":
            parts = ln.split("|", 2)
            key = parts[0].strip()
            if not key or "." not in key:
                continue
            dname = parts[1].strip() if len(parts) > 1 else ""
            if not dname and len(parts) > 2:
                modalias = parts[2].strip()

                if ":" in modalias:
                    modalias = modalias.split(":", 1)[1].strip()
                dname = modalias
            devices[key] = dname or None
            device_order.append(key)
        else:  # spidev
            if ln.startswith("/dev/spidev"):
                spidev.add(ln)
    for key in device_order:
        mname = key.split(".", 1)[0]
        m = by_name.get(mname)
        if m is None:
            m = {"name": mname, "devices": []}
            by_name[mname] = m
            masters.append(m)
        dev_node = "/dev/spidev" + key[len("spi"):]\
            if key.startswith("spi") else None


        m["devices"].append({"path": key, "name": devices[key],
                             "spidev": bool(dev_node and dev_node in spidev)})
    return {"ok": True, "masters": masters}



#




_UART_SCRIPT = (
    "ls /dev/ttyS* /dev/ttyUSB* 2>/dev/null;"
    " echo __SERIAL__; cat /proc/tty/driver/serial 2>/dev/null"
)


def uart(transport, device_id=None, timeout=_TIMEOUT):
    """Handle uart."""
    key = (device_id or _kind(transport), "uart")

    def run():
        try:
            rc, out, err = transport.exec(_UART_SCRIPT, timeout)
        except Exception as exc:
            raise PeriphError("UART 采集失败: %s" % (str(exc)[:200]))
        if rc != 0 and not (out or "").strip():
            msg = ((err or "").strip())[:200] or ("rc=%s" % rc)
            raise PeriphError("UART 采集失败: %s%s" % (msg, _perm_hint(msg)))
        return _parse_uart(out or "")

    return _cached(key, run)


def _parse_uart(out):
    nodes = []
    serial = {}
    section = None
    for ln in out.splitlines():
        ln = ln.strip()
        if ln == "__SERIAL__":
            section = "serial"
            continue
        if not ln:
            continue
        if section is None:
            if ln.startswith("/dev/"):
                nodes.append(ln[len("/dev/"):])
            continue
        m = re.match(r"^(\d+):", ln)
        if not m:
            continue
        idx = int(m.group(1))
        tm = re.search(r"\buart:(\S+)", ln)
        txm = re.search(r"\btx:(\d+)", ln)
        rxm = re.search(r"\brx:(\d+)", ln)
        serial[idx] = {
            "type": tm.group(1) if tm else None,
            "tx": int(txm.group(1)) if txm else None,
            "rx": int(rxm.group(1)) if rxm else None,
        }
    ports = []
    for node in sorted(nodes):
        info = None
        if node.startswith("ttyS"):
            idxs = node[len("ttyS"):]
            if idxs.isdigit():
                info = serial.get(int(idxs))
            ptype = (info or {}).get("type") if info is not None else None
        else:
            ptype = "usb-serial"
        ports.append({
            "name": node,
            "type": ptype,
            "tx": (info or {}).get("tx") if info is not None else None,
            "rx": (info or {}).get("rx") if info is not None else None,
        })
    return {"ok": True, "ports": ports}



#




_CLK_CMD = "cat /sys/kernel/debug/clk/clk_summary 2>&1"


def clk(transport, device_id=None, timeout=_TIMEOUT):
    """Handle clk."""
    key = (device_id or _kind(transport), "clk")

    def run():
        try:
            rc, out, err = transport.exec(_CLK_CMD, timeout)
        except Exception as exc:
            raise PeriphError("clk 采集失败: %s" % (str(exc)[:200]))
        out = out or ""
        if rc != 0 or not out.strip():
            reason = ((err or out).strip())[:200] or ("rc=%s" % rc)
            hint = _perm_hint(reason)
            msg = "无法读取 /sys/kernel/debug/clk/clk_summary"
            if hint:
                msg += hint
            elif reason:
                msg += "（%s）" % reason
            return {"ok": True, "restricted": True, "reason": msg}
        return _parse_clk(out)

    return _cached(key, run)


def _parse_clk(out):
    """Handle parse clk."""
    clocks = []
    has_protect = False
    in_header = True
    for ln in out.splitlines():
        if in_header:

            if "protect" in ln:
                has_protect = True
            if ln.lstrip().startswith("----"):
                in_header = False
            continue
        parts = ln.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        if name == "clock" or set(name) <= {"-"}:
            continue



        nums = []
        for p in parts[1:]:
            if not re.fullmatch(r"\d+", p):
                break
            nums.append(int(p))
        if len(nums) < 3:
            continue
        entry = {"name": name, "enable": nums[0], "prepare": nums[1]}
        if has_protect and len(nums) >= 3:
            entry["protect"] = nums[2]

        rate_idx = 3 if has_protect and len(nums) >= 4 else 2
        if len(nums) > rate_idx:
            entry["rate"] = nums[rate_idx]
        clocks.append(entry)
    return {"ok": True, "clocks": clocks}



#




_WATCHDOG_SCRIPT = (
    "for w in /sys/class/watchdog/watchdog*; do [ -d \"$w\" ] || continue;"
    " echo \"$(basename \"$w\")|$(cat \"$w/timeout\" 2>/dev/null)|"
    "$(cat \"$w/state\" 2>/dev/null)|$(cat \"$w/bootstatus\" 2>/dev/null)\"; done"
)


def watchdog(transport, device_id=None, timeout=_TIMEOUT):
    """Handle watchdog."""
    key = (device_id or _kind(transport), "watchdog")

    def run():
        try:
            rc, out, err = transport.exec(_WATCHDOG_SCRIPT, timeout)
        except Exception as exc:
            raise PeriphError("watchdog 采集失败: %s" % (str(exc)[:200]))
        if rc != 0 and not (out or "").strip():
            msg = ((err or "").strip())[:200] or ("rc=%s" % rc)
            raise PeriphError("watchdog 采集失败: %s%s" % (msg, _perm_hint(msg)))
        return _parse_watchdog(out or "")

    return _cached(key, run)


def _parse_watchdog(out):
    devs = []
    for ln in out.splitlines():
        parts = ln.split("|", 3)
        name = parts[0].strip()
        if not name:
            continue
        timeout_s = parts[1].strip() if len(parts) > 1 else ""
        state = parts[2].strip() if len(parts) > 2 else ""
        boot = parts[3].strip() if len(parts) > 3 else ""
        devs.append({
            "name": name,
            "timeout": int(timeout_s) if timeout_s.isdigit() else None,
            "state": state or None,
            "bootstatus": int(boot) if boot.isdigit() else None,
        })
    return {"ok": True, "devices": devs}



#



_REGULATOR_SCRIPT = (
    "for r in /sys/class/regulator/regulator.*; do [ -d \"$r\" ] || continue;"
    " n=$(cat \"$r/name\" 2>/dev/null);"
    " s=$(cat \"$r/state\" 2>/dev/null);"
    " u=$(cat \"$r/microvolts\" 2>/dev/null);"
    " echo \"${r##*/}|$n|$s|$u\"; done"
)


def regulator(transport, device_id=None, timeout=_TIMEOUT):
    """Handle regulator."""
    key = (device_id or _kind(transport), "regulator")

    def run():
        try:
            rc, out, err = transport.exec(_REGULATOR_SCRIPT, timeout)
        except Exception as exc:
            raise PeriphError("regulator 采集失败: %s" % (str(exc)[:200]))
        if rc != 0 and not (out or "").strip():
            msg = ((err or "").strip())[:200] or ("rc=%s" % rc)
            raise PeriphError("regulator 采集失败: %s%s" % (msg, _perm_hint(msg)))
        return _parse_regulator(out or "")

    return _cached(key, run)


def _parse_regulator(out):
    regs = []
    for ln in out.splitlines():
        parts = ln.split("|", 3)
        rid = parts[0].strip()
        if not rid:
            continue
        name = parts[1].strip() if len(parts) > 1 else ""
        state = parts[2].strip() if len(parts) > 2 else ""
        uv = parts[3].strip() if len(parts) > 3 else ""
        regs.append({
            "name": name or None,
            "state": state or None,
            "microvolts": int(uv) if uv.isdigit() else None,
        })
    return {"ok": True, "regulators": regs}



#





_DMA_SCRIPT = (
    "echo __PROC_DMA__; cat /proc/dma 2>&1;"
    " echo __DMAENGINE__; cat /sys/kernel/debug/dmaengine/summary 2>&1"
)


_SUMMARY_ERR_MARKS = ("permission denied", "operation not permitted",
                      "not permitted", "denied", "no such file",
                      "not a directory", "not found")


def dma(transport, device_id=None, timeout=_TIMEOUT):
    """Handle dma."""
    key = (device_id or _kind(transport), "dma")

    def run():
        try:
            rc, out, err = transport.exec(_DMA_SCRIPT, timeout)
        except Exception as exc:
            raise PeriphError("dma 采集失败: %s" % (str(exc)[:200]))
        out = out or ""
        if rc != 0 and not out.strip():
            msg = ((err or "").strip())[:200] or ("rc=%s" % rc)
            raise PeriphError("dma 采集失败: %s%s" % (msg, _perm_hint(msg)))
        proc_lines, summary_lines = _split_dma_sections(out)
        summary = "\n".join(summary_lines).strip()
        summary_ok = not _summary_unavailable(summary)
        controllers = _parse_dmaengine_summary(summary) if summary_ok else []
        channels = _parse_proc_dma("\n".join(proc_lines))
        if controllers or channels:
            result = {"ok": True}
            if controllers:
                result["controllers"] = controllers
            if channels:
                result["channels"] = channels
            return result
        if summary_ok:

            return {"ok": True, "channels": []}
        reason = summary[:200] or "输出为空（debugfs 未挂载或不可读）"
        hint = _perm_hint(reason)
        msg = "无法读取 /sys/kernel/debug/dmaengine/summary"
        if hint:
            msg += hint
        elif reason:
            msg += "（%s）" % reason
        return {"ok": True, "restricted": True, "reason": msg}

    return _cached(key, run)


def _split_dma_sections(out):
    """Handle split dma sections."""
    proc_lines, summary_lines = [], []
    section = None
    for ln in out.splitlines():
        ln = ln.strip()
        if ln == "__PROC_DMA__":
            section = "proc"
            continue
        if ln == "__DMAENGINE__":
            section = "engine"
            continue
        if section == "proc":
            proc_lines.append(ln)
        elif section == "engine":
            summary_lines.append(ln)
    return proc_lines, summary_lines


def _summary_unavailable(summary):
    """Handle summary unavailable."""
    low = summary.lower()
    return any(m in low for m in _SUMMARY_ERR_MARKS)


def _parse_proc_dma(out):
    """Handle parse proc dma."""
    entries = {}
    order = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^(\d+):\s*(.*)$", ln)
        if m:
            chan = int(m.group(1))
            name = m.group(2).strip() or None
            if chan not in entries:
                order.append(chan)
                entries[chan] = {"chan": chan, "name": name}
            elif name and entries[chan]["name"] is None:
                entries[chan]["name"] = name
            continue
        m = re.match(r"^Channel:\s*(\d+)$", ln, re.IGNORECASE)
        if m:
            chan = int(m.group(1))
            if chan not in entries:
                order.append(chan)
                entries[chan] = {"chan": chan, "name": None}
    return [entries[c] for c in order]


_RE_DMA_CTRL = re.compile(
    r"^(\S+)\s+\(([^)]*)\):\s*number of channels:\s*(\d+)\s*$")
_RE_DMA_CHAN = re.compile(r"^(\S+)\s*\|\s*(.*)$")


def _parse_dmaengine_summary(summary):
    """Handle parse dmaengine summary."""
    controllers = []
    cur = None
    for ln in summary.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = _RE_DMA_CTRL.match(ln)
        if m:
            cur = {
                "name": m.group(1),
                "addr": m.group(2).strip() or None,
                "nchannels": int(m.group(3)),
                "channels": [],
            }
            controllers.append(cur)
            continue
        m = _RE_DMA_CHAN.match(ln)
        if m and cur is not None:
            client = m.group(2).strip() or None
            if client == "(null)":
                client = None
            cur["channels"].append({"chan": m.group(1), "client": client})
    return controllers
