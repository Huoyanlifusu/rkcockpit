#!/usr/bin/env python3
"""TLS boundary, shutdown status and atomic deploy regressions."""
import contextlib
import getpass
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
sys.path.insert(0, str(BASE))

from host.core.auth import TokenAuth
from portal.portal import Handler, _close_host_for_exit


class AtomicInstallerHardwareTest(unittest.TestCase):
    def _environment(self, td):
        root = Path(td)
        bindir = root / "bin"
        bindir.mkdir()
        log = root / "systemctl.log"
        fake_id = bindir / "id"
        fake_systemctl = bindir / "systemctl"
        fake_python = bindir / "python3"
        fake_chown = bindir / "chown"
        fake_stat = bindir / "stat"
        fake_id.write_text(
            "#!/bin/sh\nif [ \"${1:-}\" = -u ]; then echo 0; exit 0; fi\n"
            "exec /usr/bin/id \"$@\"\n")
        fake_systemctl.write_text(
            "#!/bin/sh\necho \"$*\" >>\"$RKSS_TEST_SYSTEMCTL_LOG\"\n"
            "if [ \"$*\" = 'restart rkss-portal.service' ] && "
            "[ -f \"$RKSS_TEST_FAIL_ONCE\" ]; then "
            "rm -f \"$RKSS_TEST_FAIL_ONCE\"; exit 1; fi\nexit 0\n")
        fake_python.write_text(
            "#!/bin/sh\nwhile [ \"$#\" -gt 0 ]; do "
            "if [ \"$1\" = --dir ]; then shift; d=$1; fi; shift; done\n"
            "mkdir -p \"$d\"\n"
            "printf '%s\\n' '0123456789abcdef0123456789abcdef' >\"$d/auth-token\"\n"
            "chmod 600 \"$d/auth-token\"\n")
        fake_chown.write_text("#!/bin/sh\nexit 0\n")
        fake_stat.write_text(
            "#!/bin/sh\nlast=\nfor arg in \"$@\"; do last=$arg; done\n"
            "if [ \"${RKSS_TEST_UNSAFE_PATH:-}\" = \"$last\" ] && "
            "[ \"$1 $2\" = '-c %a' ]; then echo 777; exit 0; fi\n"
            "if [ \"$1 $2\" = '-c %u' ]; then echo 0; exit 0; fi\n"
            "if [ \"$1 $2\" = '-c %a' ]; then echo 755; exit 0; fi\n"
            "exec /usr/bin/stat \"$@\"\n")
        for script in (fake_id, fake_systemctl, fake_python, fake_chown,
                       fake_stat):
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        env = dict(os.environ)
        env.update({
            "PATH": str(bindir) + os.pathsep + env["PATH"],
            "RKSS_APP_ROOT": str(root / "app"),
            "RKSS_SYSTEMD_DIR": str(root / "systemd"),
            "RKSS_AUTH_DIR": str(root / "auth"),
            "RKSS_CONF_DIR": str(root / "conf"),
            "RKSS_RUN_USER": getpass.getuser(),
            "RKSS_TEST_SYSTEMCTL_LOG": str(log),
            "RKSS_TEST_FAIL_ONCE": str(root / "fail-once"),
            "RKSS_NGINX_AVAILABLE": str(root / "nginx/available"),
            "RKSS_NGINX_ENABLED": str(root / "nginx/enabled"),
        })
        return root, env, log

    def _install(self, env, release):
        selected = dict(env, RKSS_RELEASE_ID=release)
        return subprocess.run(
            ["/bin/sh", str(BASE / "deploy/install.sh"), "portal"],
            cwd=str(BASE), env=selected, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=20)

    def test_01_explicit_user_renders_unit_with_primary_group(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage3-user-") as td:
            root, env, _ = self._environment(td)
            user = getpass.getuser()
            env.pop("RKSS_RUN_USER", None)
            selected = dict(env, RKSS_RELEASE_ID="user-r1")
            run = subprocess.run(
                ["/bin/sh", str(BASE / "deploy/install.sh"), "portal",
                 "--user", user], cwd=str(BASE), env=selected,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            unit = (root / "systemd/rkss-portal.service").read_text()
            group = subprocess.check_output(
                ["/usr/bin/id", "-gn", user], text=True).strip()
            home = subprocess.check_output(
                ["getent", "passwd", user], text=True).split(":")[5]
            self.assertIn(f"User={user}", unit)
            self.assertIn(f"Group={group}", unit)
            self.assertIn(f'Environment="HOME={home}"', unit)
            self.assertIn(f'"--conf-dir={root / "conf"}"', unit)
            self.assertNotIn("@RKSS_", unit)

    def test_03_atomic_upgrade_and_restart_failure_rollback(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage3-install-") as td:
            root, env, log = self._environment(td)
            first = self._install(env, "r1")
            self.assertEqual(first.returncode, 0, first.stderr)
            current = root / "app/current"
            self.assertEqual(current.resolve().name, "r1")
            for stale in ("host", "portal", "static"):
                self.assertFalse((root / "app" / stale).exists())

            (root / "fail-once").touch()
            second = self._install(env, "r2")
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(current.resolve().name, "r1")
            self.assertFalse((root / "app/releases/r2").exists())
            calls = log.read_text().splitlines()
            self.assertLess(calls.index("daemon-reload"),
                            calls.index("enable rkss-portal.service"))
            self.assertGreaterEqual(
                calls.count("restart rkss-portal.service"), 3)

    def test_04_rejects_symlink_and_world_writable_application_roots(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage3-roots-") as td:
            root, env, _ = self._environment(td)
            victim = root / "victim"
            victim.mkdir()
            (root / "app").symlink_to(victim, target_is_directory=True)
            linked = self._install(env, "linked")
            self.assertNotEqual(linked.returncode, 0)
            self.assertIn("must not contain a symlink", linked.stderr)
            self.assertEqual(list(victim.iterdir()), [])

        with tempfile.TemporaryDirectory(prefix="rkss-stage3-roots-") as td:
            root, env, _ = self._environment(td)
            app = root / "app"
            app.mkdir()
            env["RKSS_TEST_UNSAFE_PATH"] = str(app)
            writable = self._install(env, "writable")
            self.assertNotEqual(writable.returncode, 0)
            self.assertIn("must not be group/other writable", writable.stderr)
            self.assertFalse((app / "releases").exists())

    def test_05_created_directories_are_private_before_explicit_chmod(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage3-umask-") as td:
            root, env, _ = self._environment(td)
            bindir = root / "bin"
            mode_log = root / "mkdir-modes.log"
            (bindir / "mkdir").write_text(
                "#!/bin/sh\n/usr/bin/mkdir \"$@\" || exit $?\n"
                "last=\nfor arg in \"$@\"; do last=$arg; done\n"
                "/usr/bin/stat -c %a -- \"$last\" >>"
                "\"$RKSS_TEST_MKDIR_MODE_LOG\"\n")
            path = bindir / "mkdir"
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            env["RKSS_TEST_MKDIR_MODE_LOG"] = str(mode_log)

            def permissive_umask():
                os.umask(0)

            selected = dict(env, RKSS_RELEASE_ID="umask-r1")
            run = subprocess.run(
                ["/bin/sh", str(BASE / "deploy/install.sh"), "portal"],
                cwd=str(BASE), env=selected, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=20,
                preexec_fn=permissive_umask)
            self.assertEqual(run.returncode, 0, run.stderr)
            modes = [int(value, 8) for value in mode_log.read_text().split()]
            self.assertTrue(modes)
            self.assertTrue(all(mode & 0o022 == 0 for mode in modes), modes)

    def test_06_unit_leaf_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage3-unit-link-") as td:
            root, env, _ = self._environment(td)
            systemd = root / "systemd"
            systemd.mkdir()
            victim = root / "victim.service"
            victim.write_text("do not overwrite\n")
            victim.chmod(0o640)
            before = victim.stat()
            (systemd / "rkss-portal.service").symlink_to(victim)
            run = self._install(env, "unit-link")
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("unit must not be a symlink", run.stderr)
            after = victim.stat()
            self.assertEqual(victim.read_text(), "do not overwrite\n")
            self.assertEqual((after.st_ino, stat.S_IMODE(after.st_mode)),
                             (before.st_ino, stat.S_IMODE(before.st_mode)))
            self.assertFalse((root / "app/releases/unit-link").exists())

    def test_07_tls_mode_validates_then_renders_and_reloads_nginx(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage3-tls-") as td:
            root, env, _ = self._environment(td)
            bindir = root / "bin"
            nginx_log = root / "nginx.log"
            (bindir / "stat").write_text(
                "#!/bin/sh\nif [ \"$1 $2\" = '-c %u' ]; then echo 0; "
                "elif [ \"$1 $2\" = '-c %a' ]; then echo 600; "
                "else exec /usr/bin/stat \"$@\"; fi\n")
            (bindir / "openssl").write_text(
                "#!/bin/sh\ncase \"$*\" in *-pubkey*|*-pubout*) "
                "echo TEST-PUBLIC-KEY;; esac\nexit 0\n")
            (bindir / "nginx").write_text(
                "#!/bin/sh\necho \"$*\" >>\"$RKSS_TEST_NGINX_LOG\"\n"
                "exit 0\n")
            for name in ("stat", "openssl", "nginx"):
                path = bindir / name
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            cert = root / "cert.pem"
            key = root / "key.pem"
            cert.write_text("certificate")
            key.write_text("private key")
            env["RKSS_TEST_NGINX_LOG"] = str(nginx_log)
            selected = dict(env, RKSS_RELEASE_ID="tls-r1")
            run = subprocess.run(
                ["/bin/sh", str(BASE / "deploy/install.sh"), "portal",
                 "--tls-nginx", "portal.example.com", str(cert), str(key)],
                cwd=str(BASE), env=selected, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            rendered = (root / "nginx/available/rkss-portal.conf").read_text()
            self.assertIn("server_name portal.example.com", rendered)
            self.assertNotIn("@@", rendered)
            self.assertEqual(
                (root / "auth/portal.env").read_text().strip(),
                "RKSS_EXTERNAL_HTTPS=--external-https")
            self.assertEqual(nginx_log.read_text().splitlines(),
                             ["-t", "-s reload"])

    def test_08_tls_environment_failure_restores_active_nginx_config(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage3-tls-rollback-") as td:
            root, env, _ = self._environment(td)
            bindir = root / "bin"
            nginx_log = root / "nginx.log"
            (bindir / "openssl").write_text(
                "#!/bin/sh\ncase \"$*\" in *-pubkey*|*-pubout*) "
                "echo TEST-PUBLIC-KEY;; esac\nexit 0\n")
            (bindir / "nginx").write_text(
                "#!/bin/sh\necho \"$*\" >>\"$RKSS_TEST_NGINX_LOG\"\nexit 0\n")
            (bindir / "install").write_text(
                "#!/bin/sh\nlast=\nfor arg in \"$@\"; do last=$arg; done\n"
                "if [ \"$last\" = \"$RKSS_AUTH_DIR/portal.env\" ]; then "
                "exit 71; fi\nexec /usr/bin/install \"$@\"\n")
            (bindir / "stat").write_text(
                "#!/bin/sh\nlast=\nfor arg in \"$@\"; do last=$arg; done\n"
                "if [ \"$1 $2\" = '-c %u' ]; then echo 0; "
                "elif [ \"$1 $2\" = '-c %a' ] && "
                "[ \"$last\" = \"$RKSS_TEST_KEY_FILE\" ]; then echo 600; "
                "elif [ \"$1 $2\" = '-c %a' ]; then echo 755; "
                "else exec /usr/bin/stat \"$@\"; fi\n")
            for name in ("openssl", "nginx", "install", "stat"):
                path = bindir / name
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            cert = root / "cert.pem"
            key = root / "key.pem"
            cert.write_text("certificate")
            key.write_text("private key")
            env["RKSS_TEST_KEY_FILE"] = str(key)
            nginx_available = root / "nginx/available"
            nginx_enabled = root / "nginx/enabled"
            nginx_available.mkdir(parents=True)
            nginx_enabled.mkdir(parents=True)
            config = nginx_available / "rkss-portal.conf"
            config.write_text("old nginx config\n")
            (nginx_enabled / "rkss-portal.conf").symlink_to(config)
            env["RKSS_TEST_NGINX_LOG"] = str(nginx_log)
            selected = dict(env, RKSS_RELEASE_ID="tls-fail")
            run = subprocess.run(
                ["/bin/sh", str(BASE / "deploy/install.sh"), "portal",
                 "--tls-nginx", "portal.example.com", str(cert), str(key)],
                cwd=str(BASE), env=selected, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=20)
            self.assertEqual(run.returncode, 71, run.stderr)
            self.assertEqual(config.read_text(), "old nginx config\n")
            self.assertTrue((nginx_enabled / "rkss-portal.conf").is_symlink())
            self.assertEqual(nginx_log.read_text().splitlines(),
                             ["-t", "-s reload", "-s reload"])
            self.assertFalse((root / "app/releases/tls-fail").exists())


if __name__ == "__main__":
    unittest.main()
