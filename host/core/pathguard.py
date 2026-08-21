"""Race-safe local filesystem path handling.

The security boundary is a directory descriptor, not a path string.  Every
component below that descriptor is opened with ``O_NOFOLLOW``.  Keeping this
small module independent of the HTTP layer also makes the same rules apply to
browser, agent, and background transfer entry points.
"""
import errno
import os
import stat


HOME = os.path.abspath(os.path.expanduser("~"))
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _parts(path):
    parts = []
    for item in path.split(os.sep):
        if item in ("", "."):
            continue
        if item == "..":
            raise ValueError("路径越界: %s" % path)
        if "\x00" in item:
            raise ValueError("路径包含 NUL")
        parts.append(item)
    return parts


def _open_absolute_dir(path):
    """Open an absolute directory without following any component symlink."""
    path = os.path.abspath(path)
    fd = os.open(os.sep, _DIR_FLAGS)
    try:
        for item in _parts(path):
            nxt = os.open(item, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


class DirRoot(object):
    """A trusted directory-fd anchor and relative path parser.

    Constructing the object rejects a symlink in the configured anchor itself.
    Callers own returned descriptors and must close them.
    """

    def __init__(self, path):
        self.path = os.path.abspath(path)
        try:
            self.fd = _open_absolute_dir(self.path)
        except OSError as exc:
            raise ValueError("不可信目录根 %s: %s" % (self.path, exc))

    def close(self):
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def components(self, path):
        return _parts((path or "").strip().lstrip(os.sep))

    def parent(self, components, create=False, mode=0o777):
        """Return ``(parent_fd, leaf)`` below this root."""
        if not components:
            return os.dup(self.fd), ""
        fd = os.dup(self.fd)
        try:
            for item in components[:-1]:
                try:
                    nxt = os.open(item, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(item, mode, dir_fd=fd)
                    nxt = os.open(item, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = nxt
            return fd, components[-1]
        except BaseException:
            os.close(fd)
            raise

    def open_dir(self, components):
        fd = os.dup(self.fd)
        try:
            for item in components:
                nxt = os.open(item, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = nxt
            return fd
        except BaseException:
            os.close(fd)
            raise

    def mkdirs(self, components, mode=0o777):
        fd = os.dup(self.fd)
        try:
            for item in components:
                try:
                    os.mkdir(item, mode, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(item, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = nxt
        finally:
            os.close(fd)


def rooted_components(path):
    path = (path or "").strip()
    if path in ("", "~", "/"):
        return []
    return _parts(path.lstrip(os.sep))


def local_components(path, home=None, write=False):
    """Return ``(anchor_path, components)`` preserving legacy local semantics.

    Reads may address the real filesystem and therefore anchor at ``/``.
    Mutations are anchored at ``home`` and cannot address the anchor itself.
    """
    home = os.path.abspath(home or HOME)
    raw = (path or "").strip()
    if raw == "~":
        absolute = home
    elif raw.startswith("~/"):
        absolute = os.path.join(home, raw[2:])
    else:
        absolute = os.path.abspath(raw)
    if write:
        try:
            common = os.path.commonpath((home, absolute))
        except ValueError:
            common = ""
        if common != home or absolute == home:
            raise ValueError("拒绝危险路径（上位机 home 之外）: %s" % path)
        return home, _parts(os.path.relpath(absolute, home))
    return os.sep, _parts(absolute)


# Compatibility helpers retained for callers that only need display/validation.
def resolve_local(path, home=None, write=False):
    anchor, components = local_components(path, home=home, write=write)
    return os.path.join(anchor, *components) if components else anchor


def resolve_rooted(path, root):
    return os.path.join(os.path.abspath(root), *rooted_components(path))


def guard_host_write(path, home=None):
    return resolve_local(path, home=home, write=True)


def is_dangerous_remove(path):
    return os.path.abspath(path) == "/"


def reject_symlink(st, path):
    if stat.S_ISLNK(st.st_mode):
        raise OSError(errno.ELOOP, "拒绝符号链接", path)
