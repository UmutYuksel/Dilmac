"""The subtitle server the TV web app connects to (Server-Sent Events)."""
import http.client
import json
import socket
import threading
import time
import unittest

import tv_web


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SseReader:
    """A minimal EventSource: reads events from /events on its own thread."""

    def __init__(self, port: int):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.conn.request("GET", "/events?v=0.7.0&tv=test")
        self.resp = self.conn.getresponse()
        self.events: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        name, data = "message", []
        try:
            while True:
                line = self.resp.fp.readline()
                if not line:
                    return
                line = line.decode("utf-8").rstrip("\n")
                if line == "":
                    if data:
                        with self.lock:
                            self.events.append((name, json.loads("\n".join(data))))
                    name, data = "message", []
                elif line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data.append(line[6:])
        except (OSError, ValueError):
            return

    def wait(self, n: int, timeout: float = 3) -> list:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if len(self.events) >= n:
                    return list(self.events)
            time.sleep(0.02)
        return list(self.events)

    def close(self):
        try:
            self.resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)  # like a TV page going away
        except (AttributeError, OSError):
            pass
        self.resp.close()
        self.conn.close()


class HubTest(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.hub = tv_web.Hub(free_port(), self.lines.append, lambda: {"hdmi": 2})
        self.hub.start()

    def tearDown(self):
        self.hub.stop()

    def test_events_reach_the_page(self):
        changes = []
        self.hub.listen(changes.append)
        page = SseReader(self.hub.port)
        self.assertEqual(page.resp.status, 200)
        self.assertEqual(page.resp.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(page.resp.getheader("Access-Control-Allow-Origin"), "*")
        self.assertTrue(self.hub.wait_client(2))
        self.assertEqual(self.hub.broadcast({"type": "show", "id": "speech", "text": "Selam, nasılsın?"}), 1)
        events = page.wait(2)
        self.assertEqual(events[0][1]["type"], "hello")
        self.assertEqual(events[0][1]["hdmi"], 2)
        self.assertEqual(events[1][1]["text"], "Selam, nasılsın?")
        self.assertEqual(self.hub.last_tv["platform"], "test")
        self.assertEqual(changes[0], 1)
        page.close()

    def test_page_log_lands_in_pc_log(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=3)
        body = json.dumps({"platform": "webos", "lines": ["HDMI 1: playing", "bağlandı"]})
        conn.request("POST", "/log", body.encode("utf-8"), {"Content-Type": "text/plain;charset=UTF-8"})
        self.assertEqual(conn.getresponse().status, 200)
        self.assertTrue(any("[TV 127.0.0.1] HDMI 1: playing" in line for line in self.lines))
        self.assertTrue(any("bağlandı" in line for line in self.lines))

    def test_no_page(self):
        self.assertEqual(self.hub.broadcast({"type": "clear"}), 0)
        self.assertFalse(self.hub.wait_client(0.1))
        link = tv_web.WebTvLink(self.hub, log=self.lines.append)
        self.assertFalse(link.send({"type": "clear"}))
        self.assertFalse(link.send({"type": "clear"}))
        self.assertEqual(sum("bağlı değil" in line for line in self.lines), 1)  # warned once

    def test_link_notifies_connection(self):
        seen = []
        link = tv_web.WebTvLink(self.hub, notify=lambda event, **d: seen.append((event, d)), log=self.lines.append)
        page = SseReader(self.hub.port)
        self.assertTrue(self.hub.wait_client(2))
        self.assertIn(("tv", {"connected": True}), seen)
        self.assertTrue(link.send({"type": "show", "text": "x"}))
        page.close()
        end = time.monotonic() + 15  # the server notices the closed page at its next write (ping within 10 s)
        while self.hub.clients() and time.monotonic() < end:
            self.hub.broadcast({"type": "clear"})
            time.sleep(0.1)
        self.assertEqual(self.hub.clients(), 0)
        self.assertIn(("tv", {"connected": False}), seen)

    def test_ping(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=3)
        conn.request("GET", "/ping")
        self.assertEqual(conn.getresponse().read(), b"dilmac")


class TokenTest(unittest.TestCase):
    def test_only_with_the_key(self):
        lines = []
        hub = tv_web.Hub(free_port(), lines.append, token="s3cret-key-123456")
        hub.start()
        try:
            for path, method in (("/events", "GET"), ("/log", "POST"), ("/events?k=wrong", "GET"), ("/log?k=", "POST")):
                conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=3)
                conn.request(method, path, b"{}" if method == "POST" else None)
                self.assertEqual(conn.getresponse().status, 403, path)
                conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=3)
            conn.request("POST", "/log?k=s3cret-key-123456", json.dumps({"lines": ["merhaba"]}).encode())
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=3)
            conn.request("GET", "/ping")  # stays open: the TV page searches the network with it
            self.assertEqual(conn.getresponse().read(), b"dilmac")
            conn.close()
            self.assertTrue(any("merhaba" in l for l in lines))
            self.assertFalse(any("{}" in l for l in lines))
        finally:
            hub.stop()


class AddressTest(unittest.TestCase):
    def test_local_address(self):
        self.assertEqual(tv_web.local_address_for("127.0.0.1"), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
