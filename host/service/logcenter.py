"""Utilities for host.service.logcenter."""
import re
import shlex
import subprocess
import threading
import time
from collections import deque

MAX_TAIL_BYTES = 256 * 1024
MAX_LINES = 5000
MAX_BUF_BYTES = 1024 * 1024
MAX_LINE_BYTES = 8 * 1024
POLL_SECONDS = 1.0
MAX_NEW_PER_TICK = 2000

FILE_SOURCES = (
    ("syslog", "/var/log/syslog"),
    ("messages", "/var/log/messages"),
    ("dmesg", "/var/log/dmesg"),
)
JOURNAL_SOURCE = "journalctl"


def _match(line, pattern):
    """Handle match."""
    if not pattern:
        return True
    try:
        return re.search(pattern, line) is not None
    except re.error:
        return pattern in line


class _LineBuffer:
    """Manage line buffer."""

    def __init__(self, max_lines=MAX_LINES, max_bytes=MAX_BUF_BYTES):
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._buf = deque(maxlen=max_lines)
        self._bytes = 0
        self._seq = 0

    def push(self, ts, line):
        line = line[:MAX_LINE_BYTES]
        with self._lock:
            self._buf.append((int(ts), line))
            self._bytes += len(line)
            self._seq += 1
            while self._bytes > self.max_bytes and len(self._buf) > 1:
                _ts0, ln0 = self._buf.popleft()
                self._bytes -= len(ln0)

    def snapshot(self, n=200):
        with self._lock:
            return list(self._buf)[-n:]

    def since(self, seq):
        """Handle since."""
        with self._lock:
            buf = list(self._buf)
            cur = self._seq
        start = cur - len(buf) + 1
        return cur, [(start + i, ts, text)
                     for i, (ts, text) in enumerate(buf) if start + i > seq]

    def __len__(self):
        with self._lock:
            return len(self._buf)


class _Follower:
    """Manage follower."""

    def __init__(self, device_id, transport, source, pattern):
        self.device_id = device_id
        self.transport = transport
        self.source = source
        self.pattern = pattern
        self.started_ms = int(time.time() * 1000)
        self._stop = threading.Event()
        self._thread = None
        self._proc_lock = threading.Lock()
        self._proc = None
        self.buffer = _LineBuffer()

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="logcenter-%s" % self.device_id,
            daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.kill()          # unblock journalctl stdout.readline()
            except Exception:
                pass
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout=5)

    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def info(self):
        return {"device_id": self.device_id, "source": self.source,
                "filter": self.pattern, "started_ms": self.started_ms,
                "alive": self.alive(), "lines": len(self.buffer)}

    def _run(self):
        if self.source == JOURNAL_SOURCE:
            self._run_journal()
        else:
            self._run_poll()

    def _run_poll(self):
        path = self.source
        last = 0
        while not self._stop.is_set():
            try:
                rc, out, _err = self.transport.exec(
                    "wc -l <%s 2>/dev/null" % shlex.quote(path), 10)
                total = int(out.strip()) if rc == 0 and\
                    out.strip().isdigit() else None
                if total is not None:
                    if total < last:
                        last = 0
                    if total > last:
                        first = last + 1
                        if total - last > MAX_NEW_PER_TICK:
                            first = total - MAX_NEW_PER_TICK + 1
                        rc2, out2, _e2 = self.transport.exec(
                            "tail -n +%d %s 2>/dev/null" %
                            (first, shlex.quote(path)), 10)
                        if rc2 == 0:
                            for ln in out2.splitlines():
                                if _match(ln, self.pattern):
                                    self.buffer.push(time.time() * 1000, ln)
                        last = total
            except Exception:
                pass
            self._stop.wait(POLL_SECONDS)

    def _run_journal(self):
        """Handle run journal."""
        try:
            proc = self.transport.open_cmd(
                "journalctl -f -n 0 -u rkss-capture.service --no-pager 2>/dev/null")
        except Exception:
            self._stop.wait(POLL_SECONDS)
            return
        with self._proc_lock:
            self._proc = proc
        try:
            while not self._stop.is_set():
                raw = proc.stdout.readline()
                if not raw:
                    self._stop.wait(POLL_SECONDS)
                    continue
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line and _match(line, self.pattern):
                    self.buffer.push(time.time() * 1000, line)
        except Exception:
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Keep the scheduler lease until the OS confirms terminal,
                # without making service shutdown wait without bound.
                threading.Thread(
                    target=self._reap, args=(proc,),
                    name="logcenter-reaper-%s" % self.device_id,
                    daemon=True).start()
            except Exception:
                pass
            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None

    @staticmethod
    def _reap(proc):
        try:
            proc.wait()
        except Exception:
            pass


class LogCenter:
    """Manage log center."""

    def __init__(self, host=None):
        self.host = host
        self._lock = threading.Lock()
        self._followers = {}
        self._closed = False



    def sources(self, transport):
        out = [{"name": n, "path": p,
                "accessible": self._readable(transport, p)}
               for n, p in FILE_SOURCES]
        out.append({"name": "journalctl", "path": JOURNAL_SOURCE,
                    "accessible": self._has_cmd(transport, "journalctl")})
        return out

    def default_source(self, transport):
        """Handle default source."""
        for s in self.sources(transport):
            if s["accessible"] and s["path"] != JOURNAL_SOURCE:
                return s["path"]
        return None

    def _readable(self, transport, path):
        try:
            rc, out, _err = transport.exec(
                "test -r %s && echo YES" % shlex.quote(path), 10)
            return rc == 0 and "YES" in out
        except Exception:
            return False

    def _has_cmd(self, transport, name):
        try:
            rc, out, _err = transport.exec(
                "command -v %s >/dev/null 2>&1 && echo YES" % name, 10)
            return rc == 0 and "YES" in out
        except Exception:
            return False

    # ---- tail ----

    def tail(self, transport, source, lines=200, filter=None):
        try:
            lines = int(lines)
        except (TypeError, ValueError):
            raise ValueError("lines 参数非法: %r" % (lines,))
        lines = max(1, min(lines, MAX_LINES))
        pattern = (filter or "").strip() or None
        source = (source or "").strip()
        if not source:
            raise ValueError("source 必填")
        out = self._tail_raw(transport, source, lines)
        if not out.strip():
            return {"ok": True, "source": source, "lines": []}
        if len(out) > MAX_TAIL_BYTES:
            out = out[-MAX_TAIL_BYTES:]
        all_lines = out.splitlines()
        if pattern:
            all_lines = [ln for ln in all_lines if _match(ln, pattern)]
        return {"ok": True, "source": source, "lines": all_lines[-lines:]}

    def _tail_raw(self, transport, source, lines):
        if source == JOURNAL_SOURCE:
            try:
                rc, out, _err = transport.exec(
                    "journalctl -n %d -u rkss-capture.service --no-pager 2>&1"
                    % lines, 15)
            except Exception:
                return ""
            return out if rc == 0 else ""
        cmd = "tail -n %d %s" % (lines, shlex.quote(source))
        try:
            rc, out, err = transport.exec(cmd, 15)
        except Exception:
            return ""
        if rc == 0 or out.strip():
            return out
        if "invalid option" in (err or "") or\
                "usage" in (err or "").lower():

            try:
                rc2, out2, _e2 = transport.exec(
                    "tail -%d %s" % (lines, shlex.quote(source)), 15)
            except Exception:
                return ""
            return out2 if rc2 == 0 else ""
        return ""

    # ---- follow / unfollow ----

    def follow(self, device_id, transport, source, filter=None):
        pattern = (filter or "").strip() or None
        if not source:
            source = self.default_source(transport)
            if not source:
                raise ValueError("没有可访问的日志源，请显式指定 source")
        with self._lock:
            if self._closed:
                raise RuntimeError("logcenter service is closed")
            old = self._followers.pop(device_id, None)
            if old is not None:
                old.stop()
            f = _Follower(device_id, transport, source, pattern)
            f.start()
            self._followers[device_id] = f
        return f

    def unfollow(self, device_id):
        with self._lock:
            f = self._followers.pop(device_id, None)
            if f is not None:
                f.stop()
        return True

    def remove_device(self, device_id):
        """Stop and forget a device follower during device deletion."""
        return self.unfollow(device_id)

    def close(self):
        """Idempotently stop all followers and reject future follows."""
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            followers = list(self._followers.values())
            self._followers.clear()
        for follower in followers:
            follower.stop()
        return True

    def running(self):
        with self._lock:
            return [f.info() for f in self._followers.values()]

    def snapshot(self, device_id, lines=200):
        with self._lock:
            f = self._followers.get(device_id)
        if f is None:
            return []
        return f.buffer.snapshot(lines)
