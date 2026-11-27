import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import textutil as M  # noqa: E402
from stabilizer import Stabilizer  # noqa: E402

LEXICON = M.Lexicon(["Lucia", "Jason"])


class Clock:
    def __init__(self):
        self.ms = 0.0

    def __call__(self):
        return self.ms / 1000.0

    def advance(self, ms):
        self.ms += ms


class StabilizerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.d = Stabilizer(LEXICON, clock=self.clock)

    def frame(self, text, ms=100, unchanged=False):
        self.clock.advance(ms)
        return self.d.feed(text, unchanged)

    def test_clean_punctuated_sent_first_frame(self):
        k = self.frame("Hmm, who's this?")
        self.assertEqual((k.kind, k.txt), ("send", "Hmm, who's this?"))
        # Later reads of the same line are not sent again
        for t in ["Hmm, who's this?", "Hmm, whos this?", "Hmm, who's"]:
            self.assertEqual(self.frame(t).kind, "yok")

    def test_garbled_fade_in_not_sent(self):
        self.assertEqual(self.frame("Ooo wee, I sure can't wait till veghayectaervffeoacgv").kind, "yok")
        self.assertEqual(self.frame("oooweeisure940119aittillvoehaveourbunkerready").kind, "yok")
        k = self.frame("Ooo wee, I sure can't wait till we have our bunker ready.")
        self.assertEqual(k.kind, "send")
        self.assertIn("bunker ready", k.txt)

    def test_clean_line_without_punct_waits(self):
        self.assertEqual(self.frame("Hey, what are you", 150).kind, "yok")
        self.assertEqual(self.frame("Hey, what are you", 150).kind, "yok")
        self.assertEqual(self.frame("Hey, what are you", 150).kind, "send")

    def test_continuation_is_new_line(self):
        self.assertEqual(self.frame("Look, it's my fault!").kind, "send")
        k = self.frame("Look, it's my fault! I should have called you.")
        self.assertEqual(k.kind, "send")
        self.assertTrue(k.txt.endswith("called you."))

    def test_empty_region_clears(self):
        self.frame("Just gimme a second.")
        self.assertEqual(self.frame("", 300).kind, "yok")
        self.assertEqual(self.frame("", 300).kind, "yok")
        self.assertEqual(self.frame("", 300).kind, "clear")

    def test_holds_if_region_unchanged(self):
        self.frame("Just gimme a second.")
        for _ in range(10):
            self.assertEqual(self.frame("", 300, unchanged=True).kind, "yok")
        self.assertEqual(self.frame("", 1200, unchanged=True).kind, "clear")

    def test_returning_line_not_retranslated(self):
        self.frame("Hi. Yes, I'm here.")
        self.frame("", 900)
        self.assertIsNone(self.d.sent)
        k = self.frame("Hi. Yes, I'm here.", 300)
        self.assertEqual((k.kind, k.txt), ("back", "Hi. Yes, I'm here."))

    def test_unnamed_reread_is_same_line(self):
        self.assertEqual(self.frame("JASON: Let's go, now.").kind, "send")
        self.assertEqual(self.frame("Let's go, now.").kind, "yok")

    def test_correction(self):
        # The line went out with one word misread; when the right one comes repeatedly it is corrected once.
        kinds = [self.frame("Take the eedboat to the docks.", 150).kind for _ in range(3)]
        self.assertEqual(kinds, ["yok", "yok", "send"])
        self.assertEqual(self.frame("Take the speedboat to the docks.").kind, "yok")
        k = self.frame("Take the speedboat to the docks.")
        self.assertEqual((k.kind, k.txt), ("correct", "Take the speedboat to the docks."))
        self.assertEqual(self.frame("Take the speedboat to the docks.").kind, "yok")

    def test_failed_translation_retried_after_a_second(self):
        self.frame("Look, it's my fault!")
        self.d.forget()
        self.assertEqual(self.frame("Look, it's my fault!").kind, "yok")
        self.assertEqual(self.frame("Look, it's my fault!", 1000).kind, "send")


if __name__ == "__main__":
    unittest.main()
