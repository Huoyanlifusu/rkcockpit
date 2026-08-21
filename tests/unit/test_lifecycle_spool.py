import os
import io
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))


from host.api.handlers.legacy import HostApi, _fs_upload
from host.device.store import DeviceStore
from host.task.execjob import ExecJobStore
from host.task.spool import SpoolBusy, UploadSpoolLimiter
from host.task.transfer import TransferJobStore
from host.transport.local import LocalTransport
from host.transport.scheduler import TransportScheduler


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


class LifecycleUnitTest(unittest.TestCase):
    def test_exec_store_close_reaps_live_process(self):
        store = ExecJobStore(LocalTransport())
        jid = store.run("sleep 30")
        proc = _wait_until(lambda: store._jobs[jid].get("proc"))
        self.assertIsNotNone(proc)
        self.assertTrue(store.close(timeout=5.0))
        self.assertIsNotNone(proc.poll())

    def test_transfer_store_close_reaps_live_process(self):
        store = TransferJobStore()
        started = threading.Event()

        def run(job):
            proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
            job["proc"] = proc
            started.set()
            proc.wait()

        store.submit("local", "test", "sleep", "-", "-", run)
        self.assertTrue(started.wait(2.0))
        proc = next(j["proc"] for j in store._jobs.values())
        self.assertTrue(store.close(timeout=5.0))
        self.assertIsNotNone(proc.poll())

    def test_host_close_closes_dynamic_exec_store(self):
        with tempfile.TemporaryDirectory() as conf:
            host = HostApi(conf)
            store = host._exec_store("local")
            jid = store.run("sleep 30")
            proc = _wait_until(lambda: store._jobs[jid].get("proc"))
            self.assertTrue(host.close())
            self.assertIsNotNone(proc.poll())


class SpoolTest(unittest.TestCase):
    def test_raw_upload_rejects_before_reading_body_when_spool_busy(self):
        class Handler:
            headers = {"Content-Length": "4"}
            rfile = io.BytesIO(b"data")
            client_address = ("127.0.0.1", 1)
            close_connection = False

            def _send(self, code, payload, ctype=None, headers=None):
                self.response = (code, payload, headers)
                return self.response

        with tempfile.TemporaryDirectory() as conf:
            host = HostApi(conf)
            handler = Handler()
            with mock.patch.object(host.upload_spool, "acquire",
                                   side_effect=SpoolBusy("busy")):
                result = _fs_upload(
                    handler, host, "local",
                    {"path": ["/"], "name": ["blocked.bin"]})
            self.assertEqual(result[0], 503)
            self.assertEqual(handler.rfile.tell(), 0)
            self.assertTrue(handler.close_connection)
            self.assertEqual(result[2], (("Retry-After", "2"),))
            self.assertTrue(host.close())

    def test_concurrency_and_byte_budget_are_released(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("host.task.spool.shutil.disk_usage") as usage:
                usage.return_value = mock.Mock(free=10_000)
                limiter = UploadSpoolLimiter(
                    directory, max_active=1, max_bytes=100,
                    min_free_bytes=10, stale_seconds=0)
                lease = limiter.acquire(80)
                with self.assertRaises(SpoolBusy):
                    limiter.acquire(1)
                lease.release()
                lease.release()
                second = limiter.acquire(90)
                self.assertEqual(limiter.stats()["active"], 1)
                second.release()
                self.assertEqual(limiter.stats()["reserved_bytes"], 0)

    def test_stale_upload_cleanup_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as directory, \
                tempfile.TemporaryDirectory() as outside:
            stale = os.path.join(directory, "rkss-old.up")
            with open(stale, "wb") as stream:
                stream.write(b"old")
            old = time.time() - 100
            os.utime(stale, (old, old))
            target = os.path.join(outside, "keep")
            with open(target, "wb") as stream:
                stream.write(b"keep")
            os.symlink(target, os.path.join(directory, "rkss-link.up"))
            UploadSpoolLimiter(directory, stale_seconds=10)
            self.assertFalse(os.path.exists(stale))
            with open(target, "rb") as stream:
                self.assertEqual(stream.read(), b"keep")

    def test_device_temp_files_are_created_and_unique(self):
        with tempfile.TemporaryDirectory() as conf:
            store = DeviceStore(conf)
            paths = {store.temp_file(".up") for _ in range(32)}
            self.assertEqual(len(paths), 32)
            self.assertTrue(all(os.path.isfile(path) for path in paths))

    def test_scheduler_stats_are_fixed_and_identity_free(self):
        scheduler = TransportScheduler(
            global_limit=2, device_limit=2, background_device_limit=2)
        lease = scheduler.acquire("secret-device-id")
        snapshot = scheduler.stats()
        self.assertEqual(snapshot["active"], 1)
        self.assertEqual(snapshot["peak_device_active"], 1)
        self.assertNotIn("secret-device-id", repr(snapshot))
        lease.release()


if __name__ == "__main__":
    unittest.main()
