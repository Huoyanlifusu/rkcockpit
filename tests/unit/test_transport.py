#!/usr/bin/env python3
"""Test module."""
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.transport.ssh import SSHTransport, _parse_ls
from host.transport.ssh_control import control_path
from host.transport.adb import AdbTransport


class LsParseUnitTest(unittest.TestCase):
    def test_busybox_ls(self):
        lines = [
            "total 12",
            "drwxrwxrwt  4 0 0  200 Jan  1 00:21 .",
            "drwxr-xr-x 21 0 0 4096 Jan  1 00:00 ..",
            "-rw-r--r--  1 0 0    4 Jan  1 00:00 .rkaiq_3A",
            "srwxr-xr-x  1 0 0    0 Jan  1 00:00 UNIX.domain0",
            "-rw-r--r--  1 0 0  154 Jan  1 00:32 resolv.conf",
        ]
        entries = _parse_ls(lines)
        self.assertEqual([e["name"] for e in entries],
                         [".rkaiq_3A", "UNIX.domain0", "resolv.conf"])
        self.assertTrue(all(e["mtime_ms"] > 0 for e in entries))
        self.assertEqual(entries[2]["size"], 154)
        self.assertFalse(entries[2]["is_dir"])

    def test_gnu_full_year(self):
        entries = _parse_ls([
            "-rw-r--r-- 1 root root 123 Jan 12 2024 file.txt",
            "drwxr-xr-x 2 root root 4096 Aug 13 17:30 subdir",
        ])
        self.assertEqual([e["name"] for e in entries],
                         ["file.txt", "subdir"])
        self.assertTrue(entries[0]["mtime_ms"] > 0)
        self.assertTrue(entries[1]["is_dir"])


class SshControlUnitTest(unittest.TestCase):
    def test_key_auth_reuses_hashed_private_control_path(self):
        with tempfile.TemporaryDirectory() as td:
            tr = SSHTransport("192.0.2.8", port=2222, user="root",
                              key_path="/tmp/test-key", control_dir=td)
            argv = tr._base()
            joined = " ".join(argv)
            self.assertIn("ControlMaster=auto", joined)
            self.assertIn("ControlPersist=60", joined)
            option = next(x for x in argv if x.startswith("ControlPath="))
            self.assertNotIn("192.0.2.8", option)
            self.assertEqual(os.stat(td).st_mode & 0o777, 0o700)

    def test_password_auth_does_not_reuse_master(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("host.transport.ssh.shutil.which",
                           return_value="/usr/bin/sshpass"):
            tr = SSHTransport("192.0.2.8", password="secret", control_dir=td)
            joined = " ".join(tr._base())
            self.assertNotIn("ControlMaster=auto", joined)
            self.assertIn("ControlMaster=no", joined)
            self.assertIn("ControlPath=none", joined)
            self.assertIn("BatchMode=no", joined)

    def test_identity_changes_control_path(self):
        with tempfile.TemporaryDirectory() as td:
            p1 = control_path(td, "host", 22, "root", "/tmp/key-a")
            p2 = control_path(td, "host", 22, "root", "/tmp/key-b")
            p3 = control_path(td, "host", 22, "operator", "/tmp/key-a")
            self.assertEqual(len({p1, p2, p3}), 3)

    @mock.patch("host.transport.ssh.subprocess.Popen")
    def test_open_cmd_starts_new_process_group(self, popen):
        with tempfile.TemporaryDirectory() as td:
            SSHTransport("host", control_dir=td).open_cmd("true")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


class ProcessIsolationUnitTest(unittest.TestCase):
    @mock.patch("host.transport.adb.subprocess.Popen")
    @mock.patch("host.transport.adb.shutil.which", return_value="/usr/bin/adb")
    def test_adb_open_cmd_starts_new_process_group(self, _which, popen):
        AdbTransport("serial-1").open_cmd("true")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
