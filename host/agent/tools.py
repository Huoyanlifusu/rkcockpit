"""Utilities for host.agent.tools."""
import json
import os
import tempfile
import time


MAX_RESULT_CHARS = 8000


def _schema(props, required=()):
    return {"type": "object", "properties": dict(props),
            "required": list(required)}




TOOLS = {}


def register(name, description, tier, parameters, fn):
    """Handle register."""
    if tier not in ("read", "write"):
        raise ValueError(
            "工具 tier 只允许 read|write，实际: %r（工具: %s）" % (tier, name))
    TOOLS[name] = {"name": name, "description": description, "tier": tier,
                   "parameters": parameters, "fn": fn}


def to_openai_tools(tier=None):
    """Handle to openai tools."""
    out = []
    for name in sorted(TOOLS):
        t = TOOLS[name]
        if tier is not None and t["tier"] != tier:
            continue
        out.append({"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": t["parameters"]}})
    return out




def run_tool_call(host, tool_call):
    """Handle run tool call."""
    name = tool_call.get("name") or ""
    entry = TOOLS.get(name)
    if entry is None:
        return {"ok": False, "error": "未知工具: %s" % name}
    if entry["tier"] not in ("read", "write"):
        return {"ok": False, "error": "工具 tier 非法: %s" % name}
    try:
        args = json.loads(tool_call.get("arguments") or "{}")
    except (ValueError, TypeError):
        return {"ok": False, "error": "工具参数解析失败: %s" % name}
    if not isinstance(args, dict):
        return {"ok": False, "error": "工具参数必须是 JSON 对象: %s" % name}
    try:
        result = entry["fn"](host, args)
    except Exception as exc:
        return {"ok": False,
                "error": "%s 执行失败: %s" % (name, str(exc)[:300])}
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) > MAX_RESULT_CHARS:
        ok = result.get("ok", True) if isinstance(result, dict) else True
        return {"ok": ok, "truncated": True,
                "preview": text[:MAX_RESULT_CHARS]}
    return result




def _unwrap(result):
    """Handle unwrap."""
    return result[0] if isinstance(result, tuple) else result


def _device_id(args):
    return (args.get("device_id") or "").strip()


def _t_device_list(host, args):
    r = host.devices_list()
    devs = (r or {}).get("devices") if isinstance(r, dict) else []
    out = [{"id": d.get("id"), "name": d.get("name"), "type": d.get("type"),
            "state": d.get("state") or "unknown",
            "ping_ms": d.get("ping_ms"), "remark": d.get("remark")}
           for d in devs]
    return {"ok": True, "devices": out}


def _t_device_check(host, args):
    did = _device_id(args)
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    return _unwrap(host.device_check(did))


def _t_device_sysinfo(host, args):
    did = _device_id(args)
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    r = _unwrap(host.device_sysinfo(did))
    data = r.get("data") if isinstance(r, dict) else None
    if isinstance(data, dict):
        r["data"] = {k: v for k, v in data.items() if v is not None}
    return r


def _t_diag(host, args):
    did = _device_id(args)
    kind = args.get("kind")
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    if kind not in ("video", "usb", "dmesg"):
        return {"ok": False,
                "error": "kind 必须是 video|usb|dmesg，实际: %r" % (kind,)}
    try:
        t = host._transport(did)
    except KeyError:
        return {"ok": False, "error": "设备不存在: %s" % did}
    except Exception as exc:
        return {"ok": False, "error": "设备不可达: %s" % (str(exc)[:200])}
    from host.service import diag as diag_svc
    try:
        if kind == "video":
            return diag_svc.video(t, device_id=did)
        if kind == "usb":
            return diag_svc.usb(t, device_id=did)
        try:
            lines = int(args.get("lines", 200))
        except (TypeError, ValueError):
            return {"ok": False, "error": "lines 必须是整数"}
        lines = max(1, min(lines, 200))
        return diag_svc.dmesg(t, lines=lines,
                              filter=args.get("filter"), device_id=did)
    except diag_svc.DiagError as exc:
        return {"ok": False, "error": str(exc)}


def _t_monitor(host, args):
    did = _device_id(args)
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    # Reuse the service owned by this HostApi.  Closing one portal must not
    # stop a second portal living in the same process.
    try:
        from host.api.handlers.monitor import _background_transport, _svc
        monitor_svc = _svc(host)
        monitor_svc.get_or_start(
            lambda: _background_transport(host, did), did)
        sample = monitor_svc.now(did)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": True, "sample": sample}


def _t_fs_list(host, args):
    did = _device_id(args)
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    query = {}
    path = args.get("path")
    if path:
        query["path"] = [str(path)]
    r = _unwrap(host.fs_list(did, query))
    if isinstance(r, dict) and isinstance(r.get("entries"), list):
        entries = r["entries"]
        if len(entries) > 200:
            r["entries"] = entries[:200]
            r["truncated"] = True
    return r




def _transport_or_error(host, did):
    """Handle transport or error."""
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    try:
        return host._transport(did)
    except KeyError:
        return {"ok": False, "error": "设备不存在: %s" % did}
    except Exception as exc:
        return {"ok": False, "error": "设备不可达: %s" % str(exc)[:200]}


def _host_temp_file(host, suffix=".aw"):
    """Handle host temp file."""
    store = getattr(host, "store", None)
    tf = getattr(store, "temp_file", None)
    if callable(tf):
        try:
            return tf(suffix)
        except Exception:
            pass
    fd, path = tempfile.mkstemp(prefix="rkss-agent-", suffix=suffix)
    os.close(fd)
    return path


def _t_exec_run(host, args):
    """Handle t exec run."""
    did = _device_id(args)
    cmd = str(args.get("cmd") or "").strip()
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    if not cmd:
        return {"ok": False, "error": "cmd 必填"}
    try:
        timeout = int(args.get("timeout") or 120)
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout 必须是整数"}
    timeout = max(1, min(timeout, 3600))
    r = _unwrap(host.exec_run(did, {"cmd": cmd, "timeout": timeout}))
    if not isinstance(r, dict):
        return {"ok": False, "error": "exec_run 返回异常"}
    if not r.get("ok"):
        return r
    jid = r.get("job_id") or ""
    if not jid:
        return {"ok": False, "error": "exec_run 未返回 job_id"}
    deadline = time.time() + min(timeout, 300) + 6
    while time.time() < deadline:
        try:



            p = _unwrap(host.exec_poll(did, {"job_id": [jid]}))
        except Exception as exc:
            return {"ok": False,
                    "error": "exec_poll 失败: %s" % str(exc)[:200]}
        if not isinstance(p, dict):
            return {"ok": False, "error": "exec_poll 返回异常"}
        if not p.get("ok"):
            return {"ok": True, "job_id": jid, "running": True,
                    "warning": p.get("error") or "poll 未返回 ok"}
        if not p.get("running"):
            return {"ok": True, "job_id": jid, "running": False,
                    "exit_code": p.get("exit_code"),
                    "truncated": bool(p.get("truncated")),
                    "output": (p.get("output") or "")[:MAX_RESULT_CHARS]}
        time.sleep(0.25)
    return {"ok": True, "job_id": jid, "running": True,
            "note": "命令仍在运行（等待 %ss），可再次 exec_run 查询状态或 "
                    "exec_kill 终止" % timeout}


def _t_exec_kill(host, args):
    did = _device_id(args)
    jid = str(args.get("job_id") or "").strip()
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    if not jid:
        return {"ok": False, "error": "job_id 必填"}
    return _unwrap(host.exec_kill(did, {"job_id": jid}))


def _t_fs_write(host, args):
    """Handle t fs write."""
    did = _device_id(args)
    path = str(args.get("path") or "").strip()
    content = args.get("content")
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    if not path:
        return {"ok": False, "error": "path 必填"}
    if content is None:
        return {"ok": False, "error": "content 必填"}
    if not isinstance(content, str):
        return {"ok": False,
                "error": "content 必须是字符串（当前: %s）" % type(content).__name__}
    t = _transport_or_error(host, did)
    if isinstance(t, dict):
        return t
    data = content.encode("utf-8")
    tmp = _host_temp_file(host, ".aw")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        with open(tmp, "rb") as fh:
            t.upload(fh, path, size_hint=len(data))
    except Exception as exc:
        return {"ok": False, "error": "写文件失败: %s" % str(exc)[:300]}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    size = len(data)
    try:
        st = t.stat(path)
        if isinstance(st, dict) and st.get("size") is not None:
            size = int(st["size"])
    except Exception:
        pass
    return {"ok": True, "path": path, "size": size}


def _t_fs_act(host, args):
    did = _device_id(args)
    action = str(args.get("action") or "").strip()
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    if action not in ("mkdir", "rm", "rename", "mv", "chmod"):
        return {"ok": False,
                "error": "action 必须是 mkdir|rm|rename|mv|chmod，实际: %r"
                         % (action,)}
    data = dict(args)
    data.pop("device_id", None)
    data.pop("action", None)
    return _unwrap(host.fs_act(did, action, data))


def _deploy_store(host):
    """Handle deploy store."""
    s = getattr(host, "_deploy_store", None)
    if s is None:
        from host.task.deployjob import DeployJobStore
        s = DeployJobStore(host)
        host._deploy_store = s
    return s


def _t_deploy_start(host, args):
    """Handle t deploy start."""
    did = _device_id(args)
    files = args.get("files")
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    if not isinstance(files, list) or not files:
        return {"ok": False, "error": "files 必须是非空数组"}
    target_dir = str(args.get("target_dir") or "").strip()
    cmd = str(args.get("cmd") or "").strip()
    entries = []
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            return {"ok": False, "error": "files[%d] 必须是对象" % i}
        src = str(f.get("src") or "").strip()
        dest = str(f.get("dest") or "").strip()
        mode = str(f.get("mode") or "").strip() or "0644"
        if not src:
            return {"ok": False, "error": "第 %d 个文件 src 必填" % (i + 1)}
        if not dest and target_dir:
            dest = target_dir.rstrip("/") + "/" + os.path.basename(src)
        if not dest:
            return {"ok": False, "error": "第 %d 个文件 dest 必填（或给 target_dir）"
                    % (i + 1)}
        entries.append({"src": src, "dest": dest, "mode": mode})
    try:
        timeout = int(args.get("timeout") or 60)
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout 必须是整数"}
    store = _deploy_store(host)
    try:
        r = store.plan(did, entries, cmd=cmd, timeout=timeout)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        job = store.start(r["plan_id"], did)
    except KeyError:
        return {"ok": False, "error": "部署计划不存在: %s" % r["plan_id"]}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "plan_id": r["plan_id"], "job": job}


def _t_process_signal(host, args):
    did = _device_id(args)
    sig = str(args.get("sig") or "").strip()
    if not did:
        return {"ok": False, "error": "device_id 必填"}
    try:
        pid = int(args.get("pid"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "pid 必须是整数"}
    if sig not in ("TERM", "KILL", "STOP", "CONT", "HUP"):
        return {"ok": False,
                "error": "sig 必须是 TERM|KILL|STOP|CONT|HUP，实际: %r" % (sig,)}
    t = _transport_or_error(host, did)
    if isinstance(t, dict):
        return t
    from host.service import proc as proc_svc
    try:
        return proc_svc.signal(t, pid, sig)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}




register(
    "device_list",
    "列出全部设备（id/name/type/state/ping_ms/remark 摘要，不返回连接凭据）",
    "read", _schema({}), _t_device_list)
register(
    "device_check",
    "检查指定设备的连通性（在线状态、ping_ms、基础信息）",
    "read",
    _schema({"device_id": {"type": "string",
                           "description": "设备 id（可从 device_list 获取）"}},
            ["device_id"]),
    _t_device_check)
register(
    "device_sysinfo",
    "采集指定设备的系统信息（CPU/内存/温度/负载等，None 字段省略）",
    "read",
    _schema({"device_id": {"type": "string"}}, ["device_id"]),
    _t_device_sysinfo)
register(
    "diag",
    "设备诊断：kind=video（视频设备）/ usb（USB 设备）/ dmesg（内核日志尾段）",
    "read",
    _schema({
        "device_id": {"type": "string"},
        "kind": {"type": "string", "enum": ["video", "usb", "dmesg"]},
        "lines": {"type": "integer", "minimum": 1, "maximum": 200,
                  "description": "dmesg 行数，默认 200（钳制 [1,200]）"},
        "filter": {"type": "string",
                   "description": "dmesg 大小写不敏感过滤子串，可选"},
    }, ["device_id", "kind"]),
    _t_diag)
register(
    "monitor",
    "获取指定设备的最新实时监控样本（CPU/内存/温度/负载/网络，未启动则启动采样）",
    "read",
    _schema({"device_id": {"type": "string"}}, ["device_id"]),
    _t_monitor)
register(
    "fs_list",
    "列出指定设备目录下的文件条目（最多返回 200 条）",
    "read",
    _schema({
        "device_id": {"type": "string"},
        "path": {"type": "string",
                 "description": "目录路径，缺省为设备根目录/home 目录"},
    }, ["device_id"]),
    _t_fs_list)



register(
    "exec_run",
    "在指定设备上执行命令（提交后轮询到终态，返回 job_id + 输出；"
    "命令超时/并发上限等由既有 exec 管线保证）",
    "write",
    _schema({
        "device_id": {"type": "string",
                      "description": "设备 id（可从 device_list 获取）"},
        "cmd": {"type": "string",
                "description": "要在设备上执行的 shell 命令"},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 3600,
                    "description": "命令超时秒数，默认 120"},
    }, ["device_id", "cmd"]),
    _t_exec_run)
register(
    "exec_kill",
    "终止指定设备上的一个执行任务（job_id 来自 exec_run）",
    "write",
    _schema({
        "device_id": {"type": "string"},
        "job_id": {"type": "string"},
    }, ["device_id", "job_id"]),
    _t_exec_kill)
register(
    "fs_write",
    "在指定设备上写一个文本文件（覆盖写；返回目标路径与字节数；"
    "危险路径由 pathguard 拒绝）",
    "write",
    _schema({
        "device_id": {"type": "string"},
        "path": {"type": "string",
                 "description": "设备上目标文件的绝对路径"},
        "content": {"type": "string",
                    "description": "文件内容（UTF-8 文本，如脚本）"},
    }, ["device_id", "path", "content"]),
    _t_fs_write)
register(
    "fs_act",
    "在指定设备上执行文件操作：mkdir（建目录）/ rm（删除，recursive 默认 true）"
    "/ rename（改文件名 new_name）/ mv（移动 dest）/ chmod（改权限 mode）",
    "write",
    _schema({
        "device_id": {"type": "string"},
        "action": {"type": "string",
                   "enum": ["mkdir", "rm", "rename", "mv", "chmod"]},
        "path": {"type": "string", "description": "目标路径"},
        "recursive": {"type": "boolean",
                      "description": "rm 时是否递归（默认 true）"},
        "new_name": {"type": "string", "description": "rename 新文件名"},
        "dest": {"type": "string", "description": "mv 目标路径"},
        "mode": {"type": "string", "description": "chmod 权限，如 0755"},
    }, ["device_id", "action", "path"]),
    _t_fs_act)
register(
    "deploy_start",
    "创建部署计划并启动：files=[{src(上位机路径), dest(设备路径), mode?}]；"
    "target_dir 可省 dest（拼 文件名）；cmd 可选（部署后执行）。"
    "src 不存在 / 危险 dest / timeout 越界会失败",
    "write",
    _schema({
        "device_id": {"type": "string"},
        "files": {"type": "array",
                  "items": {"type": "object", "properties": {
                      "src": {"type": "string",
                              "description": "上位机本地源文件绝对路径"},
                      "dest": {"type": "string",
                               "description": "设备目标路径（白名单目录）"},
                      "mode": {"type": "string",
                               "description": "权限，如 0755，默认 0644"}},
                      "required": ["src", "dest"]}},
        "target_dir": {"type": "string",
                       "description": "缺 dest 时的目标目录（拼文件名）"},
        "cmd": {"type": "string",
                "description": "部署完成后在设备上执行的命令"},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 3600,
                    "description": "执行超时秒数，默认 60"},
    }, ["device_id", "files"]),
    _t_deploy_start)
register(
    "process_signal",
    "向指定设备的进程发送信号（TERM/KILL/STOP/CONT/HUP；PID<=1 拒绝）",
    "write",
    _schema({
        "device_id": {"type": "string"},
        "pid": {"type": "integer", "minimum": 2,
                "description": "目标进程 PID（>1）"},
        "sig": {"type": "string",
                "enum": ["TERM", "KILL", "STOP", "CONT", "HUP"]},
    }, ["device_id", "pid", "sig"]),
    _t_process_signal)
