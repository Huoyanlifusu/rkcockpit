import base64
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.transport.base import TransportError
from host.transport.ssh import SSHTransport
from host.transport.ssh_known_hosts import (fingerprint, has_host, pin_key,
                                            validate_known_hosts)
from tools import pin_ssh_host


class _Lease:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


class _Scheduler:
    def __init__(self):
        self.lease = _Lease()

    def acquire(self, device_id, workload):
        return self.lease


class _PipeProc:
    """Child-like pipes which exceed kernel capacity unless stderr is drained."""

    def __init__(self, upload=False):
        self.returncode = None
        self.pid = 99999999
        err_r, err_w = os.pipe()
        self.stderr = os.fdopen(err_r, "rb", buffering=0)
        self._err_w = err_w
        if upload:
            in_r, in_w = os.pipe()
            self.stdin = os.fdopen(in_w, "wb", buffering=0)
            self._in_r = in_r
            self.stdout = None
        else:
            out_r, out_w = os.pipe()
            self.stdout = os.fdopen(out_r, "rb", buffering=0)
            self._out_w = out_w
            self.stdin = None
        self.received = bytearray()
        self.thread = threading.Thread(target=self._worker,
                                       args=(upload,), daemon=True)
        self.thread.start()

    def _worker(self, upload):
        with os.fdopen(self._err_w, "wb", buffering=0) as err:
            err.write(b"e" * (256 * 1024))
        if upload:
            with os.fdopen(self._in_r, "rb", buffering=0) as src:
                while True:
                    chunk = src.read(1 << 14)
                    if not chunk:
                        break
                    self.received.extend(chunk)
        else:
            with os.fdopen(self._out_w, "wb", buffering=0) as dst:
                dst.write(b"payload")
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("ssh", timeout)
        return self.returncode

    def close(self):
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class _CleanupProc:
    def __init__(self, force_kill=False):
        self.stdout = io.BytesIO(b"payload")
        self.stderr = io.BytesIO(b"remote error")
        self.stdin = io.BytesIO()
        self.returncode = None
        self.pid = 99999999
        self.force_kill = force_kill
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if not self.force_kill:
            self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited += 1
        if self.returncode is None and timeout is not None:
            raise subprocess.TimeoutExpired("ssh", timeout)
        return self.returncode


class _BadWriter:
    def write(self, data):
        raise OSError("disk full")


class _BadReader:
    def read(self, size):
        raise OSError("pipe read failed")

    def close(self):
        pass


class _BadSource:
    def read(self, size):
        raise OSError("source read failed")


class SSHHardeningUnitTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def transport(self, scheduler=None):
        return SSHTransport("board", control_dir=str(self.root / "control"),
                            scheduler=scheduler)

    def test_download_drains_more_than_pipe_capacity(self):
        proc = _PipeProc()
        self.addCleanup(proc.close)
        with mock.patch("host.transport.ssh.subprocess.Popen", return_value=proc):
            out = io.BytesIO()
            job = {}
            self.assertEqual(self.transport().download("/x", out, job), 7)
        self.assertEqual(out.getvalue(), b"payload")
        self.assertIsNone(job["proc"])

    def test_upload_drains_stderr_while_streaming(self):
        proc = _PipeProc(upload=True)
        self.addCleanup(proc.close)
        data = b"x" * (256 * 1024)
        with mock.patch("host.transport.ssh.subprocess.Popen", return_value=proc):
            self.assertEqual(self.transport().upload(io.BytesIO(data), "/x"),
                             len(data))
        self.assertEqual(bytes(proc.received), data)

    def test_writer_failure_terms_kills_reaps_then_releases(self):
        proc = _CleanupProc(force_kill=True)
        scheduler = _Scheduler()
        job = {}
        with mock.patch("host.transport.ssh.subprocess.Popen", return_value=proc):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.transport(scheduler).download("/x", _BadWriter(), job)
        self.assertEqual((proc.terminated, proc.killed), (1, 1))
        self.assertGreaterEqual(proc.waited, 2)
        self.assertEqual(proc.returncode, -9)
        self.assertIsNone(job["proc"])
        self.assertEqual(scheduler.lease.released, 1)

    def test_stderr_reader_failure_reaps_and_clears_job(self):
        proc = _CleanupProc()
        proc.stdout = io.BytesIO(b"")
        proc.stderr = _BadReader()
        job = {}
        with mock.patch("host.transport.ssh.subprocess.Popen", return_value=proc):
            with self.assertRaisesRegex(TransportError, "stderr reader failed"):
                self.transport().download("/x", io.BytesIO(), job)
        self.assertEqual(proc.returncode, -15)
        self.assertGreaterEqual(proc.waited, 1)
        self.assertIsNone(job["proc"])

    def test_upload_source_failure_reaps_before_release(self):
        proc = _CleanupProc()
        scheduler = _Scheduler()
        with mock.patch("host.transport.ssh.subprocess.Popen", return_value=proc):
            with self.assertRaisesRegex(OSError, "source read failed"):
                self.transport(scheduler).upload(_BadSource(), "/x")
        self.assertEqual(proc.returncode, -15)
        self.assertGreaterEqual(proc.waited, 1)
        self.assertEqual(scheduler.lease.released, 1)

    def test_ssh_argv_is_strict_and_uses_dedicated_file(self):
        argv = self.transport()._base()
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("GlobalKnownHostsFile=/dev/null", argv)
        self.assertIn("UserKnownHostsFile=%s" %
                      (self.root / "ssh-known-hosts"), argv)
        self.assertFalse(any("accept-new" in value for value in argv))

    def test_pin_requires_matching_fingerprint_and_refuses_change(self):
        directory = self.root / "keys"
        directory.mkdir(mode=0o700)
        path = directory / "known_hosts"
        key_a = base64.b64encode(b"key-a").decode("ascii")
        key_b = base64.b64encode(b"key-b").decode("ascii")
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            pin_key(str(path), "board", 22, "ssh-ed25519", key_a,
                    fingerprint(key_b))
        self.assertTrue(pin_key(str(path), "board", 22, "ssh-ed25519",
                                key_a, fingerprint(key_a)))
        self.assertFalse(pin_key(str(path), "board", 22, "ssh-ed25519",
                                 key_a, fingerprint(key_a)))
        with self.assertRaisesRegex(ValueError, "refusing key replacement"):
            pin_key(str(path), "board", 22, "ssh-ed25519", key_b,
                    fingerprint(key_b))
        self.assertTrue(has_host(str(path), "board", 22))
        self.assertEqual(validate_known_hosts(str(path)), str(path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, "invalid literal SSH host"):
            pin_key(str(path), "evil\nother", 22, "ssh-ed25519", key_a,
                    fingerprint(key_a))

    def test_keyscan_is_discovery_not_implicit_trust(self):
        key = base64.b64encode(b"server key").decode("ascii")
        completed = subprocess.CompletedProcess(
            [], 0, stdout=("board ssh-ed25519 %s\n" % key).encode("ascii"),
            stderr=b"")
        with mock.patch("tools.pin_ssh_host.subprocess.run",
                        return_value=completed):
            candidates = pin_ssh_host.discover("board", 22, 2)
        self.assertEqual(candidates,
                         [("ssh-ed25519", key, fingerprint(key))])


if __name__ == "__main__":
    unittest.main()
