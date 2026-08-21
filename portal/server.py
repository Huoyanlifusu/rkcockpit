"""Bounded stdlib HTTP server used by rkss-portal."""
import json
import queue
import socket
import threading
import time
from http.server import ThreadingHTTPServer

from host.core.metrics import METRICS


DEFAULT_MAX_WORKERS = 64
DEFAULT_IDLE_TIMEOUT = 30.0


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with admission control before thread creation."""

    daemon_threads = True
    request_queue_size = 128

    def __init__(self, server_address, handler_class,
                 max_workers=DEFAULT_MAX_WORKERS,
                 idle_timeout=DEFAULT_IDLE_TIMEOUT):
        max_workers = int(max_workers)
        idle_timeout = float(idle_timeout)
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if idle_timeout <= 0:
            raise ValueError("idle_timeout must be > 0")
        self.max_workers = max_workers
        self.idle_timeout = idle_timeout
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self._active_cond = threading.Condition()
        self._active_requests = 0
        self._admission_lock = threading.Lock()
        self._work = queue.Queue(maxsize=max_workers)
        self._workers_closing = False
        self.metrics_cache_lock = threading.Lock()
        self.metrics_cache_time = 0.0
        self.metrics_cache_value = None
        METRICS.gauge_set("http_max_workers", max_workers)
        super().__init__(server_address, handler_class)
        self._workers = []
        for index in range(max_workers):
            worker = threading.Thread(
                target=self._worker_loop, name="rkss-http-%d" % index,
                daemon=True)
            worker.start()
            self._workers.append(worker)

    def process_request(self, request, client_address):
        admitted = False
        with self._admission_lock:
            if not self._workers_closing and self._worker_slots.acquire(
                    blocking=False):
                METRICS.gauge_add("http_active", 1)
                with self._active_cond:
                    self._active_requests += 1
                try:
                    request.settimeout(self.idle_timeout)
                    self._work.put_nowait((request, client_address))
                    admitted = True
                except Exception:
                    METRICS.gauge_add("http_active", -1)
                    with self._active_cond:
                        self._active_requests -= 1
                        self._active_cond.notify_all()
                    self._worker_slots.release()
                    self.shutdown_request(request)
                    raise
        if not admitted:
            METRICS.increment("http_rejected")
            METRICS.observe_http_status(503)
            self._reject_busy(request)

    def _worker_loop(self):
        while True:
            try:
                item = self._work.get(timeout=0.1)
            except queue.Empty:
                if self._workers_closing:
                    return
                continue
            try:
                request, client_address = item
                self.process_request_thread(request, client_address)
            finally:
                self._work.task_done()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            METRICS.gauge_add("http_active", -1)
            with self._active_cond:
                self._active_requests -= 1
                self._active_cond.notify_all()
            self._worker_slots.release()

    def drain(self, timeout=5.0):
        """Wait for admitted handlers after accepting has stopped."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._active_cond:
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_cond.wait(remaining)
        if self._workers_closing:
            for worker in self._workers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                worker.join(remaining)
            if any(worker.is_alive() for worker in self._workers):
                return False
        return True

    def server_close(self):
        with self._admission_lock:
            self._workers_closing = True
        super().server_close()

    def _reject_busy(self, request):
        body = json.dumps({
            "ok": False,
            "code": "server_busy",
            "error": "server concurrency limit reached; retry later",
        }, separators=(",", ":")).encode("utf-8")
        head = (
            "HTTP/1.1 503 Service Unavailable\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Content-Length: %d\r\n"
            "Retry-After: 1\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n" % len(body)
        ).encode("ascii")
        try:
            request.sendall(head + body)
        except (OSError, socket.error):
            pass
        finally:
            self.shutdown_request(request)
