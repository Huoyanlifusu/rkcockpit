"""Stage 3 descriptor-relative local filesystem security regressions."""
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.transport import LocalTransport, TransportError
from host.service.fs import fs_copy, fs_copyfrom


class _ImmediateJobs(object):
    def submit(self, device, action, name, src, dest, fn, cleanup=None):
        job = {"bytes_total": 0, "bytes_done": 0, "cancelled": False}
        try:
            fn(job)
        finally:
            if cleanup:
                cleanup(job)
        return job


class LocalPathGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rkcockpit-path-")
        self.root = os.path.join(self.tmp, "root")
        self.outside = os.path.join(self.tmp, "outside")
        os.mkdir(self.root)
        os.mkdir(self.outside)
        self.transport = LocalTransport(root=self.root)

    def tearDown(self):
        self.transport.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_rejected(self, call):
        with self.assertRaises(TransportError):
            call()

    def test_configured_root_and_home_symlink_are_rejected(self):
        link = os.path.join(self.tmp, "root-link")
        os.symlink(self.root, link)
        self.assert_rejected(lambda: LocalTransport(root=link))

        home_link = os.path.join(self.tmp, "home-link")
        os.symlink(self.root, home_link)
        with mock.patch("host.transport.local.os.path.expanduser",
                        return_value=home_link):
            self.assert_rejected(LocalTransport)

    def test_intermediate_final_and_dangling_symlinks_are_rejected(self):
        os.mkdir(os.path.join(self.outside, "dir"))
        with open(os.path.join(self.outside, "secret"), "wb") as fh:
            fh.write(b"outside")
        os.symlink(self.outside, os.path.join(self.root, "hop"))
        os.symlink(os.path.join(self.outside, "secret"),
                   os.path.join(self.root, "final"))
        os.symlink(os.path.join(self.outside, "missing"),
                   os.path.join(self.root, "dangling"))

        calls = [
            lambda: self.transport.listdir("/hop"),
            lambda: self.transport.stat("/final"),
            lambda: self.transport.download("/final", io.BytesIO()),
            lambda: self.transport.upload(io.BytesIO(b"bad"), "/final"),
            lambda: self.transport.mkdir("/hop/new"),
            lambda: self.transport.chmod("/final", "0600"),
            lambda: self.transport.remove("/final"),
            lambda: self.transport.rename("/final", "renamed"),
            lambda: self.transport.move("/final", "/moved"),
            lambda: self.transport.upload(io.BytesIO(b"bad"), "/dangling"),
        ]
        for call in calls:
            self.assert_rejected(call)
        with open(os.path.join(self.outside, "secret"), "rb") as fh:
            self.assertEqual(fh.read(), b"outside")
        self.assertFalse(os.path.exists(os.path.join(self.outside, "new")))

    def test_normal_operations_remain_compatible(self):
        self.transport.mkdir("/a/b")
        self.assertEqual(self.transport.upload(io.BytesIO(b"payload"),
                                               "/a/b/data"), 7)
        out = io.BytesIO()
        self.assertEqual(self.transport.download("/a/b/data", out), 7)
        self.assertEqual(out.getvalue(), b"payload")
        self.assertEqual(self.transport.stat("/a/b/data")["size"], 7)
        self.assertEqual([e["name"] for e in self.transport.listdir("/a/b")],
                         ["data"])
        self.transport.chmod("/a/b/data", "0640")
        self.assertEqual(os.stat(os.path.join(self.root, "a/b/data")).st_mode
                         & 0o777, 0o640)
        self.transport.rename("/a/b/data", "renamed")
        self.transport.mkdir("/dest")
        self.transport.move("/a/b/renamed", "/dest/moved")
        self.transport.remove("/a", recursive=True)
        self.assertFalse(os.path.exists(os.path.join(self.root, "a")))
        self.transport.remove("/dest/moved")
        self.transport.remove("/dest", recursive=False)

    def test_roots_and_parent_components_are_never_mutable(self):
        for path in ("/", "", "~", "/../outside", "/a/../../outside"):
            self.assert_rejected(lambda path=path:
                                 self.transport.remove(path))
            self.assert_rejected(lambda path=path:
                                 self.transport.upload(io.BytesIO(b"x"), path))
        self.assertTrue(os.path.isdir(self.root))

    def test_recursive_remove_rejects_symlink_child(self):
        tree = os.path.join(self.root, "tree")
        os.mkdir(tree)
        os.symlink(self.outside, os.path.join(tree, "escape"))
        self.assert_rejected(lambda: self.transport.remove("/tree", True))
        self.assertTrue(os.path.isdir(self.outside))

    def test_intermediate_swap_race_never_writes_outside(self):
        safe = os.path.join(self.root, "safe")
        parked = os.path.join(self.root, "parked")
        os.mkdir(safe)
        stop = threading.Event()

        def swapper():
            while not stop.is_set():
                try:
                    os.rename(safe, parked)
                    os.symlink(self.outside, safe)
                    os.unlink(safe)
                    os.rename(parked, safe)
                except FileNotFoundError:
                    pass

        thread = threading.Thread(target=swapper)
        thread.start()
        try:
            for idx in range(500):
                try:
                    self.transport.upload(io.BytesIO(b"inside"),
                                          "/safe/f%d" % idx)
                except TransportError:
                    pass
        finally:
            stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(os.listdir(self.outside), [])

    def test_anchor_swap_after_construction_stays_on_original_inode(self):
        original = os.path.join(self.tmp, "original")
        os.rename(self.root, original)
        os.symlink(self.outside, self.root)
        self.transport.upload(io.BytesIO(b"safe"), "/anchored")
        self.assertTrue(os.path.isfile(os.path.join(original, "anchored")))
        self.assertFalse(os.path.exists(os.path.join(self.outside, "anchored")))

    def test_copy_services_use_guarded_local_opens(self):
        source = os.path.join(self.outside, "source")
        with open(source, "wb") as fh:
            fh.write(b"secret")
        source_link = os.path.join(self.tmp, "source-link")
        os.symlink(source, source_link)
        self.assert_rejected(lambda: fs_copy(_ImmediateJobs(), self.transport,
                                             "demo", source_link, "/copy"))

        # copyfrom's host destination is write-limited to HOME.  Give it a
        # temporary HOME containing an intermediate link to the outside.
        home = os.path.join(self.tmp, "home")
        os.mkdir(home)
        os.symlink(self.outside, os.path.join(home, "hop"))
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            self.transport.upload(io.BytesIO(b"remote"), "/remote")
            self.assert_rejected(lambda: fs_copyfrom(
                _ImmediateJobs(), self.transport, "demo", "/remote",
                os.path.join(home, "hop", "escaped")))
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
        self.assertFalse(os.path.exists(os.path.join(self.outside, "escaped")))


if __name__ == "__main__":
    unittest.main()
