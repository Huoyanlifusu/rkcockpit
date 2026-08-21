#!/usr/bin/env python3
"""Behavioral regression tests for the file-pane parent-path helper."""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[0]
while not (BASE / ".git").exists() and BASE != BASE.parent:
    BASE = BASE.parent
FILES_JS = os.path.join(BASE, "static", "js", "pages", "files.js")


@unittest.skipUnless(shutil.which("node"), "node is required for JavaScript tests")
class FilesNavigationUnitTest(unittest.TestCase):
    def test_parent_path_contract(self):
        cases = [
            ("local", "~", "/"),
            ("local", "/", "/"),
            ("remote", "/", "/"),
            ("local", "~/docs", "~"),
            ("local", "~/docs/logs", "~/docs"),
            ("remote", "/var/log", "/var"),
        ]
        script = r"""
const fs = require("fs");
global.window = {RKS: {
  dom: {$: () => null, el: () => ({})},
  store: {},
  state: {fm: {}},
  api: {fetch: async () => ({ok: true, entries: []})},
  ui: {
    hostError: () => {}, loadDevices: async () => {},
    deviceOptions: () => {}, deviceName: () => "",
  },
}};
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  R.pages.files = {\n";
if (!source.includes(marker)) throw new Error("files page export marker missing");
source = source.replace(marker, marker + "    parentPath,\n");
eval(source);
const cases = JSON.parse(process.argv[2]);
for (const [side, path, expected] of cases) {
  const actual = window.RKS.pages.files.parentPath(side, path);
  if (actual !== expected) {
    throw new Error(`${side} ${path}: expected ${expected}, got ${actual}`);
  }
}
"""
        subprocess.run(
            ["node", "-e", script, FILES_JS, json.dumps(cases)],
            cwd=BASE, check=True, timeout=10,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )


if __name__ == "__main__":
    unittest.main()
