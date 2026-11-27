"""pack_server without a server: request signing, the line queue, pack download checks."""
import gzip
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pack_server as S
import paths

# The real server's address and app secret are built in; these tests choose the server themselves and must never
# reach the real one.
_defaults = mock.patch.multiple(S, DEFAULT_URL="", DEFAULT_SECRET="")


def setUpModule():
    _defaults.start()


def tearDownModule():
    _defaults.stop()

CFG = {"pack_server": "https://example.test", "pack_secret": "s3cret", "share_lines": True}


class SignTest(unittest.TestCase):
    def test_same_formula_as_the_worker(self):
        h = S.sign("s3cret", "GET", "/v1/packs", now=1_800_000_000)
        expected = hmac.new(b"s3cret", b"GET\n/v1/packs\n1800000000", hashlib.sha256).hexdigest()
        self.assertEqual(h, {"x-dilmac-time": "1800000000", "x-dilmac-signature": expected})

    def test_settings(self):
        self.assertIsNone(S.settings({}))
        self.assertIsNone(S.settings({"pack_server": "https://x"}))
        self.assertEqual(S.settings({"pack_server": "https://x/", "pack_secret": "k"}), ("https://x", "k"))
        # our own PCs (the Legion): the admin token is enough, no app secret
        self.assertEqual(S.settings({"pack_server": "https://x", "pool_admin_token": "t"}), ("https://x", ""))

    def test_admin_token_goes_with_pack_downloads(self):
        seen = []

        def fake(base, method, path, **kw):
            seen.append(kw.get("admin"))
            return 200, {}, b'{"packs": []}'

        with tempfile.TemporaryDirectory() as d, mock.patch.object(S, "request", fake):
            S.sync_packs({"pack_server": "https://x", "pool_admin_token": "t"}, print, folder=Path(d), force=True)
        self.assertEqual(seen, ["t"])

    def test_pack_ids(self):
        self.assertEqual(S.pack_id_for("Grand Theft Auto VI", "pool-"), "pool-grand-theft-auto-vi")
        self.assertEqual(S.pack_id_for("Ölümcül Oyun: İkinci Bölüm"), "olumcul-oyun-ikinci-bolum")


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patch = mock.patch.object(paths, "DATA", self.dir)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def pool(self, cfg=CFG):
        return S.LinePool(cfg, log=lambda *a: None, queue_file=self.dir / "q.json", start=False)

    def test_off_without_server_or_when_disabled(self):
        for cfg in ({}, {**CFG, "share_lines": False}):
            p = self.pool(cfg)
            p.add("GTA VI", "We need to go.", "speech", "Bilgisayar")
            self.assertEqual(p.queue, [])

    def test_queue_rules(self):
        p = self.pool()
        p.add("Grand Theft Auto VI", "We need to go.", "speech", "Bilgisayar")
        p.add("Grand Theft Auto VI", "We need to go!", "speech", "Yerel")    # same key: once
        p.add("Grand Theft Auto VI", "From the pack.", "speech", "pack")     # pack lines are known
        p.add("Grand Theft Auto VI", "Glossary.", "speech", "sözlük")
        p.add("Grand Theft Auto VI", "Lose the cops.", "objective", "önbellek")
        self.assertEqual([(q["key"], q["track"]) for q in p.queue], [("weneedtogo", "speech"), ("losethecops", "objective")])
        self.assertEqual(p.queue[0]["game"], "grandtheftautovi")

    def test_failed_send_stays_queued(self):
        p = self.pool()
        p.add("GTA VI", "We need to go.", "speech", "Bilgisayar")
        with mock.patch.object(S, "request", side_effect=S.ServerError("yok")):
            self.assertEqual(p.flush(), 0)
        self.assertEqual(len(json.loads((self.dir / "q.json").read_text(encoding="utf-8"))), 1)
        sent = []

        def ok(base, method, path, **kw):
            sent.append(json.loads(kw["body"]))
            return 200, {}, b'{"ok":true}'

        p2 = self.pool()  # a later session picks the queue up
        with mock.patch.object(S, "request", side_effect=ok):
            self.assertEqual(p2.flush(), 1)
        self.assertEqual(sent[0]["lines"][0]["en"], "We need to go.")
        self.assertRegex(sent[0]["install"], r"^[0-9a-f-]{36}$")
        self.assertEqual(S.install_id(), sent[0]["install"])  # kept


def new_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    return key, S.public_key_text(key)


def entry(key, pid, raw, title):
    sha = hashlib.sha256(raw).hexdigest()
    kid, _ = S.public_key_text(key)
    return {"id": pid, "sha256": sha, "title": title, "key": kid, "signature": S.sign_pack(key, pid, sha)}


class SyncTest(unittest.TestCase):
    def test_only_our_signed_packs(self):
        ours, (_, ours_pub) = new_key()
        theirs, _ = new_key()
        cfg = {**CFG, "pack_trusted_keys": [ours_pub]}
        good = gzip.compress(json.dumps({"game": "Grand Theft Auto VI",
                                         "lines": {"Hi.": "Sel‮am.", "x" * 500: "too long"}}).encode())
        forged = gzip.compress(json.dumps({"game": "GTA VI", "lines": {"Hi.": "Kötü yazı"}}).encode())
        bomb = gzip.compress(b'{"lines": {"a": "' + b"b" * (S.MAX_UNPACKED + 10) + b'"}}')
        swapped = entry(ours, "gta6", good, "GTA VI")
        swapped["id"] = "other"  # a signature for another pack id
        index = {"packs": [entry(ours, "gta6", good, "GTA VI"),
                           entry(theirs, "evil", forged, "Evil"),                       # not our key
                           {"id": "unsigned", "sha256": hashlib.sha256(forged).hexdigest()},
                           swapped,
                           entry(ours, "bomb", bomb, "Bomb")]}
        files = {"/v1/packs/gta6": good, "/v1/packs/evil": forged, "/v1/packs/unsigned": forged,
                 "/v1/packs/other": good, "/v1/packs/bomb": bomb}

        def fake(base, method, path, **kw):
            if path == "/v1/packs":
                return 200, {}, json.dumps(index).encode()
            return 200, {}, files[path]

        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / "old.json").write_text("{}", encoding="utf-8")  # no longer on the server
            logs = []
            with mock.patch.object(S, "request", side_effect=fake):
                r = S.sync_packs(cfg, logs.append, folder=folder, force=True)
            self.assertEqual(r["updated"], ["GTA VI (1 satır)"])
            self.assertEqual(sorted(p.name for p in folder.glob("*.json")), ["gta6.json"])
            data = json.loads((folder / "gta6.json").read_text(encoding="utf-8"))
            self.assertEqual(data["lines"], {"Hi.": "Selam."})  # direction mark removed, the over-long line dropped
            self.assertEqual(sum("İmzası doğrulanamayan" in line for line in logs), 3)
            self.assertTrue(any("okunamadı" in line for line in logs))  # the bomb
            with mock.patch.object(S, "request", side_effect=AssertionError("indirmemeli")):
                self.assertEqual(S.sync_packs(cfg, logs.append, folder=folder)["skipped"], "az önce bakıldı")
            # A later start (the Legion may have published since) looks again: not a day, only SYNC_EVERY.
            later = time.time() + S.SYNC_EVERY + 1
            with mock.patch.object(S, "request", side_effect=fake), mock.patch.object(S.time, "time", return_value=later):
                self.assertNotIn("skipped", S.sync_packs(cfg, logs.append, folder=folder))
            self.assertLessEqual(S.SYNC_EVERY, 15 * 60)

    def test_pack_saved_by_an_older_cleaner_is_downloaded_again(self):
        # 2026-11-27: an app from before "voices" saved the voices pack without them; same sha256 on the server.
        ours, (_, ours_pub) = new_key()
        cfg = {**CFG, "pack_trusted_keys": [ours_pub]}
        voices = {"Lucia": {"sum": [1.0, 0.0], "n": 30}}
        raw = gzip.compress(json.dumps({"game": "G", "lines": {}, "voices": voices}).encode())
        index = {"packs": [entry(ours, "voices-g", raw, "G (ses profilleri)")]}
        calls = []

        def fake(base, method, path, **kw):
            calls.append(path)
            return 200, {}, (json.dumps(index).encode() if path == "/v1/packs" else raw)

        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / "voices-g.json").write_text('{"game": "G", "lines": {}}', encoding="utf-8")
            (folder / S.STATE_FILE).write_text(json.dumps(
                {"voices-g": {"sha256": hashlib.sha256(raw).hexdigest(), "version": 1}}), encoding="utf-8")
            with mock.patch.object(S, "request", side_effect=fake):
                S.sync_packs(cfg, lambda *a: None, folder=folder, force=True)
                self.assertIn("/v1/packs/voices-g", calls)
                data = json.loads((folder / "voices-g.json").read_text(encoding="utf-8"))
                self.assertEqual(data["voices"]["Lucia"]["n"], 30)
                calls.clear()
                S.sync_packs(cfg, lambda *a: None, folder=folder, force=True)
                self.assertEqual(calls, ["/v1/packs"])  # up to date now: not downloaded again

    def test_publish_signs(self):
        key, (kid, pub) = new_key()
        sent = []

        def fake(base, method, path, **kw):
            sent.append(dict(kw["headers"], body=kw["body"]))
            return 200, {}, b'{"pack": {"version": 1}}'

        with mock.patch.object(S, "signing_key", return_value=key), mock.patch.object(S, "request", side_effect=fake):
            S.publish_pack({**CFG, "pool_admin_token": "t"}, "gta6",
                           {"game": "GTA VI", "lines": {"Hello there, partner.": "Selam ortak."}})
        app, full = sent
        self.assertNotIn("x-pack-variant", app)
        self.assertEqual(full["x-pack-variant"], "full")
        app_text, full_text = (gzip.decompress(x["body"]).decode("utf-8") for x in (app, full))
        self.assertNotIn("Hello there", app_text)  # the apps' copy has no English
        self.assertIn("Selam ortak.", app_text)
        self.assertIn("Hello there, partner.", full_text)
        e = {"id": "gta6", "sha256": hashlib.sha256(app["body"]).hexdigest(), "key": app["x-pack-key"],
             "signature": app["x-pack-signature"]}
        f = {"id": "gta6" + S.FULL, "sha256": hashlib.sha256(full["body"]).hexdigest(), "key": full["x-pack-key"],
             "signature": full["x-pack-signature"]}
        self.assertEqual(app["x-pack-key"], kid)
        self.assertTrue(S.verify_entry(e, {kid: pub}))
        self.assertTrue(S.verify_entry(f, {kid: pub}))
        self.assertFalse(S.verify_entry({**e, "id": "pool-gta6"}, {kid: pub}))
        self.assertFalse(S.verify_entry({**f, "id": "gta6"}, {kid: pub}))  # the full copy cannot pass for the app's
        with mock.patch.object(S, "signing_key", return_value=None):
            with self.assertRaises(S.ServerError):
                S.publish_pack({**CFG, "pool_admin_token": "t"}, "gta6", {"lines": {}})

    def test_http_only_for_this_pc(self):
        self.assertIsNone(S.settings({"pack_server": "http://example.test", "pack_secret": "k"}))
        self.assertIsNotNone(S.settings({"pack_server": "http://127.0.0.1:8799", "pack_secret": "k"}))

    def test_without_server(self):
        self.assertEqual(S.sync_packs({}, print), {"skipped": "sunucu ayarlı değil"})


if __name__ == "__main__":
    unittest.main()
