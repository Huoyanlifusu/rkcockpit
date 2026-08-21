#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.api.handlers.legacy import HostApi            # noqa: E402
from host.api.router import load_handlers               # noqa: E402
from host.service.llm import LLMClient                  # noqa: E402
from portal.portal import DiscoverCache, Handler, RulesStore  # noqa: E402

_LLM_KEY = os.environ.get("RKSS_LLM_KEY") or ""
_LLM_BASE = os.environ.get("RKSS_LLM_BASE") or "https://api.deepseek.com"
_LLM_MODEL = os.environ.get("RKSS_LLM_MODEL") or "deepseek-chat"


@unittest.skipUnless(_LLM_KEY, "需真实 LLM 端点（设置环境变量 RKSS_LLM_KEY）")
class AgentLiveE2ETest(unittest.TestCase):
    """Test class."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="rkss-e2e-")
        cls.conf_dir = cls._tmp
        cls._write_llm_conf(cls.conf_dir, _LLM_KEY)
        host = HostApi(cls.conf_dir, sim=True)
        rules_store = RulesStore(cls.conf_dir)
        discover = DiscoverCache(host, rules_store)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.index_html = "e2e"
        srv.host = host
        srv.discover = discover
        srv.rules = rules_store
        load_handlers()
        threading.Thread(target=srv.serve_forever,
                         daemon=True, name="rkss-e2e").start()
        cls._srv = srv
        cls._discover = discover
        cls.base = "http://127.0.0.1:%d" % srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        try:
            cls._discover.stop()
        except Exception:
            pass
        try:
            cls._srv.shutdown()
            cls._srv.server_close()
        except Exception:
            pass



    @staticmethod
    def _write_llm_conf(conf_dir, api_key, **extra):
        """Test helper."""
        conf = {"base_url": _LLM_BASE, "api_key": api_key,
                "model": _LLM_MODEL, "timeout": 60, "max_tokens": 1024,
                "history_budget": 12000}
        conf.update(extra)
        path = os.path.join(conf_dir, "llm.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(conf, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)

    def _http(self, method, path, body=None, timeout=120):
        req = urllib.request.Request(self.base + path, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _new_session(self, device_id="demo"):
        status, r = self._http("POST", "/api/agent/sessions",
                               {"device_id": device_id})
        self.assertEqual(status, 201)
        self.assertTrue(r["ok"])
        return r["session"]["id"]

    def _chat(self, sid, message):
        return self._http("POST", "/api/agent/sessions/%s/chat" % sid,
                          {"message": message})



    def test_01_llmclient_complete_real(self):
        """Test helper."""
        c = LLMClient(base_url=_LLM_BASE, api_key=_LLM_KEY,
                      model=_LLM_MODEL, timeout=60, max_tokens=256)
        r = c.complete([{"role": "user", "content": "用一句话回答：1+1=?"}])
        self.assertIsInstance(r["content"], str)
        self.assertTrue(r["content"].strip(), "content 为空")
        self.assertEqual(r["tool_calls"], [], "未给 tools 参数不应有 tool_calls")
        self.assertIsInstance(r["usage"], dict)
        self.assertTrue(r["usage"], "usage 缺失")
        self.assertNotIn(_LLM_KEY, repr(r), "api_key 泄漏到响应")

    def test_02_chat_device_list_real(self):
        """Test helper."""
        sid = self._new_session("demo")
        status, r = self._chat(sid, "列出当前设备")
        self.assertEqual(status, 200)
        self.assertTrue(r["ok"], "chat ok:false（%s）" % r.get("code"))
        names = [tc["name"] for tc in r["tool_calls"]]
        self.assertIn("device_list", names,
                      "device_list 未被真实调用，tool_calls=%s" % names)
        dl = next(tc for tc in r["tool_calls"] if tc["name"] == "device_list")
        self.assertTrue(dl["result"]["ok"], "device_list result 非 ok")
        devs = dl["result"]["devices"]
        self.assertTrue(any(d.get("id") == "demo" for d in devs),
                        "result 未回填 demo 设备")
        self.assertIn("demo", r["reply"], "最终回复未引用 demo 设备")
        self.assertEqual(r["message_count"], 4,
                         "期望 user/assistant(tool_calls)/tool/assistant 4 条")

    def test_03_chat_device_sysinfo_real(self):
        """Test helper."""
        sid = self._new_session("demo")
        status, r = self._chat(sid, "检查 demo 设备的系统信息")
        self.assertEqual(status, 200)
        self.assertTrue(r["ok"], "chat ok:false（%s）" % r.get("code"))
        names = [tc["name"] for tc in r["tool_calls"]]
        self.assertIn("device_sysinfo", names,
                      "device_sysinfo 未被真实调用，tool_calls=%s" % names)
        si = next(tc for tc in r["tool_calls"]
                  if tc["name"] == "device_sysinfo")
        self.assertIn("demo", si["arguments"], "arguments 未带 device_id=demo")
        self.assertTrue(si["result"]["ok"], "device_sysinfo result 非 ok")
        data = si["result"].get("data") or {}
        self.assertTrue(any(k in data for k in
                            ("os", "kernel", "cpu_usage", "mem_total_mb")),
                        "sysinfo data 未回填真实字段: %s" % list(data)[:8])
        self.assertIn("demo", r["reply"], "最终回复未引用 demo")
        self.assertEqual(r["message_count"], 4)

    def test_04_chat_auth_error_real(self):
        """Test helper."""
        self._write_llm_conf(self.conf_dir, "sk-invalid")
        try:
            sid = self._new_session("demo")
            status, r = self._chat(sid, "列出当前设备")
            self.assertEqual(status, 200)
            self.assertFalse(r["ok"], "坏 key 不应 ok")
            self.assertEqual(r["code"], "auth",
                             "期望 code=auth，实际 %r" % r.get("code"))
            self.assertNotIn("sk-invalid", r["error"], "错误消息泄漏 key")
            self.assertNotIn(_LLM_KEY, repr(r), "api_key 泄漏到响应")
        finally:
            self._write_llm_conf(self.conf_dir, _LLM_KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
