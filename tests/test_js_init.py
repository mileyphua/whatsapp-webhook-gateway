"""The inbox script must start up without throwing (a crash at start-up kills every button on the page)."""
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ThreadScriptInitialises(unittest.TestCase):
    def test_thread_page_script_starts_without_errors(self):
        r = subprocess.run(["node", os.path.join(HERE, "js_init_smoke.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("INIT_OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
