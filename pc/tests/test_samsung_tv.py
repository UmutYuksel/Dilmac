"""Samsung TV support: certificate requests, the widget signature (checked with lxml's own canonicalization), sdb
against a fake sdbd, and a whole install with a fake certificate server."""
import base64
import datetime
import hashlib
import io
import json
import re
import socket
import struct
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

import samsung_tv as S

try:
    from lxml import etree
except ImportError:  # lxml is only a test tool
    etree = None

DS = "{http://www.w3.org/2000/09/xmldsig#}"


def make_cert(subject_cn: str, key, issuer_cn: str | None = None, issuer_key=None, csr=None):
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn or subject_cn)])
    subject = csr.subject if csr is not None else x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
            .public_key(csr.public_key() if csr is not None else key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365)).sign(issuer_key or key, hashes.SHA256()))


class FakeCertificateServer:
    """Answers the three certificate requests like Samsung's server: the certificate for the CSR in the form."""

    def __init__(self):
        self.ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.calls = []

    def __call__(self, url, body, content_type):
        boundary = content_type.split("boundary=")[1].encode()
        fields = {}
        for part in body.split(b"--" + boundary):
            m = re.search(rb'name="([^"]+)"(?:; filename="[^"]*")?\r\n[^\r]*\r\n\r\n(.*)\r\n$', part, re.S)
            if m:
                fields[m.group(1).decode()] = m.group(2)
        self.calls.append((url, {k: v for k, v in fields.items() if k != "csr"}))
        csr = x509.load_pem_x509_csr(fields["csr"])
        if url == S.DEVICE_PROFILE_URL:
            return b"<profile/>"
        cert = make_cert("", None, "Test CA", self.ca_key, csr)
        return cert.public_bytes(serialization.Encoding.PEM if url == S.AUTHOR_URL else serialization.Encoding.DER)


class CsrTest(unittest.TestCase):
    def test_requests(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        author, distributor = S.make_requests(key, "ABCDEFGH1234", "oyuncu@example.com")
        a = x509.load_pem_x509_csr(author)
        d = x509.load_pem_x509_csr(distributor)
        self.assertTrue(a.is_signature_valid and d.is_signature_valid)
        self.assertEqual(d.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value, "TizenSDK")
        self.assertEqual(d.subject.get_attributes_for_oid(NameOID.EMAIL_ADDRESS)[0].value, "oyuncu@example.com")
        san = d.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(san.get_values_for_type(x509.UniformResourceIdentifier),
                         ["URN:tizen:packageid=", "URN:tizen:deviceid=ABCDEFGH1234"])
        self.assertIsInstance(d.signature_hash_algorithm, hashes.SHA256)


class CallbackTest(unittest.TestCase):
    def test_plus_in_token_is_kept(self):
        code = json.dumps({"access_token": "ab+c/d=", "userId": "u1", "inputEmailID": "x@y.z"})
        body = "code=" + __import__("urllib.parse").parse.quote(code, safe="+") + "&state=" + S.SIGN_IN_STATE
        self.assertEqual(S.parse_callback(body), {"access_token": "ab+c/d=", "user_id": "u1", "email": "x@y.z"})

    def test_wrong_state(self):
        code = json.dumps({"access_token": "t", "userId": "u"})
        self.assertIsNone(S.parse_callback("code=" + code + "&state=other"))
        self.assertIsNone(S.parse_callback("state=" + S.SIGN_IN_STATE))

    def test_url(self):
        url = S.sign_in_url()
        self.assertIn("clientId=v285zxnl3h", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A4794%2Fsignin%2Fcallback", url)


class ParseTest(unittest.TestCase):
    def test_applist(self):
        listing = ("------------------------------\n---- app_title = Netflix ----\n---- app_tizen_id = 3201907018807 "
                   "----\n---- app_version = 5.3 ----\n------------------------------\n---- app_title = Dilmaç ----\n"
                   "---- app_tizen_id = DilmacSubs.Dilmac ----\n---- app_version = 0.8.0 ----\n----------\n")
        self.assertEqual(S.installed_version(listing), "0.8.0")
        self.assertIsNone(S.installed_version(listing.replace("DilmacSubs.Dilmac", "Other.App")))
        self.assertIsNone(S.installed_version(""))

    def test_install_output(self):
        self.assertIsNone(S.install_problem("spend time for wascmd is [1890]ms\ninstalled 100%"))
        self.assertEqual(S.install_problem("install failed[118, -12], reason: Check certificate error : "
                                           "Invalid certificate chain"), "rejected")
        self.assertEqual(S.install_problem("install failed[118, -12] same id and different certificate"), "mismatch")
        self.assertEqual(S.install_problem("install failed[118, -4]"), "incompatible")
        self.assertEqual(S.install_problem("download failed[116]"), "space")
        self.assertEqual(S.install_problem("error: transport connection lost"), "connection")
        self.assertEqual(S.install_problem("install failed[118]"), "other")

    def test_capability(self):
        body = b"platform_version:6.0\nsdk_toolpath:/home/owner/share/tmp/sdk_tools\nsecure_protocol:disabled\n"
        caps = S.parse_capability(struct.pack(">H", len(body)) + body)
        self.assertEqual(caps["platform_version"], "6.0")
        self.assertEqual(caps["sdk_toolpath"], "/home/owner/share/tmp/sdk_tools")

    def test_sdb_public_key(self):
        with tempfile.TemporaryDirectory() as d:
            key = S.SdbKey.load_or_create(Path(d) / "k.pem")
            again = S.SdbKey.load_or_create(Path(d) / "k.pem")
        self.assertEqual(key.public_payload(), again.public_payload())
        raw = base64.b64decode(key.public_payload().split(b" ")[0])
        words, n0inv = struct.unpack("<II", raw[:8])
        n = int.from_bytes(raw[8:264], "little")
        self.assertEqual(words, 64)
        self.assertEqual((n * n0inv) % (1 << 32), (1 << 32) - 1)  # n0inv = -1/n mod 2^32
        self.assertEqual(n, key.key.public_key().public_numbers().n)


class Profiles:
    """A signing profile with test certificates (self-made CA)."""

    def __init__(self, root: Path, duid="TESTDUID1234"):
        self.profile = S.Profile(duid, root)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        author = make_cert("Dilmac", key, "Test Author CA", ca_key)
        dist = make_cert("TizenSDK", key, "Test Distributor CA", ca_key)
        self.profile.save(key, author.public_bytes(serialization.Encoding.PEM),
                          dist.public_bytes(serialization.Encoding.DER), b"<p/>")


@unittest.skipIf(etree is None, "lxml yok")
class SignatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Profiles(Path(self.tmp.name)).profile
        self.files = S.package_files("192.168.1.55", 8788, key="tv-key-123")
        self.wgt = S.sign_wgt(self.files, self.profile)

    def tearDown(self):
        self.tmp.cleanup()

    def test_package(self):
        with zipfile.ZipFile(io.BytesIO(self.wgt)) as z:
            names = z.namelist()
            html = z.read("index.html").decode("utf-8")
            config = z.read("config.xml").decode("utf-8")
            platform = z.read("platform.js").decode("utf-8")
        self.assertEqual(names[:2], ["author-signature.xml", "signature1.xml"])
        self.assertNotIn("appinfo.json", names)
        self.assertIn('window.DILMAC_PC = {"host": "192.168.1.55", "port": 8788, "key": "tv-key-123"};', html)
        self.assertLess(html.index("webapis.js"), html.index('src="platform.js"'))
        self.assertIn("tizen.tvwindow", platform)
        import lg_tv
        self.assertIn(f'version="{lg_tv.web_version()}"', config)
        self.assertNotIn("{version}", config)
        self.assertIn('package="DilmacSubs"', config)

    def check(self, name: str, references: dict[str, bytes], cert):
        with zipfile.ZipFile(io.BytesIO(self.wgt)) as z:
            root = etree.fromstring(z.read(name))
        info = root.find(DS + "SignedInfo")
        canonical = etree.tostring(info, method="c14n", exclusive=True)
        value = base64.b64decode(root.find(DS + "SignatureValue").text)
        cert.public_key().verify(value, canonical, padding.PKCS1v15(), hashes.SHA512())  # raises if wrong
        refs = {r.get("URI"): r.find(DS + "DigestValue").text for r in info.findall(DS + "Reference")}
        # The #prop reference: inclusive C14N (1.0 == 1.1 for this content) of the Object subtree. lxml's subtree c14n
        # copies the element out of its document and then writes xmlns="" on the children, so the whole document is
        # canonicalized and the apex gets the inherited default namespace, as inclusive C14N of a subset renders it.
        whole = etree.tostring(root, method="c14n").decode("utf-8")
        apex = re.search(r'<Object Id="prop">.*?</Object>', whole, re.S).group(0)
        prop = apex.replace('<Object Id="prop">', f'<Object xmlns="{DS[1:-1]}" Id="prop">', 1).encode("utf-8")
        self.assertEqual(refs.pop("#prop"), base64.b64encode(hashlib.sha512(prop).digest()).decode())
        self.assertEqual(set(refs), set(references))
        for uri, data in references.items():
            self.assertEqual(refs[uri], base64.b64encode(hashlib.sha512(data).digest()).decode(), uri)
        certs = root.findall(f"{DS}KeyInfo/{DS}X509Data/{DS}X509Certificate")
        self.assertEqual(x509.load_der_x509_certificate(base64.b64decode(certs[0].text)), cert)
        return root

    def test_signatures_verify(self):
        files = dict(self.files)
        with zipfile.ZipFile(io.BytesIO(self.wgt)) as z:
            author_xml = z.read("author-signature.xml")
        root = self.check("author-signature.xml", files, self.profile.author())
        self.assertEqual(root.get("Id"), "AuthorSignature")
        self.assertIn(b"role-author", author_xml)
        root = self.check("signature1.xml", {**files, "author-signature.xml": author_xml}, self.profile.distributor())
        self.assertEqual(root.get("Id"), "DistributorSignature")


class FakeSdbd:
    """A Samsung TV's sdbd on localhost: CNXN, shell commands, capability and sync (push)."""

    def __init__(self, listing_has_app: str | None = None, install_output="spend time for wascmd is [1500]ms\n",
                 duid="TESTDUID1234", platform="6.0"):
        self.files: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.installed = listing_has_app
        self.install_output = install_output
        self.duid, self.platform = duid, platform
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        self.sock.close()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    @staticmethod
    def read(conn):
        def exact(n):
            buf = b""
            while len(buf) < n:
                c = conn.recv(n - len(buf))
                if not c:
                    raise EOFError
                buf += c
            return buf
        cmd, a0, a1, length, _, _ = struct.unpack("<6I", exact(24))
        return cmd, a0, a1, exact(length) if length else b""

    def handle(self, conn):
        send = lambda *f: conn.sendall(S.frame(*f))  # noqa: E731
        next_id = [100]
        streams: dict[int, bytearray] = {}
        try:
            while True:
                cmd, a0, a1, payload = self.read(conn)
                if cmd == S.CNXN:
                    send(S.CNXN, 0x01000000, 4096, b"device::ro.product.model=TEST;")
                elif cmd == S.OPEN:
                    local, remote = a0, next_id[0]
                    next_id[0] += 1
                    service = payload.rstrip(b"\x00").decode()
                    send(S.OKAY, remote, local)
                    if service == "sync:":
                        streams[remote] = bytearray()
                        continue
                    out = self.answer(service).encode()
                    send(S.WRTE, remote, local, out)
                    self.read(conn)  # client's OKAY
                    send(S.CLSE, remote, local)
                elif cmd == S.WRTE and a1 in streams:
                    send(S.OKAY, a1, a0)
                    streams[a1] += payload
                    if self.sync_done(streams[a1]):
                        send(S.WRTE, a1, a0, b"OKAY" + struct.pack("<I", 0))
                        self.read(conn)  # client's OKAY
                elif cmd == S.CLSE:
                    pass
        except (EOFError, OSError):
            conn.close()

    def sync_done(self, buf: bytearray) -> bool:
        pos, path, data = 0, None, bytearray()
        while pos + 8 <= len(buf):
            ident, length = bytes(buf[pos:pos + 4]), struct.unpack("<I", buf[pos + 4:pos + 8])[0]
            if ident == b"DONE":
                self.files[path] = bytes(data)
                return True
            if pos + 8 + length > len(buf):
                return False
            body = bytes(buf[pos + 8:pos + 8 + length])
            if ident == b"SEND":
                path = body.decode()
            elif ident == b"DATA":
                data += body
            pos += 8 + length
        return False

    def answer(self, service: str) -> str:
        if service == "capability:":
            body = f"platform_version:{self.platform}\nsdk_toolpath:/home/owner/share/tmp/sdk_tools\n"
            return chr(0) + chr(len(body)) + body
        cmd = service[len("shell:"):]
        self.commands.append(cmd)
        if cmd == "0 getduid":
            return self.duid + "\n"
        if cmd == "0 vd_applist":
            if not self.installed:
                return "---- app_title = Netflix ----\n---- app_tizen_id = 111 ----\n---- app_version = 1 ----\n----\n"
            return (f"---- app_title = Dilmaç ----\n---- app_tizen_id = {S.APP_ID} ----\n"
                    f"---- app_version = {self.installed} ----\n------\n")
        if cmd.startswith("0 vd_appinstall "):
            path = cmd.split()[-1]
            with zipfile.ZipFile(io.BytesIO(self.files[path])) as z:
                version = re.search(r'<widget[^>]*\sversion="([^"]+)"', z.read("config.xml").decode()).group(1)
                self.signed = "signature1.xml" in z.namelist()
            if "failed" not in self.install_output:
                self.installed = version
            return self.install_output
        if cmd.startswith("0 was_execute "):
            return f"app_id[{S.APP_ID}] launched\n"
        if cmd.startswith(("0 vd_appuninstall", "0 was_kill")):
            return "uninstalled\n"
        return "unknown command\n"


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [mock.patch.object(S, "SAMSUNG_DIR", root),
                        mock.patch("tv_web.local_address_for", return_value="192.168.1.55"),
                        mock.patch("tv_web.tv_token", return_value="tv-key-123")]
        for p in self.patches:
            p.start()
        self.lines = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def factory(self, tv):
        return lambda ip: S.Sdb("127.0.0.1", tv.port)

    def test_first_install_signs_in_and_installs(self):
        tv = FakeSdbd()
        certs = FakeCertificateServer()
        account = {"access_token": "tok", "user_id": "u1", "email": "a@b.c"}
        ok = S.install("192.168.1.80", 8788, self.lines.append, lambda: account, self.factory(tv), certs)
        tv.close()
        self.assertTrue(ok, self.lines)
        import lg_tv
        self.assertEqual(tv.installed, lg_tv.web_version())
        self.assertTrue(tv.signed)
        self.assertEqual([c[0] for c in certs.calls], [S.AUTHOR_URL, S.DEVICE_PROFILE_URL, S.DISTRIBUTOR_URL])
        self.assertEqual(certs.calls[2][1], {"access_token": b"tok", "user_id": b"u1", "platform": b"VD",
                                             "privilege_level": b"public", "developer_type": b"Individual"})
        self.assertIn(f"0 vd_appinstall {S.APP_ID} /home/owner/share/tmp/sdk_tools/dilmac.wgt", tv.commands)
        self.assertIn(f"0 was_execute {S.APP_ID}", tv.commands)
        self.assertNotIn("/home/owner/share/tmp/sdk_tools/device-profile.xml", tv.files)  # Tizen 6: not needed
        self.assertTrue(S.Profile("TESTDUID1234").valid())
        # translation start afterwards: same version, same PC address -> only opens
        tv2 = FakeSdbd(listing_has_app=lg_tv.web_version())
        self.assertTrue(S.open_app("192.168.1.80", 8788, 0, self.lines.append, self.factory(tv2)))
        self.assertFalse(any("vd_appinstall" in c for c in tv2.commands))
        # the PC's address changed: reinstalled with the saved certificate, no sign-in
        with mock.patch("tv_web.local_address_for", return_value="192.168.1.77"):
            self.assertTrue(S.open_app("192.168.1.80", 8788, 0, self.lines.append, self.factory(tv2)))
        self.assertTrue(any("vd_appinstall" in c for c in tv2.commands))
        with zipfile.ZipFile(io.BytesIO(tv2.files["/home/owner/share/tmp/sdk_tools/dilmac.wgt"])) as z:
            self.assertIn('"host": "192.168.1.77"', z.read("index.html").decode())
        tv2.close()

    def test_old_tv_gets_device_profile(self):
        tv = FakeSdbd(platform="4.0")
        Profiles(S.SAMSUNG_DIR)
        ok = S.install("192.168.1.80", 8788, self.lines.append, None, self.factory(tv))
        tv.close()
        self.assertTrue(ok, self.lines)
        self.assertEqual(tv.files["/home/owner/share/tmp/sdk_tools/device-profile.xml"], b"<p/>")

    def test_too_old(self):
        tv = FakeSdbd(platform="2.4")
        ok = S.install("192.168.1.80", 8788, self.lines.append, None, self.factory(tv))
        tv.close()
        self.assertFalse(ok)
        self.assertTrue(any("çok eski" in l for l in self.lines))

    def test_no_certificate_without_sign_in(self):
        tv = FakeSdbd()
        self.assertFalse(S.install("192.168.1.80", 8788, self.lines.append, None, self.factory(tv)))
        tv.close()
        self.assertTrue(any("Samsung sertifikası yok" in l for l in self.lines))
        self.assertFalse(S.open_app("192.168.1.80", 8788, 0, self.lines.append, self.factory(FakeSdbd())))
        self.assertTrue(any("kurulu değil" in l for l in self.lines))

    def test_mismatch_is_retried_after_uninstall(self):
        tv = FakeSdbd(install_output="install failed[118, -12] same id and different certificate\n")
        Profiles(S.SAMSUNG_DIR)
        ok = S.install("192.168.1.80", 8788, self.lines.append, None, self.factory(tv))
        tv.close()
        self.assertFalse(ok)  # the fake TV keeps refusing
        self.assertIn(f"0 vd_appuninstall {S.PACKAGE_ID}", tv.commands)
        self.assertEqual(sum(c.startswith("0 vd_appinstall") for c in tv.commands), 2)

    def test_rejected_certificate_is_discarded(self):
        tv = FakeSdbd(install_output="install failed[118, -12] Check certificate error : invalid chain\n")
        Profiles(S.SAMSUNG_DIR)
        self.assertFalse(S.install("192.168.1.80", 8788, self.lines.append, None, self.factory(tv)))
        tv.close()
        self.assertFalse(S.Profile("TESTDUID1234").exists)

    def test_unreachable_tv_explains(self):
        with mock.patch.object(S, "fetch_info", return_value={"developer_mode": True, "developer_ip": "192.168.1.20"}):
            ok = S.install("127.0.0.1", 8788, self.lines.append, None, lambda ip: S.Sdb("127.0.0.1", 1, timeout=1))
        self.assertFalse(ok)
        self.assertTrue(any("Host PC IP 192.168.1.20" in l for l in self.lines), self.lines)


if __name__ == "__main__":
    unittest.main()
