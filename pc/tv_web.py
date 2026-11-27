"""Subtitle server for the TV web app (tv-web/: LG webOS now, Samsung Tizen later).

A TV web app cannot listen on a port, so the direction is the reverse of the Android TV app: the PC listens and the
TV page connects to it.

    GET  /events   Server-Sent Events: one JSON message per event (the same messages the Android app gets over TCP),
                   "event: ping" every 10 s so the page notices a dead link.
    POST /log      the page's diagnostic lines ({"platform","version","lines":[...]}) -> the PC's log as "[TV] ...".
    GET  /ping     "ok" (for checks).

One Hub per process (hub()): the GUI's test button and the translator share it, so the port is bound once.
"""
from __future__ import annotations

import hmac
import json
import queue
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8788
PING_SECONDS = 10
MAX_LOG_BODY = 16_000


def _print_log(msg: str) -> None:
    print(msg, flush=True)


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # a TV page that went away (connection reset) is normal; no traceback on stderr


class _Client:
    def __init__(self, address: str, info: dict):
        self.address = address
        self.info = info
        self.q: queue.Queue = queue.Queue(maxsize=500)
        self.since = time.monotonic()
        self.alive = True


class Hub:
    """The HTTP server and the TV pages connected to it."""

    def __init__(self, port: int = DEFAULT_PORT, log=None, hello=None, token: str = ""):
        self.port = port
        # Access key the TV page sends as ?k=: without it nobody else on the network can read the subtitles or write
        # into the log. "" = open (tests, the browser preview).
        self.token = token
        self.log = log or _print_log
        self.hello = hello or (lambda: {})     # extra fields of the "hello" message (e.g. the HDMI setting)
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._listeners: list = []           # fn(count) when the number of connected pages changes
        self._changed = threading.Condition(self._lock)
        self._server: ThreadingHTTPServer | None = None
        self._log_times: list[float] = []
        self.last_tv: dict = {}

    # ------------------------------------------------------------ server

    def start(self) -> None:
        hub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "Dilmac"

            def log_message(self, *args):  # no access log on stderr
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

            def _plain(self, code: int, body: bytes = b"ok"):
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _allowed(self, url) -> bool:
                if not hub.token:
                    return True
                given = (parse_qs(url.query).get("k") or [""])[0]
                return hmac.compare_digest(given.encode(), hub.token.encode())

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/events" and not self._allowed(url):
                    self._plain(403, b"forbidden")
                elif url.path == "/events":
                    hub._serve_events(self, {k: v[0] for k, v in parse_qs(url.query).items()})
                elif url.path == "/ping":
                    self._plain(200, b"dilmac")  # the TV page looks for this answer when it searches the network
                else:
                    self._plain(404, b"not found")

            def do_POST(self):
                url = urlparse(self.path)
                size = int(self.headers.get("Content-Length") or 0)
                if url.path != "/log" or size > MAX_LOG_BODY:
                    self._plain(404 if url.path != "/log" else 413, b"no")
                    return
                if not self._allowed(url):
                    self._plain(403, b"forbidden")
                    return
                body = self.rfile.read(size) if size else b""
                hub._tv_log(self.client_address[0], body)
                self._plain(200)

        server = _QuietServer(("0.0.0.0", self.port), Handler)
        server.daemon_threads = True
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True, name="tv-web").start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        with self._lock:
            for c in self._clients:
                c.alive = False
            self._clients.clear()

    # ------------------------------------------------------------ TV pages

    def _serve_events(self, h: BaseHTTPRequestHandler, query: dict) -> None:
        client = _Client(h.client_address[0], {"version": query.get("v", ""), "platform": query.get("tv", "")})
        h.send_response(200)
        h._cors()
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Connection", "keep-alive")
        h.send_header("X-Accel-Buffering", "no")
        h.end_headers()
        try:
            h.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        hello = {"type": "hello", "pc": socket.gethostname(), **self.hello()}
        try:
            h.wfile.write(b"retry: 2000\n\n" + _event(hello))
            h.wfile.flush()
        except OSError:
            return
        self._add(client)
        try:
            while client.alive:
                try:
                    chunk = client.q.get(timeout=PING_SECONDS)
                except queue.Empty:
                    chunk = b"event: ping\ndata: {}\n\n"
                if chunk is None:
                    break
                h.wfile.write(chunk)
                h.wfile.flush()
        except OSError:
            pass
        finally:
            h.close_connection = True
            self._remove(client)

    def _add(self, client: _Client) -> None:
        with self._lock:
            self._clients.append(client)
            n = len(self._clients)
            self.last_tv = {"ip": client.address, **client.info}
            self._changed.notify_all()
        label = f"{client.info.get('platform') or 'TV'} {client.info.get('version') or ''}".strip()
        self.log(f"TV sayfası bağlandı: {client.address} ({label})")
        self._fire(n)

    def _remove(self, client: _Client) -> None:
        with self._lock:
            if client not in self._clients:
                return
            self._clients.remove(client)
            n = len(self._clients)
            self._changed.notify_all()
        self.log(f"TV sayfasının bağlantısı kapandı: {client.address}")
        self._fire(n)

    def _fire(self, n: int) -> None:
        for fn in list(self._listeners):
            try:
                fn(n)
            except Exception:
                pass

    def listen(self, fn) -> None:
        self._listeners.append(fn)

    def unlisten(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def clients(self) -> int:
        with self._lock:
            return len(self._clients)

    def wait_client(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self._lock:
            while not self._clients:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._changed.wait(left)
            return True

    def broadcast(self, msg: dict) -> int:
        """Sends to every connected page; returns how many got it."""
        chunk = _event(msg)
        sent = 0
        with self._lock:
            for c in self._clients:
                try:
                    c.q.put_nowait(chunk)
                    sent += 1
                except queue.Full:  # a page that stopped reading: drop it
                    c.alive = False
        return sent

    def _tv_log(self, address: str, body: bytes) -> None:
        now_ = time.monotonic()
        self._log_times = [t for t in self._log_times if now_ - t < 10]
        if len(self._log_times) > 100:  # at most ~10 lines a second from a misbehaving page
            return
        try:
            data = json.loads(body.decode("utf-8"))
            lines = data.get("lines") or []
        except (ValueError, UnicodeDecodeError, AttributeError):
            lines = [body.decode("utf-8", "replace")]
        for line in lines[:30]:
            self._log_times.append(now_)
            self.log(f"[TV {address}] {str(line)[:300]}")


def _event(msg: dict) -> bytes:
    return b"data: " + json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n\n"


# ---------------------------------------------------------------- one hub per process

_hub: Hub | None = None
_hub_lock = threading.Lock()


def hub(port: int = DEFAULT_PORT, log=None, hello=None, token: str | None = None) -> Hub:
    """The process's server, started on first use. OSError if the port is taken (another copy of the app?)."""
    global _hub
    with _hub_lock:
        if _hub is not None and _hub.port != port:
            _hub.stop()
            _hub = None
        if _hub is None:
            h = Hub(port, log, hello, token or "")
            h.start()
            _hub = h
        else:
            if log is not None:
                _hub.log = log
            if hello is not None:
                _hub.hello = hello
            if token is not None:
                _hub.token = token
        return _hub


def existing_hub() -> Hub | None:
    return _hub


def tv_token() -> str:
    """This PC's access key for the TV page (kept encrypted in pc/tv-token; made once). The PC hands it to the TV
    app: LG in the launch parameters, Samsung inside the installed package."""
    import paths
    import secure_store

    path = paths.DATA / "tv-token"
    try:
        value = secure_store.read_bytes(path).decode("ascii").strip()
        if len(value) >= 16:
            return value
    except (OSError, UnicodeDecodeError, secure_store.SecretError):
        pass
    value = secrets.token_urlsafe(18)
    try:
        secure_store.write_bytes(path, value.encode("ascii"))
    except (OSError, secure_store.SecretError):
        pass
    return value


def local_address_for(ip: str) -> str:
    """This PC's address on the network that reaches ip (what the TV must connect to). No packet is sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 9))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


class WebTvLink:
    """Same interface as subtitler.TvLink (send / close) over the Hub."""

    def __init__(self, hub_: Hub, notify=None, log=None):
        self.hub = hub_
        self.notify = notify or (lambda *a, **k: None)
        self.log = log or _print_log
        self.warned = False
        self._on_change = lambda n: self.notify("tv", connected=n > 0)
        hub_.listen(self._on_change)

    def send(self, msg: dict) -> bool:
        if self.hub.broadcast(msg):
            self.warned = False
            return True
        if not self.warned:
            self.warned = True
            self.log("TV sayfası bağlı değil; altyazı gönderilemedi. TV'de Dilmaç uygulaması açık mı?")
        return False

    def close(self) -> None:
        self.hub.unlisten(self._on_change)
        self.hub.broadcast({"type": "clear"})
