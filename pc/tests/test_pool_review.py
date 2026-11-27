"""Reviewing the pool's lines into the hand-made pack (2026-11-27): pending --pool, skipped lines, prune-pool."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pack_server as S
import pack_tool as T
import paths
import textutil as M
from pack import Pack


class Lex:
    def is_garbled(self, s):
        return "zzz" in s

    def is_turkish(self, s):
        return "ı" in s

    def score(self, s):
        return 1.0


class PoolReviewTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        root = Path(self.d.name)
        self.hand, self.server = root / "packs", root / "packs-server"
        self.hand.mkdir()
        self.server.mkdir()
        (self.hand / "g.json").write_text(json.dumps({"game": "G", "lines": {"We need to go.": "Gitmemiz lazım."},
                                                      "skipped": ["Some test line nobody saw."]}), encoding="utf-8")
        self.pool_file = self.server / "pool-g.json"
        self.pool_file.write_text(json.dumps({"game": "G", "source": "pool", "lines": {
            "We need to go.": "Gitmeliyiz.", "Some test line nobody saw.": "Test.", "Stay there!": "Orada kal!"}}),
            encoding="utf-8")
        self.patches = [mock.patch.object(paths, "SERVER_PACKS", self.server),
                        mock.patch.object(paths, "pack_dirs", lambda: [self.hand, self.server])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.d.cleanup()

    def test_pool_lines_come_with_gemma_draft_and_skip_known(self):
        pack_ = T.hand_pack("G", None)
        self.assertEqual(len(pack_), 1)  # the pool pack is not part of the hand-made pack
        skipped = T.skipped_keys("G")
        with mock.patch.object(S, "sync_packs", lambda *a, **k: {}), \
                mock.patch.object(S, "admin_lines", lambda *a, **k: [
                    {"id": 2, "en": "JASON: Move it!", "track": "speech", "installs": 1},
                    {"id": 3, "en": "zzz garbled", "track": "speech"}]):
            entries = T.from_pool({}, "G", log=lambda *a: None)
        scenes = T.pool_scenes(entries, pack_, Lex(), seen=set(), skipped=skipped)
        got = {r["en"]: r.get("draft") for s in scenes for r in s}
        self.assertEqual(got, {"Stay there!": "Orada kal!", "Move it!": None})  # known, skipped, garbled left out
        self.assertTrue(all(r["from"] == "havuz" for s in scenes for r in s))

    def test_add_keeps_drop_as_skipped_and_prune_cleans_pool(self):
        tr = Path(self.d.name) / "tr.json"
        tr.write_text(json.dumps({"lines": {"Stay there!": "Orada kal!"}, "drop": ["Bad readd line", "We need to go."]}),
                      encoding="utf-8")
        with mock.patch.object(paths, "DATA", Path(self.d.name)):
            dest = T.add(tr, "G", [])
        data = json.loads(dest.read_text(encoding="utf-8"))
        self.assertIn("Bad readd line", data["skipped"])
        self.assertNotIn("We need to go.", data["skipped"])  # a line the pack has is not "skipped"
        published = []
        with mock.patch.object(S, "sync_packs", lambda *a, **k: {}), \
                mock.patch.object(S, "publish_pack", lambda cfg, pid, d, **kw: published.append((pid, d)) or {"version": 2}):
            T.prune_pool({}, "G", None, drop=[], log=lambda *a: None)
        self.assertEqual(published[0][0], "pool-g")
        self.assertEqual(published[0][1]["lines"], {})  # in the hand pack now, or skipped
        self.assertEqual(json.loads(self.pool_file.read_text(encoding="utf-8"))["lines"], {})


if __name__ == "__main__":
    unittest.main()
