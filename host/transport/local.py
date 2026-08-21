"""Local transport with descriptor-relative, no-symlink filesystem access."""
import errno
import contextlib
import os
import stat as statmod
import subprocess

from host.core.pathguard import DirRoot, local_components, reject_symlink,\
    rooted_components
from host.transport.base import Transport, TransportError, make_entry,\
    to_epoch_ms


class LocalTransport(Transport):
    kind = "local"

    def __init__(self, root=None):
        self.root = os.path.abspath(root) if root else None
        self.home = os.path.abspath(os.path.expanduser("~"))
        try:
            self._root = DirRoot(self.root) if self.root else None
            self._home = None if self.root else DirRoot(self.home)
            self._slash = None if self.root else DirRoot(os.sep)
        except ValueError as exc:
            raise TransportError(str(exc))

    def close(self):
        for anchor in (self._root, self._home, self._slash):
            if anchor is not None:
                anchor.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _location(self, path, write=False):
        try:
            if self._root is not None:
                return self._root, rooted_components(path)
            anchor, parts = local_components(path, self.home, write)
            return (self._home if anchor == self.home else self._slash), parts
        except ValueError as exc:
            raise TransportError(str(exc))

    @staticmethod
    def _error(action, exc):
        return TransportError("%s 失败: %s" % (action, exc))

    def exec(self, cmd, timeout=30):
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                  timeout=timeout)
            return proc.returncode, proc.stdout.decode("utf-8", "replace"),\
                proc.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return 124, "", "timeout after %ss" % timeout

    def open_cmd(self, cmd):
        return subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                start_new_session=True)

    @staticmethod
    def _entry(name, st):
        return make_entry(name, statmod.S_ISDIR(st.st_mode), st.st_size,
                          oct(st.st_mode & 0o7777)[2:],
                          to_epoch_ms(st.st_mtime))

    def listdir(self, path):
        anchor, parts = self._location(path)
        try:
            fd = anchor.open_dir(parts)
            try:
                out = []
                for name in os.listdir(fd):
                    try:
                        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        reject_symlink(st, name)
                        out.append(self._entry(name, st))
                    except OSError:
                        continue
                return sorted(out, key=lambda e: (not e["is_dir"],
                                                   e["name"].lower()))
            finally:
                os.close(fd)
        except OSError as exc:
            raise self._error("list", exc)

    def stat(self, path):
        anchor, parts = self._location(path)
        try:
            if not parts:
                st = os.fstat(anchor.fd)
                return self._entry(path or "/", st)
            parent, leaf = anchor.parent(parts)
            try:
                st = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                reject_symlink(st, leaf)
                return self._entry(leaf, st)
            finally:
                os.close(parent)
        except OSError as exc:
            raise self._error("stat", exc)

    def mkdir(self, path):
        anchor, parts = self._location(path, write=True)
        if not parts:
            raise TransportError("拒绝修改目录根")
        try:
            anchor.mkdirs(parts)
        except OSError as exc:
            raise self._error("mkdir", exc)

    def _remove_tree(self, parent, leaf):
        st = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        reject_symlink(st, leaf)
        if not statmod.S_ISDIR(st.st_mode):
            os.unlink(leaf, dir_fd=parent)
            return
        fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=parent)
        try:
            for child in os.listdir(fd):
                self._remove_tree(fd, child)
        finally:
            os.close(fd)
        os.rmdir(leaf, dir_fd=parent)

    def remove(self, path, recursive=True):
        anchor, parts = self._location(path, write=True)
        if not parts:
            raise TransportError("拒绝删除危险路径: %s" % path)
        try:
            parent, leaf = anchor.parent(parts)
            try:
                st = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                reject_symlink(st, leaf)
                if statmod.S_ISDIR(st.st_mode):
                    if recursive:
                        self._remove_tree(parent, leaf)
                    else:
                        os.rmdir(leaf, dir_fd=parent)
                else:
                    os.unlink(leaf, dir_fd=parent)
            finally:
                os.close(parent)
        except OSError as exc:
            raise self._error("remove", exc)

    @staticmethod
    def _safe_leaf(name):
        return bool(name) and os.sep not in name and name not in (".", "..")\
            and "\x00" not in name

    def rename(self, path, new_name):
        if not self._safe_leaf(new_name):
            raise TransportError("非法文件名: %r" % new_name)
        anchor, parts = self._location(path, write=True)
        if not parts:
            raise TransportError("拒绝修改目录根")
        try:
            parent, leaf = anchor.parent(parts)
            try:
                reject_symlink(os.stat(leaf, dir_fd=parent,
                                       follow_symlinks=False), leaf)
                try:
                    reject_symlink(os.stat(new_name, dir_fd=parent,
                                           follow_symlinks=False), new_name)
                except FileNotFoundError:
                    pass
                os.rename(leaf, new_name, src_dir_fd=parent,
                          dst_dir_fd=parent)
            finally:
                os.close(parent)
        except OSError as exc:
            raise self._error("rename", exc)

    def move(self, path, dest):
        src_anchor, src_parts = self._location(path, write=True)
        dst_anchor, dst_parts = self._location(dest, write=True)
        if src_anchor is not dst_anchor or not src_parts or not dst_parts:
            raise TransportError("拒绝跨边界移动")
        try:
            src_parent, src_leaf = src_anchor.parent(src_parts)
            try:
                dst_parent, dst_leaf = dst_anchor.parent(dst_parts)
                try:
                    reject_symlink(os.stat(src_leaf, dir_fd=src_parent,
                                           follow_symlinks=False), src_leaf)
                    try:
                        reject_symlink(os.stat(dst_leaf, dir_fd=dst_parent,
                                               follow_symlinks=False), dst_leaf)
                    except FileNotFoundError:
                        pass
                    os.rename(src_leaf, dst_leaf, src_dir_fd=src_parent,
                              dst_dir_fd=dst_parent)
                finally:
                    os.close(dst_parent)
            finally:
                os.close(src_parent)
        except OSError as exc:
            raise self._error("move", exc)

    def chmod(self, path, mode):
        anchor, parts = self._location(path, write=True)
        if not parts:
            raise TransportError("拒绝修改目录根")
        try:
            value = int(str(mode), 8)
            if value < 0 or value > 0o7777:
                raise ValueError("权限范围应为 0000..7777")
            parent, leaf = anchor.parent(parts)
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW |
                             getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
                try:
                    os.fchmod(fd, value)
                finally:
                    os.close(fd)
            finally:
                os.close(parent)
        except (ValueError, OSError) as exc:
            raise self._error("chmod", exc)

    def download(self, remote, fh, job=None):
        n = 0
        with self.open_read(remote) as src:
            while True:
                buf = src.read(1 << 16)
                if not buf:
                    break
                fh.write(buf)
                n += len(buf)
        return n

    @contextlib.contextmanager
    def open_read(self, path):
        """Open a regular file without following a path component."""
        anchor, parts = self._location(path)
        if not parts:
            raise TransportError("文件不存在: %s" % path)
        try:
            parent, leaf = anchor.parent(parts)
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            finally:
                os.close(parent)
            try:
                if not statmod.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, "不是普通文件", path)
                with os.fdopen(fd, "rb") as src:
                    fd = -1
                    yield src
            finally:
                if fd >= 0:
                    os.close(fd)
        except OSError as exc:
            raise self._error("open read", exc)

    def upload(self, fh, remote, size_hint=0, job=None):
        n = 0
        with self.open_write(remote) as dst:
            while True:
                buf = fh.read(1 << 16)
                if not buf:
                    break
                dst.write(buf)
                n += len(buf)
        return n

    @contextlib.contextmanager
    def open_write(self, path):
        """Create/truncate a file atomically relative to the trusted anchor."""
        anchor, parts = self._location(path, write=True)
        if not parts:
            raise TransportError("拒绝覆盖目录根")
        try:
            parent, leaf = anchor.parent(parts)
            try:
                fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                             os.O_NOFOLLOW, 0o666, dir_fd=parent)
            finally:
                os.close(parent)
            try:
                n = 0
                with os.fdopen(fd, "wb") as dst:
                    fd = -1
                    yield dst
            finally:
                if fd >= 0:
                    os.close(fd)
        except OSError as exc:
            raise self._error("open write", exc)
