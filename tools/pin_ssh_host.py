#!/usr/bin/env python3
"""Discover an SSH host key and pin only an out-of-band confirmed fingerprint."""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from host.transport.ssh_known_hosts import parse_keyscan, pin_key


def discover(host, port, timeout):
    if not host or len(host) > 253 or any(ch.isspace() or ord(ch) < 32 for ch in host):
        raise ValueError("invalid host")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    proc = subprocess.run(["ssh-keyscan", "-T", str(timeout), "-p", str(port),
                           "--", host], capture_output=True, timeout=timeout + 2)
    if len(proc.stdout) > (1 << 20) or len(proc.stderr) > (1 << 16):
        raise ValueError("ssh-keyscan response exceeds limit")
    candidates = parse_keyscan(proc.stdout.decode("ascii", "strict"))
    if not candidates:
        raise ValueError("ssh-keyscan returned no valid host keys")
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--fingerprint", required=True,
                        help="out-of-band SHA256 fingerprint to confirm")
    parser.add_argument("--known-hosts-file", required=True)
    parser.add_argument("--timeout", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        matches = [item for item in discover(args.host, args.port, args.timeout)
                   if item[2] == args.fingerprint]
        if len(matches) != 1:
            raise ValueError("fingerprint did not identify exactly one discovered key")
        changed = pin_key(args.known_hosts_file, args.host, args.port,
                          matches[0][0], matches[0][1], args.fingerprint)
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print("pinned" if changed else "already pinned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
