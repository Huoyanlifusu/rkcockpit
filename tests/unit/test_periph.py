#!/usr/bin/env python3
"""Test module."""
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))
PY = sys.executable
CONF_DIR = tempfile.mkdtemp(prefix="rkss-periph-test-")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] + 100


PORTAL = _free_port()


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def wait_port(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_get("http://127.0.0.1:%d/api/health" % port, 0.5)
            return True
        except Exception:
            time.sleep(0.2)
    return False


class Proc:
    def __init__(self, args):
        self.p = subprocess.Popen(args, cwd=BASE,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  start_new_session=True)

    def stop(self):
        try:
            os.killpg(os.getpgid(self.p.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            self.p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(self.p.pid), signal.SIGKILL)
            except Exception:
                pass


class FakeTransport:
    """Test class."""

    kind = "fake"

    def __init__(self, out, rc=0):
        self.out = out
        self.rc = rc
        self.calls = 0

    def exec(self, cmd, timeout=30):
        self.calls += 1
        return self.rc, self.out, ""


class PeriphUnitTest(unittest.TestCase):
    portal = None

    @classmethod
    def setUpClass(cls):
        cls.portal = Proc([PY, "-m", "portal.portal", "--port", str(PORTAL),
                           "--bind", "127.0.0.1", "--sim",
                           "--conf-dir", CONF_DIR])
        assert wait_port(PORTAL), "portal not up"

    @classmethod
    def tearDownClass(cls):
        cls.portal.stop()
        shutil.rmtree(CONF_DIR, ignore_errors=True)

    def periph_url(self, path):
        return "http://127.0.0.1:%d/api/periph/%s" % (PORTAL, path)

    # ---- shape ----

    def test_01_i2c_shape(self):
        st, r = http_get(self.periph_url("local/i2c"))
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertIn("buses", r)
        self.assertIsInstance(r["buses"], list)
        for b in r["buses"]:
            for key in ("bus", "name", "devices"):
                self.assertIn(key, b, key)
            self.assertIsInstance(b["devices"], list)
            for d in b["devices"]:
                for key in ("name", "driver", "addr"):
                    self.assertIn(key, d, key)

    def test_02_gpio_shape(self):
        st, r = http_get(self.periph_url("local/gpio"))
        self.assertEqual(st, 200)
        if not r.get("ok"):
            self.assertIn("error", r)
            return
        self.assertIn("chips", r)
        self.assertIsInstance(r["chips"], list)
        for c in r["chips"]:
            for key in ("name", "ngpio", "lines"):
                self.assertIn(key, c, key)
            self.assertIsInstance(c["lines"], list)
            for ln in c["lines"]:
                self.assertIn("line", ln)


    # ---- 404 / 400 ----

    def test_03_unknown_device_404(self):
        for path in ("i2c", "gpio", "pwm", "spi", "uart", "clk",
                     "watchdog", "regulator", "dma"):
            url = self.periph_url("ghost/" + path)
            try:
                http_get(url)
                self.fail("expected 404 for %s" % url)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404, url)
                self.assertFalse(json.loads(exc.read()).get("ok"))

    def test_04_transport_error_400(self):
        """Test helper."""
        from host.api.handlers import periph as periph_h

        class BadHost:
            def _transport(self, did):
                raise ValueError("构造失败: boom")

        class _W:
            def __init__(self):
                self.buf = io.BytesIO()

            def write(self, b):
                self.buf.write(b)

        class _H:
            def __init__(self):
                self.wfile = _W()
                self.code = None

            def send_response(self, code):
                self.code = code

            def send_header(self, k, v):
                pass

            def end_headers(self):
                pass

        cases = [
            (periph_h.periph_i2c, "i2c"), (periph_h.periph_gpio, "gpio"),
            (periph_h.periph_pwm, "pwm"), (periph_h.periph_spi, "spi"),
            (periph_h.periph_uart, "uart"), (periph_h.periph_clk, "clk"),
            (periph_h.periph_watchdog, "watchdog"),
            (periph_h.periph_regulator, "regulator"),
            (periph_h.periph_dma, "dma"),
        ]
        for fn, name in cases:
            h = _H()
            mm = re.match(r"^/api/periph/([^/]+)/%s$" % name,
                          "/api/periph/bad/%s" % name)
            handled = fn(h, BadHost(), mm, {})
            self.assertTrue(handled, name)
            self.assertEqual(h.code, 400, name)
            payload = json.loads(h.wfile.buf.getvalue().decode("utf-8"))
            self.assertFalse(payload["ok"], name)
            self.assertIn("error", payload, name)



    def test_05_cache_hit(self):
        import host.service.peripherals as periph_mod
        out = "__BUSES__\n0|test-i2c\n__DEVICES__\n1-0050|probe|/sys/bus/i2c/drivers/x\n"
        ft = FakeTransport(out)
        r1 = periph_mod.i2c(ft, device_id="cacheprobe-periph")
        r2 = periph_mod.i2c(ft, device_id="cacheprobe-periph")
        self.assertIs(r1, r2, "第二次调用应命中缓存（同一对象）")
        self.assertEqual(ft.calls, 1, "命中缓存后不应再执行脚本")
        key = ("cacheprobe-periph", "i2c")
        self.assertIn(key, periph_mod._CACHE)
        self.assertLess(time.time() - periph_mod._CACHE[key][0], 10)

        st1, hr1 = http_get(self.periph_url("local/i2c"))
        st2, hr2 = http_get(self.periph_url("local/i2c"))
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual(hr1, hr2)



    def test_06_gpioinfo_v1_fixture(self):
        import host.service.peripherals as periph_mod
        out = (
            'gpiochip0 - 8 lines:\n'
            '\tline   0:      "unnamed"       unused   input  active-high\n'
            '\tline   4:      "power_key"     "gpio-keys"     '
            'input active-high [used]\n'
            'gpiochip1 "gpio1" - 32 lines:\n'
            '\tline   0: "unnamed" unused input active-high\n'
        )
        chips = periph_mod._parse_gpioinfo(out)
        self.assertEqual(len(chips), 2)
        self.assertEqual(chips[0], {
            "name": "gpiochip0", "ngpio": 8, "base": None, "lines": [
                {"line": 0, "label": "unnamed", "owner": None,
                 "direction": "input"},
                {"line": 4, "label": "power_key", "owner": "gpio-keys",
                 "direction": "input"},
            ]})
        self.assertEqual(chips[1]["name"], "gpiochip1")
        self.assertEqual(chips[1]["ngpio"], 32)

    def test_07_gpioinfo_v2_fixture(self):
        import host.service.peripherals as periph_mod
        out = (
            'gpiochip0 [gpio0] (32 lines):\n'
            '\tline 0:\t\t"unnamed"\tunused\tinput active-high\n'
            '\tline 12:\t"led_r"\t"leds"\toutput active-low [used]\n'
            'gpiochip1 [gpio1] (8 lines):\n'
            '\tline 0:\t"btn" "gpio-keys" input active-high [used]\n'
        )
        chips = periph_mod._parse_gpioinfo(out)
        self.assertEqual(len(chips), 2)
        self.assertEqual(chips[0]["name"], "gpiochip0")
        self.assertEqual(chips[0]["ngpio"], 32)
        self.assertEqual(chips[0]["lines"][0], {
            "line": 0, "label": "unnamed", "owner": None,
            "direction": "input"})
        self.assertEqual(chips[0]["lines"][1], {
            "line": 12, "label": "led_r", "owner": "leds",
            "direction": "output"})
        self.assertEqual(chips[1]["ngpio"], 8)

    def test_08_debugfs_fixture(self):
        import host.service.peripherals as periph_mod
        out = (
            'gpiochip0: GPIOs 0-31, parent: platform/ffd73000.pinctrl, '
            'pinctrl:\n'
            ' gpio-0  (                    |sysfs               ) in  hi\n'
            ' gpio-5  (        power_key   |gpio-keys           ) in  hi\n'
            ' gpio-12 (                     ) out lo\n'
            'gpiochip1: GPIOs 32-63, parent: platform/ffd74000.pinctrl, '
            'pinctrl:\n'
        )
        chips = periph_mod._parse_debugfs_gpio(out)
        self.assertEqual(len(chips), 2)
        self.assertEqual(chips[0]["name"], "gpiochip0")
        self.assertEqual(chips[0]["base"], 0)
        self.assertEqual(chips[0]["ngpio"], 32)
        self.assertEqual(chips[0]["lines"][0], {
            "line": 0, "label": None, "owner": "sysfs", "direction": "in"})
        self.assertEqual(chips[0]["lines"][1]["label"], "power_key")
        self.assertEqual(chips[0]["lines"][2], {
            "line": 12, "label": None, "owner": None, "direction": "out"})
        self.assertEqual(chips[1]["ngpio"], 32)
        self.assertEqual(chips[1]["lines"], [])

    def test_09_sysfs_fixture(self):
        import host.service.peripherals as periph_mod
        ft = FakeTransport("gpiochip0|32|0\ngpiochip1|32|32\n")
        chips = periph_mod._gpio_sysfs(ft, 5)
        self.assertEqual(chips, [
            {"name": "gpiochip0", "ngpio": 32, "base": 0, "lines": []},
            {"name": "gpiochip1", "ngpio": 32, "base": 32, "lines": []},
        ])

    def test_10_gpio_chain_empty_gpioinfo_degrade_debugfs(self):
        """Test helper."""
        import host.service.peripherals as periph_mod

        class _FT:
            kind = "fake"

            def __init__(self, table):
                self.table = table
                self.calls = []

            def exec(self, cmd, timeout=30):
                self.calls.append(cmd)
                for key, res in self.table.items():
                    if key in cmd:
                        return res
                return 0, "", ""

        dbg = ("gpiochip0: GPIOs 0-31, parent: platform/x, pinctrl:\n"
               " gpio-0  ( |sysfs) in hi\n")
        ft = _FT({"gpioinfo": (0, "__RC__=0\n", ""),
                  "kernel/debug": (0, dbg, "")})
        r = periph_mod.gpio(ft, device_id="chain-empty-gpioinfo")
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "debugfs")
        self.assertEqual(len(r["chips"]), 1)
        self.assertEqual(r["chips"][0]["name"], "gpiochip0")
        self.assertTrue(any("gpioinfo" in c for c in ft.calls),
                        "链应经历 gpioinfo 尝试")


        ft2 = _FT({"gpioinfo": (0, "gpiochip0 [gpio0] (8 lines):\n__RC__=0\n", ""),
                   "kernel/debug": (0, dbg, "")})
        r2 = periph_mod.gpio(ft2, device_id="chain-gpioinfo-v2-ok")
        self.assertEqual(r2["source"], "gpioinfo")
        self.assertEqual(r2["chips"][0]["ngpio"], 8)

        ft3 = _FT({"gpioinfo": (0, "some weird line\n__RC__=0\n", ""),
                   "kernel/debug": (0, dbg, "")})
        r3 = periph_mod.gpio(ft3, device_id="chain-unparseable-gpioinfo")
        self.assertEqual(r3["source"], "debugfs")



    def test_11_i2c_real_script_bus_key_fixture(self):
        """Test helper."""
        import host.service.peripherals as periph_mod
        out = (
            "__BUSES__\n"
            "i2c-0|rk3x-i2c\n"
            "i2c-1|i2c-1-mux (chan_id 0)\n"
            "__DEVICES__\n"
            "0-0049|rk805 pmic|/sys/bus/i2c/drivers/rk808\n"
            "1-001a|stmvl53l0|/sys/bus/i2c/drivers/stmvl53l0\n"
            "2-0050||\n"
        )
        r = periph_mod._parse_i2c(out)
        self.assertTrue(r["ok"])
        buses = r["buses"]
        self.assertEqual([b["bus"] for b in buses],
                         ["i2c-0", "i2c-1", "i2c-2"])
        self.assertEqual(buses[0]["name"], "rk3x-i2c")

        self.assertEqual(buses[0]["devices"], [
            {"name": "rk805 pmic", "driver": "rk808", "addr": "0049"}])
        self.assertEqual(buses[1]["devices"], [
            {"name": "stmvl53l0", "driver": "stmvl53l0", "addr": "001a"}])
        self.assertEqual(buses[2], {"bus": "i2c-2", "name": None, "devices": [
            {"name": None, "driver": None, "addr": "0050"}]})



    def test_12_pwm_parse_fixture(self):
        import host.service.peripherals as periph_mod
        out = (
            "__CHIP__|pwmchip0|4|pwm-fan\n"
            "__CH__|pwmchip0|0|1000000|500000|normal|1\n"
            "__CH__|pwmchip0|1|2000000||inversed|0\n"
            "__CHIP__|pwmchip1|2|\n"
        )
        r = periph_mod._parse_pwm(out)
        self.assertTrue(r["ok"])
        chips = r["chips"]
        self.assertEqual(len(chips), 2)
        self.assertEqual(chips[0], {
            "name": "pwmchip0", "label": "pwm-fan", "npwm": 4,
            "channels": [
                {"index": 0, "period_ns": 1000000, "duty_ns": 500000,
                 "polarity": "normal", "enabled": True},
                {"index": 1, "period_ns": 2000000, "duty_ns": None,
                 "polarity": "inversed", "enabled": False},

                {"index": 2}, {"index": 3},
            ]})

        self.assertEqual(chips[1], {"name": "pwmchip1", "label": None,
                                    "npwm": 2,
                                    "channels": [{"index": 0}, {"index": 1}]})

        ft = FakeTransport(out)
        r2 = periph_mod.pwm(ft, device_id="pwm-fixture")
        self.assertEqual(r2["chips"][0]["name"], "pwmchip0")
        r3 = periph_mod.pwm(ft, device_id="pwm-fixture")
        self.assertIs(r2, r3, "第二次调用应命中缓存")
        self.assertEqual(ft.calls, 1)

    def test_13_spi_parse_fixture(self):
        import host.service.peripherals as periph_mod
        out = (
            "__MASTERS__\nspi0\nspi2\n"
            "__DEVICES__\nspi0.0|spi-nor\nspi2.0|adc\n"
            "__SPIDEV__\n/dev/spidev0.0\n"
        )
        r = periph_mod._parse_spi(out)
        self.assertTrue(r["ok"])

        self.assertEqual(r["masters"], [
            {"name": "spi0", "devices": [
                {"path": "spi0.0", "name": "spi-nor", "spidev": True}]},
            {"name": "spi2", "devices": [
                {"path": "spi2.0", "name": "adc", "spidev": False}]},
        ])

        ft = FakeTransport(out)
        r2 = periph_mod.spi(ft, device_id="spi-fixture")
        self.assertEqual(r2["masters"][0]["devices"][0]["path"], "spi0.0")
        self.assertEqual(r2["masters"][0]["devices"][0]["spidev"], True)
        self.assertEqual(r2["masters"][1]["devices"][0]["path"], "spi2.0")
        self.assertEqual(r2["masters"][1]["devices"][0]["spidev"], False)

        out2 = ("__MASTERS__\n__DEVICES__\nspi3.1|flash\n__SPIDEV__\n"
                "/dev/spidev3.1\n")
        r3 = periph_mod._parse_spi(out2)
        self.assertEqual(r3["masters"], [{"name": "spi3", "devices": [
            {"path": "spi3.1", "name": "flash", "spidev": True}]}])

        out3 = ("__MASTERS__\n__DEVICES__\nspi0.0||spi:mcp2518fd\n"
                "__SPIDEV__\n")
        r4 = periph_mod._parse_spi(out3)
        self.assertEqual(r4["masters"][0]["devices"][0]["name"], "mcp2518fd")

    def test_14_uart_parse_fixture(self):
        import host.service.peripherals as periph_mod
        out = (
            "/dev/ttyS0\n/dev/ttyS5\n/dev/ttyUSB0\n__SERIAL__\n"
            "serinfo:1.0 driver revision:\n"
            "0: uart:16550A port:00000000 irq:114 tx:123 rx:456\n"
            "5: uart:16550A port:00000000 irq:114 tx:7 rx:9\n"
        )
        r = periph_mod._parse_uart(out)
        self.assertTrue(r["ok"])
        self.assertEqual(r["ports"], [
            {"name": "ttyS0", "type": "16550A", "tx": 123, "rx": 456},
            {"name": "ttyS5", "type": "16550A", "tx": 7, "rx": 9},
            {"name": "ttyUSB0", "type": "usb-serial", "tx": None, "rx": None},
        ])

        ft = FakeTransport(out)
        r2 = periph_mod.uart(ft, device_id="uart-fixture")
        self.assertEqual(len(r2["ports"]), 3)

        out2 = ("/dev/ttyS9\n__SERIAL__\nserinfo:1.0 driver revision:\n")
        r3 = periph_mod._parse_uart(out2)
        self.assertEqual(r3["ports"], [
            {"name": "ttyS9", "type": None, "tx": None, "rx": None}])

    def test_15_clk_parse_fixture(self):
        import host.service.peripherals as periph_mod
        out = (
            "                                 enable  prepare  protect"
            "                                duty\n"
            " clock                          count    count    count"
            "        rate                   rate phase accuracy\n"
            "---------------------------------------------------------------------------------------------\n"
            " clk_osc0                            5        5        0"
            "  24000000          0     0  80000\n"
            "    clk_osc0_dummy                   1        1        0"
            "  24000000          0     0  80000\n"
        )
        r = periph_mod._parse_clk(out)
        self.assertTrue(r["ok"])
        clks = r["clocks"]
        self.assertEqual(len(clks), 2)
        self.assertEqual(clks[0], {
            "name": "clk_osc0", "enable": 5, "prepare": 5, "protect": 0,
            "rate": 24000000,
        })
        self.assertEqual(clks[1]["name"], "clk_osc0_dummy")

        old = (
            " clock  enable  prepare  rate\n"
            "-------------------------------\n"
            " clk_osc0  5  5  24000000\n"
        )
        r2 = periph_mod._parse_clk(old)
        self.assertEqual(r2["clocks"][0], {
            "name": "clk_osc0", "enable": 5, "prepare": 5,
            "rate": 24000000})

        modern = (
            "                                 enable  prepare  protect"
            "                                duty  hardware"
            "                            connection\n"
            "   clock                          count    count    count"
            "        rate   accuracy phase  cycle    enable"
            "   consumer                         id\n"
            "---------------------------------------------------------------------------------------------\n"
            " clk_osc0                            5        5        0"
            "  24000000    80000     0     0      1                   0\n"
        )
        r3 = periph_mod._parse_clk(modern)
        self.assertEqual(r3["clocks"][0], {
            "name": "clk_osc0", "enable": 5, "prepare": 5, "protect": 0,
            "rate": 24000000})


        real = (
            "                                 enable  prepare  protect"
            "                                duty  hardware                            connection\n"
            "   clock                          count    count    count"
            "        rate   accuracy phase  cycle    enable   consumer"
            "                         id\n"
            "---------------------------------------------------------------------------------------------\n"
            " dclk3                               0       0        0"
            "        0           0          0     50000      Y"
            "   deviceless                      no_connection_id\n"
            "    port3_dclk_src                   0       0        0"
            "        0           0          0     50000      Y"
            "      deviceless                      no_connection_id\n"
        )
        r5 = periph_mod._parse_clk(real)
        self.assertEqual(len(r5["clocks"]), 2)
        self.assertEqual(r5["clocks"][0], {
            "name": "dclk3", "enable": 0, "prepare": 0, "protect": 0,
            "rate": 0})
        self.assertEqual(r5["clocks"][1]["name"], "port3_dclk_src")

        ft = FakeTransport(out)
        r4 = periph_mod.clk(ft, device_id="clk-fixture")
        self.assertEqual(r4["clocks"][0]["name"], "clk_osc0")

    def test_16_clk_restricted_and_exec_fail(self):
        """Test helper."""
        import host.service.peripherals as periph_mod

        ft = FakeTransport(
            "cat: /sys/kernel/debug/clk/clk_summary: Permission denied", rc=1)
        r = periph_mod.clk(ft, device_id="clk-restricted-1")
        self.assertTrue(r["ok"])
        self.assertTrue(r["restricted"])
        self.assertIn("clk_summary", r["reason"])
        self.assertIn("权限不足", r["reason"])

        ft2 = FakeTransport(
            "cat: /sys/kernel/debug/clk/clk_summary: No such file or directory",
            rc=1)
        r2 = periph_mod.clk(ft2, device_id="clk-restricted-2")
        self.assertTrue(r2["ok"])
        self.assertTrue(r2["restricted"])
        self.assertIn("无法读取", r2["reason"])

        class _Bad:
            kind = "fake"

            def exec(self, cmd, timeout=30):
                raise OSError("boom")

        with self.assertRaises(periph_mod.PeriphError):
            periph_mod.clk(_Bad(), device_id="clk-exec-fail")

    def test_17_p1_endpoints_shape(self):
        """Test helper."""
        cases = (("local/pwm", "chips"), ("local/spi", "masters"),
                 ("local/uart", "ports"), ("local/clk", "clocks"))
        for path, key in cases:
            st, r = http_get(self.periph_url(path))
            self.assertEqual(st, 200, path)
            self.assertTrue(r.get("ok"), path)
            if r.get("restricted"):
                self.assertIn("reason", r, path)
                continue
            self.assertIn(key, r, path)
            self.assertIsInstance(r[key], list, path)




    def test_18_watchdog_parse_fixture(self):
        import host.service.peripherals as periph_mod
        out = (
            "watchdog0|30|active|0\n"
            "watchdog1|60||2\n"
        )
        r = periph_mod._parse_watchdog(out)
        self.assertTrue(r["ok"])
        self.assertEqual(r["devices"], [
            {"name": "watchdog0", "timeout": 30, "state": "active",
             "bootstatus": 0},
            {"name": "watchdog1", "timeout": 60, "state": None,
             "bootstatus": 2},
        ])

        self.assertEqual(periph_mod._parse_watchdog("")["devices"], [])

        ft = FakeTransport(out)
        r2 = periph_mod.watchdog(ft, device_id="wd-fixture")
        self.assertEqual(r2["devices"][0]["name"], "watchdog0")
        r3 = periph_mod.watchdog(ft, device_id="wd-fixture")
        self.assertIs(r2, r3, "第二次调用应命中缓存")
        self.assertEqual(ft.calls, 1)

        class _Bad:
            kind = "fake"
            def exec(self, cmd, timeout=30):
                raise OSError("boom")
        with self.assertRaises(periph_mod.PeriphError):
            periph_mod.watchdog(_Bad(), device_id="wd-exec-fail")

    def test_19_regulator_parse_fixture(self):
        import host.service.peripherals as periph_mod
        out = (
            "regulator.0|vdd_cpu|enabled|800000\n"
            "regulator.1|vcc3v3_sys|disabled|\n"
            "regulator.2||enabled|1800000\n"
        )
        r = periph_mod._parse_regulator(out)
        self.assertTrue(r["ok"])
        self.assertEqual(r["regulators"], [
            {"name": "vdd_cpu", "state": "enabled", "microvolts": 800000},
            {"name": "vcc3v3_sys", "state": "disabled", "microvolts": None},
            {"name": None, "state": "enabled", "microvolts": 1800000},
        ])

        self.assertEqual(periph_mod._parse_regulator("")["regulators"], [])

        ft = FakeTransport(out)
        r2 = periph_mod.regulator(ft, device_id="reg-fixture")
        self.assertEqual(r2["regulators"][0]["name"], "vdd_cpu")
        self.assertEqual(r2["regulators"][0]["microvolts"], 800000)

        class _Bad:
            kind = "fake"
            def exec(self, cmd, timeout=30):
                raise OSError("boom")
        with self.assertRaises(periph_mod.PeriphError):
            periph_mod.regulator(_Bad(), device_id="reg-exec-fail")

    def test_20_dma_fixture_and_restricted(self):
        import host.service.peripherals as periph_mod

        rk_summary = (
            "dma0 (fea10000.dma-controller): number of channels: 32\n"
            " dma0chan0    | fe470000.i2s:tx\n"
            " dma0chan1    | fe470000.i2s:rx\n"
            "dma1 (fea30000.dma-controller): number of channels: 32\n"
            " dma1chan15   | feb20000.spi:tx\n"
        )
        ft = FakeTransport("__PROC_DMA__\ncat: /proc/dma: No such file\n"
                           "__DMAENGINE__\n" + rk_summary, rc=1)
        r = periph_mod.dma(ft, device_id="dma-rk")
        self.assertTrue(r["ok"])
        self.assertNotIn("restricted", r)
        self.assertNotIn("channels", r, "proc 空时不应产生扁平 channels")
        self.assertEqual(r["controllers"], [
            {"name": "dma0", "addr": "fea10000.dma-controller", "nchannels": 32,
             "channels": [
                {"chan": "dma0chan0", "client": "fe470000.i2s:tx"},
                {"chan": "dma0chan1", "client": "fe470000.i2s:rx"},
             ]},
            {"name": "dma1", "addr": "fea30000.dma-controller", "nchannels": 32,
             "channels": [
                {"chan": "dma1chan15", "client": "feb20000.spi:tx"},
             ]},
        ])

        ft_x86 = FakeTransport(
            "__PROC_DMA__\n0: XT DMA controller\n2: floppy\n"
            "__DMAENGINE__\ncat: /sys/kernel/debug/dmaengine/summary: "
            "Permission denied\n", rc=1)
        r2 = periph_mod.dma(ft_x86, device_id="dma-x86")
        self.assertTrue(r2["ok"])
        self.assertNotIn("restricted", r2)
        self.assertEqual(r2["channels"], [
            {"chan": 0, "name": "XT DMA controller"},
            {"chan": 2, "name": "floppy"},
        ])

        ft_perm = FakeTransport(
            "__PROC_DMA__\n__DMAENGINE__\n"
            "cat: /sys/kernel/debug/dmaengine/summary: Permission denied\n",
            rc=1)
        r3 = periph_mod.dma(ft_perm, device_id="dma-restricted-1")
        self.assertTrue(r3["ok"])
        self.assertTrue(r3["restricted"])
        self.assertIn("dmaengine", r3["reason"])
        self.assertIn("权限不足", r3["reason"])
        ft_empty = FakeTransport("__PROC_DMA__\n__DMAENGINE__\n", rc=0)
        r4 = periph_mod.dma(ft_empty, device_id="dma-empty")

        self.assertTrue(r4["ok"])
        self.assertNotIn("restricted", r4)
        self.assertEqual(r4.get("channels", []), [])

        self.assertEqual(periph_mod._parse_proc_dma(
            "Channel: 0\n0: XT DMA controller\n"), [
            {"chan": 0, "name": "XT DMA controller"}])
        self.assertEqual(periph_mod._parse_proc_dma(
            "--------------------------------\nChannel:\n\n"), [])
        self.assertEqual(periph_mod._parse_dmaengine_summary(
            "dma2 (x): number of channels: 32\n"
            "dma3 (y): number of channels: 16\n"
            " dma3chan0    | \n"
            "garbage line\n"), [
            {"name": "dma2", "addr": "x", "nchannels": 32, "channels": []},
            {"name": "dma3", "addr": "y", "nchannels": 16,
             "channels": [{"chan": "dma3chan0", "client": None}]},
        ])

        class _Bad:
            kind = "fake"

            def exec(self, cmd, timeout=30):
                raise OSError("boom")

        with self.assertRaises(periph_mod.PeriphError):
            periph_mod.dma(_Bad(), device_id="dma-exec-fail")

    def test_21_p2_endpoints_shape(self):
        """Test helper."""
        cases = (("local/watchdog", "devices"), ("local/regulator", "regulators"),
                 ("local/dma", ("channels", "controllers")))
        for path, key in cases:
            st, r = http_get(self.periph_url(path))
            self.assertEqual(st, 200, path)
            self.assertTrue(r.get("ok"), path)
            if r.get("restricted"):
                self.assertIn("reason", r, path)
                continue

            keys = key if isinstance(key, tuple) else (key,)
            self.assertTrue(any(k in r for k in keys), "%s 缺少 %s" % (path, keys))
            for k in keys:
                if k in r:
                    self.assertIsInstance(r[k], list, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
