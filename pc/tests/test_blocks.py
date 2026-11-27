"""Overlapping separate subtitles: splitting rows into blocks and showing each block in its own box on the TV."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import subtitler as A  # noqa: E402
import ps5_direct as D  # noqa: E402
import ocr as O  # noqa: E402


def row_(txt, y, h=0.030):
    return O.OcrLine(txt, 0.4, y, 0.2, h)


class SplitBlocksTest(unittest.TestCase):
    def test_wrapped_rows_one_subtitle(self):
        b = O.split_into_blocks([row_("Look, you can have all the best intentions in the world,", 0.841),
                             row_("but at the end of the day it's dirty work.", 0.885)], 0.049)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0].line_count, 2)
        self.assertIn("world, but at", b[0].txt)

    def test_wide_gap_separate_subtitle(self):
        b = O.split_into_blocks([row_("Stay the fuck there!", 0.780), row_("Not cool, come on man, quit it!", 0.833),
                             row_("Call a friend or something!", 0.886)], 0.049)
        self.assertEqual([x.txt for x in b], ["Stay the fuck there!", "Not cool, come on man, quit it!",
                                                "Call a friend or something!"])
        self.assertAlmostEqual(b[1].bbox[1], 0.833)

    def test_mixed(self):
        b = O.split_into_blocks([row_("Hands! Hands!", 0.789), row_("442, subject is running", 0.842),
                             row_("away.", 0.886)], 0.049)
        self.assertEqual([x.line_count for x in b], [1, 2])

    def test_no_split_without_threshold(self):
        self.assertEqual(len(O.split_into_blocks([row_("A.", 0.78), row_("B.", 0.833)], 0.0)), 1)

    def test_glossary_setting_and_disable(self):
        glossary, _ = A.make_parts({**A.DEFAULTS, "game": "GTA 6"})
        cfg = {**A.DEFAULTS, "region": {"left": 0.1, "top": 0.7, "width": 0.8, "height": 0.3}}
        self.assertGreater(A.make_layout(cfg, glossary).block_step, 0.04)
        self.assertEqual(A.make_layout({**cfg, "split_blocks": False}, glossary).block_step, 0.0)


def reading(*blocks_):
    bl = [O.Block(m, (0.4, y, 0.2, 0.03), 1) for m, y in blocks_]
    return O.Reading(txt=" ".join(m for m, _ in blocks_), bbox=O.bounding([row_(m, y) for m, y in blocks_]) if bl
                   else None, line_count=len(bl), blocks_=bl, prep="raw")


class Done(BaseException):
    pass


class BlockModeTest(unittest.TestCase):
    """run() in separate subtitle mode: every block in its own box, a shifting block shifts on the TV too, texts do not
    merge."""

    def run_scenario(self, scenario, **setting):
        frames = list(scenario)
        received = []

        class Track:
            def __init__(self, *a, **k):
                self.status, self.err = "connected", ""
                self.audio = k.get("audio")

            def start(self): pass
            def stop(self): pass

            def latest_frame(self):
                if not frames:
                    raise Done()
                if self.audio is not None:  # stereo 48 kHz audio flowing in real time (as much as has passed since the last call)
                    time.sleep(0.01)
                    now_ = time.monotonic()
                    n = max(1, min(4800, int((now_ - getattr(self, "last", now_ - 0.01)) * 48000)))
                    self.last = now_
                    self.audio.add(bytes.fromhex("e803e803") * n, n, 2, 48000)
                return Image.new("RGB", (640, 360))

        async def read(self_, img, h=1080):
            return frames.pop(0)

        class TV:
            def __init__(self, *a, **k): pass
            def send(self, m): received.append(m); return True
            def close(self): pass

        class Translator:
            def __init__(self, *a, **k): pass
            def warmup(self): pass
            def save(self): pass
            def translate(self, t): return "TR:" + t, "fake"

        cfg = A.load_config()
        cfg.update(connection="direct", ps5_ip="192.0.2.1", tv_ip="192.0.2.2", interval_ms=0, save_frames=False,
                   show_objectives=False, game="GTA 6", split_blocks=True, block_step=None, engine="local",
                   google_only=False, speaker_colors=True, voice_recognition=False)
        cfg.update(setting)
        self.last_clips = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(D, "PS5Stream", Track), mock.patch.object(D, "is_paired", return_value=True), \
                mock.patch.object(O.Reader, "read", read), mock.patch.object(O, "text_in_place", return_value=False), \
                mock.patch.object(A, "TvLink", TV), mock.patch.object(A, "Translator", Translator), \
                mock.patch.object(A, "make_ocr_engine"), mock.patch.object(A, "_write_log"), \
                mock.patch.object(A, "_log_hook", lambda s: None), mock.patch.object(A.paths, "LINES_DIR", Path(d)),                 mock.patch.object(A.paths, "VOICES", Path(d) / "voices"), mock.patch.object(A.paths, "CLIPS", Path(d) / "voices" / "clips"):
            try:
                asyncio.run(A.run(cfg))
            except Done:
                pass
            entries = Path(d) / "voices" / "clips.jsonl"
            if entries.exists():
                import json

                for line_count in entries.read_text(encoding="utf-8").splitlines():
                    k = json.loads(line_count)
                    k["wav_boy"] = (Path(d) / "voices" / "clips" / k["file"]).stat().st_size
                    self.last_clips.append(k)
        return [m for m in received if m["type"] == "show"], [m for m in received if m["type"] == "clear"]

    def test_npc_line_own_box_and_shift(self):
        a, b = "Stay there, don't move!", "Hands up! Right now!"
        shown, _ = self.run_scenario([reading((a, 0.885))] * 5 + [reading((a, 0.833), (b, 0.885))] * 5
                                  + [reading()] * 120)
        a_msgs = [m for m in shown if m["text"] == "TR:" + a]
        b_msgs = [m for m in shown if m["text"] == "TR:" + b]
        self.assertTrue(a_msgs and b_msgs)
        self.assertNotEqual(a_msgs[0]["id"], b_msgs[0]["id"])                 # separate boxes
        self.assertEqual({m["id"] for m in a_msgs}, {a_msgs[0]["id"]})
        self.assertAlmostEqual(a_msgs[0]["box"][1], 0.885)
        self.assertTrue(any(abs(m["box"][1] - 0.833) < 1e-6 for m in a_msgs))  # shifted up together with the game
        self.assertAlmostEqual(b_msgs[0]["box"][1], 0.885)
        self.assertFalse(any(a in m["text"] and b in m["text"] for m in shown))  # texts did not merge
        self.assertFalse(b_msgs[0]["new"])  # no dimmed old line while another box is above it

    def test_merged_near_threshold_is_split(self):
        """2026-11-24: the gap between two separate subtitles was measured 0.0475 in one frame (threshold 0.049) and they were
        taken for one block; the lower line was translated a second time as the "continuation" in the upper box. If
        a part is already tracked, it must be split."""
        a, b = "Hey! You! I got a bone to pick with you!", "Who scraped you off the highway?"
        joined = O.split_into_blocks([row_(a, 0.833), row_(b, 0.8805)], 0.049)
        self.assertEqual(len(joined), 1)
        merged = O.Reading(txt=joined[0].txt, bbox=joined[0].bbox, line_count=2, blocks_=joined, prep="raw")
        shown, _ = self.run_scenario([reading((a, 0.885))] * 5 + [reading((a, 0.833), (b, 0.885))] * 5 + [merged] * 5
                                  + [reading()] * 120)
        b_msgs = [m for m in shown if b in m["text"]]
        self.assertTrue(b_msgs)
        self.assertEqual({m["id"] for m in b_msgs}, {b_msgs[0]["id"]})  # the lower line in a single box
        self.assertFalse(any(a in m["text"] and b in m["text"] for m in shown))
        self.assertEqual(shown[0]["fade_ms"], A.DEFAULTS["fade_ms"])  # the fade time goes to the TV

    def test_replacing_line_same_box(self):
        a, b = "We gotta get out of here.", "Come on, let's go!"
        shown, _ = self.run_scenario([reading((a, 0.885))] * 5 + [reading((b, 0.885))] * 5 + [reading()] * 120)
        ids = {m["id"] for m in shown}
        self.assertEqual(len(ids), 1)  # a line that follows in the same place uses the same box
        b_msgs = [m for m in shown if m["text"] == "TR:" + b]
        self.assertTrue(b_msgs[0]["new"])  # the previous line stays dimmed on top (nothing above it)


def fixture_pack():
    """The few labeled lines these tests need (the real packs are not in this repo: a private one, pc/packs)."""
    from pack import Pack

    lines = {"Easy, Lu. Slow down.": "Yavaş, Lu. Yavaşla.", "Goddamn piece of shit!": "Lanet pislik!",
             "Remind me who set us up with Moses again?": "Bizi Moses'la kimin tanıştırdığını hatırlat?"}
    speakers = {"Easy, Lu. Slow down.": "Jason", "Goddamn piece of shit!": "pedestrian",
                "Remind me who set us up with Moses again?": "Jason"}
    return Pack("Grand Theft Auto VI", lines, speakers)


class PackFixture:
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(A, "make_pack", lambda cfg, glossary: fixture_pack())
        patcher.start()
        self.addCleanup(patcher.stop)


class SpeakerColorTest(PackFixture, BlockModeTest):
    """The pack's speaker label goes to the TV as a color (with the GTA VI glossary and a small labeled pack)."""
    test_npc_line_own_box_and_shift = None
    test_replacing_line_same_box = None
    test_merged_near_threshold_is_split = None

    def test_colors(self):
        color = A.make_parts({**A.DEFAULTS, "game": "GTA 6"})[0].colors
        lines_ = {"Easy, Lu. Slow down.": color["Jason"], "Goddamn piece of shit!": color["pedestrian"],
                    "Remind me who set us up with Moses again?": color["Jason"]}
        scenario = []
        for en in lines_:
            scenario += [reading((en, 0.885))] * 5
        shown, _ = self.run_scenario(scenario + [reading()] * 100)
        for en, expected in lines_.items():
            m = [x for x in shown if x["text"] == "TR:" + en]
            self.assertTrue(m, en)
            self.assertEqual(m[0].get("color"), expected, en)

    def test_unknown_white_and_disable(self):
        shown, _ = self.run_scenario([reading(("Some brand new line nobody has seen.", 0.885))] * 5 + [reading()] * 100)
        self.assertNotIn("color", shown[0])
        shown, _ = self.run_scenario([reading(("Easy, Lu. Slow down.", 0.885))] * 5 + [reading()] * 100,
                                  speaker_colors=False)
        self.assertNotIn("color", shown[0])


class AudioClipTest(PackFixture, BlockModeTest):
    """In direct mode, when a new line comes its audio (from shortly before it is first seen to after) is saved as WAV."""
    test_npc_line_own_box_and_shift = None
    test_replacing_line_same_box = None
    test_merged_near_threshold_is_split = None

    def test_line_audio_recorded(self):
        import audio as S

        en = "Easy, Lu. Slow down."
        with mock.patch.object(A, "CLIP_BEFORE", 0.05), mock.patch.object(A, "CLIP_AFTER", 0.3):
            self.run_scenario([reading((en, 0.885))] * 5 + [reading()] * 150, voice_recognition=True)
        self.assertEqual(len(self.last_clips), 1)
        k = self.last_clips[0]
        self.assertEqual((k["en"], k["speaker"]), (en, "Jason"))  # speaker from the pack
        self.assertGreater(k["loudness"], 500)
        self.assertGreater(k["wav_boy"], 44 + 0.3 * S.TARGET_RATE * 2 * 0.8)  # ~0,35 sn mono 16 kHz

    def test_voice_color_and_learning(self):
        import audio as S
        try:
            from test_audio import FakeExtractor
        except ImportError:
            from tests.test_audio import FakeExtractor

        color = A.make_parts({**A.DEFAULTS, "game": "GTA 6"})[0].colors
        new, known = "Some brand new line nobody has seen.", "Easy, Lu. Slow down."
        with tempfile.TemporaryDirectory() as d:
            t = S.SpeakerRecognizer(Path(d), "Grand Theft Auto VI", FakeExtractor())
            v = t.vector(bytes.fromhex("e803") * 32000)  # vector of the fake game audio (constant amplitude)
            for _ in range(S.MIN_PROFILE_SAMPLES):
                t.learn("Lucia", v)
            with mock.patch.object(S.SpeakerRecognizer, "open", return_value=t), \
                    mock.patch.object(A, "CLIP_BEFORE", 0.05), mock.patch.object(A, "CLIP_AFTER", 0.4), \
                    mock.patch.object(A, "RECOGNIZE_AFTER", 0.3), mock.patch.object(S, "MIN_SECONDS", 0.2):
                shown, _ = self.run_scenario([reading((new, 0.885))] * 60 + [reading()] * 10
                                          + [reading((known, 0.885))] * 60 + [reading()] * 100, voice_recognition=True)
        m = [x for x in shown if x["text"] == "TR:" + new]
        self.assertNotIn("color", m[0])                                      # white first
        self.assertTrue(any(x.get("color") == color["Lucia"] for x in m[1:]))  # Lucia's color once the voice is recognized
        self.assertEqual(t.profiles["Jason"]["n"], 1)                       # a line known from the pack taught the profile
        self.assertTrue(any(k.get("voice_speaker") == "Lucia" and k["en"] == new for k in self.last_clips))

    def test_second_chance_on_full_clip(self):
        """Undecided on the short window (too little speech): decided again on the full clip, the color comes late."""
        import audio as S
        try:
            from test_audio import FakeExtractor
        except ImportError:
            from tests.test_audio import FakeExtractor

        color = A.make_parts({**A.DEFAULTS, "game": "GTA 6"})[0].colors
        new = "Another line nobody has seen before."
        logged = []
        with tempfile.TemporaryDirectory() as d:
            t = S.SpeakerRecognizer(Path(d), "Grand Theft Auto VI", FakeExtractor())
            v = t.vector(bytes.fromhex("e803") * 32000)
            for _ in range(S.MIN_PROFILE_SAMPLES):
                t.learn("Jason", v)
            with mock.patch.object(S.SpeakerRecognizer, "open", return_value=t), \
                    mock.patch.object(A, "CLIP_BEFORE", 0.05), mock.patch.object(A, "CLIP_AFTER", 0.5), \
                    mock.patch.object(A, "RECOGNIZE_AFTER", 0.1), mock.patch.object(S, "MIN_SECONDS", 0.4), \
                    mock.patch.object(A, "log", logged.append):
                shown, _ = self.run_scenario([reading((new, 0.885))] * 90 + [reading()] * 60, voice_recognition=True)
        m = [x for x in shown if x["text"] == "TR:" + new]
        self.assertNotIn("color", m[0])
        self.assertTrue(any(x.get("color") == color["Jason"] for x in m[1:]))
        self.assertTrue(any("[ses" in s and " geç]" in s and "Jason" in s for s in logged))

    def test_off_does_not_record(self):
        self.run_scenario([reading(("Easy, Lu. Slow down.", 0.885))] * 5 + [reading()] * 60)
        self.assertEqual(self.last_clips, [])


if __name__ == "__main__":
    unittest.main()
