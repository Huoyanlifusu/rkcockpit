import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.transport.adb import AdbTransport
from host.task.transfer import TransferJobStore


class AdbTempUnitTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def transport(self, directory=None):
        value = object.__new__(AdbTransport)
        value.serial = "sim"
        value.tmp_dir = str(directory or self.root)
        value.adb = "adb"
        return value

    def test_concurrent_pushes_use_unique_private_temp_files(self):
        paths = []
        lock = threading.Lock()

        def fake_run(argv, **kwargs):
            path = argv[-2]
            with lock:
                paths.append(path)
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        transport = self.transport()
        with mock.patch("host.transport.adb.subprocess.run", side_effect=fake_run):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda n: transport.upload(
                        io.BytesIO((str(n) * 100).encode()), "/x"), range(16)))
        self.assertEqual(results, [len(str(n) * 100) for n in range(16)])
        self.assertEqual(len(set(paths)), 16)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_adb_temp_directory_is_private(self):
        directory = self.root / "adb-temp"
        transport = self.transport(directory)
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("host.transport.adb.subprocess.run",
                        return_value=completed):
            self.assertEqual(transport.upload(io.BytesIO(b"x"), "/x"), 1)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_concurrent_pulls_use_unique_temp_files_and_cleanup(self):
        paths = []
        lock = threading.Lock()

        def fake_run(argv, **kwargs):
            path = argv[-1]
            with lock:
                paths.append(path)
            with open(path, "wb") as dst:
                dst.write(b"payload")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        transport = self.transport()

        def pull(_):
            out = io.BytesIO()
            self.assertEqual(transport.download("/x", out), 7)
            return out.getvalue()

        with mock.patch("host.transport.adb.subprocess.run", side_effect=fake_run):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(pull, range(16)))
        self.assertEqual(results, [b"payload"] * 16)
        self.assertEqual(len(set(paths)), 16)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_transfer_store_close_reaps_active_adb_process(self):
        transport = self.transport()
        store = TransferJobStore()
        real_popen = subprocess.Popen

        def sleeping_process(_argv, **_kwargs):
            return real_popen(["sleep", "30"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True)

        with mock.patch("host.transport.adb.subprocess.Popen",
                        side_effect=sleeping_process):
            job = store.submit(
                "adb", "upload", "x", "-", "/x",
                lambda current: transport.upload(
                    io.BytesIO(b"payload"), "/x", job=current))
            deadline = time.monotonic() + 2
            while job.get("proc") is None and time.monotonic() < deadline:
                time.sleep(.01)
            proc = job.get("proc")
            self.assertIsNotNone(proc)
            self.assertTrue(store.close(timeout=5))
            self.assertIsNotNone(proc.poll())
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
