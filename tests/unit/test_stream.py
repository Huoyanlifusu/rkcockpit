#!/usr/bin/env python3
"""Test module."""
import io
import json
import os
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.api.handlers import diag as diag_handler
from host.service import diag as diag_svc

FMT_OUT = (
    "ioctl: VIDIOC_ENUM_FMT\n"
    "\tType: Video Capture\n"
    "\t[0]: 'NV12' (Y/CbCr 4:2:0)\n"
    "Pixel Format: 'NV12'\n"
    "\tSize: Discrete 1920x1080\n"
    "\tSize: Discrete 1280x720\n"
    "Pixel Format: 'YUYV'\n"
    "\tSize: Discrete 640x480\n"
)


class StubExec:
    """Test class."""

    def __init__(self, mapping, default=(1, "", ""), gate=None, gate_key=None):
        self.mapping = mapping
        self.default = default
        self.gate = gate
        self.gate_key = gate_key
        self.calls = []

    def __call__(self, cmd, timeout=30):
        self.calls.append((cmd, timeout))
        if self.gate is not None and self.gate_key and \
                self.gate_key in cmd:
            self.gate.wait(10)
        for key, res in self.mapping.items():
            if key in cmd:
                return res
        return self.default


class StubTransport:
    kind = "stub"

    def __init__(self, exec_fn=None):
        self._exec = exec_fn or StubExec({})
        self.calls = []

    def exec(self, cmd, timeout=30):
        self.calls.append((cmd, timeout))
        return self._exec(cmd, timeout=timeout)


class FakeHandler:
    """Test class."""

    def __init__(self, body=None, raw=None):
        self.client_address = ("127.0.0.1", 43210)
        self._code = None
        self._headers = {}
        data = (raw or json.dumps(body if body is not None else {})).encode("utf-8")
        self.rfile = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self._code = code

    def send_header(self, key, value):
        self._headers[key] = value

    def end_headers(self):
        pass


class FakeHost:
    def __init__(self, transport=None):
        self.audit = None
        self._t = transport

    def _transport(self, did):
        if did == "ghost":
            raise KeyError(did)
        if self._t is None:
            self._t = StubTransport()
        return self._t


_STREAM_RX = re.compile(r"^/api/diag/([^/]+)/stream-test$")


def call_stream_handler(host, did, body=None, raw=None):
    fh = FakeHandler(body=body, raw=raw)
    mm = _STREAM_RX.match("/api/diag/%s/stream-test" % did)
    diag_handler.stream_test(fh, host, mm, {})
    payload = json.loads(fh.wfile.getvalue().decode("utf-8"))
    return fh._code, payload


def ok_v4l2_exec():
    """Test helper."""
    return StubExec({
        "systemctl is-active": (1, "", ""),
        "command -v": (0, "HAS_V4L2_CTL\n", ""),
        "list-formats-ext": (0, FMT_OUT, ""),
        "stream-count=30": (0, "Captured 30 frames\n", ""),
        "stat -c": (0, "1843200\n", ""),
    })


class StreamTestServiceUnitTest(unittest.TestCase):
    def test_01_param_validation_400(self):
        t = StubTransport()
        for bad in (None, "", "/dev/video", "/dev/videoX",
                    "/dev/video0/extra", "video0", "/tmp/video0",
                    "/dev/video 0"):
            with self.assertRaises(ValueError):
                diag_svc.stream_test(t, device_id="local", video=bad)

        with self.assertRaises(ValueError):
            diag_svc.stream_test(t, device_id="local", video="/dev/video0",
                                 width=640)
        with self.assertRaises(ValueError):
            diag_svc.stream_test(t, device_id="local", video="/dev/video0",
                                 width=0, height=480)
        with self.assertRaises(ValueError):
            diag_svc.stream_test(t, device_id="local", video="/dev/video0",
                                 width="abc", height=480)
        with self.assertRaises(ValueError):
            diag_svc.stream_test(t, device_id="local", video="/dev/video0",
                                 pixelformat="NV12; rm -rf /")

        self.assertEqual(t.calls, [])

    def test_02_nov4l2_no_tools(self):
        ex = StubExec({"systemctl is-active": (1, "", "")},
                      default=(0, "", ""))
        with self.assertRaises(diag_svc.DiagError) as ctx:
            diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        self.assertEqual(ctx.exception.status, "NOV4L2")
        self.assertIn("v4l2-ctl", str(ctx.exception))
        self.assertIn("rk_sensor_sync", str(ctx.exception))

    def test_03_occupied_rejected_and_hook(self):

        ex = StubExec({"systemctl is-active": (0, "active\n", "")})
        with self.assertRaises(diag_svc.DiagError) as ctx:
            diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        self.assertIn("占用", str(ctx.exception))

        ex2 = StubExec({"systemctl is-active": (1, "", "Unit not found")},
                       default=(0, "", ""))
        with self.assertRaises(diag_svc.DiagError) as ctx2:
            diag_svc.stream_test(StubTransport(ex2), device_id="local",
                                 video="/dev/video0", _exec=ex2)
        self.assertEqual(ctx2.exception.status, "NOV4L2")

        with self.assertRaises(diag_svc.DiagError):
            diag_svc.stream_test(StubTransport(), device_id="local",
                                 video="/dev/video0", _exec=ok_v4l2_exec(),
                                 _occupied=lambda fn: True)
        r = diag_svc.stream_test(StubTransport(), device_id="local",
                                 video="/dev/video0", _exec=ok_v4l2_exec(),
                                 _occupied=lambda fn: False)
        self.assertEqual(r["status"], "STREAMOK")

    def test_04_streamok_resolved_format(self):
        ex = ok_v4l2_exec()
        r = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        self.assertEqual(r["status"], "STREAMOK")
        self.assertTrue(r["ok"])
        self.assertEqual(r["tool"], "v4l2-ctl")
        self.assertEqual(r["video"], "/dev/video0")

        self.assertEqual((r["width"], r["height"], r["pixelformat"]),
                         (1920, 1080, "NV12"))
        self.assertEqual(r["frames"], 30)
        self.assertEqual(r["file_size"], 1843200)
        self.assertRegex(r["file"], r"^/tmp/rkss_stream_test_video0_\d+\.raw$")
        self.assertGreaterEqual(r["duration_ms"], 0)
        cmds = [c for c, _ in ex.calls]
        self.assertTrue(any("systemctl is-active" in c for c in cmds))
        self.assertTrue(any("command -v" in c for c in cmds))
        self.assertTrue(any("list-formats-ext" in c for c in cmds))
        stream_cmd = next(c for c in cmds if "stream-count=30" in c)
        self.assertIn("--stream-count=30 --stream-to=/tmp/rkss_stream_test_",
                      stream_cmd)
        self.assertTrue(any(c.startswith("stat -c") for c in cmds))

    def test_05_explicit_format_skips_resolution(self):
        ex = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_V4L2_CTL\n", ""),
            "list-formats-ext": (0, "", ""),
            "stream-count=30": (0, "Captured 30 frames\n", ""),
            "stat -c": (0, "12345\n", ""),
        })
        r = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", width=640, height=480,
                                 pixelformat="YUYV", _exec=ex)
        self.assertEqual(r["status"], "STREAMOK")
        self.assertEqual((r["width"], r["height"], r["pixelformat"]),
                         (640, 480, "YUYV"))
        stream_cmd = next(c for c, _ in ex.calls if "stream-count=30" in c)
        self.assertIn("width=640,height=480,pixelformat=YUYV", stream_cmd)

    def test_06_stream_fail(self):
        ex = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_V4L2_CTL\n", ""),
            "list-formats-ext": (0, FMT_OUT, ""),
            "stream-count=30": (1, "ERROR: Failed to start streaming\n", ""),
            "stat -c": (1, "", "No such file"),
        })
        r = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        self.assertEqual(r["status"], "STREAM_FAIL")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["frames"])
        self.assertIsNone(r["file_size"])
        self.assertIn("Failed to start streaming", r["error"])

    def test_07_frames_mismatch(self):
        ex = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_V4L2_CTL\n", ""),
            "list-formats-ext": (0, FMT_OUT, ""),
            "stream-count=30": (0, "Captured 10 frames\n", ""),
            "stat -c": (0, "600000\n", ""),
        })
        r = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        self.assertEqual(r["status"], "STREAM_FAIL")
        self.assertIn("10", r["error"])

    def test_08_timeout(self):
        ex = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_V4L2_CTL\n", ""),
            "list-formats-ext": (0, FMT_OUT, ""),
            "stream-count=30": (124, "", "timeout after 20s"),
            "stat -c": (1, "", "No such file"),
        })
        r = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        self.assertEqual(r["status"], "TIMEOUT")
        self.assertIn("超时", r["error"])

        stream_call = next(c for c in ex.calls if "stream-count=30" in c[0])
        self.assertEqual(stream_call[1], 20)

    def test_08b_killed_timeout_137(self):



        ex = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_V4L2_CTL\n", ""),
            "list-formats-ext": (0, FMT_OUT, ""),
            "stream-count=30": (137, "", ""),
            "stat -c": (1, "", "No such file"),
        })
        r = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        self.assertEqual(r["status"], "TIMEOUT")
        self.assertIn("超时", r["error"])

        ex2 = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_RK_SENSOR_SYNC\n", ""),
            "rk_sensor_sync -d": (137, "", ""),
        })
        r2 = diag_svc.stream_test(StubTransport(ex2), device_id="local",
                                  video="/dev/video0", _exec=ex2)
        self.assertEqual(r2["status"], "TIMEOUT")

    def test_09_rk_sensor_sync_fallback(self):
        ex = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_RK_SENSOR_SYNC\n", ""),
            "rk_sensor_sync -d": (0, "", ""),
        })
        r = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        self.assertEqual(r["status"], "STREAMOK")
        self.assertEqual(r["tool"], "rk_sensor_sync")
        self.assertIsNone(r["frames"])
        self.assertIsNone(r["file_size"])
        self.assertIsNone(r["file"])

        ex2 = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_RK_SENSOR_SYNC\n", ""),
            "rk_sensor_sync -d": (1, "", "rk_sensor_sync: ioctl failed"),
        })
        r2 = diag_svc.stream_test(StubTransport(ex2), device_id="local",
                                  video="/dev/video0", _exec=ex2)
        self.assertEqual(r2["status"], "STREAM_FAIL")
        self.assertIn("ioctl failed", r2["error"])

        ex3 = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_RK_SENSOR_SYNC\n", ""),
            "rk_sensor_sync -d": (124, "", "timeout"),
        })
        r3 = diag_svc.stream_test(StubTransport(ex3), device_id="local",
                                  video="/dev/video0", _exec=ex3)
        self.assertEqual(r3["status"], "TIMEOUT")

    def test_10_device_node_missing(self):
        ex = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_V4L2_CTL\n", ""),
            "list-formats-ext": (0, "DEV_NOT_FOUND\n", ""),
        })
        with self.assertRaises(diag_svc.DiagError) as ctx:
            diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video9", _exec=ex)
        self.assertIn("不存在", str(ctx.exception))

    def test_11_concurrency_429_and_slot_release(self):
        gate = threading.Event()
        ex = StubExec({
            "systemctl is-active": (1, "", ""),
            "command -v": (0, "HAS_V4L2_CTL\n", ""),
            "list-formats-ext": (0, FMT_OUT, ""),
            "stream-count=30": (0, "Captured 30 frames\n", ""),
            "stat -c": (0, "123\n", ""),
        }, gate=gate, gate_key="stream-count=30")
        results = []

        def worker():
            results.append(diag_svc.stream_test(
                StubTransport(ex), device_id="local",
                video="/dev/video0", _exec=ex))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()

        deadline = time.time() + 5
        while time.time() < deadline and \
                sum(1 for c in ex.calls if "stream-count=30" in c[0]) < 2:
            time.sleep(0.01)
        self.assertGreaterEqual(
            sum(1 for c in ex.calls if "stream-count=30" in c[0]), 2,
            "两路应已进入 stream 段")

        with self.assertRaises(diag_svc.StreamBusy):
            diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        gate.set()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual([r["status"] for r in results],
                         ["STREAMOK", "STREAMOK"])

        r = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                 video="/dev/video0", _exec=ex)
        self.assertEqual(r["status"], "STREAMOK")

    def test_12_cache_exemption(self):
        before = dict(diag_svc._CACHE)
        ex = ok_v4l2_exec()
        r1 = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                  video="/dev/video0", _exec=ex)
        r2 = diag_svc.stream_test(StubTransport(ex), device_id="local",
                                  video="/dev/video0", _exec=ex)
        self.assertEqual(r1["status"], "STREAMOK")
        self.assertEqual(r2["status"], "STREAMOK")
        self.assertEqual(dict(diag_svc._CACHE), before,
                         "出流测试不应写入 _CACHE")
        self.assertFalse(any("stream" in str(k) for k in diag_svc._CACHE))


class StreamTestHandlerUnitTest(unittest.TestCase):
    def test_13_result_statuses_200(self):
        host = FakeHost()
        for status in ("STREAMOK", "STREAM_FAIL", "TIMEOUT"):
            with mock.patch(
                    "host.service.diag.stream_test",
                    return_value={"ok": True, "status": status,
                                  "error": None}):
                code, r = call_stream_handler(host, "local",
                                              {"video": "/dev/video0"})
            self.assertEqual(code, 200)
            self.assertTrue(r["ok"])
            self.assertEqual(r["status"], status)

    def test_14_business_fail_nov4l2(self):
        ex = StubExec({"systemctl is-active": (1, "", "")},
                      default=(0, "", ""))
        host = FakeHost(StubTransport(ex))
        code, r = call_stream_handler(host, "local", {"video": "/dev/video0"})
        self.assertEqual(code, 200)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "NOV4L2")
        self.assertIn("v4l2-ctl", r["error"])

    def test_15_diag_error_no_status(self):
        host = FakeHost()
        with mock.patch("host.service.diag.stream_test",
                        side_effect=diag_svc.DiagError("rkss-capture 占用")):
            code, r = call_stream_handler(host, "local",
                                          {"video": "/dev/video0"})
        self.assertEqual(code, 200)
        self.assertFalse(r["ok"])
        self.assertNotIn("status", r)
        self.assertIn("占用", r["error"])

    def test_16_value_error_400(self):
        host = FakeHost()
        with mock.patch("host.service.diag.stream_test",
                        side_effect=ValueError("video 参数必须形如 /dev/videoN")):
            code, r = call_stream_handler(host, "local", {"video": "nope"})
        self.assertEqual(code, 400)
        self.assertFalse(r["ok"])
        self.assertIn("video", r["error"])

    def test_17_busy_429(self):
        host = FakeHost()
        with mock.patch("host.service.diag.stream_test",
                        side_effect=diag_svc.StreamBusy("出流测试并发已满")):
            code, r = call_stream_handler(host, "local",
                                          {"video": "/dev/video0"})
        self.assertEqual(code, 429)
        self.assertIn("并发", r["error"])

    def test_18_device_404(self):
        host = FakeHost()
        with mock.patch("host.service.diag.stream_test") as m:
            code, r = call_stream_handler(host, "ghost",
                                          {"video": "/dev/video0"})
        m.assert_not_called()
        self.assertEqual(code, 404)
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])

    def test_19_bad_body_400(self):
        host = FakeHost()

        code, r = call_stream_handler(host, "local", raw="not-json")
        self.assertEqual(code, 400)
        self.assertFalse(r["ok"])

        code2, r2 = call_stream_handler(host, "local", body=[1, 2])
        self.assertEqual(code2, 400)
        self.assertIn("JSON 对象", r2["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
