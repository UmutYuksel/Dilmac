"""Read preparations: synthetic GTA-style frames (white text + dark shadow). Tests needing Windows OCR are skipped
without the OCR package."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import textutil as M  # noqa: E402
import ocr as O  # noqa: E402

W, H = 1654, 301
FONT_PATH = Path(r"C:\Windows\Fonts\arialbd.ttf")


def frame(lines_, bright=False, btn=False):
    im = Image.new("RGB", (W, H), (35, 32, 45))
    d = ImageDraw.Draw(im)
    if bright:
        d.rectangle([0, 0, W, H], fill=(215, 225, 250))
        d.rectangle([600, 40, 1100, H], fill=(240, 238, 235))
    font = ImageFont.truetype(str(FONT_PATH), 34)
    y = 150
    for s in lines_:
        x = (W - d.textlength(s, font=font)) / 2
        for dx, dy in [(2, 2), (3, 3), (1, 2), (2, 1)]:
            d.text((x + dx, y + dy), s, font=font, fill=(0, 0, 0))
        d.text((x, y), s, font=font, fill=(245, 245, 245))
        y += 44
    if btn:
        d.text((W - 330, H - 40), "SELECT   BACK", font=font, fill=(250, 250, 250))
    return im


@unittest.skipUnless(FONT_PATH.exists(), "Arial Bold yok")
class SignatureTest(unittest.TestCase):
    def test_not_in_place_after_text_gone(self):
        two = O.signature(frame(["Ooo wee, I sure can't wait till we have", "our bunker ready."]))
        self.assertTrue(O.text_in_place(two, two))
        self.assertFalse(O.text_in_place(O.signature(frame([])), two))
        single = O.signature(frame(["Hmm, who's this?"]))
        self.assertFalse(O.text_in_place(O.signature(frame([])), single))

    def test_not_in_place_without_signature(self):
        self.assertFalse(O.text_in_place(O.signature(frame([])), O.signature(frame([]))))
        self.assertFalse(O.text_in_place(O.signature(frame([])), None))


class SplitTest(unittest.TestCase):
    """GTA VI layout: region (0.15, 0.75, 0.68, 0.22); boxes are given relative to the picture first, then converted."""

    def setUp(self):
        self.d = O.Layout(0.15, 0.75, 0.68, 0.22, center_tol=0.02, objective_y=0.925)

    def line_count(self, txt, x, y, w, h=0.029):
        bx, by, bw, bh = self.d.to_region((x, y, w, h))
        return O.OcrLine(txt, bx, by, bw, bh)

    def test_speech_objective_phone_split(self):
        a = O.classify([
            self.line_count("Girl. Stop. Rent is coming.", 0.624, 0.759, 0.125),          # phone (on the right)
            self.line_count("You know, you really didn't have to go this far, man.", 0.287, 0.885, 0.426),
            self.line_count("Go to the docks", 0.447, 0.950, 0.107, 0.0175),               # objective
        ], self.d)
        self.assertEqual(O.joined(a.speech), "You know, you really didn't have to go this far, man.")
        self.assertEqual(O.joined(a.objective), "Go to the docks")
        self.assertEqual([s.txt for s in a.dropped], ["Girl. Stop. Rent is coming."])

    def test_color_split_line_merges(self):
        a = O.classify([self.line_count("Pick up", 0.45, 0.949, 0.045, 0.02),
                    self.line_count("Dr. Bill", 0.498, 0.949, 0.052, 0.02)], self.d)
        self.assertEqual(O.joined(a.objective), "Pick up Dr. Bill")

    def test_adjacent_sign_dropped(self):
        a = O.classify([self.line_count("You, give me the money.", 0.399, 0.833, 0.204),
                    self.line_count("PlßWASSER", 0.629, 0.830, 0.066, 0.031),
                    self.line_count("Yeah, yeah, whatever you want!", 0.372, 0.886, 0.256)], self.d)
        self.assertEqual(O.joined(a.speech), "You, give me the money. Yeah, yeah, whatever you want!")
        k = O.bounding(a.speech)
        self.assertAlmostEqual(k[1], 0.833, places=3)
        self.assertAlmostEqual(k[1] + k[3], 0.915, places=3)

    def test_junk_on_both_sides_keeps_center(self):
        a = O.classify([self.line_count("ollision damage.", 0.10, 0.884, 0.10),
                    self.line_count("What's the plan?", 0.432, 0.884, 0.136),
                    self.line_count("GRANGER 36", 0.80, 0.884, 0.10)], self.d)
        self.assertEqual(O.joined(a.speech), "What's the plan?")

    def test_old_rule_without_layout(self):
        d = O.Layout()
        a = O.classify([O.OcrLine("SELECT BACK", 0.80, 0.9, 0.15, 0.1),
                    O.OcrLine("Hello there.", 0.40, 0.5, 0.2, 0.1)], d)
        self.assertEqual(O.joined(a.speech), "Hello there.")


def _engine():
    try:
        return O.ocr_engine()
    except Exception:
        return None


@unittest.skipUnless(FONT_PATH.exists() and _engine() is not None, "Windows İngilizce OCR yok")
class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.reader = O.Reader(_engine(), M.Lexicon(), 0)

    def read(self, im):
        return asyncio.run(self.reader.read(im, 1369))

    def test_dark_background_raw_enough(self):
        r = self.read(frame(["Ooo wee, I sure can't wait till we have", "our bunker ready."], btn=True))
        self.assertEqual(r.txt, "Ooo wee, I sure can't wait till we have our bunker ready.")
        self.assertEqual(r.prep, "raw")

    def test_button_text_filtered(self):
        self.assertEqual(self.read(frame([], btn=True)).txt, "")

    def test_white_prep_on_bright_background(self):
        r = self.read(frame(["Ooo wee, I sure can't wait till we have", "our bunker ready."], bright=True))
        self.assertEqual(r.prep, "white")
        self.assertIn("bunker", r.txt)


if __name__ == "__main__":
    unittest.main()
