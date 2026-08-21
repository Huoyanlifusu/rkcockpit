"""Utilities for host.audit.recorder."""
import json
import os
import queue
import sys
import threading
import time
from collections import deque


ACTIONS = frozenset((
    "exec.run", "exec.kill",
    "fs.rm", "fs.chmod", "fs.upload", "fs.download",
    "fs.copy", "fs.copyfrom", "fs.mkdir", "fs.rename", "fs.mv",
    "dev.add", "dev.update", "dev.delete",
    "proc.signal", "deploy.start", "deploy.stage",
    "groups.create", "groups.update", "groups.delete", "groups.exec",
    "logcenter.tail", "logcenter.follow", "logcenter.unfollow",
    "keys.generate", "keys.delete", "keys.install",
    "diag.stream_test",

    "agent.exec.run", "agent.exec.kill",
    "agent.fs.write", "agent.fs.act",
    "agent.deploy.start", "agent.signal",
))

MAX_RING = 5000
RETENTION_DAYS = 30
MAX_SCAN_LINES = 5000
AUDIT_QUEUE_SIZE = 4096
_WRITER_RETRY_MIN = 0.01
_WRITER_RETRY_MAX = 1.0
_ERROR_LOG_INTERVAL = 5.0


def now_ms():
    return int(time.time() * 1000)


def date_str(ts_ms):
    lt = time.localtime(ts_ms / 1000.0)
    return "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)


class AuditRecorder:
    def __init__(self, conf_dir, max_ring=MAX_RING,
                 retention_days=RETENTION_DAYS,
                 queue_size=AUDIT_QUEUE_SIZE):
        self.audit_dir = os.path.join(os.path.abspath(conf_dir), "audit")
        self.max_ring = max_ring
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending_cond = threading.Condition()
        self._ring = deque(maxlen=max_ring)
        self._seq = 0
        self._last_prune = ""
        self._queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self._pending = 0
        self._accepting = True
        self._stop = threading.Event()
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "enqueued": 0,
            "fallback": 0,
            "written": 0,
            "write_failure": 0,
            "unpersisted": 0,
            "invalid": 0,
            "degraded": False,
        }
        self._last_error_log = 0.0
        try:
            os.makedirs(self.audit_dir, exist_ok=True)
            self._ok = True
        except OSError as exc:
            self._ok = False
            self._mark_write_failure(exc)
        self._prune()
        self._writer = threading.Thread(
            target=self._writer_loop, name="rkss-audit-writer", daemon=True)
        self._writer.start()



    def record(self, event):
        """Handle record."""
        try:
            ev = dict(event or {})
            if not ev.get("action"):
                return False
            ev.setdefault("ts", now_ms())
            ev.setdefault("actor", "web")
            ev.setdefault("ip", "")
            ev.setdefault("result", "ok")
            ev.setdefault("err", "")
            ev.setdefault("detail", {})
            ev.setdefault("target", {})
            if not isinstance(ev["target"], dict):
                ev["target"] = {}
            if not isinstance(ev["detail"], dict):
                ev["detail"] = {}
            try:
                # The writer must never discover a deterministic schema error:
                # one permanently bad queue head would block every later event.
                # Validate and freeze the rotation shard before acceptance.
                if isinstance(ev["ts"], bool) or not isinstance(ev["ts"], int):
                    raise ValueError("audit ts must be an integer millisecond value")
                shard = date_str(ev["ts"])
            except (TypeError, ValueError, OverflowError, OSError) as exc:
                self._metric_add("invalid")
                sys.stderr.write("[audit] event 时间戳无效: %r\n" % exc)
                return False


            with self._state_lock:
                if not self._accepting:
                    return False
                with self._lock:
                    self._seq += 1
                    ev.setdefault("id", "a%d_%04d" % (ev["ts"], self._seq))
                try:
                    line = (json.dumps(ev, ensure_ascii=False) + "\n").encode(
                        "utf-8")
                    # A JSON round trip freezes nested target/detail objects;
                    # callers cannot mutate an accepted event after return.
                    frozen = json.loads(line.decode("utf-8"))
                except (TypeError, ValueError, UnicodeError) as exc:
                    self._metric_add("invalid")
                    sys.stderr.write("[audit] event 不可序列化: %r\n" % exc)
                    return False
                with self._lock:
                    self._ring.append(frozen)
                with self._pending_cond:
                    self._pending += 1
            item = (frozen, line, shard)
            try:
                self._queue.put_nowait(item)
                self._metric_add("enqueued")
            except queue.Full:
                self._metric_add("fallback")
                persisted = self._write_fallback_once(item)
                self._finish_pending()
                return persisted
            return True
        except Exception as exc:
            sys.stderr.write("[audit] record 失败: %r\n" % exc)
            return False

    def record_ok(self, action, target, detail=None, **kw):
        """Handle record ok."""
        self.record({"action": action, "target": target,
                     "detail": detail or {}, "result": "ok", **kw})

    def record_fail(self, action, target, detail=None, error="", **kw):
        """Handle record fail."""
        self.record({"action": action, "target": target,
                     "detail": detail or {}, "result": "fail",
                     "err": error or "", **kw})

    @staticmethod
    def _write_all(fd, data):
        """Handle write all."""
        view = memoryview(data)
        done = 0
        while done < len(view):
            n = os.write(fd, view[done:])
            if n is None or n <= 0:
                raise OSError("audit short write made no progress")
            done += n

    def _append(self, item):
        _ev, line, shard = item
        path = os.path.join(self.audit_dir, shard + ".jsonl")
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            self._write_all(fd, line)
        finally:
            os.close(fd)
        today = date_str(now_ms())
        if today != self._last_prune:
            self._last_prune = today
            self._prune()

    def _writer_loop(self):
        while not self._stop.is_set() or self._pending_count() > 0:
            try:
                ev = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._write_with_retry(ev)
            finally:
                self._queue.task_done()
                self._finish_pending()

    def _write_with_retry(self, ev):
        delay = _WRITER_RETRY_MIN
        while True:
            try:
                self._write_once(ev)
                return
            except Exception as exc:
                self._mark_write_failure(exc)
                time.sleep(delay)
                delay = min(delay * 2, _WRITER_RETRY_MAX)

    def _write_once(self, ev):
        with self._write_lock:
            os.makedirs(self.audit_dir, exist_ok=True)
            self._append(ev)
        self._ok = True
        with self._metrics_lock:
            self._metrics["written"] += 1
            # A transient writer failure is recovered after this write.  A
            # known synchronous-fallback loss remains degraded and visible.
            if self._metrics["unpersisted"] == 0:
                self._metrics["degraded"] = False

    def _write_fallback_once(self, ev):
        """Queue-full path: one write attempt, never an unbounded request stall."""
        try:
            self._write_once(ev)
            return True
        except Exception as exc:
            self._mark_write_failure(exc)
            self._metric_add("unpersisted")
            return False

    def _mark_write_failure(self, exc):
        now = time.monotonic()
        should_log = False
        with self._metrics_lock:
            self._metrics["write_failure"] += 1
            self._metrics["degraded"] = True
            if now - self._last_error_log >= _ERROR_LOG_INTERVAL:
                self._last_error_log = now
                should_log = True
        if should_log:
            sys.stderr.write("[audit] writer 落盘失败，将继续重试: %r\n" % exc)

    def _metric_add(self, name):
        with self._metrics_lock:
            self._metrics[name] += 1

    def _pending_count(self):
        with self._pending_cond:
            return self._pending

    def _finish_pending(self):
        with self._pending_cond:
            self._pending -= 1
            self._pending_cond.notify_all()

    def close(self, timeout=5.0):
        """Remove or stop close."""
        with self._state_lock:
            self._accepting = False
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._pending_cond:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._pending_cond.wait(remaining)
        self._stop.set()
        self._writer.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._metrics_lock:
            clean = self._metrics["unpersisted"] == 0
        return not self._writer.is_alive() and clean



    def query(self, filters=None):
        """Return query."""
        f = filters or {}
        out = []
        for ev in self._iter_events(f.get("from_ms"), f.get("to_ms"),
                                    cap=MAX_RING):
            if f.get("action") and ev.get("action") != f["action"]:
                continue
            if f.get("device") and\
                    ev.get("target", {}).get("id") != f["device"]:
                continue
            if f.get("result") and ev.get("result") != f["result"]:
                continue
            out.append(ev)
        return out

    def stats(self, days=7, include_runtime=False):
        """Handle stats."""
        days = max(1, min(int(days or 7), 90))
        lt = time.localtime(time.time())
        day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                            0, 0, 0, 0, 0, -1))
        from_ms = int((day0 - (days - 1) * 86400) * 1000)
        to_ms = now_ms()
        by_action = {}
        by_day = {}
        for ev in self._iter_events(from_ms, to_ms, cap=20000):
            a = ev.get("action") or "unknown"
            by_action[a] = by_action.get(a, 0) + 1
            d = date_str(ev["ts"])
            by_day[d] = by_day.get(d, 0) + 1
        dates = [date_str(int((day0 - (days - 1 - i) * 86400) * 1000))
                 for i in range(days)]
        by_day_list = [{"date": d, "count": by_day.get(d, 0)} for d in dates]
        result = {"by_action": by_action, "by_day": by_day_list,
                  "total": sum(by_day.values())}
        if include_runtime:
            result["runtime"] = self.runtime_stats()
        return result

    def runtime_stats(self):
        """Return fixed in-memory counters only; never scan audit history/disk."""
        with self._metrics_lock:
            runtime = dict(self._metrics)
        with self._state_lock:
            runtime["accepting"] = self._accepting
        runtime.update({
            "queue": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "pending": self._pending_count(),
        })
        return runtime



    def _iter_events(self, from_ms=None, to_ms=None, cap=MAX_RING):
        seen = set()
        with self._lock:
            ring = list(self._ring)
        for ev in reversed(ring):
            if from_ms is not None and ev["ts"] < from_ms:
                continue
            if to_ms is not None and ev["ts"] > to_ms:
                continue
            if ev["id"] in seen:
                continue
            seen.add(ev["id"])
            yield ev
            if len(seen) >= cap:
                return
        for d in self._dates_in_window(from_ms, to_ms):
            for ev in reversed(self._read_file(d)):
                if from_ms is not None and ev["ts"] < from_ms:
                    continue
                if to_ms is not None and ev["ts"] > to_ms:
                    continue
                if ev["id"] in seen:
                    continue
                seen.add(ev["id"])
                yield ev
                if len(seen) >= cap:
                    return

    def _dates_in_window(self, from_ms=None, to_ms=None):
        if from_ms is None and to_ms is None:
            return [date_str(now_ms())]
        if to_ms is None:
            to_ms = now_ms()
        if from_ms is None:
            from_ms = int((to_ms // 86400000 - 30) * 86400000)
        start, end = date_str(from_ms), date_str(to_ms)
        out, cur = [], start
        guard = 0
        while cur <= end and guard < 120:
            if os.path.isfile(os.path.join(self.audit_dir, cur + ".jsonl")):
                out.append(cur)
            lt = time.strptime(cur, "%Y-%m-%d")
            nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                               0, 0, 0, 0, 0, -1)) + 86400
            cur = date_str(int(nxt * 1000))
            guard += 1
        return out

    def _read_file(self, d):
        path = os.path.join(self.audit_dir, d + ".jsonl")
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()[-MAX_SCAN_LINES:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("id"):
                out.append(ev)
        return out



    def _prune(self):
        try:
            now = time.time()
            for fn in os.listdir(self.audit_dir):
                if not fn.endswith(".jsonl"):
                    continue
                d = fn[:-6]
                try:
                    lt = time.strptime(d, "%Y-%m-%d")
                except ValueError:
                    continue
                if now - time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                      0, 0, 0, 0, 0, -1)) >\
                        self.retention_days * 86400:
                    try:
                        os.remove(os.path.join(self.audit_dir, fn))
                    except OSError:
                        pass
        except OSError:
            pass
