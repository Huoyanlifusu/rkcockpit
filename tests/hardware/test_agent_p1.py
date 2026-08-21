#!/usr/bin/env python3

import io
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.agent import tools as agent_tools                      # noqa: E402
from host.api.handlers import agent as agent_handler             # noqa: E402
from host.api.handlers import agent_stream                       # noqa: E402
from host.api.router import dispatch as router_dispatch          # noqa: E402
from host.service.llm import (LLMAuth, LLMClient,                # noqa: E402
                              LLMNotConfigured, LLMRateLimit,
                              LLMTimeout, LLMTokenLimit,
                              LLMUnreachable)
from host.service import llm                                      # noqa: E402

_TMP_DIRS = []


def _tmpdir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP_DIRS.append(d)
    return d




def _sse(payload, event=None):
    if event:
        return "event: %s\ndata: %s\n\n" % (
            event, json.dumps(payload, ensure_ascii=False))
    return "data: %s\n\n" % json.dumps(payload, ensure_ascii=False)


def _token_chunk(text):
    return _sse({"choices": [{"delta": {"content": text},
                              "finish_reason": None}]})


def _finish_chunk(reason="stop"):
    return _sse({"choices": [{"delta": {}, "finish_reason": reason}]})


def stream_reply_frames(text, usage=None):
    """Test helper."""
    frames = [_token_chunk(text[i:i + 3]) for i in range(0, len(text), 3)]
    frames.append(_finish_chunk("stop"))
    if usage is not None:
        frames.insert(0, _sse({"choices": [], "usage": usage}))
    frames.append("data: [DONE]\n\n")
    return frames


def stream_tool_frames(name, args, call_id="call_1"):
    """Test helper."""
    return [
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": args}}]},
            "finish_reason": "tool_calls"}]}),
        "data: [DONE]\n\n",
    ]


def parse_sse(raw):
    """Test helper."""
    out = []
    for frame in raw.decode("utf-8", errors="replace").split("\n\n"):
        frame = frame.strip("\n")
        if not frame.strip():
            continue
        event = "message"
        data = ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if not data:
            continue
        try:
            out.append((event, json.loads(data)))
        except ValueError:
            pass
    return out




class FakeStreamLLMHandler(BaseHTTPRequestHandler):
    """Test class."""
    script = []
    requests = []
    gate = None
    log_message = staticmethod(lambda *a, **k: None)

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, frames):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        for f in frames:
            self.wfile.write(f.encode("utf-8"))
            self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length).decode("utf-8")) \
            if length else {}
        FakeStreamLLMHandler.requests.append(req)
        if FakeStreamLLMHandler.gate is not None:
            if not FakeStreamLLMHandler.gate.wait(timeout=8):
                return self._send_json(500, {"error": "gate timeout"})
        if FakeStreamLLMHandler.script:
            item = FakeStreamLLMHandler.script.pop(0)
            if callable(item):
                return item(self, req)
            kind, payload = item
            if kind == "stream":
                return self._send_stream(payload)
            return self._send_json(kind, payload)
        msgs = req.get("messages") or []
        if any(m.get("role") == "tool" for m in msgs):
            return self._send_stream(stream_reply_frames("工具已执行完毕，最终回答"))
        return self._send_stream(stream_tool_frames("device_list", "{}"))




class StubTransport:
    kind = "stub"

    def exec(self, cmd, timeout=30):
        if "dmesg" in cmd or "kmsg" in cmd:
            return 0, "usb 1-1: new high-speed device\nrk-video: probe ok\n", ""
        return 0, "", ""


class FakeHost:
    """Test class."""

    def __init__(self):
        self.conf_dir = _tmpdir("rkss-agent-p1-")
        self.store = types.SimpleNamespace(conf_dir=self.conf_dir)
        self.audit = None
        self.devices = [
            {"id": "demo", "name": "demo（模拟板卡）", "type": "local",
             "host": "h", "port": 22, "user": "root", "auth": "key",
             "has_password": False, "remark": "演示", "info": {"x": 1},
             "state": "online", "ping_ms": 3},
            {"id": "unk", "name": "未知板", "type": "ssh",
             "host": "h2", "port": 22, "user": "u", "auth": "pw",
             "has_password": True, "remark": "", "info": None},
        ]

    def devices_list(self, query=None):
        return {"ok": True, "devices": [dict(d) for d in self.devices]}

    def device_check(self, did):
        for d in self.devices:
            if d["id"] == did:
                return {"ok": True, "state": "online", "ping_ms": 1,
                        "info": {"hostname": "rk-demo"}}
        return {"ok": False, "error": "设备不存在: %s" % did}, 404

    def device_sysinfo(self, did):
        return {"ok": True, "data": {
            "cpu": {"cores": 4, "usage": 12.5},
            "mem": {"total_mb": 1024, "used_mb": 300},
            "temp": None, "uptime_s": None}}

    def fs_list(self, did, query):
        return {"ok": True, "path": "/", "entries": []}

    def _transport(self, did):
        if did == "ghost":
            raise KeyError(did)
        return StubTransport()


class FakeHandler:
    """Test class."""

    def __init__(self, body=None, raw=None):
        self.client_address = ("127.0.0.1", 43210)
        self._code = None
        self._headers = {}
        data = raw if isinstance(raw, (bytes, bytearray)) else (
            json.dumps(body if body is not None else {}).encode("utf-8"))
        self.rfile = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self._code = code

    def send_header(self, key, value):
        self._headers[key] = value

    def end_headers(self):
        pass


def create_session(host, device_id="demo"):
    fh = FakeHandler(body={"device_id": device_id})
    handled = router_dispatch(fh, host, "POST", "/api/agent/sessions", {})
    assert handled, "session route not handled"
    r = json.loads(fh.wfile.getvalue().decode("utf-8"))
    assert fh._code == 201 and r["ok"], "create session failed: %s" % r
    return r["session"]["id"]


def stream_call(host, sid, message, **extra):
    """Test helper."""
    body = {"message": message}
    body.update(extra)
    fh = FakeHandler(body=body)
    handled = router_dispatch(fh, host, "POST",
                              "/api/agent/sessions/%s/stream" % sid, {})
    assert handled, "stream route not handled"
    return fh._code, dict(fh._headers), parse_sse(fh.wfile.getvalue())


def events(frames, name):
    return [p for e, p in frames if e == name]


class AgentStreamTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeStreamLLMHandler)
        cls.llm_base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.th.join(timeout=5)
        for d in _TMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)

    def setUp(self):
        FakeStreamLLMHandler.script = []
        FakeStreamLLMHandler.requests = []
        FakeStreamLLMHandler.gate = None
        agent_handler._SESSIONS.clear()


        llm.LLM_CONCURRENCY_SEM = threading.Semaphore(
            llm.MAX_LLM_CONCURRENCY)

    def make_host(self):
        return FakeHost()

    def conf_host(self, host, **llm_conf):
        conf = {"base_url": self.llm_base, "model": "fake-model",
                "api_key": "sk-test-secret"}
        conf.update(llm_conf)
        agent_handler._save_llm_conf(host, conf)
        return host



    def test_01_stream_event_sequence(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)
        FakeStreamLLMHandler.script = [
            ("stream", stream_reply_frames("你好，我是助手"))]
        code, headers, frames = stream_call(host, sid, "你好")
        self.assertEqual(code, 200)
        self.assertTrue(headers["Content-Type"].startswith(
            "text/event-stream"))

        ev_names = [e for e, _ in frames]
        self.assertEqual(ev_names[0], "session")
        self.assertEqual(ev_names[-1], "done")
        self.assertIn("token", ev_names)

        sess = events(frames, "session")[0]
        self.assertEqual(sess["session_id"], sid)
        self.assertEqual(sess["device_id"], "demo")
        self.assertEqual(sess["message_count"], 1)

        reply = "".join(p["text"] for e, p in frames if e == "token")
        self.assertEqual(reply, "你好，我是助手")

        done = events(frames, "done")[0]
        self.assertEqual(done["reply"], "你好，我是助手")
        self.assertEqual(done["session_id"], sid)
        self.assertEqual(done["message_count"], 2)     # user + assistant


        fh = FakeHandler()
        router_dispatch(fh, host, "GET",
                        "/api/agent/sessions/%s/messages" % sid, {})
        msgs = json.loads(fh.wfile.getvalue().decode("utf-8"))["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1]["content"], "你好，我是助手")



    def test_02_tool_multistep_loop(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)
        FakeStreamLLMHandler.script = [
            ("stream", stream_tool_frames("device_list", "{}", "call_1")),
            ("stream", stream_tool_frames(
                "diag", '{"device_id": "demo", "kind": "dmesg", "lines": 50}',
                "call_2")),
            ("stream", stream_tool_frames(
                "device_sysinfo", '{"device_id": "demo"}', "call_3")),
            ("stream", stream_reply_frames(
                "demo 的 CPU 为 4 核，使用率 12.5%；dmesg 显示 usb 枚举正常。")),
        ]
        code, _, frames = stream_call(host, sid, "诊断一下 demo")
        self.assertEqual(code, 200)


        tcs = events(frames, "tool_call")
        self.assertEqual([t["name"] for t in tcs],
                         ["device_list", "diag", "device_sysinfo"])
        self.assertEqual(tcs[1]["arguments"],
                         '{"device_id": "demo", "kind": "dmesg", "lines": 50}')


        trs = events(frames, "tool_result")
        self.assertEqual([t["name"] for t in trs],
                         ["device_list", "diag", "device_sysinfo"])
        for i, t in enumerate(trs):
            self.assertEqual(t["id"], tcs[i]["id"])
            self.assertTrue(t["result"]["ok"], t["result"])


        self.assertTrue(any("demo" in json.dumps(
            t["result"], ensure_ascii=False) for t in trs))
        sysinfo_res = trs[2]["result"]
        self.assertEqual(sysinfo_res["data"]["cpu"]["cores"], 4)
        self.assertEqual(sysinfo_res["data"]["cpu"]["usage"], 12.5)


        done = events(frames, "done")[0]
        self.assertIn("4 核", done["reply"])
        self.assertIn("12.5", done["reply"])
        self.assertIn("usb", done["reply"])


        fh = FakeHandler()
        router_dispatch(fh, host, "GET",
                        "/api/agent/sessions/%s/messages" % sid, {})
        msgs = json.loads(fh.wfile.getvalue().decode("utf-8"))["messages"]
        self.assertEqual([m["role"] for m in msgs],
                         ["user", "assistant", "tool", "assistant", "tool",
                          "assistant", "tool", "assistant"])

        self.assertEqual(msgs[1]["tool_calls"][0]["type"], "function")
        self.assertEqual(msgs[1]["tool_calls"][0]["function"]["name"],
                         "device_list")
        self.assertEqual(msgs[2]["tool_call_id"], "call_1")

        req1 = FakeStreamLLMHandler.requests[1]["messages"]
        self.assertIn("demo", req1[-1]["content"])
        self.assertEqual(req1[-1]["role"], "tool")
        req2 = FakeStreamLLMHandler.requests[2]["messages"]
        self.assertIn("usb", req2[-1]["content"])

        req3 = FakeStreamLLMHandler.requests[3]["messages"]
        self.assertIn("cores", req3[-1]["content"])
        self.assertEqual(len(FakeStreamLLMHandler.requests), 4)



    def test_03_error_codes(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)
        cases = [
            (401, {"error": "unauthorized"}, "auth"),
            (429, {"error": "rate limited"}, "rate_limit"),
            (500, {"error": "boom"}, "unreachable"),
            (400, {"error": {"message":
                              "maximum context length is 4096 tokens"}},
             "token_limit"),
        ]
        for st, payload, code in cases:
            FakeStreamLLMHandler.script = [(st, payload)]
            _, _, frames = stream_call(host, sid, "触发错误")
            errs = events(frames, "error")
            self.assertEqual(len(errs), 1, "status=%s" % st)
            self.assertEqual(errs[0]["code"], code, "status=%s" % st)
            self.assertTrue(errs[0]["error"])
            self.assertEqual(events(frames, "done"), [])


        host2 = self.make_host()
        sid2 = create_session(host2)
        _, _, frames2 = stream_call(host2, sid2, "你好")
        errs2 = events(frames2, "error")
        self.assertEqual(errs2[0]["code"], "not_configured")



    def test_04_session_404_and_validation(self):
        host = self.conf_host(self.make_host())
        code, headers, raw = self._raw_stream(host, "nope", "hi")
        self.assertEqual(code, 404)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertIn("会话不存在", raw)

        sid = create_session(host)
        code2, _, raw2 = self._raw_stream(host, sid, "   ")
        self.assertEqual(code2, 400)
        self.assertIn("message 必填", raw2)

        fh = FakeHandler(raw=b"[1,2]")
        handled = router_dispatch(fh, host, "POST",
                                  "/api/agent/sessions/%s/stream" % sid, {})
        self.assertTrue(handled)
        self.assertEqual(fh._code, 400)


        code3, _, raw3 = self._raw_stream(host, sid, 123)
        self.assertEqual(code3, 400)
        self.assertIn("message 必须是字符串", raw3)
        code4, _, raw4 = self._raw_stream(host, sid, ["a"])
        self.assertEqual(code4, 400)

    def _raw_stream(self, host, sid, message):
        fh = FakeHandler(body={"message": message})
        handled = router_dispatch(fh, host, "POST",
                                  "/api/agent/sessions/%s/stream" % sid, {})
        assert handled, "stream route not handled"
        return fh._code, dict(fh._headers), \
            fh.wfile.getvalue().decode("utf-8", errors="replace")



    def test_05_same_session_serialized(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)

        def slow_reply(handler, req):
            time.sleep(0.4)
            handler._send_stream(stream_reply_frames("并发完成"))

        FakeStreamLLMHandler.script = [slow_reply, slow_reply]
        results = {}

        def worker(tag):
            code, _, frames = stream_call(host, sid, "并发请求")
            results[tag] = (code, frames)

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        self.assertFalse(t1.is_alive(), "stream 线程 1 死锁")
        self.assertFalse(t2.is_alive(), "stream 线程 2 死锁")
        for tag in ("a", "b"):
            code, frames = results[tag]
            self.assertEqual(code, 200)
            self.assertEqual(events(frames, "done")[0]["reply"], "并发完成")

        self.assertEqual(len(FakeStreamLLMHandler.requests), 2)

        req1 = FakeStreamLLMHandler.requests[1]["messages"]
        self.assertIn("并发完成",
                      [m.get("content") for m in req1
                       if m["role"] == "assistant"])

        fh = FakeHandler()
        router_dispatch(fh, host, "GET",
                        "/api/agent/sessions/%s/messages" % sid, {})
        msgs = json.loads(fh.wfile.getvalue().decode("utf-8"))["messages"]
        self.assertEqual([m["role"] for m in msgs],
                         ["user", "assistant", "user", "assistant"])



    def test_06_busy_semaphore(self):
        host = self.conf_host(self.make_host())
        sa, sb, sc = (create_session(host), create_session(host),
                      create_session(host))
        gate = threading.Event()
        FakeStreamLLMHandler.gate = gate
        results = {}

        def worker(sid, tag):
            code, _, frames = stream_call(host, sid, "hi")
            results[tag] = (code, frames)

        t1 = threading.Thread(target=worker, args=(sa, "a"))
        t2 = threading.Thread(target=worker, args=(sb, "b"))
        t1.start()
        t2.start()
        try:

            deadline = time.time() + 8
            while len(FakeStreamLLMHandler.requests) < 2 \
                    and time.time() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(len(FakeStreamLLMHandler.requests), 2)

            code3, _, frames3 = stream_call(host, sc, "hi")
            self.assertEqual(code3, 200)
            errs = events(frames3, "error")
            self.assertEqual(len(errs), 1)
            self.assertEqual(errs[0]["code"], "agent_busy")
            self.assertEqual(events(frames3, "done"), [])
        finally:
            gate.set()
        t1.join(timeout=15)
        t2.join(timeout=15)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        for tag in ("a", "b"):
            code, frames = results[tag]
            self.assertEqual(code, 200)
            self.assertEqual(events(frames, "done")[0]["reply"],
                             "工具已执行完毕，最终回答")



    def test_07_complete_stream_unit(self):

        c = LLMClient()
        with self.assertRaises(LLMNotConfigured):
            list(c.complete_stream([{"role": "user", "content": "hi"}]))


        seen = {}

        def open_stream2(method, url, headers, body, timeout):
            req = json.loads(body)
            seen["stream"] = req.get("stream")
            seen["auth"] = headers.get("Authorization")
            frames = stream_reply_frames("你好", usage={"total_tokens": 9})
            return 200, io.BytesIO("".join(frames).encode("utf-8"))

        c = LLMClient(base_url="http://x/v1", model="m", api_key="k-1",
                      _stream_open=open_stream2)
        evs = list(c.complete_stream([{"role": "user", "content": "hi"}]))
        self.assertTrue(seen["stream"])
        self.assertEqual(seen["auth"], "Bearer k-1")
        tokens = "".join(e["text"] for e in evs if e["type"] == "token")
        self.assertEqual(tokens, "你好")
        done = [e for e in evs if e["type"] == "done"][0]
        self.assertEqual(done["usage"]["total_tokens"], 9)
        self.assertEqual(evs[-1]["type"], "done")


        def open_tool(method, url, headers, body, timeout):
            frames = [
                _sse({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_9",
                     "function": {"name": "device_list"}}]},
                    "finish_reason": None}]}),
                _sse({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '{"device_'}}]},
                    "finish_reason": None}]}),
                _sse({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": 'id": "demo"}'}}]},
                    "finish_reason": "tool_calls"}]}),
                "data: [DONE]\n\n",
            ]
            return 200, io.BytesIO("".join(frames).encode("utf-8"))

        c2 = LLMClient(base_url="http://x/v1", model="m",
                       _stream_open=open_tool)
        evs2 = list(c2.complete_stream([{"role": "user", "content": "hi"}]))
        tcs = [e for e in evs2 if e["type"] == "tool_calls"]
        self.assertEqual(len(tcs), 1)
        tc = tcs[0]["tool_calls"][0]
        self.assertEqual(tc["id"], "call_9")
        self.assertEqual(tc["name"], "device_list")
        self.assertEqual(tc["arguments"], '{"device_id": "demo"}')

        def open_no_done(method, url, headers, body, timeout):
            return 200, io.BytesIO(
                _sse({"choices": [{"delta": {"content": "x"},
                                   "finish_reason": None}]}).encode())
        c3 = LLMClient(base_url="http://x/v1", model="m",
                       _stream_open=open_no_done)
        evs3 = list(c3.complete_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual([e["type"] for e in evs3], ["token", "done"])


        for st, body, exc in [
            (401, b"{}", LLMAuth), (403, b"{}", LLMAuth),
            (429, b"{}", LLMRateLimit),
            (400, b'{"error":{"message":"context length exceeded"}}',
             LLMTokenLimit),
            (400, b'{"error":"bad"}', LLMUnreachable),
            (500, b"oops", LLMUnreachable),
        ]:
            c4 = LLMClient(base_url="http://x/v1", model="m",
                           _stream_open=lambda *a, st=st, b=body:
                           (st, io.BytesIO(b)))
            with self.assertRaises(exc, msg="status=%s" % st):
                list(c4.complete_stream([{"role": "user", "content": "hi"}]))

        def send_timeout(*a, **k):
            raise socket.timeout("timed out")
        c5 = LLMClient(base_url="http://x/v1", model="m",
                       _stream_open=send_timeout)
        with self.assertRaises(LLMTimeout):
            list(c5.complete_stream([{"role": "user", "content": "hi"}]))



    def test_08_big_tool_result_truncated(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)
        agent_tools.TOOLS["big_read"] = {
            "name": "big_read", "description": "t", "tier": "read",
            "parameters": {}, "fn": lambda h, a: {"data": "x" * 9000}}
        try:
            FakeStreamLLMHandler.script = [
                ("stream", stream_tool_frames("big_read", "{}", "call_big")),
                ("stream", stream_reply_frames("完成")),
            ]
            _, _, frames = stream_call(host, sid, "读大文件")
        finally:
            del agent_tools.TOOLS["big_read"]
        trs = events(frames, "tool_result")
        self.assertEqual(len(trs), 1)
        res = trs[0]["result"]
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["preview"]), 8000)
        self.assertLess(len(json.dumps(res, ensure_ascii=False)), 8200)

        fh = FakeHandler()
        router_dispatch(fh, host, "GET",
                        "/api/agent/sessions/%s/messages" % sid, {})
        msgs = json.loads(fh.wfile.getvalue().decode("utf-8"))["messages"]
        tool_msg = [m for m in msgs if m["role"] == "tool"][0]
        self.assertIn("truncated", tool_msg["content"])
        self.assertLess(len(tool_msg["content"]), 8200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
