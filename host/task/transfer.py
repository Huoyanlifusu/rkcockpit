"""Utilities for host.task.transfer."""
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from host.transport import TransportError

class TransferJobStore:
    MAX_RUNNING = 4
    MAX_KEEP = 50
    KEEP_SECONDS = 600

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._jobs = {}
        self._order = []
        self._futures = {}
        self._seq = 0
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=self.MAX_RUNNING, thread_name_prefix="rkss-xfer")

    def _new_id(self):
        self._seq += 1
        return "j%d_%d" % (int(time.time() * 1e6), self._seq)

    def submit(self, device_name, action, name, src, dest, fn, cleanup=None):
        with self._cond:
            if self._closed:
                raise TransportError("传输服务正在关闭")
            running = sum(1 for j in self._jobs.values()
                          if j["status"] == "running")
            if running >= self.MAX_RUNNING:
                raise TransportError("传输任务并发已达上限(%d)，请稍后再试"
                                     % self.MAX_RUNNING)
            job = {
                "id": self._new_id(),
                "device": device_name,
                "action": action,
                "name": name,
                "src": src,
                "dest": dest,
                "bytes_total": 0,
                "bytes_done": 0,
                "status": "running",
                "error": None,
                "cancelled": False,
                "proc": None,
                "cleanup": cleanup,
                "started_ms": int(time.time() * 1000),
                "updated_ms": int(time.time() * 1000),
            }
            self._jobs[job["id"]] = job
            self._order.append(job["id"])
            self._trim_locked()
            try:
                self._futures[job["id"]] = self._executor.submit(
                    self._run, job, fn)
            except Exception:
                self._jobs.pop(job["id"], None)
                self._order.remove(job["id"])
                raise
        return job

    def _run(self, job, fn):
        try:
            fn(job)
            with self._cond:
                job["status"] = "cancelled" if job["cancelled"] else "done"
                job["error"] = None if job["cancelled"] else job.get("error")
        except Exception as exc:
            with self._cond:
                job["status"] = "cancelled" if job["cancelled"] else "error"
                job["error"] = str(exc) if not job["cancelled"] else "已取消"
        finally:
            cleanup = job.get("cleanup")
            if cleanup:
                try:
                    cleanup(job)
                except Exception:
                    pass
            with self._cond:
                job["proc"] = None
                self._futures.pop(job["id"], None)
                job["updated_ms"] = int(time.time() * 1000)
                self._cond.notify_all()

    def _trim_locked(self):
        now = time.time()
        keep = [j for j in self._order
                if self._jobs[j]["status"] == "running" or
                now - self._jobs[j]["started_ms"] / 1000 < self.KEEP_SECONDS]
        self._order = keep[-self.MAX_KEEP:]
        for jid in list(self._jobs):
            if jid not in self._order:
                del self._jobs[jid]

    def cancel(self, jid):
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                raise KeyError(jid)
            if job["status"] == "running":
                job["cancelled"] = True
                job["updated_ms"] = int(time.time() * 1000)
                proc = job.get("proc")
                if proc:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        pass
        return job

    def list(self):
        with self._lock:
            out = []
            for j in sorted(self._jobs.values(),
                            key=lambda j: j["started_ms"], reverse=True):
                d = dict(j)
                d.pop("proc", None)
                d.pop("cleanup", None)
                out.append(d)
            return out

    @staticmethod
    def _signal(proc, sig):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (OSError, ProcessLookupError):
            try:
                proc.send_signal(sig)
            except Exception:
                pass

    def close(self, timeout=5.0):
        """Stop accepting work and reap every active transfer process."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            self._closed = True
            running = [j for j in self._jobs.values()
                       if j["status"] == "running"]
            cancelled_cleanups = []
            for job in running:
                job["cancelled"] = True
                future = self._futures.get(job["id"])
                if future is not None and future.cancel():
                    job["status"] = "cancelled"
                    job["error"] = "已取消"
                    job["updated_ms"] = int(time.time() * 1000)
                    self._futures.pop(job["id"], None)
                    if job.get("cleanup"):
                        cancelled_cleanups.append((job["cleanup"], job))
            procs = [j.get("proc") for j in running if j.get("proc")]
            self._cond.notify_all()
        for cleanup, job in cancelled_cleanups:
            try:
                cleanup(job)
            except Exception:
                pass
        for proc in procs:
            self._signal(proc, signal.SIGTERM)
        term_deadline = min(deadline, time.monotonic() + 2.0)
        with self._cond:
            while any(j["status"] == "running" for j in self._jobs.values()):
                remaining = term_deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            survivors = [j.get("proc") for j in self._jobs.values()
                         if j["status"] == "running" and j.get("proc")]
        for proc in survivors:
            self._signal(proc, signal.SIGKILL)
        with self._cond:
            while any(j["status"] == "running" for j in self._jobs.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            clean = not any(j["status"] == "running"
                            for j in self._jobs.values())
        self._executor.shutdown(wait=False, cancel_futures=True)
        return clean


class ProgressReader:
    """Manage progress reader."""

    def __init__(self, fh, job):
        self.fh = fh
        self.job = job

    def read(self, n):
        if self.job["cancelled"]:
            raise TransportError("任务已取消")
        buf = self.fh.read(n)
        if buf:
            self.job["bytes_done"] += len(buf)
            self.job["updated_ms"] = int(time.time() * 1000)
        return buf


class ProgressWriter:
    def __init__(self, fh, job):
        self.fh = fh
        self.job = job

    def write(self, buf):
        if self.job["cancelled"]:
            raise TransportError("任务已取消")
        self.fh.write(buf)
        self.job["bytes_done"] += len(buf)
        self.job["updated_ms"] = int(time.time() * 1000)
        return len(buf)
