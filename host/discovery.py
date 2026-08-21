import fnmatch
import ipaddress
import os
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_SKIP_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_CONFIG_KEYWORDS = ("host", "hostname")
_PATTERN_CHARS = ("*", "?", "!", "[", "]")


def _matches(value, rule):
    """Handle matches."""
    value = str(value or "")
    rule = str(rule or "")
    if not rule:
        return False
    if "/" in rule:
        try:
            return ipaddress.ip_address(value) in\
                ipaddress.ip_network(rule, strict=False)
        except ValueError:
            pass
    if any(ch in rule for ch in "*?"):
        return fnmatch.fnmatch(value, rule)
    return value == rule


def filter_ssh(hosts, rules):
    """Handle filter ssh."""
    ips = (rules or {}).get("ips") or []
    return [h for h in hosts if not any(
        _matches(h.get("host") if isinstance(h, dict) else h, rule)
        for rule in ips)]


def filter_adb(devices, rules):
    """Handle filter adb."""
    serials = (rules or {}).get("serials") or []
    return [d for d in devices
            if not any(_matches(d.get("serial") or "", rule)
                       for rule in serials)]


def filter_ssh_banner(hosts, rules):
    """Handle filter ssh banner."""
    exclude = (rules or {}).get("banner_exclude") or []
    if not exclude:
        return list(hosts)
    lowered = [str(k).lower() for k in exclude if str(k).strip()]
    out = []
    for h in hosts:
        banner = (h.get("banner") or "").lower() if isinstance(h, dict)\
            else ""
        if banner and any(k in banner for k in lowered):
            continue
        out.append(h)
    return out


def adb_devices():
    """Handle adb devices."""
    try:
        proc = subprocess.run(
            ["adb", "devices", "-l"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=3, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    devices = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        info = {"serial": tokens[0], "state": tokens[1],
                "product": "", "model": "", "device": "",
                "transport_id": ""}
        for token in tokens[2:]:
            if ":" in token:
                key, _, value = token.partition(":")
                if key in info:
                    info[key] = value
        devices.append(info)
    return devices


def _arp_hosts():
    """Handle arp hosts."""
    hosts = []
    try:
        proc = subprocess.run(
            ["ip", "neigh", "show"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=2, text=True)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                tokens = line.split()
                if tokens and _IPV4_RE.match(tokens[0]):
                    hosts.append(tokens[0])
            return hosts
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        with open("/proc/net/arp", encoding="utf-8") as fh:
            for line in fh:
                tokens = line.split()
                if len(tokens) < 4 or not _IPV4_RE.match(tokens[0]):
                    continue
                try:
                    flags = int(tokens[3], 16)
                except ValueError:
                    continue
                if tokens[0] == "0.0.0.0" or not flags & 0x2:
                    continue
                hosts.append(tokens[0])
    except OSError:
        pass
    return hosts


def _ssh_config_hosts():
    """Handle ssh config hosts."""
    path = os.path.join(os.path.expanduser("~"), ".ssh", "config")
    hosts = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            tokens = fh.read().split()
    except OSError:
        return hosts
    host_names, host_aliases = [], []
    for i, token in enumerate(tokens):
        if token.lower() not in _CONFIG_KEYWORDS:
            continue
        if i + 1 >= len(tokens):
            continue
        value = tokens[i + 1]
        if any(ch in value for ch in _PATTERN_CHARS):
            continue
        if token.lower() == "hostname":
            host_names.append(value)
        else:
            host_aliases.append(value)
    hosts = host_names or host_aliases
    return hosts


def _known_hosts():
    """Handle known hosts."""
    path = os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")
    hosts = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return hosts
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("|1|"):
            continue
        first = line.split()[0]
        if first.startswith("[") and "]" in first:
            first = first[1:first.index("]")]
        hosts.append(first)
    return hosts


def ssh_candidates(devices=None):
    """Handle ssh candidates."""
    hosts = []
    for device in devices or []:
        if isinstance(device, dict) and device.get("type") == "ssh"\
                and device.get("host"):
            hosts.append(str(device["host"]))
    hosts.extend(_arp_hosts())
    hosts.extend(_ssh_config_hosts())
    hosts.extend(_known_hosts())
    seen, unique = set(), []
    for host in hosts:
        key = host.lower()
        if key in _SKIP_HOSTS or key in seen:
            continue
        seen.add(key)
        unique.append(host)
    return sorted(unique)


def probe_ssh(host, port=22, timeout=0.6):
    """Handle probe ssh."""
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        banner = sock.recv(64).decode(errors="replace").strip()
        if banner.startswith("SSH-"):
            return {"host": host, "port": port, "banner": banner}
    except (OSError, socket.timeout):
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return None


def discover_ssh(devices=None, port=22, timeout=0.6, max_workers=16):
    """Handle discover ssh."""
    candidates = ssh_candidates(devices)
    if not candidates:
        return []
    found = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(candidates))) as pool:
        futures = {pool.submit(probe_ssh, host, port, timeout): host
                   for host in candidates}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                result = None
            if result:
                found.append(result)
    return sorted(found, key=lambda item: item["host"])


def discover(devices=None, port=22, timeout=0.6, rules=None):
    """Handle discover."""
    adb_all = adb_devices()
    ssh_all = discover_ssh(devices, port, timeout)
    adb = filter_adb(adb_all, rules)
    ssh_banner_filtered = filter_ssh_banner(ssh_all, rules)
    ssh = filter_ssh(ssh_banner_filtered, rules)
    return {
        "adb": adb,
        "ssh": ssh,
        "filtered": {
            "adb": len(adb_all) - len(adb),

            "ssh": len(ssh_all) - len(ssh),
        },
        "generated_at": int(time.time() * 1000),
    }


__all__ = ["adb_devices", "ssh_candidates", "probe_ssh", "discover_ssh",
           "_matches", "filter_ssh", "filter_adb", "filter_ssh_banner",
           "discover"]
