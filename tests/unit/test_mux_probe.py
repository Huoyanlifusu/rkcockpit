#!/usr/bin/env python3
"""Tests for the read-only, privacy-preserving ControlMaster probe."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from tools import probe_controlmaster as probe


class Result:
    def __init__(self, returncode):
        self.returncode = returncode


class MuxProbeUnitTest(unittest.TestCase):
    def _inventory(self, td, devices):
        root = Path(td)
        known_hosts = root / "ssh-known-hosts"
        known_hosts.write_text("pinned-host ssh-ed25519 AAAA\n")
        known_hosts.chmod(0o600)
        return str(root), devices, {}

    def _device(self, host="host-a", user="root", **extra):
        item = {"id": "private-device-id", "name": "private-name",
                "type": "ssh", "host": host, "port": 22, "user": user}
        item.update(extra)
        return item

    def test_profile_collapse_invalidates_topology(self):
        with tempfile.TemporaryDirectory() as td:
            root, devices, keys = self._inventory(
                td, [self._device(), self._device()])
            profiles, counts, topology, known_hosts = probe.build_profiles(
                root, devices, keys)
            self.assertEqual(len(profiles), 1)
            self.assertEqual(counts["unique_profiles"], 1)
            self.assertEqual(counts["profile_collapses"], 1)
            self.assertFalse(topology)
            self.assertTrue(known_hosts)

    def test_stale_socket_and_failed_check_are_aggregate_only(self):
        with tempfile.TemporaryDirectory() as td:
            root, devices, keys = self._inventory(td, [self._device(
                host="secret.example", user="secret-user")])
            profiles, _, _, _ = probe.build_profiles(root, devices, keys)
            profile = next(iter(profiles.values()))
            Path(profile["control_path"]).parent.mkdir(mode=0o700)
            Path(profile["control_path"]).touch()
            calls = []

            def failed(argv, **kwargs):
                calls.append((argv, kwargs))
                return Result(255)

            report = probe.probe(root, devices, keys, runner=failed,
                                 exists=lambda _path: True)
            self.assertEqual(report["control"], {
                "socket_present": 1, "check_attempts": 1,
                "check_success": 0, "check_success_rate": 0.0})
            self.assertEqual(report["evidence_level"], "GAP")
            rendered = json.dumps(report)
            for secret in ("secret.example", "secret-user",
                           "private-device-id", "private-name", td):
                self.assertNotIn(secret, rendered)
            self.assertEqual(calls[0][0][-3:-1], ["-O", "check"])
            self.assertIn("ControlMaster=no", " ".join(calls[0][0]))
            self.assertNotIn("ControlMaster=auto", " ".join(calls[0][0]))
            self.assertNotIn("SSHPASS", calls[0][1]["env"])

    def test_success_is_candidate_but_never_claims_hitl_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root, devices, keys = self._inventory(
                td, [self._device(host="host-%d" % index)
                     for index in range(30)])
            profiles, _, _, _ = probe.build_profiles(root, devices, keys)
            seen = []

            def passed(argv, **kwargs):
                seen.append(argv)
                return Result(0)

            report = probe.probe(root, devices, keys, runner=passed,
                                 exists=lambda _path: True)
            self.assertEqual(report["evidence_level"],
                             "SSH_HITL_CANDIDATE")
            self.assertFalse(report["hitl_pass_claimed"])
            flat = " ".join(seen[0])
            self.assertIn("StrictHostKeyChecking=yes", flat)
            self.assertIn("UserKnownHostsFile=%s" %
                          os.path.join(root, "ssh-known-hosts"), flat)

    def test_password_profiles_are_counted_but_never_executed(self):
        with tempfile.TemporaryDirectory() as td:
            root, devices, keys = self._inventory(td, [self._device(
                host="password-host", _password_b64="do-not-decode")])

            def forbidden(*_args, **_kwargs):
                self.fail("password profile reached subprocess")

            report = probe.probe(root, devices, keys, latency=True,
                                 iterations=2, runner=forbidden)
            self.assertEqual(report["counts"]["password_profiles"], 1)
            self.assertEqual(report["counts"]["mux_eligible_profiles"], 0)
            self.assertEqual(report["evidence_level"], "GAP")
            rendered = json.dumps(report)
            self.assertNotIn("password-host", rendered)
            self.assertNotIn("do-not-decode", rendered)

    def test_latency_uses_true_cold_and_warm_with_bounded_aggregates(self):
        with tempfile.TemporaryDirectory() as td:
            root, devices, keys = self._inventory(td, [self._device()])
            profiles, _, _, _ = probe.build_profiles(root, devices, keys)
            profile = next(iter(profiles.values()))
            Path(profile["control_path"]).parent.mkdir(mode=0o700)
            Path(profile["control_path"]).touch()
            commands = []
            ticks = iter(i / 1000.0 for i in range(100))

            def passed(argv, **kwargs):
                commands.append(argv)
                self.assertNotIn("SSHPASS", kwargs["env"])
                return Result(0)

            report = probe.probe(root, devices, keys, latency=True,
                                 iterations=2, timeout=7, runner=passed,
                                 exists=lambda _path: True,
                                 clock=lambda: next(ticks))
            latency_commands = commands[1:]
            self.assertEqual(len(latency_commands), 4)
            self.assertTrue(all(command[-1] == "true"
                                for command in latency_commands))
            flattened = [" ".join(command) for command in latency_commands]
            self.assertTrue(any("ControlMaster=no" in value
                                for value in flattened))
            self.assertTrue(any("ControlMaster=auto" in value
                                for value in flattened))
            self.assertTrue(all("ConnectTimeout=7" in value
                                for value in flattened))
            self.assertEqual(report["latency"]["cold"]["success"], 2)
            self.assertEqual(report["latency"]["warm"]["success"], 2)

    def test_default_probe_does_not_create_control_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root, devices, keys = self._inventory(td, [self._device()])
            control_dir = Path(root) / "ssh-control"
            report = probe.probe(root, devices, keys,
                                 runner=lambda *_a, **_k: Result(0))
            self.assertFalse(control_dir.exists())
            self.assertEqual(report["control"]["check_attempts"], 0)

    def test_profile_and_cli_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            root, devices, keys = self._inventory(
                td, [self._device(host="host-%d" % i) for i in range(129)])
            with self.assertRaisesRegex(ValueError, "more than 128"):
                probe.build_profiles(root, devices, keys)

        for option in (("--iterations", "31"), ("--iterations", "0"),
                       ("--timeout", "31"), ("--timeout", "0")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                probe.main(list(option))
            self.assertNotIn(os.path.expanduser("~"), stderr.getvalue())

    def test_missing_or_invalid_known_hosts_is_gap_without_path_leak(self):
        with tempfile.TemporaryDirectory() as td:
            report = probe.probe(td, [self._device(host="private-host")], {})
            self.assertFalse(report["known_hosts_valid"])
            self.assertEqual(report["evidence_level"], "GAP")
            rendered = json.dumps(report)
            self.assertNotIn("private-host", rendered)
            self.assertNotIn(td, rendered)


if __name__ == "__main__":
    unittest.main()
