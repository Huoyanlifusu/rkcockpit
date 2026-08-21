"""Bounded admission for HTTP upload body spooling."""
import os
import shutil
import threading
import time


class SpoolBusy(Exception):
    pass


class _Lease:
    def __init__(self, limiter, size):
        self._limiter = limiter
        self._size = size
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        self._limiter._release(self._size)


class UploadSpoolLimiter:
    """Reserve disk before reading a potentially multi-gigabyte body."""

    def __init__(self, directory, max_active=4, max_bytes=8 << 30,
                 min_free_bytes=512 << 20, stale_seconds=24 * 3600):
        self.directory = os.path.abspath(directory)
        self.max_active = int(max_active)
        self.max_bytes = int(max_bytes)
        self.min_free_bytes = int(min_free_bytes)
        self._lock = threading.Lock()
        self._active = 0
        self._reserved = 0
        self._cleanup_stale(float(stale_seconds))

    def _cleanup_stale(self, age):
        cutoff = time.time() - max(0.0, age)
        try:
            entries = list(os.scandir(self.directory))
        except OSError:
            return
        for entry in entries:
            try:
                if not entry.name.startswith("rkss-") or\
                        not entry.name.endswith(".up") or\
                        not entry.is_file(follow_symlinks=False) or\
                        entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                os.unlink(entry.path)
            except OSError:
                pass

    def acquire(self, size):
        size = int(size)
        if size <= 0 or size > self.max_bytes:
            raise SpoolBusy("上传大小超出落盘预算")
        with self._lock:
            try:
                free = shutil.disk_usage(self.directory).free
            except OSError as exc:
                raise SpoolBusy("无法确认上传临时空间: %s" % exc)
            if self._active >= self.max_active:
                raise SpoolBusy("并发上传落盘已达上限(%d)" % self.max_active)
            if self._reserved + size > self.max_bytes:
                raise SpoolBusy("上传临时空间预留已达上限")
            if free - self._reserved - size < self.min_free_bytes:
                raise SpoolBusy("磁盘剩余空间不足，已拒绝上传")
            self._active += 1
            self._reserved += size
            return _Lease(self, size)

    def _release(self, size):
        with self._lock:
            self._active = max(0, self._active - 1)
            self._reserved = max(0, self._reserved - int(size))

    def stats(self):
        with self._lock:
            return {"active": self._active, "reserved_bytes": self._reserved,
                    "max_active": self.max_active,
                    "max_bytes": self.max_bytes}
