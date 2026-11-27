"""PSN login in a small window: catching the redirect from the browser's DevTools page list (with a fake browser)."""
import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psn_login as L  # noqa: E402

PREFIX = "https://remoteplay.dl.playstation.net/remoteplay/redirect"


class FakeDevTools:
    """A local /json/list endpoint whose page list the test changes."""

    def __init__(self):
        self.pages = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(outer.pages).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server.server_address[1]

    def stop(self):
        self.server.shutdown()


class FakeProc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class HelperTest(unittest.TestCase):
    def test_find_redirect(self):
        pages = [{"url": "https://auth.api.sonyentertainmentnetwork.com/2.0/oauth/authorize?x"},
                 {"url": PREFIX + "?code=abc123&cid=1"}]
        self.assertEqual(L.find_redirect(pages, PREFIX), PREFIX + "?code=abc123&cid=1")
        self.assertIsNone(L.find_redirect(pages[:1], PREFIX))
        self.assertIsNone(L.find_redirect([{"url": PREFIX + "?error=denied"}], PREFIX))

    def test_devtools_port(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(L.devtools_port(Path(d)))
            (Path(d) / "DevToolsActivePort").write_text("51234\n/devtools/browser/x\n", encoding="utf-8")
            self.assertEqual(L.devtools_port(Path(d)), 51234)


class LoginWindowTest(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDevTools()
        self.tmp = tempfile.TemporaryDirectory()
        self.win = L.LoginWindow("https://login", PREFIX, browser=Path("fake.exe"))
        self.win.profile = Path(self.tmp.name)
        self.win.proc = FakeProc()

    def tearDown(self):
        self.dev.stop()
        self.tmp.cleanup()

    def test_waits_then_catches_redirect(self):
        self.assertIsNone(self.win.poll())  # the browser has not written its port yet
        (self.win.profile / "DevToolsActivePort").write_text(f"{self.dev.port}\n", encoding="utf-8")
        self.dev.pages = [{"type": "page", "url": "https://auth.api.sonyentertainmentnetwork.com/login"}]
        self.assertIsNone(self.win.poll())
        self.dev.pages = [{"type": "page", "url": PREFIX + "?code=XYZ"}, {"type": "service_worker", "url": "x"}]
        self.assertEqual(self.win.wait(timeout=5, interval=0.05), PREFIX + "?code=XYZ")

    def test_closed_window(self):
        (self.win.profile / "DevToolsActivePort").write_text(f"{self.dev.port}\n", encoding="utf-8")
        self.dev.pages = [{"type": "page", "url": "https://auth.api.sonyentertainmentnetwork.com/login"}]
        self.assertIsNone(self.win.poll())
        self.dev.pages = []  # window closed, the browser still running in the background
        with self.assertRaises(L.LoginClosed):
            self.win.poll()

    def test_process_exit_and_stop(self):
        self.win.proc.code = 0
        with self.assertRaises(L.LoginClosed):
            self.win.poll()
        self.win.proc.code = None
        stop = threading.Event()
        stop.set()
        with self.assertRaises(L.LoginClosed):
            self.win.wait(timeout=5, stop=stop)


if __name__ == "__main__":
    unittest.main()
