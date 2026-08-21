"""Private, explicitly pinned OpenSSH host-key storage.

``ssh-keyscan`` is only a key-discovery mechanism.  A candidate is persisted
only after its SHA256 fingerprint matches a value supplied out of band.
"""
import base64
import hashlib
import os
import re
import stat
import tempfile


_KEY_TYPE_RE = re.compile(r"^(ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+$")


def default_known_hosts_path(control_dir):
    directory = os.path.dirname(os.path.abspath(os.path.expanduser(control_dir)))
    return os.path.join(directory, "ssh-known-hosts")


def host_token(host, port):
    host = str(host)
    port = int(port)
    if (not host or len(host) > 253 or any(ch.isspace() or ord(ch) < 33
                                           for ch in host) or
            any(ch in host for ch in ",*?!|")):
        raise ValueError("invalid literal SSH host")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("SSH host must be ASCII (use its IDNA form)") from exc
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return host if port == 22 else "[%s]:%d" % (host, port)


def fingerprint(key_data):
    try:
        raw = base64.b64decode(key_data.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid SSH public-key data") from exc
    value = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")
    return "SHA256:" + value.rstrip("=")


def parse_keyscan(output):
    """Return unique ``(key_type, key_data, fingerprint)`` candidates."""
    result = []
    seen = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3 or not _KEY_TYPE_RE.match(fields[1]):
            continue
        candidate = (fields[1], fields[2])
        if candidate in seen:
            continue
        seen.add(candidate)
        result.append((candidate[0], candidate[1], fingerprint(candidate[1])))
    return result


def _validate_private_directory(directory, create=False):
    directory = os.path.abspath(os.path.expanduser(directory))
    if create:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("known_hosts directory must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("known_hosts directory must have mode 0700 or stricter")
    return directory


def validate_known_hosts(path):
    path = os.path.abspath(os.path.expanduser(path))
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("known_hosts must be an owner-only regular file (0600)")
    _validate_private_directory(os.path.dirname(path))
    return path


def has_host(path, host, port):
    token = host_token(host, port)
    with open(validate_known_hosts(path), "r", encoding="utf-8") as src:
        for line in src:
            fields = line.split()
            if fields and token in fields[0].split(","):
                return True
    return False


def file_identity(path):
    digest = hashlib.sha256()
    with open(validate_known_hosts(path), "rb") as src:
        while True:
            chunk = src.read(1 << 16)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def pin_key(path, host, port, key_type, key_data, expected_fingerprint):
    """Atomically append one verified key; never replace a host's existing key."""
    actual = fingerprint(key_data)
    if actual != expected_fingerprint:
        raise ValueError("fingerprint mismatch: discovered %s" % actual)
    if not _KEY_TYPE_RE.match(key_type):
        raise ValueError("invalid SSH key type")
    path = os.path.abspath(os.path.expanduser(path))
    directory = _validate_private_directory(os.path.dirname(path), create=True)
    existing = b""
    if os.path.lexists(path):
        validate_known_hosts(path)
        with open(path, "rb") as src:
            existing = src.read(1 << 20)
            if src.read(1):
                raise ValueError("known_hosts exceeds 1 MiB")
        if has_host(path, host, port):
            line = "%s %s %s" % (host_token(host, port), key_type, key_data)
            if line.encode("ascii") in existing.splitlines():
                return False
            raise ValueError("host already pinned; refusing key replacement")
    line = ("%s %s %s\n" % (host_token(host, port), key_type, key_data)).encode("ascii")
    fd, tmp = tempfile.mkstemp(prefix=".ssh-known-hosts-", dir=directory)
    os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, "wb") as dst:
            dst.write(existing)
            if existing and not existing.endswith(b"\n"):
                dst.write(b"\n")
            dst.write(line)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return True


__all__ = ["default_known_hosts_path", "file_identity", "fingerprint",
           "has_host", "host_token", "parse_keyscan", "pin_key",
           "validate_known_hosts"]
