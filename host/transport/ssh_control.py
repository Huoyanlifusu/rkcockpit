"""OpenSSH ControlMaster option builder.

The control socket name contains only a digest of the connection identity.  A
private directory is used so another local user cannot attach to the master.
Password/sshpass sessions deliberately do not use this helper.
"""
import hashlib
import os
import stat


DEFAULT_PERSIST_SECONDS = 60
_MAX_CONTROL_PATH = 96


def _private_dir(path):
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(path, mode=0o700, exist_ok=True)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        os.chmod(path, 0o700)
    return path


def control_path(control_dir, host, port, user, key_path=None,
                 known_hosts_identity=None):
    """Return a short, private ControlPath for one SSH authentication profile."""
    identity = "\0".join((str(host), str(port), str(user),
                           os.path.realpath(key_path) if key_path else "<default>",
                           str(known_hosts_identity or "<legacy>")))
    digest = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()
    directory = _private_dir(control_dir)
    path = os.path.join(directory, "cm-" + digest[:32])
    if len(path.encode("utf-8")) > _MAX_CONTROL_PATH:
        fallback = os.path.join("/tmp", "rkss-ssh-%d" % os.getuid(),
                                hashlib.sha256(directory.encode("utf-8")).hexdigest()[:12])
        path = os.path.join(_private_dir(fallback), "cm-" + digest[:32])
    return path


def control_options(control_dir, host, port, user, key_path=None,
                    persist_seconds=DEFAULT_PERSIST_SECONDS,
                    known_hosts_identity=None):
    path = control_path(control_dir, host, port, user, key_path,
                        known_hosts_identity)
    persist = min(max(int(persist_seconds), 1), 3600)
    return ["-o", "ControlMaster=auto",
            "-o", "ControlPersist=%d" % persist,
            "-o", "ControlPath=%s" % path]


__all__ = ["control_options", "control_path", "DEFAULT_PERSIST_SECONDS"]
