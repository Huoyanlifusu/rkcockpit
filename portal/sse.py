"""Utilities for portal.sse."""
import json
import socket
import threading
import time

MAX_CONNECTIONS = 16
KIND_CAPS = {"monitor": 8, "logcenter": 8, "agent": 4}
KINDS = ("monitor", "logcenter", "agent", "generic")
WRITE_TIMEOUT_S = 2.0
HEARTBEAT_S = 15.0
IDLE_TIMEOUT_S = 600.0


class SseConn:
    """Manage sse conn."""

    def __init__(self, wfile, sock=None, write_timeout=WRITE_TIMEOUT_S):
        self.wfile = wfile
        self.sock = sock
        self.write_timeout = float(write_timeout)
        self._lock = threading.Lock()
        self.created = time.time()
        self.last_write = time.time()
        self.kind = "generic"
        self.reject_reason = ""
        self.write_timed_out = False
        self._timeout_reported = False

    def write(self, payload, event=None):
        frame = []
        if event:
            frame.append("event: %s\n" % event)
        data = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"))
        frame.append("data: %s\n\n" % data)
        return self._emit("".join(frame).encode("utf-8"))

    def ping(self):
        return self._emit(b":ping\n\n")

    def _emit(self, data):
        """Handle emit."""
        with self._lock:
            old_timeout = None
            changed_timeout = False
            try:
                if self.sock is not None:
                    old_timeout = self.sock.gettimeout()
                    self.sock.settimeout(self.write_timeout)
                    changed_timeout = True
                self.wfile.write(data)
                self.wfile.flush()
                self.last_write = time.time()
                return True
            except (socket.timeout, TimeoutError):
                self.write_timed_out = True
                return False
            except Exception:
                return False
            finally:
                if changed_timeout:
                    try:
                        self.sock.settimeout(old_timeout)
                    except Exception:
                        pass

    def ping_if_stale(self, now=None):
        now = time.time() if now is None else now
        if now - self.last_write >= HEARTBEAT_S:
            return self.ping()
        return True


class SseHub:
    """Manage sse hub."""

    def __init__(self, max_connections=MAX_CONNECTIONS, kind_caps=None):
        self._max = int(max_connections)
        self._kind_caps = dict(KIND_CAPS if kind_caps is None else kind_caps)
        self._conns = {}
        self._by_kind = {}
        self._accepted = 0
        self._rejected = 0
        self._rejected_by_kind = {}
        self._write_timeouts = 0
        self._lock = threading.Lock()

    def add(self, conn, kind="generic"):
        """Create or update add."""
        kind = str(kind or "generic")
        if kind not in KINDS:
            kind = "generic"
        with self._lock:
            if id(conn) in self._conns:
                return True
            reason = ""
            if len(self._conns) >= self._max:
                reason = "total"
            cap = self._kind_caps.get(kind)
            if not reason and cap is not None and\
                    self._by_kind.get(kind, 0) >= cap:
                reason = "kind"
            if reason:
                conn.kind = kind
                conn.reject_reason = reason
                self._rejected += 1
                self._rejected_by_kind[kind] =\
                    self._rejected_by_kind.get(kind, 0) + 1
                return False
            conn.kind = kind
            conn.reject_reason = ""
            self._conns[id(conn)] = (conn, kind)
            self._by_kind[kind] = self._by_kind.get(kind, 0) + 1
            self._accepted += 1
            return True

    def remove(self, conn):
        with self._lock:
            item = self._conns.pop(id(conn), None)
            if item is None:
                return
            _stored, kind = item
            count = self._by_kind.get(kind, 0) - 1
            if count > 0:
                self._by_kind[kind] = count
            else:
                self._by_kind.pop(kind, None)
            if conn.write_timed_out and not conn._timeout_reported:
                conn._timeout_reported = True
                self._write_timeouts += 1

    def count(self, kind=None):
        with self._lock:
            if kind is not None:
                return self._by_kind.get(kind, 0)
            return len(self._conns)

    def stats(self):
        with self._lock:
            return {
                "active": len(self._conns),
                "active_by_kind": {
                    kind: self._by_kind.get(kind, 0) for kind in KINDS},
                "accepted": self._accepted,
                "rejected": self._rejected,
                "rejected_by_kind": {
                    kind: self._rejected_by_kind.get(kind, 0)
                    for kind in KINDS},
                "write_timeout": self._write_timeouts,
            }

    def broadcast(self, payload, event=None):
        """Handle broadcast."""
        with self._lock:
            conns = [item[0] for item in self._conns.values()]
        dead = [c for c in conns if not c.write(payload, event=event)]
        for c in dead:
            self.remove(c)

    def heartbeat(self):
        """Handle heartbeat."""
        with self._lock:
            conns = [item[0] for item in self._conns.values()]
        dead = [c for c in conns if not c.ping_if_stale()]
        for c in dead:
            self.remove(c)
        return len(dead)


HUB = SseHub()
