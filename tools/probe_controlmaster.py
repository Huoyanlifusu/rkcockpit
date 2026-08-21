#!/usr/bin/env python3
"""Privacy-preserving, read-only probe for rkss OpenSSH ControlMaster reuse.

The default mode only inspects existing sockets and asks local OpenSSH masters
for their status.  ``--latency`` additionally executes the harmless remote
command ``true`` with multiplexing disabled/enabled.  Output is aggregate JSON:
connection identifiers and subprocess diagnostics are deliberately omitted.
"""
import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from host.transport.ssh_control import DEFAULT_PERSIST_SECONDS
from host.transport.ssh_known_hosts import (default_known_hosts_path,
                                             file_identity)


MAX_PROFILES = 128
MAX_CONFIG_BYTES = 4 << 20
MAX_ITERATIONS = 30
MAX_TIMEOUT = 30
_MAX_CONTROL_PATH = 96


def _read_json(path, default):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | \
        getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return default
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("configuration input is not a regular file")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("configuration input exceeds 4 MiB")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("configuration input is not valid JSON") from exc


def load_inventory(conf_dir):
    """Read devices/key metadata without constructing stores or writing files."""
    root = os.path.abspath(os.path.expanduser(conf_dir))
    devices = _read_json(os.path.join(root, "devices.json"), [])
    keys = _read_json(os.path.join(root, "keys", "keys.json"), {})
    if not isinstance(devices, list) or not isinstance(keys, dict):
        raise ValueError("configuration input has an invalid top-level shape")
    return root, devices, keys


def _key_path(root, keys, key_ref):
    if not key_ref:
        return None, True
    meta = keys.get(key_ref)
    if not isinstance(meta, dict) or meta.get("type") not in ("ed25519", "rsa"):
        return None, False
    path = os.path.join(root, "keys", str(key_ref),
                        "id_" + meta["type"])
    try:
        info = os.lstat(path)
    except OSError:
        return None, False
    if not stat.S_ISREG(info.st_mode):
        return None, False
    return path, True


def _profile_key(device, key_path, password_mode, known_hosts_identity):
    return (str(device.get("host")), int(device.get("port") or 22),
            str(device.get("user") or "root"),
            os.path.realpath(key_path) if key_path else "<default>",
            "password" if password_mode else str(known_hosts_identity))


def _control_path_readonly(control_dir, profile):
    """Mirror ssh_control.control_path without creating/chmodding directories."""
    host, port, user, key_identity, known_hosts_identity = profile
    identity = "\0".join((host, str(port), user, key_identity,
                           known_hosts_identity))
    digest = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()
    directory = os.path.abspath(os.path.expanduser(control_dir))
    path = os.path.join(directory, "cm-" + digest[:32])
    if len(path.encode("utf-8")) > _MAX_CONTROL_PATH:
        fallback = os.path.join(
            "/tmp", "rkss-ssh-%d" % os.getuid(),
            hashlib.sha256(directory.encode("utf-8")).hexdigest()[:12])
        path = os.path.join(fallback, "cm-" + digest[:32])
    return path


def build_profiles(root, devices, keys):
    """Return private in-memory profiles plus aggregate inventory counts."""
    control_dir = os.path.join(root, "ssh-control")
    known_hosts = default_known_hosts_path(control_dir)
    try:
        known_hosts_identity = file_identity(known_hosts)
        known_hosts_valid = True
    except (OSError, ValueError):
        known_hosts_identity = "unpinned"
        known_hosts_valid = False

    ssh_devices = 0
    invalid = 0
    password_devices = 0
    eligible_devices = 0
    unique_all = set()
    profiles = {}
    for device in devices:
        if not isinstance(device, dict) or device.get("type") != "ssh":
            continue
        ssh_devices += 1
        host = device.get("host")
        user = device.get("user") or "root"
        try:
            port = int(device.get("port") or 22)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if (not isinstance(host, str) or not host or not isinstance(user, str)
                or not user or not 1 <= port <= 65535):
            invalid += 1
            continue
        key_path, key_valid = _key_path(root, keys, device.get("key_ref"))
        if not key_valid:
            invalid += 1
            continue
        has_password = bool(device.get("_password_b64"))
        password_mode = has_password and not key_path
        profile_key = _profile_key(device, key_path, password_mode,
                                   known_hosts_identity)
        unique_all.add(profile_key)
        if password_mode:
            password_devices += 1
            continue
        eligible_devices += 1
        if profile_key not in profiles:
            profiles[profile_key] = {
                "host": host, "port": port, "user": user,
                "key_path": key_path, "known_hosts": known_hosts,
                "control_path": _control_path_readonly(control_dir,
                                                       profile_key),
            }
    if len(unique_all) > MAX_PROFILES:
        raise ValueError("configuration contains more than 128 SSH profiles")
    counts = {
        "ssh_devices": ssh_devices,
        "invalid_profiles": invalid,
        "password_profiles": password_devices,
        "mux_eligible_profiles": len(profiles),
        "mux_eligible_devices": eligible_devices,
        "unique_profiles": len(unique_all),
        "profile_collapses": max(eligible_devices - len(profiles), 0),
    }
    topology_valid = bool(ssh_devices == 30 and invalid == 0 and
                          password_devices == 0 and
                          eligible_devices == len(profiles) == 30)
    return profiles, counts, topology_valid, known_hosts_valid


def _ssh_base(profile, timeout, control_mode):
    argv = ["ssh", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=%d" % timeout,
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=%s" % profile["known_hosts"],
            "-o", "GlobalKnownHostsFile=/dev/null"]
    if profile["key_path"]:
        argv += ["-o", "IdentitiesOnly=yes", "-i", profile["key_path"]]
    if control_mode == "cold":
        argv += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    elif control_mode == "check":
        # A control operation must never create a replacement master or make a
        # network connection when a stale socket is encountered.
        argv += ["-o", "ControlMaster=no",
                 "-o", "ControlPath=%s" % profile["control_path"]]
    else:
        argv += ["-o", "ControlMaster=auto",
                 "-o", "ControlPersist=%d" % DEFAULT_PERSIST_SECONDS,
                 "-o", "ControlPath=%s" % profile["control_path"]]
    if profile["port"] != 22:
        argv += ["-p", str(profile["port"])]
    argv.append("%s@%s" % (profile["user"], profile["host"]))
    return argv


def _run_quiet(argv, timeout, runner=subprocess.run):
    env = dict(os.environ)
    env.pop("SSHPASS", None)
    try:
        result = runner(argv, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=timeout,
                        env=env, check=False)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _rate(success, attempts):
    return round(success / attempts, 6) if attempts else None


def _control_socket_exists(path):
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


def _latency_summary(values, attempts, success):
    ordered = sorted(values)
    p95 = None
    if ordered:
        p95 = ordered[max(0, int(len(ordered) * .95 + .999999) - 1)]
    return {
        "attempts": attempts,
        "success": success,
        "success_rate": _rate(success, attempts),
        "mean_ms": round(sum(ordered) / len(ordered), 3) if ordered else None,
        "p95_ms": round(p95, 3) if p95 is not None else None,
    }


def probe(root, devices, keys, latency=False, iterations=3, timeout=10,
          runner=subprocess.run, exists=_control_socket_exists,
          clock=time.monotonic):
    if not 1 <= int(iterations) <= MAX_ITERATIONS:
        raise ValueError("iterations must be between 1 and 30")
    if not 1 <= int(timeout) <= MAX_TIMEOUT:
        raise ValueError("timeout must be between 1 and 30 seconds")
    iterations = int(iterations)
    timeout = int(timeout)
    profiles, counts, topology_valid, known_hosts_valid = build_profiles(
        root, devices, keys)
    socket_present = 0
    check_attempts = 0
    check_success = 0
    for profile in profiles.values():
        if not exists(profile["control_path"]):
            continue
        socket_present += 1
        check_attempts += 1
        argv = _ssh_base(profile, timeout, "check")
        argv[-1:-1] = ["-O", "check"]
        if _run_quiet(argv, timeout, runner):
            check_success += 1

    report = {
        "schema_version": 1,
        "mode": "latency" if latency else "check",
        "counts": counts,
        "topology_valid": topology_valid,
        "known_hosts_valid": known_hosts_valid,
        "control": {
            "socket_present": socket_present,
            "check_attempts": check_attempts,
            "check_success": check_success,
            "check_success_rate": _rate(check_success, check_attempts),
        },
    }
    if latency:
        latency_data = {}
        for mode in ("cold", "warm"):
            samples = []
            attempts = 0
            successes = 0
            for profile in profiles.values():
                for _ in range(iterations):
                    attempts += 1
                    started = clock()
                    ok = _run_quiet(_ssh_base(profile, timeout, mode) +
                                    ["true"], timeout, runner)
                    elapsed = max((clock() - started) * 1000.0, 0.0)
                    if ok:
                        successes += 1
                        samples.append(elapsed)
            latency_data[mode] = _latency_summary(samples, attempts, successes)
        latency_data["iterations"] = iterations
        report["latency"] = latency_data

    all_checked = bool(profiles) and socket_present == len(profiles) and \
        check_success == len(profiles)
    latency_ok = (not latency or all(
        report["latency"][mode]["attempts"] > 0 and
        report["latency"][mode]["success"] ==
        report["latency"][mode]["attempts"] for mode in ("cold", "warm")))
    if topology_valid and known_hosts_valid and all_checked and latency_ok:
        evidence = "SSH_HITL_CANDIDATE"
    elif latency and bool(profiles):
        evidence = "STRESS_ONLY"
    else:
        evidence = "GAP"
    report["evidence_level"] = evidence
    report["hitl_pass_claimed"] = False
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conf-dir", default=os.path.join(
        os.path.expanduser("~"), ".rkss"))
    parser.add_argument("--latency", action="store_true")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= MAX_ITERATIONS:
        parser.error("--iterations must be between 1 and 30")
    if not 1 <= args.timeout <= MAX_TIMEOUT:
        parser.error("--timeout must be between 1 and 30 seconds")
    try:
        root, devices, keys = load_inventory(args.conf_dir)
        result = probe(root, devices, keys, args.latency, args.iterations,
                       args.timeout)
    except OSError:
        parser.error("configuration could not be read")
    except ValueError as exc:
        parser.error(str(exc))
    sys.stdout.write(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
