import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import textutil as M  # noqa: E402

LEXICON = M.Lexicon(["Lucia", "Jason", "Vice City", "Leonida"])


class KeyTest(unittest.TestCase):
    def test_turkish_letters_kept(self):
        self.assertEqual(M.text_key("Güç Tasarrufu Görünümü"), "güçtasarrufugörünümü")

    def test_punctuation_and_quotes(self):
        self.assertEqual(M.text_key("I’m here!"), M.text_key("I'm here"))

    def test_ocr_similarity(self):
        self.assertGreaterEqual(M.similarity("Well, take care of yourself.", "Well, take care of yourse1f."), 0.9)
        self.assertLess(M.similarity("Well, take care of yourself.", "Hmm, who's this?"), 0.5)


class LexiconTest(unittest.TestCase):
    def test_turkish_menu(self):
        self.assertTrue(LEXICON.is_turkish("Güç Tasarrufu Görünümü"))
        self.assertTrue(LEXICON.is_turkish("Guq Tasarrufu Gorunum"))  # as English OCR reads it
        self.assertTrue(LEXICON.is_turkish("Diger Baglantllar"))
        self.assertFalse(LEXICON.is_turkish("Can you please back up out my grill? Damn."))
        self.assertFalse(LEXICON.is_turkish("Lucia, get in the car."))
        self.assertTrue(LEXICON.is_turkish("Lutfen bekleyin"))
        self.assertTrue(LEXICON.is_turkish("Arkadas Etkinligi Ana Ekran"))
        self.assertTrue(LEXICON.is_turkish("Iptal"))
        self.assertTrue(LEXICON.is_turkish("Arkadas Etkinlioi Grand Theft Auto VI Konsol Depolama Alant BEACH"))
        self.assertFalse(LEXICON.is_turkish("Tell Mendoza Ramirez to call me."))
        self.assertFalse(LEXICON.is_turkish("Shit, neiurcanii you evrl think about how much noise we can make then"))
        # Garbled English must not count as Turkish
        self.assertFalse(LEXICON.is_turkish("Hrnrn, whds this?"))
        self.assertFalse(LEXICON.is_turkish("000 wee, J sure waft if}} we hgye our bunker ready."))

    def test_garbled(self):
        self.assertTrue(LEXICON.is_garbled("Ooo wee, I sure can't wait till veghayectaervffeoacgv"))
        self.assertTrue(LEXICON.is_garbled("06 wei sure canlt bunker ragy"))
        self.assertTrue(LEXICON.is_garbled("f fe & ie want it."))
        self.assertFalse(LEXICON.is_garbled("Ooo wee, I sure can't wait till we have our bunker ready."))
        self.assertFalse(LEXICON.is_garbled("I ain't even bout to play with you."))

    def test_clean(self):
        self.assertTrue(LEXICON.is_clean("Hmm, who's this?"))
        self.assertTrue(LEXICON.is_clean("Jason, we gotta go."))
        self.assertFalse(LEXICON.is_clean("Hmm, whos1s this?"))

    def test_score_picks_better_reading(self):
        self.assertGreater(LEXICON.score("Well, take care of yourself."), LEXICON.score("Well, akecare ofelf."))


class SpeakerTest(unittest.TestCase):
    def test_uppercase_name(self):
        self.assertEqual(M.split_speaker("JASON: Let's go."), ("JASON", "Let's go."))

    def test_two_word_name(self):
        self.assertEqual(M.split_speaker("Cal Hampton: Hey."), ("Cal Hampton", "Hey."))

    def test_no_name(self):
        self.assertEqual(M.split_speaker("We need to talk."), (None, "We need to talk."))
        self.assertEqual(M.split_speaker("12:30 at the docks."), (None, "12:30 at the docks."))
        self.assertEqual(M.split_speaker("Listen to me, this is it: we go now."),
                         (None, "Listen to me, this is it: we go now."))


class SentenceTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(M.split_sentences("Look, it's my fault! I'm sorry. Okay?"),
                         ["Look, it's my fault!", "I'm sorry.", "Okay?"])

    def test_abbreviation(self):
        self.assertEqual(M.split_sentences("Talk to Mr. Hampton first."), ["Talk to Mr. Hampton first."])

    def test_ellipsis_lowercase(self):
        self.assertEqual(M.split_sentences("I was gonna... you know what, forget it."),
                         ["I was gonna... you know what, forget it."])

    def test_dialogue_dash(self):
        self.assertEqual(M.split_sentences("- Where are you? - Home."), ["- Where are you?", "- Home."])

    def test_punctuation(self):
        self.assertTrue(M.ends_with_punct('He said "go."'))
        self.assertFalse(M.ends_with_punct("and then we"))


class ChunkTest(unittest.TestCase):
    def test_short_sentences_merge(self):
        self.assertEqual(M.chunk_sentences(M.split_sentences("Girl. Stop. Rent is coming.")), ["Girl. Stop. Rent is coming."])

    def test_long_sentences_stay_separate(self):
        c = ["You don't wanna do this!", "I'm likable and shit!", "I don't know...",
             "Feel like you got more somewhere."]
        self.assertEqual(M.chunk_sentences(c), c)
        self.assertEqual(M.chunk_sentences(["Hey.", "It's me.", "Let's see what these new folks are up to..."]),
                         ["Hey. It's me.", "Let's see what these new folks are up to..."])

    def test_short_tail_joins_previous(self):
        self.assertEqual(M.chunk_sentences(["Get on the floor, right now!", "Now!"]), ["Get on the floor, right now! Now!"])

    def test_empty(self):
        self.assertEqual(M.chunk_sentences([]), [])


class NewPartTest(unittest.TestCase):
    def test_appended_below(self):
        self.assertEqual(M.new_part("Hey. It's me.", "Hey. It's me. Let's see what these new folks are up to..."),
                         "Let's see what these new folks are up to...")

    def test_top_line_shifts(self):
        # The game dropped the oldest, moved the previous line up and added a new one below.
        self.assertEqual(M.new_part("Shit. Act natural.", "Act natural. I am acting natural."),
                         "I am acting natural.")

    def test_ocr_difference_tolerated(self):
        self.assertEqual(M.new_part("Whoa, dude!", "Wh0a, dude! And don't get cute."), "And don't get cute.")

    def test_none_without_overlap(self):
        self.assertIsNone(M.new_part("Where's the doctor?", "You first. Where's the stuff?"))
        self.assertIsNone(M.new_part("A lot of guys here.", "A lot of guys here."))


if __name__ == "__main__":
    unittest.main()
