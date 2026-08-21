"""Utilities for host.task.execjob."""
import os
import signal
import subprocess
import threading
import time

from host.task.output_buffer import OutputBuffer

MAX_CONCURRENT = 8
MAX_OUTPUT = 512 * 1024
KEEP_SECONDS = 60
MAX_JOBS = 100

_GLOBAL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)


class ExecQueueFull(Exception):
    pass


def _signal_proc(proc, sig):
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (OSError, ProcessLookupError):
        try:
            proc.send_signal(sig)
        except Exception:
            pass


class ExecJobStore:
    def __init__(self, transport, tmp_dir=None):
        self.transport = transport
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._jobs = {}
        self._seq = 0
        self._closed = False

    def _new_id(self):
        self._seq += 1
        return "e%d" % self._seq

    def run(self, cmd, timeout=120):
        timeout = min(max(int(timeout or 120), 1), 3600)
        if not _GLOBAL_SLOTS.acquire(blocking=False):
            raise ExecQueueFull("exec 队列已满（全局并发上限 %d）" %
                                MAX_CONCURRENT)
        with self._cond:
            if self._closed:
                _GLOBAL_SLOTS.release()
                raise ExecQueueFull("exec 服务正在关闭")
            now = time.time()
            for jid in list(self._jobs):
                if not self._jobs[jid]["running"] and\
                        now - self._jobs[jid]["ended_ms"] > KEEP_SECONDS:
                    del self._jobs[jid]
            if len(self._jobs) >= MAX_JOBS:
                _GLOBAL_SLOTS.release()
                raise ExecQueueFull("exec 历史记录已达上限")
            jid = self._new_id()
            job = {
                "id": jid, "cmd": cmd, "running": True,
                "exit_code": None, "output": OutputBuffer(MAX_OUTPUT),
                "started_ms": int(time.time() * 1000), "ended_ms": 0,
                "timeout": timeout, "slot_released": False,
            }
            self._jobs[jid] = job

        def release_slot_locked():
            if not job["slot_released"]:
                job["slot_released"] = True
                _GLOBAL_SLOTS.release()

        try:
            proc = self.transport.open_cmd(cmd)
        except Exception as exc:
            with self._cond:
                job["running"] = False
                job["ended_ms"] = time.time()
                job["output"].append(str(exc).encode("utf-8", "replace"))
                release_slot_locked()
                self._cond.notify_all()
            return jid
        job["proc"] = proc

        def reader():
            try:
                while True:
                    chunk = proc.stdout.read(1 << 16)
                    if not chunk:
                        break
                    with self._cond:
                        job["output"].append(chunk)
            finally:
                try:
                    proc.wait()
                except Exception:
                    pass
                with self._cond:
                    job["running"] = False
                    job["exit_code"] = proc.returncode
                    job["ended_ms"] = time.time()
                    release_slot_locked()
                    self._cond.notify_all()
                if proc.returncode is None:
                    proc.kill()

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        def timer():
            deadline = time.time() + timeout
            while time.time() < deadline and job["running"]:
                time.sleep(0.5)
            if job["running"]:
                try:
                    _signal_proc(proc, signal.SIGTERM)
                except Exception:
                    pass
                time.sleep(3)
                if job["running"]:
                    try:
                        _signal_proc(proc, signal.SIGKILL)
                    except Exception:
                        pass

        threading.Thread(target=timer, daemon=True).start()
        return jid

    def poll(self, jid, offset=None):
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                raise KeyError(jid)
            output, next_offset, base_offset, reset =\
                job["output"].snapshot(offset, final=not job["running"])
            return {
                "job_id": jid,
                "running": job["running"],
                "exit_code": job["exit_code"],
                "output": output,
                "truncated": job["output"].truncated,
                "offset": next_offset,
                "base_offset": base_offset,
                "reset": reset,
                "started_ms": job["started_ms"],
            }

    def kill(self, jid):
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                raise KeyError(jid)
            proc = job.get("proc")
        if proc:
            try:
                _signal_proc(proc, signal.SIGTERM)
            except Exception:
                pass

            def hard_kill():
                time.sleep(3)
                if self._jobs.get(jid, {}).get("running"):
                    try:
                        _signal_proc(proc, signal.SIGKILL)
                    except Exception:
                        pass

            threading.Thread(target=hard_kill, daemon=True).start()
        return jid

    def running(self):
        with self._lock:
            return [{"job_id": jid, "cmd": j["cmd"],
                     "running": j["running"],
                     "started_ms": j["started_ms"]}
                    for jid, j in sorted(
                        self._jobs.items(),
                        key=lambda kv: kv[1]["started_ms"])]

    def close(self, timeout=5.0):
        """Stop accepting jobs and reap every active command process."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            self._closed = True
            procs = [job.get("proc") for job in self._jobs.values()
                     if job["running"] and job.get("proc")]
        for proc in procs:
            _signal_proc(proc, signal.SIGTERM)
        term_deadline = min(deadline, time.monotonic() + 2.0)
        with self._cond:
            while any(job["running"] for job in self._jobs.values()):
                remaining = term_deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            survivors = [job.get("proc") for job in self._jobs.values()
                         if job["running"] and job.get("proc")]
        for proc in survivors:
            _signal_proc(proc, signal.SIGKILL)
        with self._cond:
            while any(job["running"] for job in self._jobs.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            clean = not any(job["running"] for job in self._jobs.values())
        closer = getattr(self.transport, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                clean = False
        return clean
