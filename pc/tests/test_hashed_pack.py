"""Hashed packs (2026-11-27): what every app gets holds no English; lookups still work, misreads too."""
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pack
import pack_server as S
from pack import Pack, to_hashed

LINES = {
    "We need to get out of here before the cops show up.": "Polisler gelmeden buradan çıkmalıyız.",
    "Lose the cops.": "Polisleri ekte.",
    "Hey.": "Hey.",
    "I told you this was a bad idea, Jason.": "Sana bunun kötü bir fikir olduğunu söylemiştim, Jason.",
}
DATA = {"game": "GTA", "source": "test", "lines": LINES,
        "speakers": {"I told you this was a bad idea, Jason.": "Lucia"}}


def hashed_pack() -> Pack:
    p = Pack("GTA")
    p.add_hashed(json.loads(json.dumps(to_hashed(DATA))))
    return p


class HashedPackTest(unittest.TestCase):
    def test_no_english_in_the_hashed_file(self):
        text = json.dumps(to_hashed(DATA), ensure_ascii=False)
        for en in LINES:
            if en != LINES[en]:  # "Hey." is the same in Turkish
                self.assertNotIn(en, text)
            for word in ("cops", "Jason", "idea"):
                self.assertNotIn(word, text.replace("Jason.", ""))  # only the Turkish mentions Jason
        self.assertIn("Polisleri ekte.", text)

    def test_exact_ocr_and_misread_lookups(self):
        p = hashed_pack()
        self.assertEqual(p.lookup("Lose the cops."), "Polisleri ekte.")
        self.assertEqual(p.lookup("lose the cops!"), "Polisleri ekte!")       # punctuation follows the read line
        self.assertEqual(p.lookup("We need to get out of here before the cops show up."),
                         "Polisler gelmeden buradan çıkmalıyız.")
        self.assertEqual(p.lookup("We need to get out of hcre before the cops shovv up."),
                         "Polisler gelmeden buradan çıkmalıyız.")               # two misread letters
        self.assertIsNone(p.lookup("Where did you put the keys to the boat?"))
        self.assertIsNone(p.lookup("Hcy."))                                     # short lines are never matched loosely
        self.assertEqual(p.speaker("I told you this was a bad idea, Jason."), "Lucia")
        self.assertEqual(p.similar("We need to get out of here."), [])          # no English, no prompt examples

    def test_text_pack_first_hashed_second_is_merged_without_doubles(self):
        p = Pack("GTA", {"Lose the cops.": "Kendi çevirim."})
        p.add_hashed(to_hashed(DATA))
        self.assertEqual(p.lookup("Lose the cops."), "Kendi çevirim.")  # the file that came first wins
        self.assertEqual(len(p), len(LINES))

    def test_loaded_from_files_and_cleaned_by_the_sync(self):
        cleaned = S.clean_pack(json.loads(json.dumps(to_hashed(DATA))))
        self.assertEqual(cleaned["format"], pack.HASHED)
        self.assertEqual(len(cleaned["h"]), len(LINES))
        bad = to_hashed(DATA)
        bad["h"]["not-hex"] = "x"
        bad["m"][next(iter(bad["h"]))] = "zzz"
        bad["o"]["0" * 20] = "f" * 20  # points to no line
        cleaned = S.clean_pack(bad)
        self.assertNotIn("not-hex", cleaned["h"])
        self.assertNotIn("0" * 20, cleaned["o"])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "gta.json").write_text(json.dumps(cleaned), encoding="utf-8")
            with mock.patch.object(pack.paths, "pack_dirs", lambda: [Path(d)]):
                p = Pack.load("GTA")
        self.assertEqual(p.lookup("Lose the cops."), "Polisleri ekte.")


class AdminSyncTest(unittest.TestCase):
    def test_our_pcs_take_the_full_copy_the_apps_the_hashed_one(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        kid, pub = S.public_key_text(key)
        app_raw = gzip.compress(json.dumps(to_hashed(DATA)).encode())
        full_raw = gzip.compress(json.dumps(DATA).encode())
        sha_a, sha_f = (hashlib.sha256(x).hexdigest() for x in (app_raw, full_raw))
        index = {"packs": [{"id": "gta6", "sha256": sha_a, "key": kid, "signature": S.sign_pack(key, "gta6", sha_a),
                            "full": {"sha256": sha_f, "key": kid, "signature": S.sign_pack(key, "gta6" + S.FULL, sha_f)}}]}
        files = {"/v1/packs/gta6": app_raw, "/v1/admin/packs/gta6/full": full_raw}

        def fake(base, method, path, **kw):
            if path == "/v1/packs":
                return 200, {}, json.dumps(index).encode()
            if path.startswith("/v1/admin/") and not kw.get("admin"):
                return 403, {}, b""
            return 200, {}, files[path]

        base_cfg = {"pack_server": "https://example.test", "pack_trusted_keys": [pub]}
        with mock.patch.multiple(S, DEFAULT_URL="", DEFAULT_SECRET=""), mock.patch.object(S, "request", fake):
            for cfg, want_text in (({**base_cfg, "pack_secret": "s"}, False), ({**base_cfg, "pool_admin_token": "t"}, True)):
                with tempfile.TemporaryDirectory() as d:
                    S.sync_packs(cfg, lambda *a: None, folder=Path(d), force=True)
                    text = (Path(d) / "gta6.json").read_text(encoding="utf-8")
                    self.assertEqual("Lose the cops." in text, want_text, cfg)
            # a full copy signed as the app's copy (or not signed as "#full") is refused
            index["packs"][0]["full"]["signature"] = S.sign_pack(key, "gta6", sha_f)
            with tempfile.TemporaryDirectory() as d:
                logs = []
                S.sync_packs({**base_cfg, "pool_admin_token": "t"}, logs.append, folder=Path(d), force=True)
                self.assertFalse((Path(d) / "gta6.json").exists())
                self.assertTrue(any("İmzası doğrulanamayan" in x for x in logs))


if __name__ == "__main__":
    unittest.main()
