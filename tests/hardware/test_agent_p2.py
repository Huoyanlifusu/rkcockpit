#!/usr/bin/env python3

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.agent import tools as agent_tools          # noqa: E402
from host.api.handlers import agent as agent_handler  # noqa: E402
from host.api.router import dispatch as router_dispatch  # noqa: E402
from host.audit.recorder import AuditRecorder        # noqa: E402
from host.task.execjob import ExecJobStore           # noqa: E402
from host.transport import LocalTransport            # noqa: E402

_TMP_DIRS = []


def _tmpdir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP_DIRS.append(d)
    return d




def _reply_payload(content):
    return {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


def _tool_call_payload(name, args, call_id="call_p2"):
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
        return self._send_json(200, _reply_payload("默认终答"))




class FakeStore:
    def __init__(self, conf_dir):
        self.conf_dir = conf_dir
        self.tmp_dir = os.path.join(conf_dir, "tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

    def temp_file(self, suffix=""):
        return os.path.join(self.tmp_dir,
                            "rkss-%d%s" % (int(time.time() * 1e6), suffix))


class FakeHost2:
    """Test class."""

    def __init__(self):
        self.conf_dir = _tmpdir("rkss-agent-p2-")
        self.root = os.path.join(self.conf_dir, "demo-root")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(os.path.join(self.root, "tmp"), exist_ok=True)
        self.store = FakeStore(self.conf_dir)
        self.audit = AuditRecorder(self.conf_dir)
        self.devices = [
            {"id": "demo", "name": "demo（模拟板卡）", "type": "local",
             "host": "", "port": 0, "user": "", "auth": "",
             "has_password": False, "remark": "演示",
             "state": "online", "ping_ms": 1},
        ]
        self._transports = {"demo": LocalTransport(root=self.root)}
        self._exec_stores = {}
        self._deploy_store = None

    def _device(self, did):
        for d in self.devices:
            if d["id"] == did:
                return d
        raise KeyError(did)

    def _transport(self, did):
        if did not in self._transports:
            raise KeyError(did)
        return self._transports[did]

    def _exec_store(self, did):
        es = self._exec_stores.get(did)
        if es is None:
            es = ExecJobStore(self._transport(did))
            self._exec_stores[did] = es
        return es



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
        return {"ok": True, "data": {"cpu": {"cores": 4, "usage": 1.0}}}

    def fs_list(self, did, query):
        if did == "ghost":
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        return {"ok": True, "path": "/", "entries": []}



    def exec_run(self, did, data):
        cmd = data.get("cmd") or ""
        timeout = data.get("timeout") or 120
        es = self._exec_store(did)         # ghost → KeyError
        jid = es.run(cmd, timeout)
        return {"ok": True, "job_id": jid}

    def exec_poll(self, did, query):


        jid = (query.get("job_id") or [""])[0]
        es = self._exec_store(did)
        return {"ok": True, **es.poll(jid)}

    def exec_kill(self, did, data):
        jid = data["job_id"]
        return {"ok": True, "killed": self._exec_store(did).kill(jid)}

    def fs_act(self, did, action, data):
        t = self._transport(did)
        if action == "mkdir":
            t.mkdir(data.get("path") or "")
            return {"ok": True, "path": data.get("path")}
        if action == "rm":
            t.remove(data.get("path") or "", bool(data.get("recursive", True)))
            return {"ok": True, "path": data.get("path")}
        if action == "rename":
            t.rename(data.get("path") or "", data.get("new_name") or "")
            return {"ok": True}
        if action == "mv":
            t.move(data.get("path") or "", data.get("dest") or "")
            return {"ok": True}
        if action == "chmod":
            t.chmod(data.get("path") or "", str(data.get("mode") or ""))
            return {"ok": True}
        return {"ok": False, "error": "未知操作: %s" % action}


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


def wait_deploy(host, plan_id, timeout=15):
    store = host._deploy_store
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = store.get(plan_id)
        if job["state"] != "running":
            return job
        time.sleep(0.1)
    raise AssertionError("deploy job 超时（>%ss）: %s" % (timeout, plan_id))


_WRITE_TOOLS = {"exec_run", "exec_kill", "fs_write", "fs_act",
                "deploy_start", "process_signal"}


class AgentP2Test(unittest.TestCase):

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

    def conf_host(self, host):
        agent_handler._save_llm_conf(host, {
            "base_url": self.llm_base, "model": "fake-model",
            "api_key": "sk-p2-secret"})
        return host



    def test_01_tier_validation_and_filter(self):

        for bad in ("exec", "admin", "sudo"):
            with self.assertRaises(ValueError):
                agent_tools.register("probe_%s" % bad, "x", bad,
                                     {}, lambda h, a: None)
            self.assertNotIn("probe_%s" % bad, agent_tools.TOOLS)

        agent_tools.register("probe_write", "x", "write",
                             {}, lambda h, a: {"ok": True})
        try:
            self.assertIn("probe_write", agent_tools.TOOLS)
            self.assertEqual(agent_tools.TOOLS["probe_write"]["tier"], "write")

            all_names = {t["function"]["name"]
                         for t in agent_tools.to_openai_tools()}
            self.assertTrue(_WRITE_TOOLS.issubset(all_names))
            self.assertIn("probe_write", all_names)


            write_names = {t["function"]["name"]
                           for t in agent_tools.to_openai_tools("write")}
            self.assertTrue((_WRITE_TOOLS | {"probe_write"})
                            <= write_names, write_names)
            read_names = {t["function"]["name"]
                          for t in agent_tools.to_openai_tools("read")}
            self.assertEqual(read_names, {"device_list", "device_check",
                                          "device_sysinfo", "diag",
                                          "monitor", "fs_list"})
            self.assertNotIn("probe_write", read_names)
        finally:
            del agent_tools.TOOLS["probe_write"]


        for n in sorted(_WRITE_TOOLS):
            t = agent_tools.TOOLS[n]
            self.assertEqual(t["tier"], "write", n)
            self.assertEqual(t["parameters"]["type"], "object", n)
            self.assertTrue(t["parameters"]["required"], n)


        host = FakeHost2()
        code, r, _ = http_call(host, "GET", "/api/agent/tools")
        self.assertEqual(code, 200)
        names = {t["function"]["name"] for t in r["tools"]}
        self.assertEqual(names, {"device_list", "device_check",
                                 "device_sysinfo", "diag", "monitor",
                                 "fs_list"})



    def test_02_write_tool_behavior(self):
        host = FakeHost2()
        demo_root = host.root


        content = "hello-rkss-α-部署\n"
        r = agent_tools.run_tool_call(host, {"id": "c1", "name": "fs_write",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "path": "/tmp/p2_write.txt",
                                                 "content": content})})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["path"], "/tmp/p2_write.txt")
        self.assertEqual(r["size"], len(content.encode("utf-8")))
        target = os.path.join(demo_root, "tmp/p2_write.txt")
        self.assertTrue(os.path.isfile(target))
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), content)


        r = agent_tools.run_tool_call(host, {"id": "c2", "name": "exec_run",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "cmd": "echo P2_EXEC_OK_42",
                                                 "timeout": 15})})
        self.assertTrue(r["ok"], r)
        self.assertIn("job_id", r)
        self.assertFalse(r.get("running"))
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("P2_EXEC_OK_42", r["output"])


        r = host.exec_run("demo", {"cmd": "sleep 30", "timeout": 60})
        jid = r["job_id"]
        rk = agent_tools.run_tool_call(host, {"id": "c4", "name": "exec_kill",
                                              "arguments": json.dumps({
                                                  "device_id": "demo",
                                                  "job_id": jid})})
        self.assertTrue(rk["ok"], rk)
        self.assertEqual(rk.get("killed"), jid)
        deadline = time.time() + 8
        p = None
        while time.time() < deadline:
            p = host.exec_poll("demo", {"job_id": [jid]})
            if not p["running"]:
                break
            time.sleep(0.2)
        self.assertFalse(p["running"])

        # fs_act：mkdir + chmod
        r = agent_tools.run_tool_call(host, {"id": "c5", "name": "fs_act",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "action": "mkdir",
                                                 "path": "/opt/p2dir"})})
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.isdir(os.path.join(demo_root, "opt/p2dir")))
        r = agent_tools.run_tool_call(host, {"id": "c6", "name": "fs_act",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "action": "chmod",
                                                 "path": "/tmp/p2_write.txt",
                                                 "mode": "0755"})})
        self.assertTrue(r["ok"], r)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o755)


        r = agent_tools.run_tool_call(host, {"id": "c7",
                                             "name": "process_signal",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "pid": 12345,
                                                 "sig": "TERM"})})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["pid"], 12345)
        self.assertEqual(r["sig"], "TERM")


        src = os.path.join(host.conf_dir, "p2-app.sh")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho DEPLOY_MARK_77\n")
        r = agent_tools.run_tool_call(host, {"id": "c8", "name": "deploy_start",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "files": [
                                                     {"src": src,
                                                      "dest": "/tmp/p2-app.sh",
                                                      "mode": "0755"}],
                                                 "cmd": "echo DEPLOY_CMD_88"})})
        self.assertTrue(r["ok"], r)
        self.assertIn("plan_id", r)
        job = wait_deploy(host, r["plan_id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["result"]["exit_code"], 0)
        self.assertIn("DEPLOY_CMD_88", job["result"]["output_tail"])
        deployed = os.path.join(demo_root, "tmp/p2-app.sh")
        self.assertTrue(os.path.isfile(deployed))
        self.assertEqual(os.stat(deployed).st_mode & 0o777, 0o755)



    def test_03_write_tool_failures(self):
        host = FakeHost2()


        r = agent_tools.run_tool_call(host, {"id": "c1", "name": "fs_write",
                                             "arguments": json.dumps({
                                                 "device_id": "ghost",
                                                 "path": "/tmp/x",
                                                 "content": "hi"})})
        self.assertFalse(r["ok"])
        self.assertIn("设备不存在", r["error"])


        r = agent_tools.run_tool_call(host, {"id": "c2", "name": "exec_run",
                                             "arguments":
                                                 '{"device_id": "demo"}'})
        self.assertFalse(r["ok"])
        self.assertIn("cmd 必填", r["error"])
        r = agent_tools.run_tool_call(host, {"id": "c3", "name": "exec_run",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "cmd": "echo x",
                                                 "timeout": "abc"})})
        self.assertFalse(r["ok"])


        r = agent_tools.run_tool_call(host, {"id": "c4", "name": "exec_kill",
                                             "arguments":
                                                 '{"device_id": "demo"}'})
        self.assertFalse(r["ok"])
        r = agent_tools.run_tool_call(host, {"id": "c5", "name": "fs_act",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "action": "explode",
                                                 "path": "/x"})})
        self.assertFalse(r["ok"])
        self.assertIn("action 必须是", r["error"])


        r = agent_tools.run_tool_call(host, {"id": "c6",
                                             "name": "process_signal",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "pid": 1,
                                                 "sig": "KILL"})})
        self.assertFalse(r["ok"])
        self.assertIn("受保护", r["error"])
        r = agent_tools.run_tool_call(host, {"id": "c7",
                                             "name": "process_signal",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "pid": 99,
                                                 "sig": "SIGUSR1"})})
        self.assertFalse(r["ok"])
        r = agent_tools.run_tool_call(host, {"id": "c8",
                                             "name": "process_signal",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "pid": "abc",
                                                 "sig": "TERM"})})
        self.assertFalse(r["ok"])


        r = agent_tools.run_tool_call(host, {"id": "c9",
                                             "name": "deploy_start",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "files": [
                                                     {"src": "/no/such/src",
                                                      "dest": "/tmp/x"}]})})
        self.assertFalse(r["ok"])
        self.assertIn("src 不存在", r["error"])
        r = agent_tools.run_tool_call(host, {"id": "c10",
                                             "name": "deploy_start",
                                             "arguments":
                                                 '{"device_id": "demo"}'})
        self.assertFalse(r["ok"])


        r = agent_tools.run_tool_call(host, {"id": "c11", "name": "nope",
                                             "arguments": "{}"})
        self.assertFalse(r["ok"])



    def test_04_coding_loop_h5(self):
        host = self.conf_host(FakeHost2())
        sid = create_session(host, "demo")
        script_path = "/tmp/rkss_p2_script.sh"

        def final_summary(req):
            contents = [m.get("content") for m in req["messages"]
                        if m["role"] == "tool"]
            return 200, _reply_payload(
                "脚本执行完成，输出: " + " | ".join(contents))

        FakeLLMHandler.script = [
            (200, _tool_call_payload("fs_write", json.dumps({
                "device_id": "demo", "path": script_path,
                "content": "#!/bin/sh\necho CODING_OK_123"}), call_id="c1")),
            (200, _tool_call_payload("exec_run", json.dumps({
                "device_id": "demo",
                "cmd": "sh %s/tmp/rkss_p2_script.sh" % host.root,
                "timeout": 15}), call_id="c2")),
            (200, _tool_call_payload("fs_act", json.dumps({
                "device_id": "demo", "action": "chmod",
                "path": script_path, "mode": "0755"}), call_id="c3")),
            final_summary,
        ]
        code, r, _ = chat(host, sid, "帮我写个脚本并执行")
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"], r)

        self.assertIn("CODING_OK_123", r["reply"])

        self.assertEqual(len(r["tool_calls"]), 3)
        for tc in r["tool_calls"]:
            self.assertTrue(tc["result"]["ok"], tc)

        deployed = os.path.join(host.root, "tmp/rkss_p2_script.sh")
        self.assertTrue(os.path.isfile(deployed))
        with open(deployed, encoding="utf-8") as fh:
            self.assertIn("echo CODING_OK_123", fh.read())
        self.assertEqual(os.stat(deployed).st_mode & 0o777, 0o755)

        evs = [e for e in host.audit.query()
               if e["action"].startswith("agent.")]
        self.assertEqual(len(evs), 3)
        by_action = {e["action"]: e for e in evs}
        self.assertEqual(set(by_action),
                         {"agent.fs.write", "agent.exec.run", "agent.fs.act"})
        for e in evs:
            self.assertEqual(e["result"], "ok")
            self.assertEqual(e["target"]["kind"], "agent")
            self.assertEqual(e["target"]["id"], sid)
            self.assertEqual(e["detail"]["session_id"], sid)
            self.assertEqual(e["detail"]["device_id"], "demo")
            self.assertEqual(e["detail"]["result"], "ok")
        self.assertEqual(
            by_action["agent.fs.write"]["detail"]["params"]["path"],
            script_path)
        self.assertIn(
            "rkss_p2_script.sh",
            by_action["agent.exec.run"]["detail"]["params"]["cmd"])
        self.assertEqual(
            by_action["agent.fs.act"]["detail"]["params"]["action"], "chmod")



    def test_05_write_audit_fail_recorded(self):
        host = self.conf_host(FakeHost2())
        sid = create_session(host, "demo")
        FakeLLMHandler.script = [
            (200, _tool_call_payload("fs_write", json.dumps({
                "device_id": "ghost", "path": "/tmp/x", "content": "hi"}),
                call_id="c1")),
            (200, _reply_payload("写入失败，设备不存在")),
        ]
        code, r, _ = chat(host, sid, "写个文件到不存在的设备")
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["tool_calls"]), 1)
        self.assertFalse(r["tool_calls"][0]["result"]["ok"])
        evs = [e for e in host.audit.query()
               if e["action"].startswith("agent.")]
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev["action"], "agent.fs.write")
        self.assertEqual(ev["result"], "fail")
        self.assertIn("设备不存在", ev["err"])
        self.assertEqual(ev["detail"]["session_id"], sid)
        self.assertEqual(ev["detail"]["device_id"], "ghost")
        self.assertEqual(ev["detail"]["result"], "fail")
        self.assertEqual(ev["detail"]["params"]["path"], "/tmp/x")



    def test_06_write_result_truncated(self):
        host = FakeHost2()
        r = agent_tools.run_tool_call(host, {"id": "c1", "name": "exec_run",
                                             "arguments": json.dumps({
                                                 "device_id": "demo",
                                                 "cmd": "python3 -c \"import"
                                                        " sys; sys.stdout."
                                                        "write('x' * 9000)\"",
                                                 "timeout": 20})})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["truncated"], r)
        self.assertEqual(len(r["preview"]), agent_tools.MAX_RESULT_CHARS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
