"""Samsung TV (Tizen): installs and opens the TV web app (tv-web/) over the TV's developer mode.

What the user does once on the TV (the GUI's Samsung window shows these steps):
  Apps screen › 1 2 3 4 5 on the remote › Developer mode On › Host PC IP = this PC's address › restart the TV fully
  (hold the power button until the Samsung logo appears).

Samsung only installs a package signed for that TV, so the PC:
  1. signs the user in with their (free) Samsung account in the browser: Samsung's sign-in page posts the token to
     http://localhost:4794/signin/callback, where a small listener here takes it (the same way Tizen Studio does),
  2. asks Samsung's certificate server for an author certificate and a distributor certificate bound to the TV's
     DUID (PKCS#10 requests, a 2048-bit RSA key made here) and keeps them in pc/samsung/<DUID>/ (private),
  3. builds the .wgt from tv-web/ with this PC's address inside and signs it (W3C widget signature: author-signature.xml
     and signature1.xml),
  4. copies it to the TV over sdb (Samsung's adb: port 26101, accepted only from the Host PC IP) and installs it with
     "0 vd_appinstall"; "0 was_execute" opens it.

The protocol details (sign-in gate, certificate endpoints and fields, sdb framing, the shell commands) follow what
Apps2Samsung / TizenSdb (MIT, Patrick Stel) and CouchSubs document; this is an independent Python implementation.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import paths

APP_ID = "DilmacSubs.Dilmac"
PACKAGE_ID = "DilmacSubs"
APP_TITLE = "Dilmaç"
SDB_PORT = 26101
INFO_PORT = 8001
MIN_TIZEN = (3, 0)
DEVICE_PROFILE_UP_TO = (4, 0)   # these TVs want the distributor's device profile next to the package

SIGN_IN_GATE = "https://account.samsung.com/accounts/be1dce529476c1a6d407c4c7578c31bd/signInGate"
CLIENT_ID = "v285zxnl3h"
SIGN_IN_STATE = "accountcheckdogeneratedstatetext"
CALLBACK_PORT = 4794
CALLBACK_PATH = "/signin/callback"
AUTHOR_URL = "https://svdca.samsungqbe.com/apis/v3/authors"
DEVICE_PROFILE_URL = "https://svdca.samsungqbe.com/apis/v1/distributors"
DISTRIBUTOR_URL = "https://svdca.samsungqbe.com/apis/v3/distributors"

SAMSUNG_DIR = paths.DATA / "samsung"          # private: signing keys, certificates, sdb key, state
CA_DIR = paths.BUNDLE / "samsung_ca"           # Samsung's public intermediate certificates (from Tizen Studio)


class SamsungError(Exception):
    """A step failed; the message (Turkish) is shown to the user."""


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", str(text or "").strip()) if p != "") or (0,)


# ---------------------------------------------------------------- TV info (http://TV:8001/api/v2/)

def fetch_info(ip: str, timeout: float = 2.0) -> dict | None:
    """Name, model and developer mode of the TV; None if it does not answer."""
    try:
        with urllib.request.urlopen(f"http://{ip}:{INFO_PORT}/api/v2/", timeout=timeout) as r:
            device = json.loads(r.read(200_000).decode("utf-8", "replace")).get("device") or {}
    except (urllib.error.URLError, OSError, ValueError):
        return None

    def text(name):
        v = str(device.get(name) or "").replace("&quot;", '"').strip()
        return v or None

    dev = text("developerMode")
    dev_ip = text("developerIP")
    return {"name": text("name"), "model": text("modelName"), "duid": text("duid"),
            "developer_mode": None if dev is None else dev in ("1", "true", "True"),
            "developer_ip": None if dev_ip in (None, "0.0.0.0") else dev_ip,
            "resolution": text("resolution"), "os": text("OS")}


def refusal_hint(info: dict | None, pc_ip: str) -> str:
    """Why the TV refuses the developer connection, from what it says about itself."""
    if info is None:
        return "TV açık ve bu bilgisayarla aynı ağda mı?"
    if info.get("developer_mode") is False:
        return ("TV'de geliştirici modu kapalı: Uygulamalar ekranında kumandadan 1 2 3 4 5'e bas, Developer mode'u aç, "
                f"Host PC IP'ye {pc_ip} yaz ve TV'yi tam yeniden başlat.")
    dev_ip = info.get("developer_ip")
    if dev_ip and dev_ip != pc_ip:
        return (f"TV'deki Host PC IP {dev_ip}, ama bu bilgisayarın adresi {pc_ip}. Uygulamalar › 1 2 3 4 5 ile "
                f"Host PC IP'yi {pc_ip} yap ve TV'yi tam yeniden başlat.")
    return ("Ayar doğru görünüyor ama TV o zamandan beri tam yeniden başlamamış olabilir: güç tuşuna Samsung logosu "
            "çıkana kadar (~5 sn) basılı tut ya da fişini 30 sn çek.")


# ---------------------------------------------------------------- sdb (Samsung's adb)

CNXN, AUTH, OPEN, OKAY, WRTE, CLSE = (struct.unpack("<I", n)[0] for n in (b"CNXN", b"AUTH", b"OPEN", b"OKAY", b"WRTE",
                                                                           b"CLSE"))
_SDB_VERSION = 0x01000000
_SYNC_CHUNK = 64 * 1024


def frame(cmd: int, arg0: int, arg1: int, payload: bytes = b"") -> bytes:
    """A 24-byte little-endian header (command, two arguments, length, byte sum, command xor ~0) and the data."""
    return struct.pack("<6I", cmd, arg0, arg1, len(payload), sum(payload) & 0xFFFFFFFF, cmd ^ 0xFFFFFFFF) + payload


def sync_packet(ident: str, payload: bytes) -> bytes:
    return ident.encode("ascii") + struct.pack("<I", len(payload)) + payload


def parse_capability(data: bytes) -> dict:
    """"capability:" answer: two bytes of length, then key:value lines."""
    out: dict[str, str] = {}
    for line in data[2:].decode("utf-8", "replace").split("\n"):
        parts = [p for p in line.split(":") if p]
        if len(parts) == 2 and parts[0] not in out:
            out[parts[0]] = parts[1].strip()
    return out


class SdbKey:
    """sdb's authentication key (most Samsung TVs do not ask). Kept in pc/samsung/sdb-key.pem."""

    def __init__(self, private_key):
        self.key = private_key

    @classmethod
    def load_or_create(cls, path: Path | None = None) -> "SdbKey":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        import secure_store

        path = path or SAMSUNG_DIR / "sdb-key.pem"
        try:
            return cls(serialization.load_pem_private_key(secure_store.read_bytes(path), None))
        except (OSError, ValueError, secure_store.SecretError):
            pass
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        secure_store.write_bytes(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
        return cls(key)

    def sign(self, token: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return self.key.sign(token, padding.PKCS1v15(), hashes.SHA1())

    def public_payload(self) -> bytes:
        """The Android RSAPublicKey structure (word count, n0inv, modulus and R² little-endian, exponent), base64."""
        numbers = self.key.public_key().public_numbers()
        n, size = numbers.n, 256
        n0inv = (-pow(n % (1 << 32), -1, 1 << 32)) % (1 << 32)
        rr = pow(1 << (size * 8), 2, n)
        raw = struct.pack("<II", size // 4, n0inv) + n.to_bytes(size, "little") + rr.to_bytes(size, "little") + \
            struct.pack("<I", numbers.e)
        return base64.b64encode(raw) + b" dilmac@pc\x00"


class Sdb:
    """An sdb connection. One channel at a time (the steps run one after another)."""

    def __init__(self, host: str, port: int = SDB_PORT, key: SdbKey | None = None, timeout: float = 10):
        self.key = key
        self.max_data = 4096
        self.banner = ""
        self._next_id = 0
        self._buf = b""
        last: Exception | None = None
        for attempt in range(3):  # Samsung's sdbd sometimes drops a fresh connection mid-handshake
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                sock.connect((host, port))
                sock.settimeout(120)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock = sock
                self._handshake()
                return
            except (OSError, SamsungError) as e:
                last = e
                sock.close()
                if attempt < 2:
                    time.sleep(0.4)
        raise SamsungError(f"TV'nin geliştirici bağlantısına (sdb {host}:{port}) bağlanılamadı: {last}")

    # -------- frames

    def _send(self, cmd: int, arg0: int, arg1: int, payload: bytes = b"") -> None:
        self.sock.sendall(frame(cmd, arg0, arg1, payload))

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise SamsungError("TV bağlantıyı kapattı")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read(self) -> tuple[int, int, int, bytes]:
        cmd, arg0, arg1, length, checksum, magic = struct.unpack("<6I", self._recv_exact(24))
        if magic != cmd ^ 0xFFFFFFFF or length > 16 * 1024 * 1024:
            raise SamsungError(f"sdb: geçersiz çerçeve ({cmd:08X})")
        payload = self._recv_exact(length) if length else b""
        if sum(payload) & 0xFFFFFFFF != checksum:
            raise SamsungError("sdb: denetim toplamı tutmuyor")
        return cmd, arg0, arg1, payload

    def _handshake(self) -> None:
        self._send(CNXN, _SDB_VERSION, self.max_data, b"host::sdb-net-client")
        sent_public = False
        while True:
            cmd, arg0, arg1, payload = self._read()
            if cmd == CNXN:
                if arg1 > 0:
                    self.max_data = min(self.max_data, arg1)
                self.banner = payload.decode("utf-8", "replace")
                return
            if cmd != AUTH:
                raise SamsungError("sdb: CNXN ya da AUTH bekleniyordu")
            key = self.key or SdbKey.load_or_create()
            if arg0 == 1 and not sent_public:   # token: sign it
                self._send(AUTH, 2, 0, key.sign(payload))
                sent_public = True
            elif arg0 == 1:                     # signature refused: offer the public key (the TV may ask to allow it)
                self._send(AUTH, 3, 0, key.public_payload())
            else:
                raise SamsungError(f"sdb: bilinmeyen AUTH türü {arg0}")

    # -------- channels

    def _open(self, service: str) -> dict:
        self._next_id += 1
        ch = {"local": self._next_id, "remote": 0, "data": [], "closed": False, "acked": False}
        self._send(OPEN, ch["local"], 0, service.encode("utf-8") + b"\x00")
        while not ch["remote"]:
            if ch["closed"]:
                raise SamsungError(f"TV kanalı açmadı ({service.split(':')[0]})")
            self._pump(ch)
        return ch

    def _pump(self, ch: dict) -> None:
        cmd, arg0, arg1, payload = self._read()
        if arg1 != ch["local"]:
            if cmd in (WRTE, OPEN):
                self._send(CLSE, arg1, arg0)
            return
        if cmd == OKAY:
            if not ch["remote"]:
                ch["remote"] = arg0
            else:
                ch["acked"] = True
        elif cmd == WRTE:
            if not ch["remote"]:
                ch["remote"] = arg0
            ch["data"].append(payload)
            self._send(OKAY, ch["local"], arg0)
        elif cmd == CLSE:
            ch["closed"] = True
            try:
                self._send(CLSE, ch["local"], arg0)
            except OSError:
                pass

    def _write(self, ch: dict, data: bytes) -> None:
        for i in range(0, len(data), self.max_data):
            if ch["closed"]:
                raise SamsungError("TV kanalı kapattı")
            ch["acked"] = False
            self._send(WRTE, ch["local"], ch["remote"], data[i:i + self.max_data])
            while not ch["acked"]:
                if ch["closed"]:
                    raise SamsungError("TV kanalı kapattı")
                self._pump(ch)

    def _read_all(self, ch: dict) -> bytes:
        while not ch["closed"]:
            self._pump(ch)
        out = b"".join(ch["data"])
        ch["data"].clear()
        return out

    def _read_exact(self, ch: dict, n: int) -> bytes:
        while sum(len(d) for d in ch["data"]) < n:
            if ch["closed"]:
                raise SamsungError("sdb: kanal erken kapandı")
            self._pump(ch)
        joined = b"".join(ch["data"])
        ch["data"][:] = [joined[n:]] if len(joined) > n else []
        return joined[:n]

    def _close(self, ch: dict) -> None:
        if not ch["closed"]:
            ch["closed"] = True
            try:
                self._send(CLSE, ch["local"], ch["remote"])
            except OSError:
                pass

    # -------- services

    def shell(self, command: str) -> str:
        ch = self._open(f"shell:{command}")
        try:
            return self._read_all(ch).decode("utf-8", "replace")
        finally:
            self._close(ch)

    def capability(self) -> dict:
        ch = self._open("capability:")
        try:
            return parse_capability(self._read_all(ch))
        finally:
            self._close(ch)

    def push(self, data: bytes, remote_path: str) -> None:
        ch = self._open("sync:")
        try:
            self._write(ch, sync_packet("SEND", remote_path.encode("utf-8")))
            for i in range(0, len(data), _SYNC_CHUNK):
                self._write(ch, sync_packet("DATA", data[i:i + _SYNC_CHUNK]))
            self._write(ch, sync_packet("DONE", struct.pack("<I", int(time.time()))))
            while True:
                head = self._read_exact(ch, 8)
                ident, length = head[:4].decode("ascii", "replace"), struct.unpack("<I", head[4:])[0]
                payload = self._read_exact(ch, length) if length else b""
                if ident == "OKAY":
                    return
                if ident == "FAIL":
                    raise SamsungError(f"Dosya TV'ye gönderilemedi: {payload.decode('utf-8', 'replace') or 'FAIL'}")
        finally:
            self._close(ch)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---------------------------------------------------------------- Samsung account sign-in

def sign_in_url() -> str:
    redirect = urllib.parse.quote(f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}", safe="")
    return (f"{SIGN_IN_GATE}?locale=&clientId={CLIENT_ID}&redirect_uri={redirect}&state={SIGN_IN_STATE}"
            f"&tokenType=TOKEN")


def parse_callback(body: str) -> dict | None:
    """The sign-in page's POST body: code=<JSON with access_token, userId, inputEmailID>&state=..."""
    fields = {}
    for part in body.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            fields[urllib.parse.unquote(k)] = urllib.parse.unquote(v)  # not unquote_plus: a '+' in the token stays
    if fields.get("state") != SIGN_IN_STATE or not fields.get("code"):
        return None
    try:
        data = json.loads(fields["code"])
    except ValueError:
        return None
    token, user = data.get("access_token"), data.get("userId")
    if not isinstance(token, str) or not token or not isinstance(user, str) or not user:
        return None
    email = data.get("inputEmailID") if isinstance(data.get("inputEmailID"), str) else ""
    return {"access_token": token, "user_id": user, "email": email}


_DONE_PAGE = ("<!doctype html><meta charset=utf-8><title>Dilmaç</title><body style='font-family:system-ui;"
              "padding:2em'><h2>Samsung girişi tamam</h2><p>Dilmaç'a dönebilirsin; bu sekmeyi kapatabilirsin.</p>")


def sign_in(open_browser, timeout: float = 600, stop: threading.Event | None = None) -> dict:
    """Waits for the Samsung sign-in to come back to localhost:4794; open_browser(url) shows the sign-in page."""
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            size = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            body = self.rfile.read(size).decode("utf-8", "replace")
            account = parse_callback(body) if self.path.startswith(CALLBACK_PATH) else None
            page = (_DONE_PAGE if account else "Giriş bilgisi alınamadı. Dilmaç'ta yeniden dene.").encode("utf-8")
            self.send_response(200 if account else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            if account:
                result.update(account)

        def do_GET(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    try:
        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
    except OSError as e:
        raise SamsungError(f"Giriş dönüşü için localhost:{CALLBACK_PORT} açılamadı ({e}). Tizen Studio açık mı?")
    server.timeout = 0.5
    try:
        open_browser(sign_in_url())
        end = time.monotonic() + timeout
        while not result:
            if time.monotonic() > end:
                raise SamsungError("Samsung girişi zamanında tamamlanmadı; yeniden dene.")
            if stop is not None and stop.is_set():
                raise SamsungError("Samsung girişi iptal edildi.")
            server.handle_request()
    finally:
        server.server_close()
    return result


# ---------------------------------------------------------------- certificates

def _load_cert(data: bytes):
    from cryptography import x509

    return x509.load_pem_x509_certificate(data) if b"-----BEGIN" in data else x509.load_der_x509_certificate(data)


def make_requests(key, duid: str, email: str) -> tuple[bytes, bytes]:
    """PKCS#10 requests (PEM): the author's, and the distributor's naming the TV's DUID (the TV checks it)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID

    author = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Dilmac")])).sign(key, hashes.SHA256())
    names = [x509.NameAttribute(NameOID.COMMON_NAME, "TizenSDK")]
    if email:
        names.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    distributor = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(names)).add_extension(
        x509.SubjectAlternativeName([x509.UniformResourceIdentifier("URN:tizen:packageid="),
                                     x509.UniformResourceIdentifier(f"URN:tizen:deviceid={duid}")]),
        critical=False).sign(key, hashes.SHA256())
    return (author.public_bytes(serialization.Encoding.PEM), distributor.public_bytes(serialization.Encoding.PEM))


def multipart(fields: list[tuple[str, str]], file_field: str, file_name: str, data: bytes) -> tuple[bytes, str]:
    boundary = "dilmac-" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n"
                f"Content-Type: text/plain; charset=utf-8\r\n\r\n").encode("utf-8") + value.encode("utf-8") + b"\r\n"
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{file_name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8") + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode("ascii")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _post(url: str, body: bytes, content_type: str) -> bytes:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    step = urllib.parse.urlparse(url).path
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        text = e.read(600).decode("utf-8", "replace").strip()
        raise SamsungError(f"Samsung sertifika sunucusu {step}: {e.code} {e.reason} {text[:300]}".strip())
    except (urllib.error.URLError, OSError) as e:
        raise SamsungError(f"Samsung'un sertifika sunucusuna ulaşılamadı ({step}): {e}")


class Profile:
    """Signing files for one TV: key.pem, author.cer, distributor.cer, device-profile.xml (pc/samsung/<DUID>/)."""

    def __init__(self, duid: str, root: Path | None = None):
        self.duid = duid
        self.dir = (root or SAMSUNG_DIR) / re.sub(r"[^A-Za-z0-9_\-]", "_", duid)

    @property
    def exists(self) -> bool:
        return all((self.dir / n).is_file() for n in ("key.pem", "author.cer", "distributor.cer"))

    def key(self):
        import secure_store
        from cryptography.hazmat.primitives import serialization

        return serialization.load_pem_private_key(secure_store.read_bytes(self.dir / "key.pem"), None)

    def author(self):
        return _load_cert((self.dir / "author.cer").read_bytes())

    def distributor(self):
        return _load_cert((self.dir / "distributor.cer").read_bytes())

    def device_profile(self) -> bytes | None:
        p = self.dir / "device-profile.xml"
        return p.read_bytes() if p.is_file() else None

    def valid(self, margin: float = 86400) -> bool:
        if not self.exists:
            return False
        import secure_store

        try:
            self.key()  # encrypted for another Windows user / PC: not usable here, a new certificate is needed
            now = time.time() + margin
            return all(c.not_valid_before_utc.timestamp() <= now <= c.not_valid_after_utc.timestamp()
                       for c in (self.author(), self.distributor()))
        except (OSError, ValueError, TypeError, secure_store.SecretError):
            return False

    def save(self, key, author: bytes, distributor: bytes, device_profile: bytes | None) -> None:
        from cryptography.hazmat.primitives import serialization

        import secure_store

        self.dir.mkdir(parents=True, exist_ok=True)
        # The signing key is encrypted for this Windows user (DPAPI); the certificates are public anyway.
        secure_store.write_bytes(self.dir / "key.pem", key.private_bytes(serialization.Encoding.PEM,
                                                                          serialization.PrivateFormat.PKCS8,
                                                                          serialization.NoEncryption()))
        (self.dir / "author.cer").write_bytes(author)
        (self.dir / "distributor.cer").write_bytes(distributor)
        if device_profile:
            (self.dir / "device-profile.xml").write_bytes(device_profile)
        (self.dir / "duid.txt").write_text(self.duid, encoding="utf-8")

    def discard(self) -> None:
        """The TV refused these certificates: put them aside so the next install asks Samsung for new ones."""
        if self.dir.is_dir():
            self.dir.rename(self.dir.with_name(f"{self.dir.name}.rejected-{time.strftime('%Y%m%d%H%M%S')}"))


def request_certificates(account: dict, duid: str, profile: Profile, log, post=_post) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    author_csr, distributor_csr = make_requests(key, duid, account.get("email", ""))
    base = [("access_token", account["access_token"]), ("user_id", account["user_id"]), ("platform", "VD")]
    dist_fields = base + [("privilege_level", "public"), ("developer_type", "Individual")]
    log("Samsung'dan yazar sertifikası isteniyor…")
    author = post(AUTHOR_URL, *multipart(base, "csr", "author.csr", author_csr))
    log("Samsung'dan bu TV'ye özel dağıtıcı sertifikası isteniyor…")
    device_profile = None
    try:
        device_profile = post(DEVICE_PROFILE_URL, *multipart(dist_fields, "csr", "distributor.csr", distributor_csr))
    except SamsungError as e:  # only old TVs need it
        log(f"Cihaz profili alınamadı (yalnız 2017-2018 TV'ler için gerekli): {e}")
    distributor = post(DISTRIBUTOR_URL, *multipart(dist_fields, "csr", "distributor.csr", distributor_csr))
    for name, data in (("yazar", author), ("dağıtıcı", distributor)):
        try:
            _load_cert(data)
        except ValueError:
            raise SamsungError(f"Samsung {name} sertifikası vermedi: {data[:300].decode('utf-8', 'replace')}")
    profile.save(key, author, distributor, device_profile)
    log("Sertifikalar alındı.")


def chain(leaf) -> list:
    """The leaf and the Samsung intermediate that issued it (no root), as the TV expects in the signature."""
    out = [leaf]
    if CA_DIR.is_dir():
        issuer = leaf.issuer.rfc4514_string()
        for p in sorted(CA_DIR.glob("*.pem")):
            ca = _load_cert(p.read_bytes())
            if ca.subject.rfc4514_string() == issuer and ca.subject != ca.issuer:
                out.append(ca)
                break
    return out


# ---------------------------------------------------------------- the .wgt package and its signatures

DSIG = "http://www.w3.org/2000/09/xmldsig#"
_DSP = "http://www.w3.org/2009/xmldsig-properties"
_EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
_C14N11 = "http://www.w3.org/2006/12/xml-c14n11"
_RSA_SHA512 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha512"
_SHA512 = "http://www.w3.org/2001/04/xmlenc#sha512"


def _attr(value: str) -> str:
    """Attribute escaping as canonical XML writes it."""
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;").replace("\t", "&#x9;")
            .replace("\n", "&#xA;").replace("\r", "&#xD;"))


def _digest(data: bytes) -> str:
    return base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def prop_object(sig_id: str, canonical: bool) -> str:
    """<Object Id="prop">: profile, role and identifier. Canonical (C14N 1.1) form carries the inherited xmlns."""
    role = "author" if sig_id == "AuthorSignature" else "distributor"
    target = _attr("#" + sig_id)

    def prop(name, child, uri):
        u = f' URI="{_attr(uri)}"' if uri else ""
        return f'<SignatureProperty Id="{name}" Target="{target}"><dsp:{child}{u}></dsp:{child}></SignatureProperty>'

    head = f'<Object xmlns="{DSIG}" Id="prop">' if canonical else '<Object Id="prop">'
    return (head + f'<SignatureProperties xmlns:dsp="{_DSP}">'
            + prop("profile", "Profile", "http://www.w3.org/ns/widgets-digsig#profile")
            + prop("role", "Role", f"http://www.w3.org/ns/widgets-digsig#role-{role}")
            + prop("identifier", "Identifier", None)
            + "</SignatureProperties></Object>")


def signed_info(files: list[tuple[str, bytes]], prop_digest: str) -> str:
    """SignedInfo as written in the file (its namespace comes from <Signature>)."""
    parts = ["<SignedInfo>", f'<CanonicalizationMethod Algorithm="{_EXC_C14N}"></CanonicalizationMethod>',
             f'<SignatureMethod Algorithm="{_RSA_SHA512}"></SignatureMethod>']
    for name, data in files:
        parts.append(f'<Reference URI="{_attr(urllib.parse.quote(name))}"><DigestMethod Algorithm="{_SHA512}">'
                     f'</DigestMethod><DigestValue>{_digest(data)}</DigestValue></Reference>')
    parts.append(f'<Reference URI="#prop"><Transforms><Transform Algorithm="{_C14N11}"></Transform></Transforms>'
                 f'<DigestMethod Algorithm="{_SHA512}"></DigestMethod><DigestValue>{prop_digest}</DigestValue>'
                 "</Reference></SignedInfo>")
    return "".join(parts)


def signature_xml(sig_id: str, files: list[tuple[str, bytes]], key, certs: list) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    info = signed_info(files, _digest(prop_object(sig_id, canonical=True).encode("utf-8")))
    canonical_info = f'<SignedInfo xmlns="{DSIG}">' + info[len("<SignedInfo>"):]  # exclusive C14N
    value = key.sign(canonical_info.encode("utf-8"), padding.PKCS1v15(), hashes.SHA512())
    x509 = "".join("<X509Certificate>\n" + _wrap(base64.b64encode(c.public_bytes(serialization.Encoding.DER))) +
                   "\n</X509Certificate>" for c in certs)
    return (f'<Signature xmlns="{DSIG}" Id="{sig_id}">' + info +
            f"<SignatureValue>{base64.b64encode(value).decode('ascii')}</SignatureValue>"
            f"<KeyInfo><X509Data>{x509}</X509Data></KeyInfo>" + prop_object(sig_id, canonical=False) +
            "</Signature>").encode("utf-8")


def _wrap(b: bytes) -> str:
    s = b.decode("ascii")
    return "\n".join(s[i:i + 76] for i in range(0, len(s), 76))


def package_files(pc_host: str, pc_port: int, src: Path | None = None, key: str = "") -> list[tuple[str, bytes]]:
    """The Samsung package's files from tv-web/: platform-tizen.js as platform.js, config.xml with the version, the
    PC's address and Samsung's webapis.js in index.html, an icon."""
    import lg_tv

    src = src or lg_tv.web_dir()
    info = lg_tv.app_info(src)
    version = str(info["version"])
    files: dict[str, bytes] = {}
    for name in ("app.js", "style.css"):
        files[name] = (src / name).read_bytes()
    files["platform.js"] = (src / "platform-tizen.js").read_bytes()
    files["config.xml"] = (src / "config.xml").read_text(encoding="utf-8").replace("{version}", version).encode("utf-8")
    pc = json.dumps({"host": pc_host, "port": int(pc_port), "key": key})
    html = (src / "index.html").read_text(encoding="utf-8")
    inject = (f'  <script>window.DILMAC_PC = {pc};</script>\n'
              '  <script src="$WEBAPIS/webapis/webapis.js"></script>\n'
              '  <script src="platform.js"></script>')
    if '  <script src="platform.js"></script>' not in html:
        raise SamsungError("tv-web/index.html beklenen biçimde değil")
    files["index.html"] = html.replace('  <script src="platform.js"></script>', inject).encode("utf-8")
    files["icon.png"] = lg_tv._icon_png(512)
    return list(files.items())


def sign_wgt(files: list[tuple[str, bytes]], profile: Profile) -> bytes:
    """The signed .wgt: author-signature.xml (over the files), signature1.xml (over that and the files), the files."""
    files = [(n, d) for n, d in files if n.lower() not in ("author-signature.xml", "signature1.xml")]
    key = profile.key()
    author = ("author-signature.xml", signature_xml("AuthorSignature", files, key, chain(profile.author())))
    distributor = ("signature1.xml", signature_xml("DistributorSignature", [author] + files, key,
                                                   chain(profile.distributor())))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in [author, distributor] + files:
            z.writestr(name, data)
    return out.getvalue()


# ---------------------------------------------------------------- install output

def installed_version(listing: str) -> str | None:
    """Our app's version in "0 vd_applist" output (blocks of "---- key = value ----" lines)."""
    fields: dict[str, str] = {}
    found = None

    def flush():
        nonlocal found
        ids = {fields.get("app_tizen_id", "").strip().lower(), fields.get("app_id", "").strip().lower()}
        if found is None and ids & {APP_ID.lower(), PACKAGE_ID.lower()} and fields.get("app_version"):
            found = fields["app_version"]
        fields.clear()

    for raw in (listing or "").replace("\r", "").split("\n"):
        if "=" not in raw and "----" in raw:
            flush()
            continue
        m = re.match(r"^[\s-]*([A-Za-z_]+)\s*=\s*(.*?)[\s-]*$", raw)
        if m:
            fields[m.group(1).lower()] = m.group(2).strip()
    flush()
    return found


def install_problem(output: str) -> str | None:
    """None if "0 vd_appinstall" succeeded, otherwise a short kind (the command itself never fails)."""
    low = (output or "").lower()
    lost = any(t in low for t in ("transport connection", "connection reset by peer", "broken pipe"))
    if not (lost or "install failed" in low or "val[fail]" in low or "not enough space" in low
            or "download failed[116]" in low):
        return None
    if lost:
        return "connection"
    if "download failed[116]" in low or "not enough space" in low:
        return "space"
    if any(t in low for t in ("[118, -4]", "[118, -19]", "[118019]")):
        return "incompatible"
    certificate = any(t in low for t in ("[118, -12]", "[118012]", "[118, -11]", "[118011]"))
    if certificate and "check certificate error" in low and "same id and different certificate" not in low:
        return "clock" if "not valid yet" in low else "rejected"
    if certificate or "author certificate not match" in low:
        return "mismatch"
    if any(t in low for t in ("[118, -22]", "[118022]")):
        return "blocked"
    return "other"


PROBLEM_TEXT = {
    "connection": "TV ile bağlantı kurulum sırasında koptu.",
    "space": "TV'de yer yok.",
    "incompatible": "TV paketi bu sürümüyle uyumsuz buldu.",
    "rejected": "TV, Samsung sertifikasını kabul etmedi; bir sonraki denemede yeni sertifika alınacak.",
    "clock": "TV'nin saati yanlış (sertifika henüz geçerli değil görünüyor); TV'nin saatini düzelt.",
    "mismatch": "TV'de başka bir imzayla kurulmuş eski bir Dilmaç var.",
    "blocked": "TV paketi güvenlik nedeniyle engelledi.",
    "other": "TV kurulumu bitiremedi.",
}


def summary(output: str) -> str:
    lines = [l.strip() for l in (output or "").split("\n") if l.strip()]
    key = [l for l in lines if any(w in l.lower() for w in ("fail", "error", "reason", "not match", "space"))]
    return " | ".join(dict.fromkeys(key[:3]) if key else lines[-3:])[:400] or "TV yanıt vermedi"


# ---------------------------------------------------------------- whole steps (GUI / translator)

def _state() -> dict:
    try:
        return json.loads((SAMSUNG_DIR / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    SAMSUNG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SAMSUNG_DIR / "state.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SAMSUNG_DIR / "state.json")


def _pc_mark(host: str, port: int, key: str) -> str:
    """What the installed package knows about this PC: address and (a fingerprint of) the access key."""
    return f"{host}:{port}:{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def has_profile() -> bool:
    """A certificate was fetched before (for some TV)."""
    return any(p.is_dir() and (p / "distributor.cer").is_file() for p in SAMSUNG_DIR.glob("*")
               if ".rejected-" not in p.name) if SAMSUNG_DIR.is_dir() else False


def _connect(ip: str, pc_host: str, sdb_factory=Sdb) -> Sdb:
    try:
        return sdb_factory(ip)
    except SamsungError as e:
        raise SamsungError(f"{e}\n{refusal_hint(fetch_info(ip), pc_host)}")


def read_duid(sdb: Sdb) -> str:
    last = ""
    for _ in range(3):
        last = sdb.shell("0 getduid")
        duid = last.split("\n")[0].strip()
        if re.fullmatch(r"[A-Za-z0-9_\-+=/]{8,}", duid):
            return duid
        time.sleep(0.5)
    raise SamsungError(f"TV kimliği (DUID) okunamadı; TV'nin yanıtı: {last.strip()[:200] or 'boş'}")


def launch(sdb: Sdb, log) -> bool:
    reply = sdb.shell(f"0 was_execute {APP_ID}")
    if "fail" not in reply.lower() and ("] launched" in reply or "] resumed" in reply):
        return True
    log(f"Uygulama açılamadı: {reply.strip()[:200]}")
    return False


def install(ip: str, pc_port: int, log, sign_in_fn=None, sdb_factory=Sdb, post=_post, key: str | None = None) -> bool:
    """Installs (or updates) the app and opens it. sign_in_fn() -> account is called only when a certificate is needed;
    without it a missing certificate is an error."""
    import lg_tv
    import tv_web

    host = tv_web.local_address_for(ip)
    key = tv_web.tv_token() if key is None else key
    if not host:
        log(f"Bu bilgisayarın {ip} ağındaki adresi bulunamadı.")
        return False
    want = lg_tv.web_version()
    try:
        with _connect(ip, host, sdb_factory) as sdb:
            caps = sdb.capability()
            platform = caps.get("platform_version", "")
            tool = caps.get("sdk_toolpath") or "/opt/usr/apps/tmp"
            log(f"Samsung TV: Tizen {platform or '?'}")
            if platform and _version_tuple(platform) < MIN_TIZEN:
                raise SamsungError(f"Bu TV'nin Tizen sürümü ({platform}) çok eski; Tizen 3.0 (2017) ve sonrası gerekiyor.")
            duid = read_duid(sdb)
            profile = Profile(duid)
            if not profile.valid():
                if sign_in_fn is None:
                    raise SamsungError("Bu TV için Samsung sertifikası yok. TV satırında 'Kur'a bas ve Samsung hesabınla "
                                       "giriş yap.")
                log("Samsung hesabıyla giriş bekleniyor (tarayıcıda açıldı)…")
                account = sign_in_fn()
                log(f"Samsung girişi tamam{(' (' + account['email'] + ')') if account.get('email') else ''}.")
                request_certificates(account, duid, profile, log, post)
            wgt = sign_wgt(package_files(host, pc_port, key=key), profile)
            log(f"Paket imzalandı ({len(wgt) // 1024} KB), TV'ye gönderiliyor…")
            device_profile = profile.device_profile()
            if device_profile and platform and _version_tuple(platform) <= DEVICE_PROFILE_UP_TO:
                sdb.push(device_profile, f"{tool}/device-profile.xml")
            remote = f"{tool}/dilmac.wgt"
            for attempt in range(2):
                sdb.push(wgt, remote)
                log("TV'de kuruluyor…")
                output = sdb.shell(f"0 vd_appinstall {APP_ID} {remote}")
                problem = install_problem(output)
                if problem is None:
                    break
                log(f"Kurulum çıktısı: {summary(output)}")
                if problem == "rejected":
                    profile.discard()
                if attempt == 0 and problem in ("mismatch", "blocked", "other"):
                    log("TV'deki eski kopya kaldırılıp yeniden deneniyor…")
                    for ident in (PACKAGE_ID, APP_ID):
                        sdb.shell(f"0 vd_appuninstall {ident}")
                    continue
                raise SamsungError(PROBLEM_TEXT.get(problem, PROBLEM_TEXT["other"]) + f" ({summary(output)})")
            state = _state()
            state[duid] = {"version": want, "pc": _pc_mark(host, pc_port, key), "ip": ip, "installed_at": int(time.time())}
            state["last_duid"] = duid
            _save_state(state)
            log(f"Dilmaç {want} Samsung TV'ye kuruldu.")
            launch(sdb, log)
    except SamsungError as e:
        log(f"Samsung TV: {e}")
        return False
    except OSError as e:
        log(f"Samsung TV: bağlantı hatası ({e})")
        return False
    log(f"Samsung TV'de Dilmaç açıldı; TV bu bilgisayara bağlanacak ({host}:{pc_port}).")
    return True


def open_app(ip: str, pc_port: int, hdmi: int, log, sdb_factory=Sdb, key: str | None = None) -> bool:
    """At translation start: updates the app if tv-web changed or this PC's address changed (with the saved
    certificate, no sign-in), then opens it."""
    import lg_tv
    import tv_web

    host = tv_web.local_address_for(ip)
    key = tv_web.tv_token() if key is None else key
    if not host:
        log(f"Bu bilgisayarın {ip} ağındaki adresi bulunamadı.")
        return False
    want = lg_tv.web_version()
    try:
        with _connect(ip, host, sdb_factory) as sdb:
            listing = sdb.shell("0 vd_applist")
            duid = read_duid(sdb)
            have = installed_version(listing)
            if have is None and listing.strip() == "":
                have = (_state().get(duid) or {}).get("version")   # the list could not be read
            stale_pc = (_state().get(duid) or {}).get("pc") != _pc_mark(host, pc_port, key)
            if have == want and not stale_pc:
                if launch(sdb, log):
                    log(f"Samsung TV'de Dilmaç açıldı ({host}:{pc_port}).")
                    return True
                return False
            if not Profile(duid).valid():
                if have is None:
                    log("Samsung TV'de Dilmaç kurulu değil. TV satırında 'Kur'a bas.")
                    return False
                log(f"TV'deki Dilmaç {have}, yenisi {want}; güncellemek için TV satırında 'Kur'a bas.")
                return launch(sdb, log)
            log("Samsung TV uygulaması " + ("güncelleniyor" if have != want else "bu bilgisayarın yeni adresiyle "
                                            "yeniden kuruluyor") + "…")
    except SamsungError as e:
        log(f"Samsung TV: {e}")
        return False
    except OSError as e:
        log(f"Samsung TV: bağlantı hatası ({e})")
        return False
    return install(ip, pc_port, log, None, sdb_factory, key=key)
