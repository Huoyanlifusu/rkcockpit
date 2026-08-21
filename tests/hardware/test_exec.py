"""exec buffering, cursor and global concurrency regression tests."""
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.api.handlers.legacy import HostApi
from host.task.execjob import ExecJobStore, ExecQueueFull, MAX_CONCURRENT
from host.task.output_buffer import OutputBuffer


class ChunkStream:
    def __init__(self, chunks, gate=None):
        self._chunks = list(chunks)
        self._gate = gate

    def read(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        if self._gate is not None:
            self._gate.wait(5)
        return b""


class FakeProc:
    _next_pid = 900000

    def __init__(self, chunks=(), gate=None):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.stdout = ChunkStream(chunks, gate)
        self.returncode = None
        self._gate = gate

    def wait(self):
        if self._gate is not None:
            self._gate.wait(5)
        self.returncode = 0
        return 0

    def send_signal(self, _sig):
        if self._gate is not None:
            self._gate.set()

    def kill(self):
        self.send_signal(None)


class FakeTransport:
    def __init__(self, factory):
        self.factory = factory

    def open_cmd(self, _cmd):
        return self.factory()


class FailingTransport:
    def open_cmd(self, _cmd):
        raise RuntimeError("open failed")


def wait_done(store, jid, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = store.poll(jid)
        if not result["running"]:
            return result
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class OutputBufferTests(unittest.TestCase):
    def test_ring_keeps_exact_newest_tail(self):
        buf = OutputBuffer(8)
        buf.append(b"abcdef")
        buf.append(b"ghijkl")

        text, offset, base, reset = buf.snapshot(final=True)

        self.assertEqual(text, "efghijkl")
        self.assertEqual((base, offset, reset), (4, 12, False))
        self.assertTrue(buf.truncated)

    def test_ring_keeps_512_kib_ascii_tail(self):
        limit = 512 * 1024
        buf = OutputBuffer(limit)
        buf.append(b"a" * (limit - 7))
        buf.append(b"0123456789")

        text, offset, base, reset = buf.snapshot(final=True)

        self.assertEqual(len(text.encode("utf-8")), limit)
        self.assertTrue(text.endswith("0123456789"))
        self.assertEqual((base, offset, reset), (3, limit + 3, False))

    def test_absolute_cursor_delta_and_stale_reset(self):
        buf = OutputBuffer(8)
        buf.append(b"abcd")
        self.assertEqual(buf.snapshot(offset=0), ("abcd", 4, 0, False))
        self.assertEqual(buf.snapshot(offset=4), ("", 4, 0, False))
        buf.append(b"efghijkl")
        self.assertEqual(buf.snapshot(offset=4), ("efghijkl", 12, 4, False))
        self.assertEqual(buf.snapshot(offset=0), ("efghijkl", 12, 4, True))
        with self.assertRaises(ValueError):
            buf.snapshot(offset=13)

    def test_cursor_must_be_on_utf8_boundary(self):
        buf = OutputBuffer(64)
        buf.append("😀".encode("utf-8"))
        with self.assertRaises(ValueError):
            buf.snapshot(offset=1)

    def test_split_utf8_is_withheld_until_complete(self):
        raw = "A😀B".encode("utf-8")
        buf = OutputBuffer(64)
        buf.append(raw[:3])
        self.assertEqual(buf.snapshot(offset=0), ("A", 1, 0, False))
        buf.append(raw[3:])
        self.assertEqual(buf.snapshot(offset=1), ("😀B", len(raw), 0, False))


class ExecJobTests(unittest.TestCase):
    def test_open_failure_releases_global_slot(self):
        store = ExecJobStore(FailingTransport())
        for _ in range(MAX_CONCURRENT + 2):
            jid = store.run("fail")
            result = store.poll(jid)
            self.assertFalse(result["running"])
            self.assertIn("open failed", result["output"])

    def test_reader_preserves_utf8_across_transport_chunks(self):
        raw = "开😀终".encode("utf-8")
        chunks = (raw[:2], raw[2:5], raw[5:])
        store = ExecJobStore(FakeTransport(lambda: FakeProc(chunks)))

        jid = store.run("ignored")
        result = wait_done(store, jid)

        self.assertEqual(result["output"], "开😀终")
        self.assertEqual(result["offset"], len(raw))

    def test_global_limit_is_shared_across_stores(self):
        gates = [threading.Event() for _ in range(MAX_CONCURRENT)]
        stores = [ExecJobStore(FakeTransport(
            lambda gate=gate: FakeProc(gate=gate))) for gate in gates]
        jobs = [store.run("block") for store in stores]
        ninth = ExecJobStore(FakeTransport(lambda: FakeProc()))
        try:
            with self.assertRaises(ExecQueueFull):
                ninth.run("must-not-start")
        finally:
            for gate in gates:
                gate.set()
            for store, jid in zip(stores, jobs):
                wait_done(store, jid)


class HostApiExecTests(unittest.TestCase):
    def test_exec_store_creation_is_singleton_under_race(self):
        with TemporaryDirectory() as tmp:
            host = HostApi(tmp)
            calls = []
            transport = object()

            def make(_did):
                calls.append(1)
                time.sleep(0.02)
                return transport

            host._transport = make
            stores = []
            threads = [threading.Thread(
                target=lambda: stores.append(host._exec_store("dev")))
                for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(calls), 1)
            self.assertEqual(len({id(store) for store in stores}), 1)

    def test_exec_poll_rejects_invalid_offset(self):
        with TemporaryDirectory() as tmp:
            host = HostApi(tmp)

            class Store:
                def poll(self, _jid, offset=None):
                    if offset == 99:
                        raise ValueError("offset exceeds current output")
                    return {"offset": offset}

            host.exec_stores["dev"] = Store()
            for value in ("abc", "-1", "+1", "1_0", " 1", "99"):
                result, status = host.exec_poll(
                    "dev", {"job_id": ["e1"], "offset": [value]})
                self.assertEqual(status, 400)
                self.assertFalse(result["ok"])


class FrontendContractTests(unittest.TestCase):
    def test_terminal_uses_server_cursor_and_recursive_timeout(self):
        source = (Path(__file__).parents[1] /
                  "static/js/pages/term.js").read_text(encoding="utf-8")
        self.assertIn('"&offset=" + encodeURIComponent(TERM.offset)', source)
        self.assertIn("setTimeout(pollOnce, 500)", source)
        self.assertNotIn("setInterval", source)
        self.assertNotIn("outLen", source)


if __name__ == "__main__":
    unittest.main()
