"""The header countdown runs from the buyer's last message and locks the composer the moment it reaches zero."""
import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class Countdown(unittest.TestCase):
    def test_countdown_behaviour(self):
        r = subprocess.run(["node", os.path.join(HERE, "countdown_check.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertRegex(out["open"][0], r"^⏱ 01:02:0[3-5] left to reply$")      # counts down as HH:MM:SS (a second may tick over while it runs)
        self.assertEqual(out["open"][1], "1h 2m")                              # the banner's text follows it
        self.assertEqual(out["open"][2], 0)                                    # still open: composer untouched
        self.assertEqual(out["expired"][0], "⏱ window closed · templates only")
        self.assertEqual(json.loads(out["expired"][1]), [["render", False]])   # locked once, not on every tick
        self.assertEqual(out["none"], ["⏱ no buyer message yet", 0])


if __name__ == "__main__":
    unittest.main()
