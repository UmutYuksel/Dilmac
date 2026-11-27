"""Game pack: lookup (OCR tolerant), punctuation, similar examples, the tool (pending/add)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import translation as C  # noqa: E402
import textutil as M  # noqa: E402
import pack_tool as PA  # noqa: E402
from pack import Pack  # noqa: E402

PACK_LINES = {
    "A lot of fucking guys here, Bill.": "Burada bir sürü adam var lan, Bill.",
    "Don't get cute.": "Akıllılık etme.",
    "Meet me at the trailer.": "Karavanda buluşalım.",
    "Go to the docks": "Rıhtıma git",
    "Okay.": "Tamam.",
}


class PackTest(unittest.TestCase):
    def setUp(self):
        self.p = Pack("Grand Theft Auto VI", PACK_LINES)

    def test_exact_match_and_punctuation(self):
        self.assertEqual(self.p.lookup("Meet me at the trailer."), "Karavanda buluşalım.")
        self.assertEqual(self.p.lookup("meet me at the trailer"), "Karavanda buluşalım.")  # OCR missed the dot
        self.assertEqual(self.p.lookup("Go to the docks!"), "Rıhtıma git!")

    def test_ocr_error(self):
        self.assertEqual(self.p.lookup("A lot of fucking guys here, Bills ̄"), "Burada bir sürü adam var lan, Bill.")
        self.assertEqual(self.p.lookup("A Iot of fucking guys here, BiII."), "Burada bir sürü adam var lan, Bill.")

    def test_short_different_line_not_found(self):
        self.assertIsNone(self.p.lookup("Okey"))
        self.assertIsNone(self.p.lookup("A lot of cops here, Bill."))

    def test_speaker(self):
        p = Pack("x", PACK_LINES, {"A lot of fucking guys here, Bill.": "Dunk", "Meet me at the trailer.": "Lucia",
                                  "Satır pakette yok.": "Jason"})
        self.assertEqual(p.speaker("A Iot of fucking guys here, BiII."), "Dunk")  # found despite an OCR error
        self.assertEqual(p.speaker("meet me at the trailer"), "Lucia")
        self.assertIsNone(p.speaker("Don't get cute."))  # unlabeled line
        self.assertIsNone(p.speaker("Satır pakette yok."))
        p.add({}, {"Don't get cute.": "pedestrian"})  # a file with labels only, for a line from an earlier file
        self.assertEqual(p.speaker("Don't get cute."), "pedestrian")
        p.add({}, {"Don't get cute.": "Jason"})  # the one that came first wins
        self.assertEqual(p.speaker("Don't get cute."), "pedestrian")

    def test_speaker_by_sentence(self):
        """The game showed two sentences in one subtitle, the pack has them separately: looked up sentence by sentence (for
        audio clips)."""
        p = Pack("x", PACK_LINES, {"Meet me at the trailer.": "Lucia"})
        self.assertIsNone(p.speaker("Don't get cute. Meet me at the trailer."))
        self.assertEqual(p.speaker_by_sentence("Don't get cute. Meet me at the trailer."), "Lucia")
        self.assertEqual(p.speaker_by_sentence("LUCIA: Meet me at the trailer."), "Lucia")
        self.assertIsNone(p.speaker_by_sentence("Nothing anybody knows about at all."))

    def test_similar(self):
        samples = self.p.similar("Don't get cute with me.")
        self.assertEqual(samples[0], ("Don't get cute.", "Akıllılık etme."))
        self.assertEqual(self.p.similar("Don't get cute."), [])  # the same line is not its own example


class TranslatorPackTest(unittest.TestCase):
    def test_pack_first_and_examples_in_prompt(self):
        reqs = []

        class Fake(C.Translator):
            def _local(self, prompt, n):
                reqs.append(prompt)
                return ["Bana akıllılık etme."] * n

        c = Fake({"engine": "local"}, None, log=lambda *_: None, cache_data=None,
                  pack_=Pack("Grand Theft Auto VI", PACK_LINES))
        self.assertEqual(c.translate("Don't get cute."), ("Akıllılık etme.", "pack"))
        self.assertEqual(reqs, [])
        self.assertEqual(c.translate("Don't get cute with me.")[0], "Bana akıllılık etme.")
        self.assertIn("EN: Don't get cute.\nTR: Akıllılık etme.", reqs[0])


class ToolTest(unittest.TestCase):
    def test_pending_merge_and_skip_pack(self):
        lexicon = M.Lexicon(["Dunk"])
        lines_ = [
            {"t": 0, "track": "speech", "en": "Meet me at the trailer. Hey, Dunk, what's up?"},
            {"t": 1, "track": "speech", "en": "Hey, Dunk, what's up?"},
            {"t": 2, "track": "speech", "en": "xqzv bnmkl prtw"},
            {"t": 100, "track": "objective", "en": "Find Dunk"},
        ]
        scenes = PA.pending_lines(lines_, Pack("x", PACK_LINES), lexicon)
        self.assertEqual(scenes, [[{"en": "Hey, Dunk, what's up?", "track": "speech"}],
                                    [{"en": "Find Dunk", "track": "objective"}]])

    def test_add(self):
        with tempfile.TemporaryDirectory() as d:
            old_data = PA.paths.DATA
            PA.paths.DATA = Path(d)
            try:
                f = Path(d) / "yeni.json"
                f.write_text(json.dumps({"Find Dunk": "Dunk'ı bul"}), encoding="utf-8")
                dest = PA.add(f, "Grand Theft Auto VI", ["GTA 6"])
                data = json.loads(dest.read_text(encoding="utf-8"))
            finally:
                PA.paths.DATA = old_data
        self.assertEqual(dest.name, "gta6.json")
        self.assertEqual(data["lines"], {"Find Dunk": "Dunk'ı bul"})
        self.assertEqual(data["game"], "Grand Theft Auto VI")


if __name__ == "__main__":
    unittest.main()
