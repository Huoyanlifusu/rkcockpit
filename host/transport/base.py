"""Utilities for host.transport.base."""


class TransportError(Exception):
    """Error raised for transport error."""


def to_epoch_ms(mtime):
    return int(mtime * 1000)


def make_entry(name, is_dir, size, mode, mtime_ms):
    return {"name": name, "is_dir": bool(is_dir), "size": int(size),
            "mode": mode, "mtime_ms": int(mtime_ms)}


class Transport:
    kind = "base"

    def exec(self, cmd, timeout=30):
        raise NotImplementedError

    def open_cmd(self, cmd):
        """Handle open cmd."""
        raise NotImplementedError

    def listdir(self, path):
        raise NotImplementedError

    def stat(self, path):
        raise NotImplementedError

    def mkdir(self, path):
        raise NotImplementedError

    def remove(self, path, recursive=True):
        raise NotImplementedError

    def rename(self, path, new_name):
        raise NotImplementedError

    def move(self, path, dest):
        raise NotImplementedError

    def chmod(self, path, mode):
        raise NotImplementedError

    def download(self, remote, fh, job=None):
        """Handle download."""
        raise NotImplementedError

    def upload(self, fh, remote, size_hint=0, job=None):
        """Handle upload."""
        raise NotImplementedError
