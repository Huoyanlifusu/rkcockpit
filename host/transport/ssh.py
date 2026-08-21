"""Utilities for host.transport.ssh."""
import os
import re
import shutil
import subprocess
import threading
import time
from calendar import month_abbr

from host.transport.base import Transport, TransportError, make_entry,\
    to_epoch_ms
from host.transport.ssh_control import DEFAULT_PERSIST_SECONDS, control_options
from host.transport.ssh_known_hosts import default_known_hosts_path, file_identity
from host.core.metrics import METRICS

_LS_RE = re.compile(r"^([\-dbclps])[rwxsStT\-]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
                    r"(\w{3})\s+(\d{1,2})\s+(\d{2}:\d{2}|\d{4})\s+(.+)$")
_STDERR_LIMIT = 64 * 1024
_TERM_WAIT_SECONDS = 2


class _PipeTail:
    """Continuously drain a pipe while retaining only a bounded error tail."""

    def __init__(self, pipe, proc, limit=_STDERR_LIMIT):
        self.pipe = pipe
        self.proc = proc
        self.limit = limit
        self.data = bytearray()
        self.error = None
        self.thread = threading.Thread(target=self._run,
                                       name="rkss-ssh-stderr", daemon=True)

    def _run(self):
        try:
            while True:
                chunk = self.pipe.read(1 << 14)
                if not chunk:
                    return
                self.data.extend(chunk)
                if len(self.data) > self.limit:
                    del self.data[:-self.limit]
        except Exception as exc:  # surfaced by finish(), after child cleanup
            self.error = exc
            # Do not leave the producer blocked on a full stderr pipe after
            # its only consumer has failed.
            _terminate_reap(self.proc)

    def start(self):
        self.thread.start()

    def finish(self):
        self.thread.join(_TERM_WAIT_SECONDS)
        if self.thread.is_alive():
            try:
                self.pipe.close()
            except Exception:
                pass
            self.thread.join(_TERM_WAIT_SECONDS)
        if self.thread.is_alive():
            raise TransportError("SSH stderr reader did not stop")
        if self.error is not None:
            raise TransportError("SSH stderr reader failed: %s" % self.error)

    def text(self):
        return bytes(self.data).decode("utf-8", "replace")


def _terminate_reap(proc):
    """TERM, bounded wait, then KILL and reap. Return only after wait()."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, 15)
        except (AttributeError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=_TERM_WAIT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, 9)
        except (AttributeError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    # A killed child is always reaped before a scheduler lease can be released.
    proc.wait()


def _clear_job(job, proc):
    if job is not None and job.get("proc") is proc:
        job["proc"] = None


def _shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _parse_ls(lines):
    """Handle parse ls."""
    out = []
    now = time.localtime()
    for ln in lines:
        m = _LS_RE.match(ln.strip())
        if not m:
            continue
        typ, size, mon, day, tod, name = m.groups()
        if name in (".", ".."):
            continue
        mon_num = list(month_abbr).index(mon[:3]) if mon[:3] in month_abbr else 1
        year = now.tm_year if len(tod) == 5 else int(tod)
        try:
            t = time.mktime((year, mon_num, int(day), 0, 0, 0, 0, 0, -1))
            if len(tod) == 5:
                hh, mm = int(tod[:2]), int(tod[3:])
                t += hh * 3600 + mm * 60
        except (ValueError, OverflowError):
            t = 0
        mode = {"d": "40755", "-": "40644", "l": "120777", "c": "20660",
                "b": "20660", "p": "10644", "s": "10644"}.get(typ, "40644")
        out.append(make_entry(name, typ == "d", int(size), mode,
                              to_epoch_ms(t)))
    return out


class ScheduledPopen:
    """Popen-compatible proxy that releases one scheduler lease at terminal use."""

    def __init__(self, proc, lease):
        self._proc = proc
        self._lease = lease

    def poll(self):
        result = self._proc.poll()
        if result is not None:
            self._lease.release()
        return result

    def wait(self, *args, **kwargs):
        result = self._proc.wait(*args, **kwargs)
        self._lease.release()
        return result

    def communicate(self, *args, **kwargs):
        result = self._proc.communicate(*args, **kwargs)
        self._lease.release()
        return result

    def kill(self):
        try:
            return self._proc.kill()
        finally:
            # A signal request is not a terminal state.  Retain the channel
            # until Popen confirms exit so admission never exceeds the real
            # number of live SSH subprocesses.
            if self._proc.poll() is not None:
                self._lease.release()

    def terminate(self):
        try:
            return self._proc.terminate()
        finally:
            if self._proc.poll() is not None:
                self._lease.release()

    def send_signal(self, signal):
        try:
            return self._proc.send_signal(signal)
        finally:
            if self._proc.poll() is not None:
                self._lease.release()

    def __enter__(self):
        self._proc.__enter__()
        return self

    def __exit__(self, typ, value, traceback):
        try:
            return self._proc.__exit__(typ, value, traceback)
        finally:
            if self._proc.returncode is not None:
                self._lease.release()

    def __getattr__(self, name):
        return getattr(self._proc, name)


class SSHTransport(Transport):
    kind = "ssh"

    def __init__(self, host, port=22, user="root", password=None,
                 timeout=5, key_path=None, control_dir=None,
                 control_persist=DEFAULT_PERSIST_SECONDS, scheduler=None,
                 device_id=None, workload="foreground"):
        self.host = host
        self.port = int(port or 22)
        self.user = user
        self.password = password
        self.timeout = timeout
        self.key_path = key_path
        self.control_dir = control_dir or os.path.join(
            os.path.expanduser("~"), ".rkss", "ssh-control")
        self.known_hosts_path = default_known_hosts_path(self.control_dir)
        self.control_persist = control_persist
        self.scheduler = scheduler
        self.device_id = str(device_id or host or "<unknown>")
        if workload not in ("foreground", "background"):
            raise ValueError("workload must be foreground or background")
        self.workload = workload
        self.sshpass = shutil.which("sshpass")

    def _acquire(self):
        if self.scheduler is None:
            return None
        return self.scheduler.acquire(self.device_id, self.workload)

    def _use_password(self):
        """Handle use password."""
        return bool(self.password) and not self.key_path

    def _base(self):
        password_mode = self._use_password()
        try:
            known_hosts_identity = file_identity(self.known_hosts_path)
        except (OSError, ValueError):
            # Strict checking still fails closed.  This sentinel gives a clear
            # one-time migration path via tools/pin_ssh_host.py without
            # preventing callers from inspecting argv or surfacing ssh's error.
            known_hosts_identity = "unpinned"
        base = ["ssh", "-o", "BatchMode=%s" %
                ("no" if password_mode else "yes"),
                "-o", "ConnectTimeout=%d" % self.timeout,
                "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile=%s" % self.known_hosts_path,
                "-o", "GlobalKnownHostsFile=/dev/null"]
        if self.key_path:
            base += ["-o", "IdentitiesOnly=yes", "-i", self.key_path]
        if password_mode:
            # Do not inherit a multiplexing profile from ~/.ssh/config: a
            # password-authenticated invocation must never attach to or create
            # a master belonging to another authentication context.
            base += ["-o", "ControlMaster=no", "-o", "ControlPath=none",
                     "-o", "NumberOfPasswordPrompts=1"]
        else:
            base += control_options(self.control_dir, self.host, self.port,
                                    self.user, self.key_path,
                                    self.control_persist,
                                    known_hosts_identity)
        if self.port != 22:
            base += ["-p", str(self.port)]
        base += ["%s@%s" % (self.user, self.host)]
        return base

    def _env(self):
        env = dict(os.environ)
        if self._use_password():
            env["SSHPASS"] = self.password
        return env

    def _check_password_support(self):
        if self._use_password() and not self.sshpass:
            METRICS.increment("ssh_password")
            METRICS.increment("ssh_fail")
            raise TransportError(
                "密码认证需要 sshpass（未找到）。请安装：apt install sshpass，"
                "或改用密钥认证。")

    def _record_attempt(self, argv):
        if self._use_password():
            METRICS.increment("ssh_password")
            return
        METRICS.increment("ssh_mux_eligible")
        for arg in argv:
            if arg.startswith("ControlPath=") and os.path.exists(arg.split("=", 1)[1]):
                METRICS.increment("ssh_reused_hint")
                break

    def exec(self, cmd, timeout=30):
        self._check_password_support()
        argv = (["sshpass", "-e"] if self._use_password() else []) +\
            self._base() + [cmd]
        lease = self._acquire()
        try:
            self._record_attempt(argv)
            try:
                proc = subprocess.run(argv, capture_output=True,
                                      timeout=timeout, env=self._env())
            except subprocess.TimeoutExpired:
                METRICS.increment("ssh_fail")
                return 124, "", "timeout after %ss" % timeout
            except Exception:
                METRICS.increment("ssh_fail")
                raise
            if proc.returncode != 0:
                METRICS.increment("ssh_fail")
            return proc.returncode, proc.stdout.decode("utf-8", "replace"),\
                proc.stderr.decode("utf-8", "replace")
        finally:
            if lease is not None:
                lease.release()

    def open_cmd(self, cmd):
        self._check_password_support()
        argv = (["sshpass", "-e"] if self._use_password() else []) +\
            self._base() + [cmd]
        lease = self._acquire()
        try:
            self._record_attempt(argv)
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=self._env(),
                                    start_new_session=True)
        except Exception:
            METRICS.increment("ssh_fail")
            if lease is not None:
                lease.release()
            raise
        return ScheduledPopen(proc, lease) if lease is not None else proc

    def listdir(self, path):

        rc, out, err = self.exec(
            "LC_ALL=C ls -lan --group-directories-first %s" % _shq(path), 15)
        if rc != 0:
            return []
        return _parse_ls(out.splitlines())

    def stat(self, path):

        rc, out, err = self.exec(
            "LC_ALL=C stat -c '%%F|%%s|%%a|%%Y' %s" % _shq(path), 10)
        if rc != 0:
            raise TransportError("stat 失败: %s" % (err or out).strip())
        kind, size, mode, mt = out.strip().split("|")
        is_dir = kind == "directory"
        return make_entry(os.path.basename(path.rstrip("/")) or path,
                          is_dir, int(size), mode.zfill(4),
                          int(float(mt) * 1000))

    def mkdir(self, path):
        rc, out, err = self.exec("mkdir -p %s" % _shq(path), 10)
        if rc != 0:
            raise TransportError("mkdir 失败: %s" % (err or out).strip())

    def remove(self, path, recursive=True):
        cmd = "rm -rf -- %s" if recursive else "rmdir -- %s"
        rc, out, err = self.exec(cmd % _shq(path), 20)
        if rc != 0:
            raise TransportError("删除失败: %s" % (err or out).strip())

    def rename(self, path, new_name):
        if not new_name or "/" in new_name or new_name in (".", ".."):
            raise TransportError("非法文件名: %r" % new_name)
        dest = os.path.join(os.path.dirname(path.rstrip("/")), new_name)
        self.move(path, dest)

    def move(self, path, dest):
        rc, out, err = self.exec("mv -- %s %s" % (_shq(path), _shq(dest)), 20)
        if rc != 0:
            raise TransportError("移动失败: %s" % (err or out).strip())

    def chmod(self, path, mode):
        rc, out, err = self.exec("chmod %s %s" % (mode, _shq(path)), 10)
        if rc != 0:
            raise TransportError("chmod 失败: %s" % (err or out).strip())

    def download(self, remote, fh, job=None):
        self._check_password_support()
        argv = (["sshpass", "-e"] if self._use_password() else []) +\
            self._base() + ["cat %s" % _shq(remote)]
        lease = self._acquire()
        proc = None
        stderr = None
        try:
            self._record_attempt(argv)
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=self._env(),
                                        start_new_session=True)
            except Exception:
                raise
            if job is not None:
                job["proc"] = proc
            stderr = _PipeTail(proc.stderr, proc)
            stderr.start()
            n = 0
            try:
                while True:
                    buf = proc.stdout.read(1 << 16)
                    if not buf:
                        break
                    fh.write(buf)
                    n += len(buf)
                proc.wait()
            except BaseException:
                _terminate_reap(proc)
                raise
            stderr.finish()
            if proc.returncode != 0:
                raise TransportError("下载失败: %s" % stderr.text())
            return n
        except BaseException:
            if proc is not None and proc.poll() is None:
                _terminate_reap(proc)
            METRICS.increment("ssh_fail")
            raise
        finally:
            if proc is not None:
                _clear_job(job, proc)
            if lease is not None:
                lease.release()

    def upload(self, fh, remote, size_hint=0, job=None):
        self._check_password_support()
        argv = (["sshpass", "-e"] if self._use_password() else []) +\
            self._base() + ["cat > %s" % _shq(remote)]
        lease = self._acquire()
        proc = None
        stderr = None
        try:
            self._record_attempt(argv)
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=self._env(),
                                        start_new_session=True)
            except Exception:
                raise
            if job is not None:
                job["proc"] = proc
            stderr = _PipeTail(proc.stderr, proc)
            stderr.start()
            n = 0
            try:
                while True:
                    buf = fh.read(1 << 16)
                    if not buf:
                        break
                    proc.stdin.write(buf)
                    n += len(buf)
                proc.stdin.close()
                proc.wait()
            except BaseException:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                _terminate_reap(proc)
                raise
            stderr.finish()
            if proc.returncode != 0:
                raise TransportError("上传失败: %s" % stderr.text())
            return n
        except BaseException:
            if proc is not None and proc.poll() is None:
                _terminate_reap(proc)
            METRICS.increment("ssh_fail")
            raise
        finally:
            if proc is not None:
                _clear_job(job, proc)
            if lease is not None:
                lease.release()
