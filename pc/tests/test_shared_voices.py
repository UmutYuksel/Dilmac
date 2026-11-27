"""Shared voice profiles (2026-11-27): signed "voices-*" packs, the app's vector queue and the nightly merge filter."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audio
import nightly as N
import pack_server as S
import paths

_defaults = mock.patch.multiple(S, DEFAULT_URL="", DEFAULT_SECRET="")


def setUpModule():
    _defaults.start()


def tearDownModule():
    _defaults.stop()


def unit(i: int, dim: int = 8, noise: int | None = None) -> list[float]:
    v = [0.0] * dim
    v[i] = 1.0
    if noise is not None:
        v[noise] = 0.3
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def profile(i: int, n: int, dim: int = 8) -> dict:
    return {"sum": [x * n for x in unit(i, dim)], "n": n}


class CleanVoicesTest(unittest.TestCase):
    def test_only_sane_profiles_are_kept(self):
        raw = {"Lucia": profile(0, 30), "Jason": {"sum": [1, "x"], "n": 3}, "": profile(1, 3),
               "Bret": {"sum": [float("inf")] * 8, "n": 3}, "Billy": {"sum": [1.0] * 5, "n": 4},
               "Vicki": {"sum": [1.0] * 8, "n": True}}
        out = S.clean_pack({"game": "G", "lines": {}, "voices": raw})["voices"]
        self.assertEqual(sorted(out), ["Lucia"])  # wrong type, empty name, inf, other length, bool count dropped
        self.assertNotIn("voices", S.clean_pack({"game": "G", "lines": {}}))


class SharedProfilesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        (self.root / "server").mkdir()
        self.rec = audio.SpeakerRecognizer(self.root, "GTA", extractor=None)

    def tearDown(self):
        self.d.cleanup()

    def write_shared(self, voices: dict, name: str = "voices-gta.json", game: str = "GTA"):
        (self.root / "server" / name).write_text(json.dumps({"game": game, "lines": {}, "voices": voices}),
                                                 encoding="utf-8")

    def test_shared_profile_decides_and_is_not_saved(self):
        import numpy as np

        self.write_shared({"Lucia": profile(0, 40), "Jason": profile(1, 40)})
        self.write_shared({"Lucia": profile(0, 99)}, "voices-other.json", game="Another game")
        self.rec.load_shared(self.root / "server")
        self.assertEqual(self.rec.shared["Lucia"]["n"], 40)  # the other game's file is not read
        who, sim, _ = self.rec.speaker(np.asarray(unit(0, noise=2), dtype=np.float32))
        self.assertEqual(who, "Lucia")
        self.rec.learn("Jason", np.asarray(unit(1), dtype=np.float32))
        self.rec.save()
        saved = json.loads((self.root / "profiles.json").read_text(encoding="utf-8"))["GTA"]
        self.assertEqual(sorted(saved), ["Jason"])  # only what we learned; the shared profiles stay out
        self.assertEqual(saved["Jason"]["n"], 1)

    def test_bigger_profile_wins_never_added(self):
        self.rec.profiles = {"Lucia": profile(0, 25)}
        self.write_shared({"Lucia": profile(1, 60)})
        self.rec.load_shared(self.root / "server")
        with self.rec._lock:
            self.assertEqual(self.rec._decision_profiles()["Lucia"]["n"], 60)
        self.rec.profiles = {"Lucia": profile(0, 80)}
        with self.rec._lock:
            self.assertEqual(self.rec._decision_profiles()["Lucia"]["n"], 80)


class VoiceQueueTest(unittest.TestCase):
    CFG = {"pack_server": "https://example.test", "pack_secret": "s3cret", "share_lines": True}

    def pool(self, cfg):
        d = tempfile.mkdtemp()
        return S.LinePool(cfg, log=lambda *a: None, queue_file=Path(d) / "q.json", start=False)

    def test_vectors_go_to_the_server(self):
        p = self.pool(self.CFG)
        p.add_voice("Grand Theft Auto VI", "Lucia", unit(0))
        p.add_voice("Grand Theft Auto VI", "", unit(0))      # no speaker: dropped
        p.add_voice("Grand Theft Auto VI", "Jason", None)    # no vector: dropped
        sent = []

        def fake(base, method, path, **kw):
            sent.append((path, json.loads(kw["body"])))
            return 200, {}, b"{}"

        with mock.patch.object(S, "request", fake):
            self.assertEqual(p.flush_voices(), 1)
        self.assertEqual(sent[0][0], "/v1/voices")
        self.assertEqual(sent[0][1]["game"], "grandtheftautovi")
        self.assertEqual([x["speaker"] for x in sent[0][1]["voices"]], ["Lucia"])

    def test_our_own_pcs_and_opt_out_send_none(self):
        for cfg in ({**self.CFG, "pool_admin_token": "t"}, {**self.CFG, "share_voices": False},
                    {**self.CFG, "share_lines": False}):
            p = self.pool(cfg)
            p.add_voice("G", "Lucia", unit(0))
            self.assertEqual(p.voices, [], cfg)


class MergeVoicesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.server = Path(self.d.name)
        base = {"Lucia": profile(0, 40), "Jason": profile(1, 40), "Dunk": profile(2, 5)}
        (self.server / "voices-gta.json").write_text(json.dumps({"game": "GTA", "lines": {}, "voices": base}),
                                                     encoding="utf-8")
        self.patches = [mock.patch.object(paths, "SERVER_PACKS", self.server),
                        mock.patch.object(N, "game_title", lambda k: "GTA"),
                        mock.patch.object(N.Glossary, "load", lambda t: None),
                        mock.patch.object(S, "pack_id_for", lambda t, prefix="": f"{prefix}gta")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.d.cleanup()

    def row(self, speaker, v, n, source="a"):
        return {"game": "gta", "speaker": speaker, "source": source, "n": n, "sum": [x * n for x in v]}

    def test_close_rows_taken_far_rows_refused_big_rows_capped(self):
        rows = [self.row("Lucia", unit(0, noise=3), 200, "a"),   # Lucia's voice, many clips: capped at VOICE_CAP
                self.row("Lucia", unit(1), 30, "b"),             # sounds like Jason (party chat, wrong label)
                self.row("Lucia", unit(4), 30, "c"),             # nobody we know
                self.row("Jason", unit(1), 2, "d"),              # too few clips
                self.row("Dunk", unit(2), 30, "e")]              # we have no usable profile of Dunk
        published = []
        with mock.patch.object(S, "admin_voices", lambda cfg: rows), \
                mock.patch.object(S, "publish_pack", lambda cfg, pid, data, **kw: published.append((pid, data)) or {}):
            out = N.merge_voices({}, log=lambda *a: None)
        self.assertEqual(out["GTA"], {"rows": 5, "taken": 1, "refused": 2})
        pid, data = published[0]
        self.assertEqual(pid, "voices-pool-gta")
        self.assertEqual(data["voices"]["Lucia"]["n"], 40 + N.VOICE_CAP)
        self.assertEqual(data["voices"]["Jason"]["n"], 40)
        # nothing new next time: not published again
        with mock.patch.object(S, "admin_voices", lambda cfg: rows), \
                mock.patch.object(S, "publish_pack", lambda *a, **kw: self.fail("published twice")):
            N.merge_voices({}, log=lambda *a: None)


if __name__ == "__main__":
    unittest.main()
