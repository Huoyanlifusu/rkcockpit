"""Utilities for host.api.handlers.legacy."""
import json
import os
import platform
import re
import shutil
import sys
import threading
import time

from host.audit.recorder import AuditRecorder
from host.device.groups import get_group_store
from host.device.store import DeviceStore
from host.core.metrics import METRICS
from host.task.execjob import ExecJobStore, ExecQueueFull
from host.service.fs import fs_copy, fs_copyfrom
from host.task.transfer import TransferJobStore
from host.task.spool import SpoolBusy, UploadSpoolLimiter
from host.service.sysinfo import collect as collect_sysinfo
from host.transport import TransportError, make_transport
from host.transport.scheduler import TransportScheduler

HOST_API_VERSION = "0.2.0"

_LOCAL_DEVICE = {"id": "local", "name": "上位机本地",
                 "type": "local", "host": "", "port": 0,
                 "user": "", "auth": "", "has_password": False,
                 "remark": "上位机文件系统（双栏左栏）"}

_RE_DEV = re.compile(r"^/api/devices/([^/]+)$")
_RE_DEV_ACT = re.compile(r"^/api/devices/([^/]+)/(check|sysinfo)$")
_RE_FS = re.compile(r"^/api/fs/([^/]+)/([^/]+)$")
_RE_EXEC = re.compile(r"^/api/exec/([^/]+)/(run|poll|kill|running)$")
_RE_JOB = re.compile(r"^/api/jobs/([^/]+)/cancel$")


class HostApi:
    def __init__(self, conf_dir, sim=False, sim_remote=None):
        self.store = DeviceStore(conf_dir)
        self.audit = AuditRecorder(conf_dir)
        self._audit_tls = threading.local()
        self.jobs = TransferJobStore()
        self.upload_spool = UploadSpoolLimiter(self.store.tmp_dir)
        self.exec_stores = {}          # device_id -> ExecJobStore
        self._exec_stores_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._cleanup_callbacks = []   # [(remove_device, close), ...]
        self._closed = False
        self.ssh_control_dir = os.path.join(conf_dir, "ssh-control")
        self.ssh_scheduler = TransportScheduler()
        self.register_cleanup(self.ssh_scheduler.remove_device, None)
        self.register_cleanup(None, self._close_exec_stores)
        self.register_cleanup(None, self.jobs.close)
        self.sim = sim
        if sim:
            root = sim_remote or os.path.join(conf_dir, "demo-remote")
            os.makedirs(root, exist_ok=True)
            if "demo" not in self.store._devices:
                self.store.add({
                    "id": "demo",
                    "name": "demo（模拟板卡）", "type": "local",
                    "host": "", "local_root": root,
                    "remark": "--sim 自动注册的演示设备，可自由增删",
                })

    def register_cleanup(self, remove_device=None, close=None):
        """Register idempotent device-removal and process-close callbacks."""
        entry = (remove_device, close)
        call_close = None
        with self._cleanup_lock:
            if self._closed:
                call_close = close
            elif entry not in self._cleanup_callbacks:
                self._cleanup_callbacks.append(entry)
        if call_close is not None:
            call_close()

    def _cleanup_device(self, did):
        with self._cleanup_lock:
            callbacks = list(self._cleanup_callbacks)
        for remove_device, _close in callbacks:
            if remove_device is None:
                continue
            try:
                remove_device(did)
            except Exception as exc:
                sys.stderr.write("[lifecycle] remove_device 失败: %r\n" % exc)

    def _close_exec_stores(self):
        with self._exec_stores_lock:
            stores = list(self.exec_stores.values())
        ok = True
        for store in stores:
            try:
                if store.close(timeout=5.0) is False:
                    ok = False
            except Exception as exc:
                ok = False
                sys.stderr.write("[lifecycle] exec close 失败: %r\n" % exc)
        return ok

    def close(self):
        """Idempotently stop registered services, then drain audit."""
        with self._cleanup_lock:
            if self._closed:
                return True
            self._closed = True
            callbacks = list(self._cleanup_callbacks)
        ok = True
        for _remove_device, close in reversed(callbacks):
            if close is None:
                continue
            try:
                if close() is False:
                    ok = False
            except Exception as exc:
                ok = False
                sys.stderr.write("[lifecycle] close 失败: %r\n" % exc)
        try:
            if self.audit.close(timeout=5.0) is False:
                ok = False
        except Exception as exc:
            ok = False
            sys.stderr.write("[audit] close 失败: %r\n" % exc)
        return ok

    def _device(self, did):
        if did == "local":
            return _LOCAL_DEVICE
        return self.store.get(did)

    def _transport(self, did, workload="foreground"):
        dev = self._device(did)
        if did == "local":
            from host.transport import LocalTransport
            return LocalTransport()
        return make_transport(
            dev, control_dir=self.ssh_control_dir,
            scheduler=self.ssh_scheduler, device_id=did, workload=workload)

    def _exec_store(self, did):
        with self._exec_stores_lock:
            if self._closed:
                raise TransportError("服务正在关闭")
            es = self.exec_stores.get(did)
            if es is None:
                es = ExecJobStore(self._transport(did))
                self.exec_stores[did] = es
            return es



    def _audit(self, action, target, ok, detail=None, err=""):
        ip = getattr(self._audit_tls, "ip", "") or ""
        _audit_record(self, ip, action, target,
                      "ok" if ok else "fail", detail, err)



    def devices_list(self, query=None):


        if query:
            v = query.get("group")
            if v and v[0]:
                name = v[0]
                try:
                    g = get_group_store(self.store.conf_dir).get(name)
                except KeyError:
                    return {"ok": True, "devices": []}
                ids = set(g["device_ids"])
                return {"ok": True, "devices": [
                    d for d in self.store.list() if d["id"] in ids]}
        return {"ok": True, "devices": self.store.list()}

    def devices_add(self, data):
        try:
            dev = self.store.add(data)
        except (ValueError, KeyError) as exc:
            self._audit("dev.add", {"kind": "device", "id": data.get("id") or ""},
                        False, data, str(exc))
            return {"ok": False, "error": str(exc)}, 400
        self._audit("dev.add", {"kind": "device", "id": dev["id"]}, True,
                    {"name": dev.get("name"), "type": dev.get("type")})
        return {"ok": True, "device": dev}, 201

    def devices_update(self, did, data):
        try:
            dev = self.store.update(did, data)
        except KeyError:
            self._audit("dev.update", {"kind": "device", "id": did}, False,
                        data, "设备不存在: %s" % did)
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        except (ValueError, TypeError) as exc:
            self._audit("dev.update", {"kind": "device", "id": did}, False,
                        data, str(exc))
            return {"ok": False, "error": str(exc)}, 400
        self._audit("dev.update", {"kind": "device", "id": did}, True, data)
        return {"ok": True, "device": dev}

    def devices_delete(self, did):
        try:
            self.store.delete(did)
        except KeyError:
            self._audit("dev.delete", {"kind": "device", "id": did}, False,
                        None, "设备不存在: %s" % did)
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        try:

            get_group_store(self.store.conf_dir).remove_device(did)
        except Exception as exc:
            sys.stderr.write("[groups] remove_device 失败: %r\n" % exc)
        with self._exec_stores_lock:
            exec_store = self.exec_stores.pop(did, None)
        if exec_store is not None:
            exec_store.close(timeout=5.0)
        self._cleanup_device(did)
        self._audit("dev.delete", {"kind": "device", "id": did}, True, None)
        return {"ok": True, "deleted": did}

    def device_check(self, did):
        try:
            dev = self._device(did)
        except KeyError:
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        if dev["type"] == "local":
            info = {"hostname": platform.node(),
                    "os": platform.system() + " " + platform.release(),
                    "kernel": platform.release(),
                    "model": platform.machine()}
            self.store.set_state(did, "online", 0, info)
            return {"ok": True, "state": "online", "ping_ms": 0, "info": info}
        try:
            t = self._transport(did)
            t0 = time.time()
            rc, out, err = t.exec(
                "echo __H__$(hostname)__H__$(cat /etc/os-release 2>/dev/null |"
                " grep -m1 PRETTY_NAME | cut -d= -f2 | tr -d '\"' )__H__"
                "$(uname -r)__H__$(cat /proc/uptime | cut -d' ' -f1)__H__"
                "$(cat /proc/device-tree/model 2>/dev/null ||"
                " getprop ro.board.platform 2>/dev/null)", 10)
            ping = int((time.time() - t0) * 1000)
            if rc != 0:
                raise TransportError((err or out).strip()[:200])
            fields = out.split("__H__")
            info = {
                "hostname": fields[1].strip() if len(fields) > 1 else None,
                "os": fields[2].strip() if len(fields) > 2 else None,
                "kernel": fields[3].strip() if len(fields) > 3 else None,
                "uptime_s": int(float(fields[4].strip()))
                if len(fields) > 4 and fields[4].strip().replace(".", "").isdigit()
                else None,
                "model": fields[5].strip() if len(fields) > 5 else None,
            }
            self.store.set_state(did, "online", ping, info)
            return {"ok": True, "state": "online", "ping_ms": ping, "info": info}
        except Exception as exc:
            self.store.set_state(did, "offline", None)
            return {"ok": False, "state": "offline", "error": str(exc)}

    def device_sysinfo(self, did):
        try:
            dev = self._device(did)
        except KeyError:
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        try:
            t = self._transport(did)
            return {"ok": True, "data": collect_sysinfo(t)}
        except Exception as exc:
            return {"ok": True, "data": {"error": str(exc)}}



    def fs_list(self, did, query):
        path = (query.get("path") or [None])[0]
        try:
            t = self._transport(did)
            entries = t.listdir(path) if path is not None else\
                t.listdir("~" if did == "local" else "/")
            return {"ok": True, "path": path or
                    ("~" if did == "local" else "/"),
                    "entries": entries}
        except KeyError:
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        except TransportError as exc:
            return {"ok": False, "error": str(exc)}, 400

    def fs_act(self, did, action, data):
        try:
            t = self._transport(did)
        except KeyError:
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        except TransportError as exc:
            return {"ok": False, "error": str(exc)}, 400
        audit_action = "fs." + action
        target = {"kind": "file", "id": did,
                  "path": data.get("path") or data.get("src") or ""}
        detail = dict(data or {})
        detail.pop("password", None)
        try:
            if action == "mkdir":
                t.mkdir(data.get("path") or "")
                self._audit(audit_action, target, True, detail)
                return {"ok": True, "path": data.get("path")}
            if action == "rm":
                t.remove(data.get("path") or "", bool(data.get("recursive", True)))
                self._audit(audit_action, target, True, detail)
                return {"ok": True, "path": data.get("path")}
            if action == "rename":
                t.rename(data.get("path") or "", data.get("new_name") or "")
                self._audit(audit_action, target, True, detail)
                return {"ok": True}
            if action == "mv":
                t.move(data.get("path") or "", data.get("dest") or "")
                self._audit(audit_action, target, True, detail)
                return {"ok": True}
            if action == "chmod":
                t.chmod(data.get("path") or "", str(data.get("mode") or ""))
                self._audit(audit_action, target, True, detail)
                return {"ok": True}
            if action == "copy":
                job = fs_copy(self.jobs, t, self._device(did)["name"],
                              data.get("src") or "", data.get("dest") or "")
                detail["job"] = job["id"]
                self._audit(audit_action, target, True, detail)
                return {"ok": True, "job": job["id"]}
            if action == "copyfrom":
                job = fs_copyfrom(self.jobs, t, self._device(did)["name"],
                                  data.get("src") or "", data.get("dest") or "")
                detail["job"] = job["id"]
                self._audit(audit_action, target, True, detail)
                return {"ok": True, "job": job["id"]}
            return {"ok": False, "error": "未知操作: %s" % action}, 400
        except KeyError:
            self._audit(audit_action, target, False, detail, "设备不存在")
            return {"ok": False, "error": "设备不存在"}, 404
        except TransportError as exc:
            self._audit(audit_action, target, False, detail, str(exc))
            return {"ok": False, "error": str(exc)}, 400
        except OSError as exc:
            self._audit(audit_action, target, False, detail, str(exc))
            return {"ok": False, "error": str(exc)}, 400



    def exec_run(self, did, data):
        cmd = data.get("cmd") or ""
        timeout = data.get("timeout") or 120
        try:
            es = self._exec_store(did)
            jid = es.run(cmd, timeout)
        except KeyError:
            self._audit("exec.run", {"kind": "device", "id": did}, False,
                        {"cmd": cmd, "timeout": timeout}, "设备不存在: %s" % did)
            return {"ok": False, "error": "设备不存在: %s" % did}, 404
        except ExecQueueFull as exc:
            self._audit("exec.run", {"kind": "device", "id": did}, False,
                        {"cmd": cmd, "timeout": timeout}, str(exc))
            return {"ok": False, "error": str(exc)}, 429
        self._audit("exec.run", {"kind": "device", "id": did}, True,
                    {"cmd": cmd, "timeout": timeout, "job_id": jid})
        return {"ok": True, "job_id": jid}

    def exec_poll(self, did, query):
        try:
            es = self._exec_store(did)
            jid = (query.get("job_id") or [""])[0]
            if not jid:
                return {"ok": False, "error": "job_id 必填"}, 400
            raw_offset = (query.get("offset") or [None])[0]
            offset = None
            if raw_offset is not None:
                raw_offset = str(raw_offset)
                if re.fullmatch(r"[0-9]+", raw_offset) is None:
                    return {"ok": False, "error": "offset 必须为非负整数"}, 400
                offset = int(raw_offset)
            try:
                result = es.poll(jid, offset=offset)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}, 400
            if offset is None:
                METRICS.increment("poll_legacy")
            else:
                METRICS.increment("poll_delta")
                retained = max(0, result["offset"] - result["base_offset"])
                sent = len(result["output"].encode("utf-8"))
                METRICS.increment("poll_wire_bytes_saved",
                                  max(0, retained - sent))
            return {"ok": True, **result}
        except KeyError:
            return {"ok": False, "error": "job 不存在"}, 404

    def exec_kill(self, did, data):
        jid = data.get("job_id") or ""
        try:
            es = self._exec_store(did)
            jid = es.kill(jid)
        except KeyError:
            self._audit("exec.kill", {"kind": "proc", "id": did}, False,
                        {"job_id": jid}, "job 不存在")
            return {"ok": False, "error": "job 不存在"}, 404
        self._audit("exec.kill", {"kind": "proc", "id": did}, True,
                    {"job_id": jid})
        return {"ok": True, "killed": jid}

    def exec_running(self, did):
        try:
            es = self._exec_store(did)
            return {"ok": True, "jobs": es.running()}
        except KeyError:
            return {"ok": False, "error": "设备不存在: %s" % did}, 404


def _read_body(handler):
    if handler.headers.get("Transfer-Encoding"):
        return handler._send(400, {"ok": False,
                                   "error": "chunked uploads are not supported"})
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return handler._send(400, {"ok": False,
                                   "error": "invalid Content-Length"})
    if length <= 0:
        return {}
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except ValueError:
        return {}


def _query(query, key, default=None):
    v = query.get(key)
    return v[0] if v else default


def _scrub(detail):
    """Handle scrub."""
    out = dict(detail or {})
    for k in ("password", "_password", "_password_b64"):
        out.pop(k, None)
    return out


def _audit_record(host, ip, action, target, result, detail=None, err=""):
    """Handle audit record."""
    try:
        host.audit.record({
            "action": action,
            "target": target or {},
            "detail": _scrub(detail),
            "result": result,
            "err": err or "",
            "ip": ip or "",
        })
    except Exception as exc:
        sys.stderr.write("[audit] record 失败: %r\n" % exc)


def host_api_dispatch(handler, host, method, path, query):
    """Handle host api dispatch."""
    try:
        host._audit_tls.ip = handler.client_address[0]
    except Exception:
        pass

    def send(payload, code=200):
        if isinstance(payload, tuple):
            payload, code = payload
        handler._send(code, payload)
        return True

    if method == "GET" and path == "/api/host":
        return send({
            "ok": True, "name": platform.node(),
            "platform": platform.platform(),
            "system": platform.system(), "release": platform.release(),
            "python": sys.version.split()[0],
            "conf_dir": host.store.conf_dir,
            "has_sshpass": bool(shutil.which("sshpass")),
            "has_adb": bool(shutil.which("adb")),
            "version": HOST_API_VERSION,
        })

    if method == "GET" and path == "/api/devices":
        return send(host.devices_list(query))

    if method == "POST" and path == "/api/devices":
        payload, code = host.devices_add(_read_body(handler))
        return send(payload, code)

    m = _RE_DEV_ACT.match(path)
    if m and method == "POST":
        did, act = m.group(1), m.group(2)
        if act == "check":
            return send(host.device_check(did))
        if act == "sysinfo":
            return send(host.device_sysinfo(did))

    m = _RE_DEV.match(path)
    if m:
        did = m.group(1)
        if method == "PUT":
            return send(host.devices_update(did, _read_body(handler)))
        if method == "DELETE":
            return send(host.devices_delete(did))

    m = _RE_FS.match(path)
    if m:
        did, action = m.group(1), m.group(2)
        if method == "GET" and action == "list":
            return send(host.fs_list(did, query))
        if method == "GET" and action == "download":
            return _fs_download(handler, host, did, query)
        if method == "POST":
            if action == "upload":
                return _fs_upload(handler, host, did, query)
            return send(host.fs_act(did, action, _read_body(handler)))

    m = _RE_EXEC.match(path)
    if m:
        did, action = m.group(1), m.group(2)
        if method == "GET" and action == "poll":
            return send(host.exec_poll(did, query))
        if method == "GET" and action == "running":
            return send(host.exec_running(did))
        if method == "POST" and action == "run":
            return send(host.exec_run(did, _read_body(handler)))
        if method == "POST" and action == "kill":
            return send(host.exec_kill(did, _read_body(handler)))

    if method == "GET" and path == "/api/jobs":
        return send({"ok": True, "jobs": host.jobs.list()})

    m = _RE_JOB.match(path)
    if m and method == "POST":
        try:
            job = host.jobs.cancel(m.group(1))
            return send({"ok": True, "cancelled": job["id"]})
        except KeyError:
            return send({"ok": False, "error": "任务不存在"}, 404)

    return False


def _fs_download(handler, host, did, query):
    path = _query(query, "path")
    if not path:
        return handler._send(400, {"ok": False, "error": "path 必填"})
    try:
        t = host._transport(did)
    except (KeyError, TransportError) as exc:
        _audit_record(host, handler.client_address[0], "fs.download",
                      {"kind": "file", "id": did, "path": path}, "fail",
                      {}, str(exc))
        return handler._send(404, {"ok": False, "error": str(exc)})
    name = os.path.basename(path.rstrip("/")) or "download"
    try:
        st = t.stat(path)
    except Exception:
        st = None
    size = st.get("size") if st else None
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        from urllib.parse import quote
        handler.send_header("Content-Disposition",
                            "attachment; filename=\"%s\"" % name)
        if size:
            handler.send_header("Content-Length", str(size))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        t.download(path, handler.wfile)
        _audit_record(host, handler.client_address[0], "fs.download",
                      {"kind": "file", "id": did, "path": path},
                      "ok", {"name": name, "size": size})
        return True
    except BrokenPipeError:
        return True
    except Exception as exc:
        _audit_record(host, handler.client_address[0], "fs.download",
                      {"kind": "file", "id": did, "path": path},
                      "fail", {}, str(exc))
        try:
            handler._send(500, {"ok": False, "error": str(exc)})
        except Exception:
            pass
        return True


def _fs_upload(handler, host, did, query):
    path = _query(query, "path") or "/"
    name = _query(query, "name")
    if not name:
        return handler._send(400, {"ok": False, "error": "name 必填"})
    try:
        t = host._transport(did)
        dev = host._device(did)
    except (KeyError, TransportError) as exc:
        _audit_record(host, handler.client_address[0], "fs.upload",
                      {"kind": "file", "id": did, "path": path}, "fail",
                      {"name": name}, str(exc))
        return handler._send(404, {"ok": False, "error": str(exc)})

    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        _audit_record(host, handler.client_address[0], "fs.upload",
                      {"kind": "file", "id": did, "path": path}, "fail",
                      {"name": name}, "空 body")
        return handler._send(400, {"ok": False, "error": "空 body"})
    if length > 2 << 30:
        _audit_record(host, handler.client_address[0], "fs.upload",
                      {"kind": "file", "id": did, "path": path}, "fail",
                      {"name": name, "size": length}, "单文件超过 2GB")
        return handler._send(413, {"ok": False, "error": "单文件超过 2GB"})



    try:
        spool_lease = host.upload_spool.acquire(length)
    except SpoolBusy as exc:
        handler.close_connection = True
        _audit_record(host, handler.client_address[0], "fs.upload",
                      {"kind": "file", "id": did, "path": path}, "fail",
                      {"name": name, "size": length}, str(exc))
        return handler._send(503, {"ok": False, "code": "upload_spool_busy",
                                   "error": str(exc)},
                             headers=(("Retry-After", "2"),))
    tmp = None
    handed_off = False
    try:
        tmp = host.store.temp_file(".up")
        left = length
        with open(tmp, "wb") as fh:
            while left > 0:
                buf = handler.rfile.read(min(1 << 16, left))
                if not buf:
                    break
                fh.write(buf)
                left -= len(buf)
        if left:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            spool_lease.release()
            _audit_record(host, handler.client_address[0], "fs.upload",
                          {"kind": "file", "id": did, "path": path}, "fail",
                          {"name": name, "received": length - left,
                           "expected": length}, "上传 body 提前结束")
            return handler._send(400, {"ok": False,
                                       "error": "上传 body 提前结束"})
        size = os.path.getsize(tmp)

        def cleanup(job):
            try:
                if tmp:
                    os.remove(tmp)
            except OSError:
                pass
            finally:
                spool_lease.release()

        dest = os.path.join(path.rstrip("/"), os.path.basename(name))
        job = fs_copy(host.jobs, t, dev["name"], tmp, dest,
                      total_hint=size, cleanup=cleanup)
        handed_off = True
        _audit_record(host, handler.client_address[0], "fs.upload",
                      {"kind": "file", "id": did, "path": dest}, "ok",
                      {"name": name, "size": size, "job": job["id"]})
        return handler._send(200, {"ok": True, "size": size, "job": job["id"]})
    except TransportError as exc:
        _audit_record(host, handler.client_address[0], "fs.upload",
                      {"kind": "file", "id": did, "path": path}, "fail",
                      {"name": name}, str(exc))
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        spool_lease.release()
        return handler._send(400, {"ok": False, "error": str(exc)})
    except Exception as exc:
        _audit_record(host, handler.client_address[0], "fs.upload",
                      {"kind": "file", "id": did, "path": path}, "fail",
                      {"name": name}, str(exc))
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        spool_lease.release()
        return handler._send(500, {"ok": False, "error": str(exc)})
    finally:
        if not handed_off:
            spool_lease.release()
