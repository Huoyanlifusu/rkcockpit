#!/usr/bin/env python3
"""Security regression tests for privileged token installation."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from deploy.create_auth_token import TokenInstallError, ensure_token


class TokenInstallUnitTest(unittest.TestCase):
    def _ids(self):
        return os.geteuid(), os.getegid()

    def test_create_and_reuse_valid_token_without_rewriting(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = os.path.join(parent, "auth")
            uid, gid = self._ids()
            path = ensure_token(directory, uid, gid)
            before = os.stat(path)
            with open(path, "rb") as fh:
                content = fh.read()
            self.assertEqual(stat.S_IMODE(before.st_mode), 0o600)
            self.assertGreaterEqual(len(content.strip()), 32)
            self.assertEqual(ensure_token(directory, uid, gid), path)
            after = os.stat(path)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), content)
            self.assertEqual((after.st_ino, after.st_uid, after.st_mode),
                             (before.st_ino, before.st_uid, before.st_mode))

    def test_leaf_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = os.path.join(parent, "auth")
            os.mkdir(directory, 0o755)
            target = os.path.join(parent, "root-owned-target")
            with open(target, "wb") as fh:
                fh.write(b"do-not-touch")
            os.chmod(target, 0o640)
            before = os.stat(target)
            with open(target, "rb") as fh:
                content = fh.read()
            os.symlink(target, os.path.join(directory, "auth-token"))
            uid, gid = self._ids()
            with self.assertRaises(TokenInstallError):
                ensure_token(directory, uid, gid)
            after = os.stat(target)
            with open(target, "rb") as fh:
                self.assertEqual(fh.read(), content)
            self.assertEqual((after.st_ino, after.st_uid, after.st_gid,
                              after.st_mode, after.st_size),
                             (before.st_ino, before.st_uid, before.st_gid,
                              before.st_mode, before.st_size))

    def test_directory_symlink_and_hardlinked_token_are_rejected(self):
        with tempfile.TemporaryDirectory() as parent:
            real = os.path.join(parent, "real")
            os.mkdir(real, 0o755)
            os.chmod(real, 0o755)
            uid, gid = self._ids()
            link = os.path.join(parent, "link")
            os.symlink(real, link)
            with self.assertRaises(TokenInstallError):
                ensure_token(link, uid, gid)

            token = ensure_token(real, uid, gid)
            os.link(token, os.path.join(parent, "second-link"))
            with self.assertRaises(TokenInstallError):
                ensure_token(real, uid, gid)

    def test_group_writable_auth_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = os.path.join(parent, "auth")
            os.mkdir(directory, 0o777)
            os.chmod(directory, 0o777)
            uid, gid = self._ids()
            with self.assertRaises(TokenInstallError):
                ensure_token(directory, uid, gid)

    def test_new_directory_mode_is_independent_of_umask(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = os.path.join(parent, "auth")
            uid, gid = self._ids()
            previous = os.umask(0o077)
            try:
                ensure_token(directory, uid, gid)
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o755)

    def test_installer_restarts_after_replacing_unit(self):
        script = (BASE / "deploy" / "install.sh").read_text(
            encoding="utf-8")
        render_unit = script.index(
            '"$SRC/deploy/rkss-portal.service" >"$RENDERED_UNIT"')
        copy_unit = script.index(
            'install -m 644 "$RENDERED_UNIT" "$UNIT"', render_unit)
        reload_unit = script.index("systemctl daemon-reload", copy_unit)
        enable_unit = script.index("systemctl enable rkss-portal.service",
                                   reload_unit)
        restart_unit = script.index("systemctl restart rkss-portal.service",
                                    enable_unit)
        self.assertLess(copy_unit, reload_unit)
        self.assertLess(reload_unit, enable_unit)
        self.assertLess(enable_unit, restart_unit)
        self.assertNotIn("enable --now rkss-portal.service", script)


if __name__ == "__main__":
    unittest.main()
