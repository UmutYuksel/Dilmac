"""Voice profiles on the Legion: the main PC sends each clip's voice vectors (no audio), the Legion builds the same
profiles from them without the voice model (voice_tool --server)."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio as S  # noqa: E402
import pack_server as P  # noqa: E402
import paths  # noqa: E402
import voice_tool as V  # noqa: E402
from test_audio import FakeExtractor, mono_sine  # noqa: E402

GAME = "Grand Theft Auto VI"


class FakePack:
    def __init__(self, labels: dict[str, str]):
        self.labels = labels

    def speaker_by_sentence(self, en: str):
        return self.labels.get(en)


def old_segment(pcm16k: bytes, k: dict, end_time: float) -> bytes:
    """voice_tool.segment before the vectors moved to audio.py (2026-11-28)."""
    started = max(0, int((k["lead"] - 0.1) * S.TARGET_RATE))
    last = int((k["lead"] + min(end_time, k.get("end", end_time))) * S.TARGET_RATE)
    return pcm16k[started * 2: last * 2]


class WindowTest(unittest.TestCase):
    def test_same_as_before(self):
        pcm = bytes(range(256)) * 400
        for k in ({"lead": 0.5, "end": 2.5}, {"lead": 0.5, "end": 0.9}, {"lead": 0.5}, {"lead": 0.05, "end": 0.0}):
            for secs in (S.LONG_WINDOW, S.LIVE_WINDOW):
                self.assertEqual(V.segment(pcm, k, secs), old_segment(pcm, k, secs))
                self.assertEqual(S.window(pcm, k["lead"], k.get("end"), secs), old_segment(pcm, k, secs))

    def test_clip_vectors(self):
        rec = S.SpeakerRecognizer(Path(tempfile.gettempdir()), "x", FakeExtractor())
        record = {"file": "20261127-221530-123.wav", "time": "2026-11-27 22:15:30", "en": "Let's go.", "speaker": None,
                  "loudness": 900, "lead": 0.5, "end": 1.2, "first": 1.0}
        c = S.clip_vectors(rec, mono_sine(3.0, 220), record)
        self.assertEqual(c["file"], record["file"])
        self.assertNotIn("speaker", c)  # unknown fields are left out, not sent as null
        self.assertNotIn("lead", c)
        self.assertEqual(c["end"], 1.2)
        self.assertEqual(len(c["vec_long"]), 3)
        self.assertAlmostEqual(sum(x * x for x in c["vec_live"]), 1.0, places=3)
        self.assertIsNone(S.clip_vectors(rec, bytes(96000), record))  # silence: nothing to send

    def test_offline_recognizer_has_no_model(self):
        with tempfile.TemporaryDirectory() as d:
            rec = S.SpeakerRecognizer.offline(Path(d), GAME)
            with self.assertRaises(RuntimeError):
                rec.vector(mono_sine(1.5, 220))
            self.assertTrue(rec.learn("Jason", [1.0, 0.0, 0.0]))


class UploaderTest(unittest.TestCase):
    CFG = {"pack_server": "https://example.test", "pool_admin_token": "x" * 40}

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.queue = Path(self.d.name) / "clip-queue.json"

    def tearDown(self):
        self.d.cleanup()

    def clip(self, i):
        return {"file": f"f{i}.wav", "en": f"Line {i}.", "vec_long": [1.0, 0.0, 0.0], "vec_live": None}

    def test_off_without_admin_token(self):
        u = P.ClipUploader({"pack_server": "https://example.test", "pack_secret": "s"}, queue_file=self.queue,
                           start=False)
        u.add(GAME, self.clip(1))
        self.assertEqual(u.queue, [])
        self.assertFalse(P.ClipUploader({**self.CFG, "upload_clips": False}, queue_file=self.queue, start=False).on)

    def test_batches_admin_only_and_kept_when_the_server_fails(self):
        calls = []

        def ok(base, method, path, **kw):
            calls.append((method, path, kw.get("admin"), json.loads(kw["body"])))
            return 200, {}, json.dumps({"ok": True, "added": len(json.loads(kw["body"])["clips"])}).encode()

        u = P.ClipUploader(self.CFG, queue_file=self.queue, start=False)
        for i in range(P.CLIPS_PER_POST + 5):
            u.add(GAME, self.clip(i))
        u.add(GAME, None)  # a clip with no usable vector is not queued
        with mock.patch.object(P, "request", side_effect=P.ServerError("offline")):
            self.assertEqual(u.flush(), 0)
        self.assertEqual(len(json.loads(self.queue.read_text())), P.CLIPS_PER_POST + 5)  # kept on disk
        again = P.ClipUploader(self.CFG, queue_file=self.queue, start=False)  # the app was closed meanwhile
        with mock.patch.object(P, "request", side_effect=ok):
            self.assertEqual(again.flush(), P.CLIPS_PER_POST + 5)
        self.assertEqual([len(c[3]["clips"]) for c in calls], [P.CLIPS_PER_POST, 5])
        self.assertTrue(all(c[:3] == ("POST", "/v1/admin/clips", "x" * 40) for c in calls))
        self.assertEqual(calls[0][3]["game"], "grandtheftautovi")
        self.assertEqual(calls[0][3]["clips"][0]["key"], "line0")
        self.assertEqual(json.loads(self.queue.read_text()), [])


class LegionEquivalenceTest(unittest.TestCase):
    """The profiles and the validation from the server's vectors (the Legion, no voice model) are the ones the main PC
    gets from its WAVs."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        base = Path(self.d.name)
        self.main_pc, self.legion = base / "main", base / "legion"
        clips = self.main_pc / "clips"
        clips.mkdir(parents=True)
        (self.main_pc / "model").mkdir()
        (self.main_pc / "model" / S.MODEL_NAME).write_bytes(b"model")  # a voice model is there (FakeExtractor)
        self.labels = {}
        records = []
        voices = [("Lucia", 220), ("Jason", 700), ("pedestrian", 450)]
        for i in range(3 * (S.MIN_PROFILE_SAMPLES + 4)):
            who, hz = voices[i % 3]
            en = f"Line number {i}."
            name = f"20261127-2215{i:02d}-123.wav" if i < 100 else f"20261127-2216{i - 100:02d}-123.wav"
            secs = 1.4 if i % 7 == 0 else 3.0  # some clips are cut short by the next line
            S.write_wav(clips / name, mono_sine(secs, hz + (i % 5) * 4))
            records.append({"file": name, "time": f"2026-11-27 22:{15 + i // 60:02d}:{i % 60:02d}", "game": GAME,
                            "en": en, "speaker": None, "loudness": 900, "first": 1000.0 + i * 4, "lead": 0.5,
                            "end": 0.9 if i % 7 == 0 else 2.5})
            if i % 11 != 0:  # some lines the pack does not know (yet)
                self.labels[en] = who
        (self.main_pc / paths.CLIP_LIST).write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        self.cfg = {"pack_server": "https://example.test", "pool_admin_token": "x" * 40}
        self.server: list[dict] = []
        self.fetched_after: list[int] = []

    def tearDown(self):
        self.d.cleanup()

    def run_tool(self, root: Path, *argv: str, model: bool) -> str:
        def fake_open(root_, game):
            if not model:
                raise AssertionError("the voice model must not be opened on the Legion")
            return S.SpeakerRecognizer(root_, game, FakeExtractor())

        def post_clips(cfg, game, clips):
            gk = P.game_key(game)
            have = {c["file"] for c in self.server}
            new = [c for c in clips if c["file"] not in have]
            for c in new:
                self.server.append({**c, "id": len(self.server) + 1, "game": gk, "key": "k"})
            return len(new)

        def admin_clips(cfg, game, after=0, limit=100000):
            self.fetched_after.append(after)
            return [dict(c) for c in self.server if c["game"] == P.game_key(game) and c["id"] > after][:limit]

        out = io.StringIO()
        with mock.patch.object(paths, "VOICES", root), \
                mock.patch.object(V, "prepare", return_value=(self.cfg, GAME, FakePack(self.labels))), \
                mock.patch.object(S.SpeakerRecognizer, "open", side_effect=fake_open), \
                mock.patch.object(P, "post_clips", side_effect=post_clips), \
                mock.patch.object(P, "admin_clips", side_effect=admin_clips), \
                contextlib.redirect_stdout(out):
            self.assertEqual(V.main(list(argv)), 0)
        return out.getvalue()

    def profiles(self, root: Path) -> dict:
        return json.loads((root / "profiles.json").read_text(encoding="utf-8"))[GAME]

    def test_same_profiles_and_validation(self):
        self.run_tool(self.main_pc, "profile", model=True)
        main_validation = self.run_tool(self.main_pc, "validate", model=True)
        upload = self.run_tool(self.main_pc, "upload", model=True)
        self.assertIn(f"{len(self.server)} yeni", upload)
        allowed = {"file", "time", "en", "speaker", "label", "voice_speaker", "similarity", "loudness", "end", "pitch",
                   "vec_long", "vec_live"}
        self.assertLessEqual(set(self.server[0]) - {"id", "game", "key"}, allowed)  # vectors only, no audio
        self.assertIn("0 yeni", self.run_tool(self.main_pc, "upload", model=True))  # again: nothing twice

        self.run_tool(self.legion, "profile", model=False)  # no voice model on the Legion: from the server
        self.assertTrue((self.legion / V.SERVER_CACHE).is_file())
        mine, legion = self.profiles(self.main_pc), self.profiles(self.legion)
        self.assertEqual(sorted(mine), sorted(legion))
        self.assertNotIn("pedestrian", legion)
        for who in mine:
            self.assertEqual(mine[who]["n"], legion[who]["n"])
            for a, b in zip(mine[who]["sum"], legion[who]["sum"]):
                self.assertAlmostEqual(a, b, delta=1e-4 * mine[who]["n"])
        legion_validation = self.run_tool(self.legion, "validate", model=False)
        self.assertEqual(legion_validation.splitlines()[1:], main_validation.splitlines()[1:])
        self.assertIn("Doğrulama", legion_validation)
        self.assertIn("Yaya/polis", legion_validation)

        listing = self.run_tool(self.legion, "list", "2026-11-27", model=False)
        self.assertIn("Line number 1.", listing)
        status = self.run_tool(self.legion, "status", model=False)
        self.assertIn("sunucudaki ses vektörleri", status)
        self.assertIn("Lucia", status)

    def test_only_new_clips_are_fetched(self):
        self.run_tool(self.main_pc, "upload", model=True)
        self.run_tool(self.legion, "status", model=False)
        n = len(self.server)
        cache = self.legion / V.SERVER_CACHE
        self.assertEqual(len(cache.read_text(encoding="utf-8").splitlines()), n)
        self.server.append({**self.server[0], "id": n + 1, "file": "20261128-100000-000.wav"})  # played again
        self.run_tool(self.legion, "status", model=False)
        self.assertEqual(self.fetched_after, [0, n])  # only what came after the last one it has
        self.assertEqual(len(cache.read_text(encoding="utf-8").splitlines()), n + 1)
        self.run_tool(self.legion, "status", model=False)  # nothing new: the copy is not doubled
        self.assertEqual(len(cache.read_text(encoding="utf-8").splitlines()), n + 1)

    def test_upload_needs_the_model(self):
        out = io.StringIO()
        with mock.patch.object(paths, "VOICES", self.legion), \
                mock.patch.object(V, "prepare", return_value=(self.cfg, GAME, FakePack(self.labels))), \
                contextlib.redirect_stdout(out):
            self.assertEqual(V.main(["upload", "--server"]), 1)
        self.assertIn("ana PC", out.getvalue())


if __name__ == "__main__":
    unittest.main()
