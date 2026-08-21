#!/usr/bin/env python3
"""Test module."""
import os
import subprocess
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent

INSTALLER = os.path.join(BASE, "deploy", "install.sh")
UNIT_TEMPLATE = os.path.join(BASE, "deploy", "rkss-portal.service")


class InstallerUnitTest(unittest.TestCase):
    def test_shell_syntax(self):
        subprocess.run(["/bin/sh", "-n", INSTALLER], check=True)

    def test_user_selection_contract(self):
        with open(INSTALLER, encoding="utf-8") as fh:
            script = fh.read()

        self.assertNotRegex(
            script, r"\bid\s+[A-Za-z][A-Za-z0-9_.-]*\b",
            "installer must not contain a hard-coded account name")
        self.assertIn("RUN_USER=${RKSS_RUN_USER:-}", script)
        self.assertIn("${SUDO_USER:-}", script)
        self.assertIn("stat -c %U", script)
        self.assertIn("--user", script)
        self.assertIn('[ "$RUN_USER" != root ]', script)
        self.assertIn('RUN_GROUP=$(id -gn "$RUN_USER")', script)
        self.assertIn('getent passwd "$RUN_USER"', script)
        self.assertIn('RENDERED_UNIT=$AUTH_DIR/.unit-rendered.$$', script)
        self.assertIn("systemctl restart rkss-portal.service", script)

    def test_service_template_renders_selected_account(self):
        with open(UNIT_TEMPLATE, encoding="utf-8") as fh:
            unit = fh.read()

        values = {
            "@RKSS_USER@": "operator",
            "@RKSS_GROUP@": "operators",
            "@RKSS_HOME@": "/srv/operator",
            "@RKSS_CONF_DIR@": "/srv/operator/.rkss",
        }
        for token, value in values.items():
            unit = unit.replace(token, value)

        self.assertNotIn("@RKSS_", unit)
        self.assertIn("User=operator", unit)
        self.assertIn("Group=operators", unit)
        self.assertIn('Environment="HOME=/srv/operator"', unit)
        self.assertIn('"--conf-dir=/srv/operator/.rkss"', unit)


if __name__ == "__main__":
    unittest.main()
