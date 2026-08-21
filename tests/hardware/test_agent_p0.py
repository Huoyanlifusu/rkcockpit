#!/usr/bin/env python3

import io
import json
import os
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
from unittest import mock

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.agent import tools as agent_tools          # noqa: E402
from host.api.handlers import agent as agent_handler  # noqa: E402
from host.api.router import dispatch as router_dispatch  # noqa: E402
from host.service.llm import (LLMAuth, LLMClient, LLMNotConfigured,  # noqa: E402
                              LLMRateLimit, LLMTimeout, LLMTokenLimit,
                              LLMUnreachable, estimate_tokens)

_TMP_DIRS = []


def _tmpdir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP_DIRS.append(d)
    return d




def _reply_payload(content):
    return {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


def _tool_call_payload(name, args, call_id="call_1"):
    return {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": args}}]},
        "finish_reason": "tool_calls"}]}


class FakeLLMHandler(BaseHTTPRequestHandler):
    """Test class."""
    script = []
    requests = []
    log_message = staticmethod(lambda *a, **k: None)

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length).decode("utf-8")) \
            if length else {}
        FakeLLMHandler.requests.append(req)
        if FakeLLMHandler.script:
            item = FakeLLMHandler.script.pop(0)
            if callable(item):
                return self._send_json(*item(req))
            return self._send_json(*item)
        msgs = req.get("messages") or []
        if any(m.get("role") == "tool" for m in msgs):
            return self._send_json(200, _reply_payload("工具已执行完毕，最终回答"))
        return self._send_json(200, _tool_call_payload("device_list", "{}"))




class StubTransport:
    kind = "stub"

    def exec(self, cmd, timeout=30):
        return 0, "", ""


class FakeMonitorSvc:
    def __init__(self):
        self.calls = []

    def get_or_start(self, factory, device_id):
        self.calls.append(("start", device_id))

    def now(self, device_id):
        return {"ts": 1, "cpu": {"usage": 5.0}}


class FakeHost:
    """Test class."""

    def __init__(self):
        self.conf_dir = _tmpdir("rkss-agent-test-")
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
            {"id": "nostate", "name": "无状态", "type": "adb",
             "host": "h3", "port": 5555, "user": "root", "auth": "key",
             "has_password": False, "remark": ""},
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
        if did == "ghost":
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        return {"ok": True, "data": {
            "cpu": {"cores": 4, "usage": 12.5},
            "mem": {"total_mb": 1024, "used_mb": 300},
            "temp": None, "uptime_s": None}}

    def fs_list(self, did, query):
        if did == "ghost":
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        return {"ok": True, "path": "/",
                "entries": [{"n": "f%d" % i, "s": i}
                            for i in range(250)]}

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
        data = (raw or json.dumps(body if body is not None else {})
                ).encode("utf-8")
        self.rfile = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self._code = code

    def send_header(self, key, value):
        self._headers[key] = value

    def end_headers(self):
        pass


def http_call(host, method, path, body=None, raw=None, query=None):
    """Test helper."""
    fh = FakeHandler(body=body, raw=raw)
    handled = router_dispatch(fh, host, method, path, query or {})
    if not handled:
        return None, None, fh
    payload = json.loads(fh.wfile.getvalue().decode("utf-8")) \
        if fh.wfile.getvalue() else None
    return fh._code, payload, fh


def create_session(host, device_id="demo"):
    code, r, _ = http_call(host, "POST", "/api/agent/sessions",
                           {"device_id": device_id})
    assert code == 201 and r["ok"], "create session failed: %s" % r
    return r["session"]["id"]


def chat(host, sid, message, **extra):
    body = {"message": message}
    body.update(extra)
    return http_call(host, "POST", "/api/agent/sessions/%s/chat" % sid, body)


class AgentApiTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLMHandler)
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
        FakeLLMHandler.script = []
        FakeLLMHandler.requests = []
        agent_handler._SESSIONS.clear()

    def make_host(self):
        return FakeHost()

    def conf_host(self, host, **llm_conf):
        conf = {"base_url": self.llm_base, "model": "fake-model",
                "api_key": "sk-test-secret"}
        conf.update(llm_conf)
        agent_handler._save_llm_conf(host, conf)
        return host



    def test_01_plain_reply_and_multiturn(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)
        FakeLLMHandler.script = [
            (200, _reply_payload("你好，我是助手")),
            (200, _reply_payload("第二轮回答")),
        ]
        code, r, _ = chat(host, sid, "你好")
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["reply"], "你好，我是助手")
        self.assertEqual(r["tool_calls"], [])
        self.assertEqual(r["session_id"], sid)
        self.assertEqual(r["message_count"], 2)      # user + assistant

        code2, r2, _ = chat(host, sid, "再问一个问题")
        self.assertEqual(code2, 200)
        self.assertEqual(r2["reply"], "第二轮回答")
        self.assertEqual(r2["message_count"], 4)


        req = FakeLLMHandler.requests[1]["messages"]
        self.assertEqual(
            [m["content"] for m in req if m["role"] == "user"],
            ["你好", "再问一个问题"])
        self.assertIn(
            "你好，我是助手",
            [m.get("content") for m in req if m["role"] == "assistant"])

        req0 = FakeLLMHandler.requests[0]["messages"]
        self.assertEqual([m["role"] for m in req0], ["system", "user"])



    def test_02_tool_roundtrip(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)
        code, r, _ = chat(host, sid, "看看有哪些设备")
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["reply"], "工具已执行完毕，最终回答")
        self.assertEqual(len(r["tool_calls"]), 1)
        tc = r["tool_calls"][0]
        self.assertEqual(tc["name"], "device_list")
        self.assertTrue(tc["result"]["ok"])
        self.assertIn("devices", tc["result"])


        code2, r2, _ = http_call(host, "GET",
                                 "/api/agent/sessions/%s/messages" % sid)
        self.assertEqual(code2, 200)
        roles = [m["role"] for m in r2["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        asst = r2["messages"][1]
        self.assertEqual(asst["tool_calls"][0]["type"], "function")
        self.assertEqual(asst["tool_calls"][0]["function"]["name"],
                         "device_list")
        tool_msg = r2["messages"][2]
        self.assertEqual(tool_msg["tool_call_id"], "call_1")
        self.assertIn("devices", tool_msg["content"])


        req = FakeLLMHandler.requests[1]["messages"]
        self.assertEqual(req[-1]["role"], "tool")
        self.assertIn("demo", req[-1]["content"])



    def test_03_tool_iter_cap(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)
        FakeLLMHandler.script = [
            (200, _tool_call_payload("device_list", "{}"))] * 8
        code, r, _ = chat(host, sid, "一直调用工具")
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])
        self.assertIn("上限", r["reply"])
        self.assertEqual(len(r["tool_calls"]), 8)
        self.assertEqual(len(FakeLLMHandler.requests), 8)



    def test_04_tool_behavior(self):
        host = self.make_host()

        r = agent_tools.run_tool_call(host, {"id": "c1",
                                             "name": "device_list",
                                             "arguments": "{}"})
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["devices"]), 3)
        for d in r["devices"]:
            for k in ("host", "user", "auth", "has_password", "info"):
                self.assertNotIn(k, d)
        by_id = {d["id"]: d for d in r["devices"]}
        self.assertEqual(by_id["demo"]["state"], "online")
        self.assertEqual(by_id["nostate"]["state"], "unknown")


        r2 = agent_tools.run_tool_call(host, {"id": "c2",
                                              "name": "device_sysinfo",
                                              "arguments":
                                                  '{"device_id": "demo"}'})
        self.assertTrue(r2["ok"])
        self.assertNotIn("temp", r2["data"])
        self.assertNotIn("uptime_s", r2["data"])
        self.assertIn("cpu", r2["data"])


        r3 = agent_tools.run_tool_call(host, {"id": "c3", "name": "diag",
                                              "arguments":
                                                  '{"device_id": "demo",'
                                                  ' "kind": "bogus"}'})
        self.assertFalse(r3["ok"])
        self.assertIn("kind", r3["error"])
        with mock.patch("host.service.diag.dmesg",
                        return_value={"ok": True, "lines": [],
                                      "truncated": False}) as m:
            r4 = agent_tools.run_tool_call(
                host, {"id": "c4", "name": "diag",
                       "arguments": '{"device_id": "demo", "kind": "dmesg",'
                                    ' "lines": 9999}'})
            self.assertTrue(r4["ok"])
            self.assertEqual(m.call_args.kwargs["lines"], 200)


        r5 = agent_tools.run_tool_call(host, {"id": "c5", "name": "fs_list",
                                              "arguments":
                                                  '{"device_id": "demo"}'})
        self.assertTrue(r5["ok"])
        self.assertEqual(len(r5["entries"]), 200)
        self.assertTrue(r5.get("truncated"))


        fake_svc = FakeMonitorSvc()
        with mock.patch("host.api.handlers.monitor._svc",
                        return_value=fake_svc):
            r6 = agent_tools.run_tool_call(host, {"id": "c6",
                                                  "name": "monitor",
                                                  "arguments":
                                                      '{"device_id": "demo"}'})
        self.assertTrue(r6["ok"])
        self.assertEqual(r6["sample"]["cpu"]["usage"], 5.0)
        self.assertEqual(fake_svc.calls, [("start", "demo")])



    def test_05_unknown_tool_and_bad_args(self):
        host = self.make_host()
        r = agent_tools.run_tool_call(host, {"id": "c1", "name": "nope",
                                             "arguments": "{}"})
        self.assertFalse(r["ok"])
        self.assertIn("未知工具", r["error"])
        r2 = agent_tools.run_tool_call(host, {"id": "c2",
                                              "name": "device_list",
                                              "arguments": "not-json"})
        self.assertFalse(r2["ok"])
        self.assertIn("解析失败", r2["error"])
        r3 = agent_tools.run_tool_call(host, {"id": "c3",
                                              "name": "device_list",
                                              "arguments": "[1, 2]"})
        self.assertFalse(r3["ok"])


        class BoomHost:
            def device_check(self, did):
                raise RuntimeError("boom")
        r4 = agent_tools.run_tool_call(BoomHost(), {"id": "c4",
                                                    "name": "device_check",
                                                    "arguments":
                                                        '{"device_id": "x"}'})
        self.assertFalse(r4["ok"])
        self.assertIn("boom", r4["error"])


        agent_tools.TOOLS["big_read"] = {
            "name": "big_read", "description": "t", "tier": "read",
            "parameters": {}, "fn": lambda h, a: {"data": "x" * 9000}}
        try:
            r5 = agent_tools.run_tool_call(host, {"id": "c5",
                                                  "name": "big_read",
                                                  "arguments": "{}"})
        finally:
            del agent_tools.TOOLS["big_read"]
        self.assertTrue(r5["ok"])
        self.assertTrue(r5["truncated"])
        self.assertEqual(len(r5["preview"]), 8000)



    def test_06_llm_error_triage(self):
        c = LLMClient()
        self.assertFalse(c.configured)
        with self.assertRaises(LLMNotConfigured):
            c.complete([{"role": "user", "content": "hi"}])

        def send_timeout(*a, **k):
            raise socket.timeout("timed out")
        c = LLMClient(base_url="http://x/v1", model="m", _send=send_timeout)
        with self.assertRaises(LLMTimeout):
            c.complete([{"role": "user", "content": "hi"}])

        for st in (401, 403):
            c = LLMClient(base_url="http://x/v1", model="m",
                          _send=lambda *a, st=st: (st, b"{}"))
            with self.assertRaises(LLMAuth):
                c.complete([{"role": "user", "content": "hi"}])

        c = LLMClient(base_url="http://x/v1", model="m",
                      _send=lambda *a: (429, b"{}"))
        with self.assertRaises(LLMRateLimit):
            c.complete([{"role": "user", "content": "hi"}])

        c = LLMClient(base_url="http://x/v1", model="m",
                      _send=lambda *a: (400, b'{"error":{"message":'
                                           b'"maximum context length is '
                                           b'4096 tokens"}}'))
        with self.assertRaises(LLMTokenLimit):
            c.complete([{"role": "user", "content": "hi"}])
        c = LLMClient(base_url="http://x/v1", model="m",
                      _send=lambda *a: (400, b'{"error":"bad"}'))
        with self.assertRaises(LLMUnreachable):
            c.complete([{"role": "user", "content": "hi"}])

        c = LLMClient(base_url="http://x/v1", model="m",
                      _send=lambda *a: (500, b"oops"))
        with self.assertRaises(LLMUnreachable):
            c.complete([{"role": "user", "content": "hi"}])
        c = LLMClient(base_url="http://x/v1", model="m",
                      _send=lambda *a: (200, b'{"foo": 1}'))
        with self.assertRaises(LLMUnreachable):
            c.complete([{"role": "user", "content": "hi"}])


        seen = {}

        def send_ok(method, url, headers, body, timeout):
            seen["auth"] = headers.get("Authorization")
            return 200, json.dumps({
                "choices": [{"message": {"role": "assistant",
                                         "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"total_tokens": 5}}).encode("utf-8")
        c = LLMClient(base_url="http://x/v1", model="m", api_key="k-123",
                      _send=send_ok)
        r = c.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(r["content"], "ok")
        self.assertEqual(r["tool_calls"], [])
        self.assertEqual(r["finish_reason"], "stop")
        self.assertEqual(r["usage"]["total_tokens"], 5)
        self.assertEqual(seen["auth"], "Bearer k-123")


        def send_tool(method, url, headers, body, timeout):
            return 200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_x", "type": "function",
                                "function": {"name": "device_list",
                                             "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}]}).encode("utf-8")
        c = LLMClient(base_url="http://x/v1", model="m", _send=send_tool)
        r = c.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(r["tool_calls"][0]["name"], "device_list")
        self.assertEqual(r["tool_calls"][0]["arguments"], "{}")

        self.assertGreater(
            estimate_tokens([{"role": "user", "content": "你好" * 40}]), 0)



    def test_07_chat_error_codes(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)
        cases = [
            (401, {"error": "unauthorized"}, "auth"),
            (429, {"error": "rate"}, "rate_limit"),
            (500, {}, "unreachable"),
            (400, {"error": {"message": "context length exceeded"}},
             "token_limit"),
            (400, {"error": "bad"}, "unreachable"),
        ]
        for st, payload, code in cases:
            FakeLLMHandler.script = [(st, payload)]
            c, r, _ = chat(host, sid, "触发错误")
            self.assertEqual(c, 200)
            self.assertFalse(r["ok"])
            self.assertEqual(r["code"], code, "status=%s" % st)


        host2 = self.make_host()
        sid2 = create_session(host2)
        c, r, _ = chat(host2, sid2, "你好")
        self.assertEqual(c, 200)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "not_configured")



    def test_08_write_guard(self):

        with self.assertRaises(ValueError):
            agent_tools.register("bad_exec", "x", "exec",
                                 {}, lambda h, a: None)
        self.assertNotIn("bad_exec", agent_tools.TOOLS)

        agent_tools.register("_probe_write", "x", "write",
                             {}, lambda h, a: None)
        self.assertEqual(agent_tools.TOOLS["_probe_write"]["tier"], "write")



    def test_09_lru_eviction(self):
        host = self.make_host()
        sids = [create_session(host) for _ in range(35)]
        self.assertEqual(len(agent_handler._SESSIONS), 32)
        for sid in sids[:3]:
            self.assertNotIn(sid, agent_handler._SESSIONS)
        for sid in sids[3:]:
            self.assertIn(sid, agent_handler._SESSIONS)

        http_call(host, "GET",
                  "/api/agent/sessions/%s/messages" % sids[10])
        with agent_handler._SESS_LOCK:
            order = list(agent_handler._SESSIONS)
        self.assertEqual(order[-1], sids[10])



    def test_10_ttl_and_message_cap(self):
        host = self.make_host()
        sid = create_session(host)
        with agent_handler._SESS_LOCK:
            agent_handler._SESSIONS[sid]["updated_ms"] = \
                int(time.time() * 1000) - (agent_handler.TTL_SECONDS * 1000 + 1)
        agent_handler._sweep_sessions()
        self.assertNotIn(sid, agent_handler._SESSIONS)
        c, r, _ = chat(host, sid, "hi")
        self.assertEqual(c, 404)


        sid2 = create_session(host)
        with agent_handler._SESS_LOCK:
            sess = agent_handler._SESSIONS[sid2]
            sess["messages"] = [
                {"role": "user" if i % 2 == 0 else "assistant",
                 "content": "m%d" % i} for i in range(250)]
            agent_handler._trim_session(sess)
        self.assertEqual(len(sess["messages"]), 200)
        self.assertEqual(sess["messages"][0]["role"], "user")


        sid3 = create_session(host)
        with agent_handler._SESS_LOCK:
            sess = agent_handler._SESSIONS[sid3]
            sess["messages"] = [
                {"role": "user", "content": "u0"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c0", "type": "function",
                                 "function": {"name": "x",
                                              "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c0", "content": "{}"},
                {"role": "assistant", "content": "a0"},
            ] + [{"role": "user" if i % 2 == 0 else "assistant",
                  "content": "m%d" % i} for i in range(250)]
            agent_handler._trim_session(sess)
        self.assertLessEqual(len(sess["messages"]), 200)
        self.assertEqual(sess["messages"][0]["role"], "user")
        self.assertNotEqual(sess["messages"][0]["role"], "tool")



    def test_11_history_budget(self):
        host = self.make_host()
        sid = create_session(host)
        with agent_handler._SESS_LOCK:
            sess = agent_handler._SESSIONS[sid]
            for i in range(20):
                sess["messages"].append({"role": "user", "content": "q%d" % i})
                sess["messages"].append({"role": "assistant",
                                         "content": "a%d" % i})
        wire = [{"role": "system", "content": "SYS"}] + list(sess["messages"])
        trimmed = agent_handler._trim_history(wire, 30)
        self.assertLess(len(trimmed), len(wire))
        self.assertEqual(trimmed[0]["role"], "system")
        self.assertGreaterEqual(len(trimmed), 3)
        self.assertEqual(trimmed[-1]["content"], "a19")

        self.assertEqual(len(sess["messages"]), 40)

        full = agent_handler._trim_history(wire, 10 ** 9)
        self.assertEqual(len(full), len(wire))



    def test_12_system_prompt(self):
        host = self.make_host()
        text = agent_handler._build_system(host, "demo")
        self.assertIn("运维智能助手", text)
        self.assertIn("device_list", text)
        self.assertIn("demo", text)
        self.assertIn("nostate", text)
        self.assertIn("unknown", text)
        self.assertIn("系统信息", text)
        self.assertIn("cores", text)

        host.devices = host.devices * 12
        text2 = agent_handler._build_system(host, "demo")
        self.assertLessEqual(text2.count('"id"'), 20)



    def test_13_key_never_leaks(self):
        host = self.conf_host(self.make_host())
        code, r, _ = http_call(host, "GET", "/api/agent/config")
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])
        self.assertTrue(r["configured"])
        self.assertEqual(r["model"], "fake-model")
        self.assertNotIn("api_key", r)
        self.assertNotIn("sk-test-secret",
                         json.dumps(r, ensure_ascii=False))
        self.assertTrue(r["base_url_masked"].startswith("http://127.0.0.1"))
        mode = os.stat(os.path.join(host.conf_dir, "llm.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)


        host2 = self.make_host()
        code2, r2, _ = http_call(host2, "GET", "/api/agent/config")
        self.assertEqual(code2, 200)
        self.assertFalse(r2["configured"])
        self.assertEqual(r2["model"], "")
        self.assertEqual(r2["base_url_masked"], "")


        host3 = self.conf_host(
            self.make_host(),
            base_url="https://user:pass@llm.example.com/v1?k=sekret")
        code3, r3, _ = http_call(host3, "GET", "/api/agent/config")
        self.assertEqual(code3, 200)
        self.assertNotIn("sekret", json.dumps(r3, ensure_ascii=False))
        self.assertNotIn("user:pass", r3["base_url_masked"])
        self.assertIn("llm.example.com", r3["base_url_masked"])



    def test_14_http_semantics(self):
        host = self.make_host()
        code, r, _ = http_call(host, "POST", "/api/agent/sessions",
                               {"device_id": "demo"})
        self.assertEqual(code, 201)
        self.assertTrue(r["ok"])
        sid = r["session"]["id"]
        self.assertEqual(r["session"]["device_id"], "demo")
        self.assertEqual(r["session"]["message_count"], 0)

        code2, r2, _ = http_call(host, "GET", "/api/agent/sessions")
        self.assertEqual(code2, 200)
        self.assertEqual(len(r2["sessions"]), 1)

        http_call(host, "POST", "/api/agent/sessions", {"device_id": "local"})
        code3, r3, _ = http_call(host, "GET", "/api/agent/sessions")
        self.assertEqual(len(r3["sessions"]), 2)
        ups = [s["updated_ms"] for s in r3["sessions"]]
        self.assertEqual(ups, sorted(ups, reverse=True))

        code4, r4, _ = http_call(host, "DELETE", "/api/agent/sessions/" + sid)
        self.assertEqual(code4, 200)
        self.assertEqual(r4["deleted"], sid)
        code5, r5, _ = http_call(host, "DELETE", "/api/agent/sessions/" + sid)
        self.assertEqual(code5, 404)
        self.assertFalse(r5["ok"])

        code6, r6, _ = chat(host, sid, "hi")
        self.assertEqual(code6, 404)

        sid2 = create_session(host)
        code7, r7, _ = chat(host, sid2, "   ")
        self.assertEqual(code7, 400)
        code8, r8, _ = http_call(host, "POST",
                                 "/api/agent/sessions/%s/chat" % sid2,
                                 body=[1])
        self.assertEqual(code8, 400)

        code8b, r8b, _ = chat(host, sid2, 123)
        self.assertEqual(code8b, 400)
        code8c, r8c, _ = chat(host, sid2, ["a"])
        self.assertEqual(code8c, 400)

        code9, r9, _ = http_call(host, "GET",
                                 "/api/agent/sessions/%s/messages" % sid2)
        self.assertEqual(code9, 200)
        self.assertEqual(r9["messages"], [])
        code10, r10, _ = http_call(host, "GET",
                                   "/api/agent/sessions/zzz/messages")
        self.assertEqual(code10, 404)

        code11, r11, _ = http_call(host, "GET", "/api/agent/tools")
        self.assertEqual(code11, 200)
        names = {t["function"]["name"] for t in r11["tools"]}
        self.assertEqual(names, {"device_list", "device_check",
                                 "device_sysinfo", "diag", "monitor",
                                 "fs_list"})

        code12, r12, _ = http_call(host, "GET", "/api/agent/config")
        self.assertEqual(code12, 200)
        self.assertIn("configured", r12)



    def test_15_concurrent_chat_no_deadlock(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)

        def slow_reply(req):
            time.sleep(0.3)
            return 200, _reply_payload("并发完成")
        FakeLLMHandler.script = [slow_reply, slow_reply]
        results = []

        def worker():
            code, r, _ = chat(host, sid, "并发请求")
            results.append((code, r))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive(), "chat 线程 1 死锁")
        self.assertFalse(t2.is_alive(), "chat 线程 2 死锁")
        self.assertEqual(len(results), 2)
        for code, r in results:
            self.assertEqual(code, 200)
            self.assertTrue(r["ok"])
            self.assertEqual(r["reply"], "并发完成")

        self.assertEqual(len(FakeLLMHandler.requests), 2)



    def test_16_chat_swept_session_404_not_500(self):
        host = self.conf_host(self.make_host())
        sid = create_session(host)
        with agent_handler._SESS_LOCK:
            sess = agent_handler._SESSIONS[sid]
            sess["updated_ms"] = int(time.time() * 1000) - \
                (agent_handler.TTL_SECONDS * 1000 + 1)
        code, r, _ = chat(host, sid, "过期会话")
        self.assertEqual(code, 404)
        self.assertFalse(r["ok"])
        self.assertIn("会话不存在", r["error"])
        self.assertNotIn(sid, agent_handler._SESSIONS)

        agent_handler._SESSIONS.clear()
        agent_handler._touch(sess)
        self.assertEqual(sess["message_count"], len(sess["messages"]))



    def test_17_url_credentials_not_leaked(self):
        secret_url = "https://user:sekret@llm.example.com/v1"

        def send_timeout(*a, **k):
            raise socket.timeout("timed out")
        c = LLMClient(base_url=secret_url, model="m", _send=send_timeout)
        with self.assertRaises(LLMTimeout) as ctx:
            c.complete([{"role": "user", "content": "hi"}])
        msg = str(ctx.exception)
        self.assertNotIn("sekret", msg)
        self.assertNotIn("user:", msg)
        self.assertIn("llm.example.com", msg)

        c = LLMClient(base_url=secret_url, model="m",
                      _send=lambda *a: (500, b"oops"))
        with self.assertRaises(LLMUnreachable) as ctx2:
            c.complete([{"role": "user", "content": "hi"}])
        self.assertNotIn("sekret", str(ctx2.exception))
        self.assertNotIn("user:", str(ctx2.exception))

        host = self.conf_host(
            self.make_host(),
            base_url="http://user:sekret@127.0.0.1:%d/v1"
                     % self.srv.server_address[1])
        sid = create_session(host)
        FakeLLMHandler.script = [(500, {"error": "boom"})]
        code, r, _ = chat(host, sid, "触发错误")
        self.assertEqual(code, 200)
        self.assertEqual(r["code"], "unreachable")
        self.assertNotIn("sekret", json.dumps(r, ensure_ascii=False))
        self.assertNotIn("user:", json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
