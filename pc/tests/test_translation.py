import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import translation as C  # noqa: E402


class ReplyTest(unittest.TestCase):
    def test_sequence(self):
        self.assertEqual(C.reply_seq('["Selam.", "Naber?"]', 2), ["Selam.", "Naber?"])

    def test_code_fence_and_object(self):
        self.assertEqual(C.reply_seq('```json\n{"translations": ["Selam."]}\n```', 1), ["Selam."])

    def test_single_text(self):
        self.assertEqual(C.reply_seq("Selam.", 1), ["Selam."])

    def test_error_on_count_mismatch(self):
        with self.assertRaises(ValueError):
            C.reply_seq('["a"]', 2)


class GlossaryTest(unittest.TestCase):
    def setUp(self):
        self.s = C.Glossary.load("GTA 6")

    def test_found_by_alias(self):
        self.assertIsNotNone(self.s)
        self.assertEqual(self.s.game_title, "Grand Theft Auto VI")
        self.assertIs(C.Glossary.load("Bilinmeyen Oyun"), None)

    def test_relevant(self):
        names, term = self.s.relevant("Dude, Cal Hampton is waiting in Vice City.")
        self.assertIn("Cal Hampton", names)
        self.assertNotIn("Cal", names)
        self.assertIn("Vice City", names)
        self.assertEqual(term, {"dude": "ahbap"})

    def test_ready_line(self):
        self.assertEqual(self.s.ready("Wasted"), "ÖLDÜN")
        self.assertEqual(self.s.ready("Lose the cops."), "Polisleri ekte.")
        self.assertEqual(self.s.ready("Lose the cops"), "Polisleri ekte")


class FakeTranslator(C.Translator):
    def __init__(self, answer):
        super().__init__({"engine": "local", "game": "Grand Theft Auto VI"}, C.Glossary.load("GTA 6"),
                         log=lambda *_: None, cache_data=None)
        self.answer = answer
        self.reqs = []

    def _local(self, prompt, n):
        self.reqs.append(prompt)
        seq = json.loads(prompt.rsplit("Translate:\n", 1)[1])
        return [self.answer.get(c, "-") for c in seq][:n]


class TranslatorTest(unittest.TestCase):
    def test_cache_per_sentence(self):
        c = FakeTranslator({"Look, it's my fault!": "Bak, benim hatam!", "I should have called.": "Aramalıydım."})
        tr, source = c.translate("Look, it's my fault!")
        self.assertEqual((tr, source), ("Bak, benim hatam!", "Yerel"))
        tr, _ = c.translate("Look, it's my fault! I should have called.")
        self.assertEqual(tr, "Bak, benim hatam! Aramalıydım.")
        # The second request carried only the new sentence
        self.assertTrue(c.reqs[1].endswith('["I should have called."]'))

    def test_speaker_name_kept_and_listed(self):
        c = FakeTranslator({"Where's Lucia?": "Lucia nerede?", "Ask Jason.": "Jason'a sor."})
        tr, _ = c.translate("JASON: Where's Lucia?")
        self.assertEqual(tr, "JASON: Lucia nerede?")
        self.assertIn("Speaker: JASON", c.reqs[0])
        self.assertIn("Lucia", c.reqs[0].split("Keep exactly as written (names): ")[1].split("\n")[0])
        c.translate("Ask Jason.")
        self.assertIn("Jason", c.reqs[1].split("Keep exactly as written (names): ")[1].split("\n")[0])

    def test_glossary_line_skips_model(self):
        c = FakeTranslator({})
        self.assertEqual(c.translate("WASTED"), ("ÖLDÜN", "sözlük"))
        self.assertEqual(c.reqs, [])

    def test_junk_line(self):
        c = FakeTranslator({})
        self.assertEqual(c.translate("xkq zzrt vbn")[0], "-")

    def test_nickname_in_prompt(self):
        c = FakeTranslator({"You okay, Lu?": "İyi misin Lu?"})
        self.assertEqual(c.translate("You okay, Lu?")[0], "İyi misin Lu?")
        self.assertIn("Lu = nickname of Lucia", c.reqs[0])
        c.translate("Look out below.")
        self.assertNotIn("nickname", c.reqs[1])  # "lu" inside a word does not count as the nickname

    def test_short_sentences_sent_as_one(self):
        c = FakeTranslator({"Girl. Stop. Rent is coming.": "Kızım, dur. Kira geliyor."})
        self.assertEqual(c.translate("Girl. Stop. Rent is coming.")[0], "Kızım, dur. Kira geliyor.")
        self.assertTrue(c.reqs[0].endswith('["Girl. Stop. Rent is coming."]'))

    def test_context_in_prompt(self):
        c = FakeTranslator({"Hi.": "Selam.", "and then?": "peki sonra?"})
        c.translate("Hi.")
        c.translate("and then?")
        self.assertIn("EN: Hi.\nTR: Selam.", c.reqs[1])


if __name__ == "__main__":
    unittest.main()
