#!/usr/bin/env python3
"""Safely create or validate the portal's single-admin token.

The destination directory must be controlled by the installer identity.  An
existing token is validated but never chmod/chown'ed, which prevents a
privileged installer from following a user-planted symlink.
"""
import argparse
import os
import pwd
import secrets
import stat
import tempfile


class TokenInstallError(RuntimeError):
    pass


def _validate_directory(directory, installer_uid):
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        os.mkdir(directory, 0o755)
        # mkdir is filtered by the caller's umask.  Normalize the new,
        # installer-owned directory so the service user can traverse it even
        # when root runs the installer with umask 077.
        dir_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fchmod(dir_fd, 0o755)
        finally:
            os.close(dir_fd)
        info = os.lstat(directory)
    except OSError as exc:
        raise TokenInstallError("cannot inspect auth directory: %s" % exc)
    if not stat.S_ISDIR(info.st_mode):
        raise TokenInstallError("auth directory must be a real directory")
    if info.st_uid != installer_uid:
        raise TokenInstallError("auth directory must be owned by the installer")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise TokenInstallError("auth directory must not be group/other writable")
    if not stat.S_IMODE(info.st_mode) & 0o001:
        raise TokenInstallError("auth directory must be traversable by the service user")


def _validate_existing(path, service_uid):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise TokenInstallError("cannot inspect existing token: %s" % exc)
    if not stat.S_ISREG(info.st_mode):
        raise TokenInstallError("existing token must be a regular file, not a link")
    if info.st_nlink != 1:
        raise TokenInstallError("existing token must have exactly one link")
    if info.st_uid != service_uid:
        raise TokenInstallError("existing token has an unexpected owner")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise TokenInstallError("existing token permissions must be 0600")
    return path


def ensure_token(directory, service_uid, service_gid, filename="auth-token",
                 installer_uid=None):
    """Atomically create a 0600 token or validate an existing safe token."""
    installer_uid = os.geteuid() if installer_uid is None else installer_uid
    directory = os.path.abspath(directory)
    _validate_directory(directory, installer_uid)
    path = os.path.join(directory, filename)
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        return _validate_existing(path, service_uid)

    fd = None
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".auth-token.", dir=directory)
        os.fchmod(fd, 0o600)
        os.fchown(fd, service_uid, service_gid)
        payload = (secrets.token_urlsafe(32) + "\n").encode("ascii")
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        # directory is installer-owned and not writable by the service user,
        # so the absent destination cannot be replaced between checks.
        os.replace(temporary, path)
        temporary = None
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return _validate_existing(path, service_uid)
    except OSError as exc:
        raise TokenInstallError("cannot create auth token: %s" % exc)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--user", required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("must run as root")
    try:
        account = pwd.getpwnam(args.user)
        ensure_token(args.dir, account.pw_uid, account.pw_gid)
    except (KeyError, TokenInstallError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
