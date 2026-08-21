"""Utilities for host.task.deployjob."""
import os
import re
import threading
import time

from host.task.transfer import ProgressReader, TransferJobStore
from host.transport import TransportError

_MODE_RE = re.compile(r"^0?[0-7]{3}$")


_DEST_WL_EXACT = ("/usr/bin", "/usr/local/bin")
_DEST_WL_TOP = ("opt", "home", "data", "userdata", "tmp", "root")
OUTPUT_TAIL = 4096
KEEP_JOBS = 20


def check_dest(dest):
    """Handle check dest."""
    d = os.path.normpath((dest or "").strip())
    if not d.startswith("/") or d == "/":
        raise ValueError("危险 dest（/ 或非绝对路径）: %r" % dest)
    for wl in _DEST_WL_EXACT:
        if d == wl or d.startswith(wl + "/"):
            return d
    top = d.lstrip("/").split("/", 1)[0]
    if top not in _DEST_WL_TOP:
        raise ValueError("危险 dest（白名单外目录）: %s" % dest)
    return d


class DeployJobStore:
    def __init__(self, host):
        self._host = host
        self._lock = threading.Lock()
        self._plans = {}
        self._jobs = {}
        self._history = []
        self._progs = {}



        self._xfer = getattr(host, "jobs", None) or TransferJobStore()



    def _device(self, did):
        return self._host._device(did)

    def _transport(self, did):
        return self._host._transport(did)

    def _exec_store(self, did):
        return self._host._exec_store(did)



    def plan(self, device_id, files, cmd="", timeout=60, restart=False):
        """Handle plan."""
        if not isinstance(files, list) or not files:
            raise ValueError("files 必须是非空数组")
        if timeout in (None, ""):
            timeout = 60
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise ValueError("timeout 非法: %r" % timeout)
        if not 1 <= timeout <= 3600:
            raise ValueError("timeout 必须在 1..3600 秒")
        entries, sizes = [], {}
        for i, f in enumerate(files):
            src = str((f or {}).get("src") or "").strip()
            dest = str((f or {}).get("dest") or "").strip()
            mode = str((f or {}).get("mode") or "").strip()
            if not src or not os.path.isfile(src):
                raise ValueError("第 %d 个文件 src 不存在: %s" % (i + 1, src))
            if dest.endswith("/"):
                dest += os.path.basename(src)
            dest = check_dest(dest)
            if not _MODE_RE.match(mode):
                raise ValueError(
                    "第 %d 个文件 mode 非法: %r（须为 0-7 组成的 3~4 位八进制）"
                    % (i + 1, mode))
            st = os.stat(src)
            sizes[src] = (st.st_size, st.st_mtime)
            entries.append({"src": src, "dest": dest, "mode": mode})
        plan_id = "p%d" % int(time.time() * 1e6)
        plan = {
            "plan_id": plan_id, "device_id": device_id, "files": entries,
            "cmd": str(cmd or "").strip(), "timeout": timeout,
            "restart": bool(restart),
            "created_ms": int(time.time() * 1000), "_sizes": sizes,
        }
        with self._lock:
            self._plans[plan_id] = plan
        return {"ok": True, "plan_id": plan_id}



    def start(self, plan_id, did=None):
        """Handle start."""
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan:
                raise KeyError(plan_id)
            if did is not None and plan["device_id"] != did:
                raise KeyError(plan_id)
            if self._jobs.get(plan_id, {}).get("state") == "running":
                raise ValueError("部署计划 %s 正在执行中" % plan_id)
            did = plan["device_id"]
            dev = self._device(did)
            for src, (size, mtime) in plan["_sizes"].items():
                if not os.path.isfile(src):
                    raise ValueError(
                        "源文件已被删除（计划后不可修改，请重新 plan）: %s" % src)
                st = os.stat(src)
                if st.st_size != size or abs(st.st_mtime - mtime) > 0.01:
                    raise ValueError(
                        "源文件已变更（计划后不可修改，请重新 plan）: %s" % src)
            now_ms = int(time.time() * 1000)
            total = sum(s for s, _ in plan["_sizes"].values())
            job = {
                "id": plan_id, "type": "deploy",
                "device_id": did, "device_name": dev.get("name") or did,
                "state": "running",
                "progress": {"bytes_total": total, "bytes_done": 0},
                "stages": self._build_stages(plan),
                "result": {"exit_code": None, "output_tail": "",
                           "truncated": False},
                "error": None,
                "created_ms": now_ms, "started_ms": 0, "ended_ms": 0,
                "updated_ms": now_ms,
            }
            self._jobs[plan_id] = job
            self._progs[plan_id] = {"cancelled": False, "bytes_done": 0,
                                    "updated_ms": now_ms}
            if plan_id in self._history:
                self._history.remove(plan_id)
            self._history.insert(0, plan_id)
            del self._history[KEEP_JOBS:]

            for old in [pid for pid in self._jobs
                        if pid not in self._history and
                        self._jobs[pid]["state"] != "running"]:
                del self._jobs[old]
        threading.Thread(target=self._run, args=(plan_id,),
                         name="deploy-%s" % plan_id, daemon=True).start()
        return self._snapshot(job)

    @staticmethod
    def _build_stages(plan):
        """Handle build stages."""
        stages = []
        for f in plan["files"]:
            name = os.path.basename(f["dest"].rstrip("/")) or f["dest"]
            stages.append({"name": "upload", "file": name,
                           "state": "pending", "detail": ""})
            stages.append({"name": "chmod", "file": name,
                           "state": "pending", "detail": ""})
        if plan["cmd"]:
            stages.append({"name": "exec", "file": None,
                           "state": "pending", "detail": ""})
        if plan["restart"]:
            stages.append({"name": "restart", "file": None,
                           "state": "pending", "detail": ""})
        return stages

    def _run(self, plan_id):
        with self._lock:
            job = self._jobs.get(plan_id)
        if job is None:
            return
        plan = self._plans.get(plan_id)
        did = plan["device_id"]
        job["started_ms"] = int(time.time() * 1000)
        prog = self._progs.get(plan_id) or {"cancelled": False,
                                            "bytes_done": 0}
        try:
            transport = self._transport(did)
        except Exception as exc:
            self._abort(job, plan, "传输通道初始化失败: %s" % exc)
            return
        nfiles = len(plan["files"])
        fail_msg, cancelled = None, False

        def touch():
            job["updated_ms"] = int(time.time() * 1000)

        for i, st in enumerate(job["stages"]):
            if prog.get("cancelled"):
                cancelled = True
                break
            st["state"] = "running"
            touch()
            rc = None
            try:
                if st["name"] == "upload":
                    f = plan["files"][i // 2]
                    before = prog.get("bytes_done", 0)
                    self._upload(job, f, transport, prog)
                    st["detail"] = "上传 %d 字节 -> %s" % (
                        prog.get("bytes_done", 0) - before, f["dest"])
                    st["state"] = "done"
                elif st["name"] == "chmod":
                    f = plan["files"][(i - 1) // 2]
                    try:
                        transport.chmod(f["dest"], f["mode"])
                        st["state"] = "done"
                        st["detail"] = "chmod %s" % f["mode"]
                    except TransportError as exc:
                        if "不支持" in str(exc):
                            st["state"] = "skipped"
                            st["detail"] = str(exc)
                        else:
                            raise
                elif st["name"] == "exec":
                    rc, tail, truncated = self._run_exec(
                        did, plan["cmd"], plan["timeout"])
                    job["result"]["exit_code"] = rc
                    job["result"]["output_tail"] = tail
                    job["result"]["truncated"] = truncated
                    if rc == 0:
                        st["state"] = "done"
                        st["detail"] = "exit 0"
                    else:
                        st["state"] = "failed"
                        st["detail"] = "exit %d" % rc
                        fail_msg = "exec 阶段失败: exit code %d" % rc
                else:
                    st["state"] = "skipped"
                    st["detail"] = "重启为 stub，未执行（P1 提供）"
            except Exception as exc:
                if prog.get("cancelled"):
                    cancelled = True
                    st["state"] = "failed"
                    st["detail"] = "任务已取消"
                else:
                    fail_msg = "%s 阶段失败: %s" % (st["name"], exc)
                    st["state"] = "failed"
                    st["detail"] = str(exc)
            self._audit_stage(did, plan, st, rc)
            touch()
            if st["state"] == "failed" and not cancelled:
                break


        if cancelled:
            job["state"] = "cancelled"
            job["error"] = "已取消"
            for s in job["stages"]:
                if s["state"] == "pending":
                    s["state"] = "skipped"
                    s["detail"] = "已取消"
        elif fail_msg:
            job["state"] = "error"
            job["error"] = fail_msg
            for s in job["stages"]:
                if s["state"] == "pending":
                    s["state"] = "failed"
                    s["detail"] = "因前一阶段失败未执行"
        else:
            job["state"] = "done"
        job["ended_ms"] = int(time.time() * 1000)
        touch()
        self._audit_start(did, plan, job)
        with self._lock:
            self._progs.pop(plan_id, None)

    def _upload(self, job, f, transport, prog):
        """Handle upload."""
        if prog.get("cancelled"):
            raise TransportError("任务已取消")
        total = os.path.getsize(f["src"])
        store = self._xfer

        def run(tjob):
            tjob["bytes_total"] = total
            with open(f["src"], "rb") as fh:
                transport.upload(ProgressReader(fh, tjob), f["dest"],
                                 total, job=tjob)

        tjob = store.submit(job.get("device_name") or f["dest"], "upload",
                            os.path.basename(f["dest"]),
                            f["src"], f["dest"], run)
        prog["_tjob"] = tjob


        if prog.get("cancelled"):
            try:
                store.cancel(tjob["id"])
            except KeyError:
                pass
        try:
            while tjob["status"] == "running" and not prog.get("cancelled"):
                time.sleep(0.05)
        finally:
            prog.pop("_tjob", None)
        if prog.get("cancelled") or tjob["status"] == "cancelled":
            raise TransportError("任务已取消")
        if tjob["status"] == "error":
            raise TransportError(tjob["error"] or "上传失败")
        job["progress"]["bytes_done"] = (prog.get("bytes_done", 0)
                                         + tjob["bytes_done"])
        prog["bytes_done"] = job["progress"]["bytes_done"]
        job["updated_ms"] = int(time.time() * 1000)

    def _run_exec(self, did, cmd, timeout):
        """Handle run exec."""
        es = self._exec_store(did)
        jid = es.run(cmd, timeout)
        deadline = time.time() + timeout + 10
        while time.time() < deadline:
            p = es.poll(jid)
            if not p["running"]:
                rc = p["exit_code"]
                out = p["output"] or ""
                if rc is None:
                    raise TransportError((out.strip() or "exec 启动失败")[:200])
                tail = out[-OUTPUT_TAIL:]
                truncated = p["truncated"] or len(out) > OUTPUT_TAIL
                return rc, tail, truncated
            time.sleep(0.2)
        es.kill(jid)
        raise TransportError("exec 超时（%ss）" % timeout)

    def _abort(self, job, plan, msg):
        job["state"] = "error"
        job["error"] = msg
        for s in job["stages"]:
            if s["state"] == "pending":
                s["state"] = "failed"
                s["detail"] = "因部署失败未执行"
        job["ended_ms"] = int(time.time() * 1000)
        job["updated_ms"] = int(time.time() * 1000)
        self._audit_start(plan["device_id"], plan, job)



    def cancel(self, plan_id, did=None):
        """Remove or stop cancel."""
        with self._lock:
            job = self._jobs.get(plan_id)
            if not job:
                raise KeyError(plan_id)
            if did is not None and job["device_id"] != did:
                raise KeyError(plan_id)
            if job["state"] != "running":
                return {"ok": True, "cancelled": False}
            prog = self._progs.get(plan_id)
            if prog is not None:
                prog["cancelled"] = True
                tjob = prog.get("_tjob")
                if tjob is not None:
                    try:
                        self._xfer.cancel(tjob["id"])
                    except KeyError:
                        pass
            job["updated_ms"] = int(time.time() * 1000)
        return {"ok": True, "cancelled": True}

    def get(self, plan_id, did=None):
        with self._lock:
            job = self._jobs.get(plan_id)
            if not job:
                raise KeyError(plan_id)
            if did is not None and job["device_id"] != did:
                raise KeyError(plan_id)
            return self._snapshot(job)

    def list(self):
        """Return list."""
        with self._lock:
            return [self._snapshot(self._jobs[pid])
                    for pid in self._history if pid in self._jobs]

    @staticmethod
    def _snapshot(job):
        out = dict(job)
        out["stages"] = [dict(s) for s in job["stages"]]
        return out



    def _audit(self, ok, action, did, detail, err=""):
        try:
            rec = getattr(self._host, "audit", None)
            if rec is None:
                return
            target = {"kind": "deploy", "id": did}
            if ok:
                rec.record_ok(action, target, detail)
            else:
                rec.record_fail(action, target, detail, error=err)
        except Exception:
            pass

    def _audit_stage(self, did, plan, st, rc=None):
        ok = st["state"] != "failed"
        self._audit(ok, "deploy.stage", did, {
            "plan_id": plan["plan_id"], "stage": st["name"],
            "file": st.get("file") or "", "rc": rc,
        }, err="" if ok else st["detail"])

    def _audit_start(self, did, plan, job):
        ok = job["state"] in ("done", "cancelled")
        self._audit(ok, "deploy.start", did, {
            "plan_id": plan["plan_id"], "files": len(plan["files"]),
            "cmd": plan["cmd"] or "", "timeout": plan["timeout"],
            "restart": plan["restart"],
        }, err=job.get("error") or "")
