"""Runs tests/e2e/run.js (real UI + real bridge in jsdom) when node and jsdom are installed."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _jsdom_path():
    for candidate in (os.environ.get("JSDOM_PATH"), str(ROOT / "node_modules" / "jsdom"),
                      str(ROOT / "tests" / "e2e" / "node_modules" / "jsdom"), "/tmp/jsd/node_modules/jsdom"):
        if candidate and Path(candidate).is_dir():
            return candidate
    return None


@unittest.skipUnless(shutil.which("node") and _jsdom_path(), "needs node + jsdom (npm install jsdom)")
class EndToEndUiTests(unittest.TestCase):
    def test_real_ui_against_real_bridge(self):
        env = dict(os.environ, JSDOM_PATH=_jsdom_path())
        done = subprocess.run(["node", str(ROOT / "tests" / "e2e" / "run.js"), str(ROOT)], env=env,
                              capture_output=True, text=True, timeout=240)
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr[-2000:])
        self.assertIn("failed=0", done.stdout)


if __name__ == "__main__":
    unittest.main()
