"""LG TV support: the .ipk package, the TV's key, the dev install service's answers."""
import gzip
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lg_tv
import paths


def ar_members(data: bytes) -> list[tuple[str, bytes]]:
    assert data[:8] == b"!<arch>\n"
    out, pos = [], 8
    while pos < len(data):
        header = data[pos:pos + 60]
        assert header[58:60] == b"`\n", header
        name = header[:16].decode("ascii").rstrip()
        size = int(header[48:58].decode("ascii"))
        out.append((name, data[pos + 60:pos + 60 + size]))
        pos += 60 + size + (size % 2)
    return out


def tar_files(data: bytes) -> dict[str, bytes | None]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
        out = {}
        for m in tar.getmembers():
            assert m.uid == 0 and m.gid == 0, m.name
            out[m.name] = None if m.isdir() else tar.extractfile(m).read()
        return out


class IpkTest(unittest.TestCase):
    def setUp(self):
        self.ipk = lg_tv.build_ipk(mtime=1_700_000_000)
        self.info = lg_tv.app_info()

    def test_ar_layout(self):
        names = [n for n, _ in ar_members(self.ipk)]
        self.assertEqual(names, ["debian-binary", "control.tar.gz", "data.tar.gz"])
        self.assertEqual(ar_members(self.ipk)[0][1], b"2.0\n")

    def test_control(self):
        control = tar_files(dict(ar_members(self.ipk))["control.tar.gz"])["./control"].decode("utf-8")
        fields = dict(line.split(": ", 1) for line in control.strip().splitlines())
        self.assertEqual(fields["Package"], "com.dilmac.tv")
        self.assertEqual(fields["Version"], self.info["version"])
        self.assertEqual(fields["Architecture"], "all")
        self.assertEqual(fields["webOS-Package-Format-Version"], "2")

    def test_data(self):
        files = tar_files(dict(ar_members(self.ipk))["data.tar.gz"])
        app = "./usr/palm/applications/com.dilmac.tv/"
        for name in ("appinfo.json", "index.html", "app.js", "platform.js", "style.css", "icon.png", "largeIcon.png"):
            self.assertIn(app + name, files)
        self.assertEqual(files[app + "icon.png"][:8], b"\x89PNG\r\n\x1a\n")
        info = json.loads(files["./usr/palm/packages/com.dilmac.tv/packageinfo.json"])
        self.assertEqual(info["id"], "com.dilmac.tv")
        self.assertEqual(info["version"], self.info["version"])
        self.assertIsNone(files[app.rstrip("/")])  # folders are folders

    def test_samsung_files_left_out(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d)
            for p in lg_tv.web_dir().iterdir():
                if p.is_file():
                    (src / p.name).write_bytes(p.read_bytes())
            (src / "platform-tizen.js").write_text("// samsung", encoding="utf-8")
            (src / "config.xml").write_text("<widget/>", encoding="utf-8")
            files = tar_files(dict(ar_members(lg_tv.build_ipk(src)))["data.tar.gz"])
        self.assertFalse(any(n.endswith(("platform-tizen.js", "config.xml")) for n in files))

    def test_page_and_appinfo_agree(self):
        """The version the page reports to the PC is the package's version (the PC updates the TV when it differs)."""
        js = (lg_tv.web_dir() / "app.js").read_text(encoding="utf-8")
        self.assertIn(f"var VERSION = '{self.info['version']}';", js)
        self.assertEqual(self.info["id"], lg_tv.APP_ID)
        self.assertTrue(self.info.get("disableBackHistoryAPI"))


class KeyTest(unittest.TestCase):
    @staticmethod
    def encrypted_pem(passphrase: str) -> str:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # The old OpenSSL format with Proc-Type/DEK-Info headers, like the TV's key server gives.
        return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.BestAvailableEncryption(passphrase.encode())).decode("ascii")

    def test_opens_with_passphrase(self):
        pem = self.encrypted_pem("A1b2C3")
        self.assertIn("ENCRYPTED", pem)
        self.assertIsNotNone(lg_tv.private_key(pem, "A1b2C3"))

    def test_wrong_passphrase(self):
        pem = self.encrypted_pem("A1b2C3")
        with self.assertRaises(lg_tv.LgError):
            lg_tv.private_key(pem, "a1b2c3")

    def test_fetch_saves_key(self):
        pem = self.encrypted_pem("XyZ123")

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(paths, "LG_FILE", Path(d) / "lg-tv.json"), \
                mock.patch("urllib.request.urlopen", return_value=Answer(pem.replace("\n", "\r\n").encode())) as get:
            lg_tv.fetch_key("192.168.1.80", " XyZ123 ")
            self.assertEqual(get.call_args[0][0], "http://192.168.1.80:9991/webos_rsa")
            state = lg_tv.load_state()
            self.assertTrue(lg_tv.has_key())
        self.assertEqual(state["passphrase"], "XyZ123")
        self.assertNotIn("\r", state["key"])

    def test_empty_passphrase(self):
        with self.assertRaises(lg_tv.LgError):
            lg_tv.fetch_key("192.168.1.80", "  ")


class LunaTest(unittest.TestCase):
    def test_install_answers(self):
        self.assertIsNone(lg_tv.install_state('{"id":"x","details":{"state":"installing","progress":40},'
                                              '"returnValue":true}'))
        self.assertEqual(lg_tv.install_state('{"details":{"state":"installed"},"returnValue":true}'), "installed")
        self.assertEqual(lg_tv.install_state('{"details":{"state":"install failed","errorCode":-5},'
                                             '"returnValue":true}'), "failed")
        self.assertEqual(lg_tv.install_state('{"returnValue":false,"errorText":"Denied"}'), "failed")
        self.assertEqual(lg_tv.install_state("luna-send-pub: not found"), "failed")
        self.assertIsNone(lg_tv.install_state('{"subscribed":true,"returnValue":true}'))

    def test_command_quoting(self):
        cmd = lg_tv._luna_command("luna://com.webos.applicationManager/launch",
                                  {"id": lg_tv.APP_ID, "params": {"host": "192.168.1.55", "port": 8788, "hdmi": 0}})
        self.assertEqual(cmd, "luna-send-pub -n 1 'luna://com.webos.applicationManager/launch' "
                              "'{\"id\":\"com.dilmac.tv\",\"params\":{\"host\":\"192.168.1.55\",\"port\":8788,"
                              "\"hdmi\":0}}'")
        with self.assertRaises(lg_tv.LgError):
            lg_tv._luna_command("luna://x/y", {"a": "it's"})


if __name__ == "__main__":
    unittest.main()
