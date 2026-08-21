#!/usr/bin/env python3
"""Stage 2 SSH scheduler fairness and lease lifecycle tests."""
import io
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent
while not (ROOT / ".git").exists() and ROOT != ROOT.parent:
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))


from host.transport.base import TransportError
from host.transport.factory import make_transport
from host.transport.scheduler import BACKGROUND, FOREGROUND, TransportScheduler
from host.transport.ssh import SSHTransport, ScheduledPopen


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition was not reached")


class SchedulerBoundsUnitTest(unittest.TestCase):
    def test_wait_queue_bounds_and_timeout_have_stable_errors(self):
        arbiter = TransportScheduler(
            global_limit=1, device_limit=1, background_device_limit=1,
            global_wait_limit=1, device_wait_limit=1, wait_timeout=0.1)
        held = arbiter.acquire("held")
        errors = []

        def wait_one():
            try:
                arbiter.acquire("queued")
            except TransportError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=wait_one)
        thread.start()
        wait_until(lambda: arbiter.waiting == 1)
        with self.assertRaisesRegex(TransportError,
                                    "ssh_busy: global wait queue full"):
            arbiter.acquire("other")
        thread.join(timeout=1)
        self.assertEqual(errors, ["ssh_busy: scheduler wait timeout"])
        held.release()
        self.assertEqual((arbiter.active, arbiter.waiting), (0, 0))

        arbiter = TransportScheduler(
            global_limit=1, device_limit=1, background_device_limit=1,
            global_wait_limit=4, device_wait_limit=1, wait_timeout=0.1)
        held = arbiter.acquire("held")
        thread = threading.Thread(target=lambda: self._ignore_busy(
            arbiter, "same"))
        thread.start()
        wait_until(lambda: arbiter.waiting_for("same") == 1)
        with self.assertRaisesRegex(TransportError,
                                    "ssh_busy: device wait queue full"):
            arbiter.acquire("same")
        thread.join(timeout=1)
        held.release()

    @staticmethod
    def _ignore_busy(arbiter, device):
        try:
            arbiter.acquire(device)
        except TransportError:
            pass

    def test_background_reserves_two_device_slots_for_foreground(self):
        arbiter = TransportScheduler(wait_timeout=1)
        background = [arbiter.acquire("dev", BACKGROUND) for _ in range(6)]
        acquired = []
        release_bg = threading.Event()

        def seventh_background():
            lease = arbiter.acquire("dev", BACKGROUND)
            acquired.append("background")
            release_bg.wait(1)
            lease.release()

        bg_thread = threading.Thread(target=seventh_background)
        bg_thread.start()
        wait_until(lambda: arbiter.waiting_for("dev") == 1)
        foreground = [arbiter.acquire("dev", FOREGROUND) for _ in range(2)]
        self.assertEqual(arbiter.active_for("dev"), 8)

        release_fg = threading.Event()

        def third_foreground():
            lease = arbiter.acquire("dev", FOREGROUND)
            acquired.append("foreground")
            release_fg.wait(1)
            lease.release()

        fg_thread = threading.Thread(target=third_foreground)
        fg_thread.start()
        wait_until(lambda: arbiter.waiting_for("dev") == 2)
        foreground[0].release()
        wait_until(lambda: acquired == ["foreground"])
        self.assertNotIn("background", acquired)
        background[0].release()
        wait_until(lambda: "background" in acquired)
        release_fg.set()
        release_bg.set()
        for lease in foreground[1:] + background[1:]:
            lease.release()
        fg_thread.join(timeout=1)
        bg_thread.join(timeout=1)
        self.assertEqual(arbiter.active, 0)


class SchedulerFairnessUnitTest(unittest.TestCase):
    def test_foreground_priority_and_per_class_device_round_robin(self):
        arbiter = TransportScheduler(
            global_limit=1, device_limit=1, background_device_limit=1,
            wait_timeout=1)
        held = arbiter.acquire("held")
        order = []
        gates = []
        threads = []

        def enqueue(device, workload):
            gate = threading.Event()
            gates.append(gate)

            def worker():
                lease = arbiter.acquire(device, workload)
                order.append(device)
                gate.wait(1)
                lease.release()

            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()
            wait_until(lambda: arbiter.waiting == len(threads))

        enqueue("background-first", BACKGROUND)
        enqueue("a", FOREGROUND)
        enqueue("b", FOREGROUND)
        enqueue("a", FOREGROUND)
        enqueue("b", FOREGROUND)
        held.release()
        expected = ["a", "b", "a", "b", "background-first"]
        for index, value in enumerate(expected):
            wait_until(lambda index=index: len(order) > index)
            self.assertEqual(order[index], value)
            gates[["background-first", "a", "b", "a", "b"].index(value)
                  if value == "background-first" else index + 1].set()

        # The foreground gates above are indexed by enqueue order; release any
        # duplicate-id gate that was not selected by the compact assertion loop.
        for gate in gates:
            gate.set()
        for thread in threads:
            thread.join(timeout=1)
        self.assertEqual(order, expected)
        self.assertEqual(arbiter.active, 0)

    def test_continuous_foreground_cannot_starve_background(self):
        arbiter = TransportScheduler(
            global_limit=1, device_limit=1, background_device_limit=1,
            wait_timeout=1)
        held = arbiter.acquire("held")
        order = []
        threads = []

        def enqueue(name, workload):
            def worker():
                lease = arbiter.acquire(name, workload)
                order.append(name)
                lease.release()

            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()
            wait_until(lambda: arbiter.waiting == len(threads))

        enqueue("background", BACKGROUND)
        for index in range(9):
            enqueue("foreground-%d" % index, FOREGROUND)
        held.release()
        for thread in threads:
            thread.join(timeout=1)
        self.assertLessEqual(order.index("background"), 8)
        self.assertEqual(len(order), 10)
        self.assertEqual(arbiter.active, 0)

    def test_device_removal_cancels_waiters_without_leaking_counts(self):
        arbiter = TransportScheduler(global_limit=1, wait_timeout=1)
        held = arbiter.acquire("held")
        errors = []

        def worker():
            try:
                arbiter.acquire("removed")
            except TransportError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=worker)
        thread.start()
        wait_until(lambda: arbiter.waiting_for("removed") == 1)
        arbiter.remove_device("removed")
        thread.join(timeout=1)
        self.assertEqual(errors, ["ssh_busy: device removed"])
        self.assertEqual(arbiter.waiting, 0)
        held.release()


class _FakeProc:
    def __init__(self):
        self.pid = 12345
        self.returncode = None
        self.stdout = object()
        self.stderr = object()
        self.kills = 0

    def poll(self):
        return self.returncode

    def wait(self, *args, **kwargs):
        self.returncode = 0
        return 0

    def communicate(self, *args, **kwargs):
        self.returncode = 0
        return (b"", b"")

    def kill(self):
        self.kills += 1
        self.returncode = -9


class _BufferProc(_FakeProc):
    def __init__(self, output=b""):
        super().__init__()
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO()
        self.stdin = io.BytesIO()


class TransportLeaseTest(unittest.TestCase):
    def _transport(self, arbiter, directory):
        return SSHTransport("example", control_dir=directory,
                            scheduler=arbiter, device_id="dev")

    def test_open_failure_and_sync_exec_release_without_holding_lock(self):
        arbiter = TransportScheduler(global_limit=1, wait_timeout=0.1)
        with tempfile.TemporaryDirectory() as td:
            transport = self._transport(arbiter, td)
            with mock.patch("host.transport.ssh.subprocess.Popen",
                            side_effect=OSError("open failed")):
                with self.assertRaisesRegex(OSError, "open failed"):
                    transport.open_cmd("true")
            self.assertEqual(arbiter.active, 0)

            def run(*args, **kwargs):
                self.assertEqual(arbiter.active, 1)
                acquired = arbiter._cond.acquire(blocking=False)
                self.assertTrue(acquired)
                if acquired:
                    arbiter._cond.release()
                return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

            with mock.patch("host.transport.ssh.subprocess.run", side_effect=run):
                self.assertEqual(transport.exec("true"), (0, "ok", ""))
            self.assertEqual(arbiter.active, 0)

    def test_popen_poll_wait_communicate_kill_and_double_release(self):
        arbiter = TransportScheduler(global_limit=1, wait_timeout=0.1)
        with tempfile.TemporaryDirectory() as td:
            transport = self._transport(arbiter, td)
            raw = _FakeProc()
            with mock.patch("host.transport.ssh.subprocess.Popen",
                            return_value=raw):
                proc = transport.open_cmd("true")
            self.assertIsInstance(proc, ScheduledPopen)
            self.assertEqual(arbiter.active, 1)
            self.assertIsNone(proc.poll())
            self.assertEqual(arbiter.active, 1)
            raw.returncode = 0
            self.assertEqual(proc.poll(), 0)
            self.assertEqual(arbiter.active, 0)
            self.assertEqual(proc.wait(), 0)
            proc.kill()
            self.assertEqual(arbiter.active, 0)

            for method in ("wait", "communicate", "kill"):
                raw = _FakeProc()
                with mock.patch("host.transport.ssh.subprocess.Popen",
                                return_value=raw):
                    proc = transport.open_cmd("true")
                getattr(proc, method)()
                self.assertEqual(arbiter.active, 0)

            # kill() only requests termination; a still-live process keeps
            # its lease until poll/wait confirms the terminal state.
            raw = _FakeProc()
            raw.kill = lambda: None
            with mock.patch("host.transport.ssh.subprocess.Popen",
                            return_value=raw):
                proc = transport.open_cmd("true")
            proc.kill()
            self.assertEqual(arbiter.active, 1)
            raw.returncode = -9
            self.assertEqual(proc.poll(), -9)
            self.assertEqual(arbiter.active, 0)

            raw = _FakeProc()
            raw.kill = mock.Mock(side_effect=OSError("signal failed"))
            with mock.patch("host.transport.ssh.subprocess.Popen",
                            return_value=raw):
                proc = transport.open_cmd("true")
            with self.assertRaisesRegex(OSError, "signal failed"):
                proc.kill()
            self.assertEqual(arbiter.active, 1)
            raw.returncode = 1
            proc.wait()
            self.assertEqual(arbiter.active, 0)

    def test_download_and_upload_hold_lease_until_function_exit(self):
        arbiter = TransportScheduler(global_limit=1, wait_timeout=0.1)
        with tempfile.TemporaryDirectory() as td:
            transport = self._transport(arbiter, td)
            download_proc = _BufferProc(b"payload")
            upload_proc = _BufferProc()

            def popen(*args, **kwargs):
                self.assertEqual(arbiter.active, 1)
                return download_proc if kwargs.get("stdout") is not None and \
                    kwargs.get("stdin") is None else upload_proc

            with mock.patch("host.transport.ssh.subprocess.Popen",
                            side_effect=popen):
                target = io.BytesIO()
                self.assertEqual(transport.download("/remote", target), 7)
                self.assertEqual(arbiter.active, 0)
                self.assertEqual(target.getvalue(), b"payload")
                self.assertEqual(transport.upload(io.BytesIO(b"abc"),
                                                  "/remote"), 3)
                self.assertEqual(arbiter.active, 0)

    def test_factory_propagates_scheduler_identity_and_workload(self):
        arbiter = TransportScheduler()
        with tempfile.TemporaryDirectory() as td:
            transport = make_transport({
                "id": "stored-id", "type": "ssh", "host": "example",
            }, control_dir=td, scheduler=arbiter, device_id="explicit-id",
                workload=BACKGROUND)
        self.assertIs(transport.scheduler, arbiter)
        self.assertEqual((transport.device_id, transport.workload),
                         ("explicit-id", BACKGROUND))


if __name__ == "__main__":
    unittest.main()
