#!/usr/bin/env python3
"""Stage3 TLS boundary, shutdown status and atomic deploy regressions."""
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


class CookieAndCliUnitTest(unittest.TestCase):
    def test_secure_cookie_is_explicit_for_login_and_logout(self):
        plain = TokenAuth("x" * 32, clock=lambda: 1000)
        secure = TokenAuth("x" * 32, clock=lambda: 1000,
                           secure_cookie=True)
        self.assertNotIn("; Secure", plain.login_cookie())
        self.assertNotIn("; Secure", plain.logout_cookie())
        self.assertIn("; Secure", secure.login_cookie())
        self.assertIn("; Secure", secure.logout_cookie())

    def test_direct_non_loopback_http_fails_before_listen(self):
        run = subprocess.run(
            [sys.executable, "-m", "portal.portal", "--bind", "0.0.0.0",
             "--auth-token-file", "/does/not/matter"], cwd=str(BASE),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=5)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("direct non-loopback HTTP is disabled", run.stderr)

    def test_external_https_and_trusted_http_are_incompatible(self):
        run = subprocess.run(
            [sys.executable, "-m", "portal.portal", "--bind", "0.0.0.0",
             "--external-https", "--trusted-http", "--no-auth"],
            cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=5)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("cannot be combined", run.stderr)
        self.assertIn("WARNING: trusted-http", run.stderr)


class ShutdownStatusUnitTest(unittest.TestCase):
    def test_shutdown_success_and_failure_exit_status(self):
        class Host:
            def __init__(self, result): self.result = result
            def close(self): return self.result

        self.assertEqual(_close_host_for_exit(Host(True)), 0)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(_close_host_for_exit(Host(False)), 1)
        self.assertIn("did not drain", stderr.getvalue())

    def test_shutdown_exception_is_visible_and_nonzero(self):
        class Host:
            def close(self): raise RuntimeError("disk stuck")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(_close_host_for_exit(Host()), 1)
        self.assertIn("disk stuck", stderr.getvalue())


class ConfigContractUnitTest(unittest.TestCase):
    def test_access_log_is_opt_in(self):
        handler = object.__new__(Handler)
        handler.client_address = ("127.0.0.1", 1234)
        handler.server = type("Server", (), {"access_log": False})()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            handler.log_message("%s", "quiet")
        self.assertEqual(stderr.getvalue(), "")
        handler.server.access_log = True
        with contextlib.redirect_stderr(stderr):
            handler.log_message("%s", "visible")
        self.assertIn("visible", stderr.getvalue())

    def test_nginx_and_systemd_security_contract(self):
        nginx = (BASE / "deploy/nginx-rkss-portal.conf").read_text()
        unit = (BASE / "deploy/rkss-portal.service").read_text()
        for value in ("ssl_protocols TLSv1.2 TLSv1.3",
                      "access_log off",
                      "client_max_body_size 1m",
                      "client_max_body_size 2g", "proxy_buffering off",
                      "Strict-Transport-Security", "X-Frame-Options"):
            self.assertIn(value, nginx)
        self.assertIn("--bind 127.0.0.1", unit)
        self.assertIn("/current/portal/portal.py", unit)
        self.assertIn("$RKSS_EXTERNAL_HTTPS", unit)
        for token in ("@RKSS_USER@", "@RKSS_GROUP@", "@RKSS_HOME@",
                      "@RKSS_CONF_DIR@"):
            self.assertIn(token, unit)


class AtomicInstallerUnitTest(unittest.TestCase):
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

    def test_02_root_service_user_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="rkss-stage3-root-user-") as td:
            _, env, _ = self._environment(td)
            env.pop("RKSS_RUN_USER", None)
            run = subprocess.run(
                ["/bin/sh", str(BASE / "deploy/install.sh"), "portal",
                 "--user", "root"], cwd=str(BASE), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=20)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("must not be root", run.stderr)


if __name__ == "__main__":
    unittest.main()
