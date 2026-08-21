"""Utilities for host.transport.adb."""
import os
import shutil
import subprocess
import tempfile
import stat

from host.transport.base import Transport, TransportError
from host.transport.ssh import _clear_job, _parse_ls, _terminate_reap


class AdbTransport(Transport):
    kind = "adb"

    def __init__(self, serial, tmp_dir=None):
        self.serial = serial
        self.tmp_dir = tmp_dir or os.path.join(
            os.path.expanduser("~"), ".rkss", "tmp")
        self.adb = shutil.which("adb")
        if not self.adb:
            raise TransportError(
                "未找到 adb。请安装：apt install android-tools-adb（RK3588 Debian）。")

    def _base(self):
        return [self.adb, "-s", self.serial]

    def _private_tmp_dir(self):
        path = os.path.abspath(os.path.expanduser(self.tmp_dir))
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise TransportError("ADB 临时目录必须由当前用户拥有")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(path, 0o700)
        return path

    def _shq(self, s):
        return "'" + str(s).replace("'", "'\\''") + "'"

    def _shell(self, cmd, timeout=30):
        try:
            proc = subprocess.run(self._base() + ["shell", cmd],
                                  capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "timeout after %ss" % timeout
        return proc.returncode, proc.stdout.decode("utf-8", "replace"),\
            proc.stderr.decode("utf-8", "replace")

    def exec(self, cmd, timeout=30):
        return self._shell(cmd, timeout)

    def open_cmd(self, cmd):
        return subprocess.Popen(self._base() + ["shell", cmd],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                start_new_session=True)

    def listdir(self, path):

        rc, out, err = self._shell("LC_ALL=C ls -lan %s" % self._shq(path), 15)
        if rc != 0:
            return []
        entries = _parse_ls(out.splitlines())
        if not entries and out.strip() and "No such file" not in out:
            raise TransportError("ls 解析失败（原始输出）:\n%s" % out[:400])
        return entries

    def stat(self, path):
        rc, out, err = self._shell("LC_ALL=C ls -lan %s" % self._shq(path), 10)
        if rc != 0:
            raise TransportError("stat 失败: %s" % (err or out).strip())
        entries = _parse_ls(out.splitlines())
        for e in entries:
            if e["name"] == os.path.basename(path.rstrip("/")):
                return e
        return {"name": os.path.basename(path.rstrip("/")) or path,
                "is_dir": False, "size": 0, "mode": "0000", "mtime_ms": 0}

    def mkdir(self, path):
        rc, out, err = self._shell("mkdir -p %s" % self._shq(path), 10)
        if rc != 0:
            raise TransportError("mkdir 失败: %s" % (err or out).strip())

    def remove(self, path, recursive=True):
        cmd = "rm -rf -- %s" if recursive else "rmdir -- %s"
        rc, out, err = self._shell(cmd % self._shq(path), 20)
        if rc != 0:
            raise TransportError("删除失败: %s" % (err or out).strip())

    def rename(self, path, new_name):
        if not new_name or "/" in new_name or new_name in (".", ".."):
            raise TransportError("非法文件名: %r" % new_name)
        dest = os.path.join(os.path.dirname(path.rstrip("/")), new_name)
        self.move(path, dest)

    def move(self, path, dest):
        rc, out, err = self._shell("mv -- %s %s" % (self._shq(path),
                                                    self._shq(dest)), 20)
        if rc != 0:
            raise TransportError("移动失败: %s" % (err or out).strip())

    def chmod(self, path, mode):
        """Handle chmod."""
        rc, out, err = self._shell("chmod %s %s" % (mode, self._shq(path)), 10)
        if rc != 0:
            raise TransportError("chmod 失败: %s" % (err or out).strip())

    def _run_transfer(self, argv, timeout, job):
        if job is None:
            return subprocess.run(argv, capture_output=True, timeout=timeout)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                start_new_session=True)
        job["proc"] = proc
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_reap(proc)
                raise
            return subprocess.CompletedProcess(
                argv, proc.returncode, stdout, stderr)
        except BaseException:
            if proc.poll() is None:
                _terminate_reap(proc)
            raise
        finally:
            _clear_job(job, proc)

    def _pull(self, remote, fh, job=None):
        directory = self._private_tmp_dir()
        fd, tmp = tempfile.mkstemp(prefix="adb-pull-", dir=directory)
        os.close(fd)
        try:
            proc = self._run_transfer(
                self._base() + ["pull", remote, tmp], 600, job)
            if proc.returncode != 0:
                raise TransportError("adb pull 失败: %s" %
                                     proc.stderr.decode("utf-8", "replace"))
            n = 0
            with open(tmp, "rb") as src:
                while True:
                    buf = src.read(1 << 16)
                    if not buf:
                        break
                    fh.write(buf)
                    n += len(buf)
            return n
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _push(self, fh, remote, size_hint, job=None):
        directory = self._private_tmp_dir()
        fd, tmp = tempfile.mkstemp(prefix="adb-push-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as dst:
                n = 0
                while True:
                    buf = fh.read(1 << 16)
                    if not buf:
                        break
                    dst.write(buf)
                    n += len(buf)
            proc = self._run_transfer(
                self._base() + ["push", tmp, remote], 600, job)
            if proc.returncode != 0:
                raise TransportError("adb push 失败: %s" %
                                     proc.stderr.decode("utf-8", "replace"))
            return n
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def download(self, remote, fh, job=None):
        return self._pull(remote, fh, job)

    def upload(self, fh, remote, size_hint=0, job=None):
        return self._push(fh, remote, size_hint, job)
