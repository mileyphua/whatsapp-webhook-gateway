"""Opens the real inbox pages in a browser stand-in (jsdom) and clicks every button. Skipped when jsdom is not installed:
    npm install jsdom@22   (anywhere), then   JSDOM_PATH=/that/node_modules/jsdom python3 -m unittest tests.test_ui_audit"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
JSDOM = os.environ.get("JSDOM_PATH", "")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


@unittest.skipUnless(shutil.which("node") and JSDOM and os.path.isdir(JSDOM), "set JSDOM_PATH to a jsdom install to run the button audit")
class ButtonAudit(unittest.TestCase):
    def test_every_button_works(self):
        port = _free_port()
        server = subprocess.Popen([sys.executable, os.path.join(HERE, "ui_audit", "server.py"), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/inbox/login", timeout=1); break
                except Exception:
                    time.sleep(0.5)
            r = subprocess.run(["node", os.path.join(HERE, "ui_audit", "audit.js"), f"http://127.0.0.1:{port}"], capture_output=True, text=True,
                               timeout=300, env={**os.environ, "JSDOM_PATH": JSDOM})
            out = json.loads(r.stdout.strip().splitlines()[-1])
            failed = [f"{x['name']}: {x['detail']}" for x in out["results"] if not x["ok"]]
            self.assertEqual(failed, [], "\n".join(failed))
            self.assertGreater(out["total"], 60)
        finally:
            server.terminate()


if __name__ == "__main__":
    unittest.main()
