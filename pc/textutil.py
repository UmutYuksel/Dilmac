"""Pure (side-effect free) helpers for the read subtitle text.

The cache key, the English / Turkish / garbled decisions, splitting off the speaker name, splitting into sentences
and an OCR-tolerant similarity live here. No screen capture or network; tests in tests/test_textutil.py.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

# ---------------------------------------------------------------- basic form

_QUOTE = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'", "“": '"', "”": '"', "„": '"'})


def normalize_text(text: str) -> str:
    """NFKC, straight quotes, single spaces."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE)
    return re.sub(r"\s+", " ", text).strip()


def text_key(text: str) -> str:
    """Cache key: lower case, letters and digits only. Turkish letters are kept (the old version dropped them)."""
    return re.sub(r"[\W_]+", "", normalize_text(text).casefold())


# Letters OCR confuses with each other are folded into one form (only for similarity, not used in the key).
_OCR_MAP = [("rn", "m"), ("vv", "w"), ("cl", "d")]
_OCR_CHARS = str.maketrans({"l": "i", "1": "i", "|": "i", "j": "i", "ı": "i", "0": "o", "5": "s", "8": "b"})


def ocr_key(text: str) -> str:
    k = re.sub(r"[^\w|]+", "", normalize_text(text).casefold())
    for a, b in _OCR_MAP:
        k = k.replace(a, b)
    return re.sub(r"[\W_]+", "", k.translate(_OCR_CHARS))


def similarity(a: str, b: str) -> float:
    ka, kb = ocr_key(a), ocr_key(b)
    if not ka or not kb:
        return 1.0 if ka == kb else 0.0
    return difflib.SequenceMatcher(None, ka, kb, autojunk=False).ratio()


def coverage(short: str, long_vec: str) -> float:
    """How much of short's letters (in order) are inside long: for a partial read or a continuation line."""
    ka, k_long = ocr_key(short), ocr_key(long_vec)
    if not ka:
        return 0.0
    blocks = difflib.SequenceMatcher(None, ka, k_long, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(ka)


# ---------------------------------------------------------------- words

_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*", re.UNICODE)
# 1-2 letter words that occur on their own in English; other short pieces are OCR junk.
SHORT_WORDS = {
    "a", "i", "o", "ah", "am", "an", "as", "at", "aw", "ay", "be", "by", "do", "eh", "go", "ha", "he", "hi", "hm",
    "if", "in", "is", "it", "me", "mm", "my", "no", "of", "oh", "ok", "on", "or", "ow", "so", "to", "uh", "um", "up",
    "us", "we", "ya", "yo", "ye", "em", "mr", "ms", "dr", "st", "tv", "id", "ad", "ex", "ox", "pa", "ma",
}
# Colloquial words and slang missing from the dictionary but common in game dialogue (counted as unknown, they held
# lines back).
SLANG = {
    "pssp", "scuse", "ey", "bro", "asswipe", "meds", "doofus", "oughta", "dumbass", "ahh", "uhh", "hmm", "mmm",
    "shh", "aww", "argh", "ugh", "whoa", "jeez", "gonna", "wanna", "gotta", "kinda", "sorta", "outta", "lemme",
    "gimme", "dunno", "nah", "yep", "nope", "sup", "finna", "tryna", "cuz", "lotta", "hafta", "homie", "homies",
    "y'know", "y'all", "c'mon", "ain't", "em", "ya", "yo", "da", "mf", "motherfucker", "motherfuckers", "badass",
    "bullshit", "dipshit", "jackass", "asshole", "assholes", "fuckers", "fucker", "dickhead", "shitty",
}
# Marks that never appear in English subtitles, letter-digit mixes, capitals in the middle of a word.
_JUNK = re.compile(r"[@#$%^&*_=|\\{}<>~]|[a-zA-Z]\d|\d[a-zA-Z]|[a-z][A-Z]")
_TURKISH_CHARS = re.compile(r"[ğĞışŞİ]")


def words_(text: str) -> list[str]:
    return _WORD.findall(normalize_text(text))


class Lexicon:
    """English dictionary (pyspellchecker) + names known for the game."""

    def __init__(self, extras: list[str] | None = None):
        from spellchecker import SpellChecker

        self._sc = SpellChecker(language="en", distance=1)
        self._extra: set[str] = set()
        self.add(extras or [])

    def add(self, names) -> None:
        for name in names:
            for w in words_(name):
                self._extra.add(w.casefold())

    def is_known(self, w: str) -> bool:
        k = w.casefold()
        if k in self._extra:
            return True
        if len(k) <= 2:
            return k in SHORT_WORDS
        if k in self._sc or k in SLANG:
            return True
        # Street speech: "fuckin'", "holdin'", "somethin'" (the trailing apostrophe is not part of the word)
        if len(k) >= 4 and k.endswith("in") and k + "g" in self._sc:
            return True
        # Possessive and contraction endings: "Jason's", "Lucia'll", "bunker's"
        base = re.sub(r"'(s|ll|re|ve|d|m|t)$", "", k)
        return base != k and (base in self._extra or base in self._sc)

    def tally(self, text: str) -> tuple[int, int]:
        """(known words, all words)."""
        ws = words_(text)
        return sum(1 for w in ws if self.is_known(w)), len(ws)

    def ratio_(self, text: str) -> float:
        known, total = self.tally(text)
        return known / total if total else 0.0

    def score(self, text: str) -> float:
        """How much a read looks like 'real English'; to pick the best of a few reads."""
        known, total = self.tally(text)
        junk = junk_count(text)
        return known - 1.5 * (total - known) - 2 * junk + 0.001 * len(text)

    def is_turkish(self, text: str) -> bool:
        """Is the text on screen already Turkish (PS5 menu, notification): then it must not go to the translator.
        English OCR reads Turkish letters plainly ("Güç" -> "Guq"); so the check is whether most words are not in
        the dictionary but look like words (plenty of vowels). Garbled English ("Hrnrn, whds this?") is made of
        vowelless pieces and is not taken for Turkish."""
        if _TURKISH_CHARS.search(text):
            return True
        ws = [w for w in words_(text) if len(w) >= 3]
        if not ws:
            return False
        tr = sum(1 for w in ws if _fold(w) in TURKISH_WORDS)
        if len(ws) == 1:
            return tr == 1  # "Iptal", "Ayarlar"
        known = sum(1 for w in ws if self.is_known(w) and _fold(w) not in TURKISH_WORDS)
        foreign = sum(1 for w in ws if not self.is_known(w) and (_fold(w) in TURKISH_WORDS or _word_like(w)))
        foreign = max(foreign, tr)
        return foreign >= 2 and foreign >= known and (tr >= 1 or known / len(ws) < 0.34)

    def is_garbled(self, text: str) -> bool:
        """Does it look like OCR junk (an in-between state read while the text fades in/out)."""
        ws = words_(text)
        if not ws:
            return True
        if junk_count(text):
            return True
        unknown = [w for w in ws if not self.is_known(w)]
        # An unknown word starting with a capital is mostly a name; on its own it does not count as garbled.
        suspicious = [w for w in unknown if not (w[0].isupper() and len(w) >= 3 and w[1:].islower())]
        # A merged/made-up long string ("veghayectaervffeoacgv") is junk on its own.
        if any(len(w) >= 12 for w in suspicious):
            return True
        return len(suspicious) >= 2 or len(suspicious) / len(ws) > 0.25

    def is_clean(self, text: str) -> bool:
        """Every word is in the dictionary or looks like a name (capitalized); no junk marks."""
        ws = words_(text)
        if not ws or junk_count(text):
            return False
        return all(self.is_known(w) or (w[0].isupper() and len(w) >= 3 and w[1:].islower()) for w in ws)


_VOWELS = set("aeıioöuüAEIİOÖUÜ")
_FOLD = str.maketrans("çğıöşüâî", "cgiosuai")

# Words common in the PS5's Turkish interface and notifications (Turkish letters flattened, as OCR reads them).
TURKISH_WORDS = {
    "iptal", "tamam", "ayarlar", "ayar", "geri", "sec", "secin", "oyna", "oyun", "oyunu", "oyunlar", "magaza",
    "kitaplik", "bildirim", "bildirimler", "ana", "ekran", "ekrani", "lutfen", "bekleyin", "diger", "baglantilar",
    "baglanti", "arkadas", "arkadaslar", "etkinlik", "etkinligi", "etkinlikler", "konsol", "depolama", "alani",
    "haberler", "hikaye", "guc", "tasarrufu", "gorunumu", "gorunum", "evet", "hayir", "devam", "kaydet", "yukle",
    "yukleniyor", "cikis", "icin", "ile", "veya", "simdi", "daha", "fazla", "bilgi", "goruntule", "ayrintilar",
    "uygulamalar", "uygulama", "kupalar", "kupa", "oturum", "kapat", "yeniden", "baslat", "download", "indiriliyor",
    "guncelleme", "sesli", "sohbet", "parti", "medya", "galeri", "yakala", "paylas", "kontrol", "merkezi",
}


def _fold(w: str) -> str:
    return w.casefold().translate(_FOLD).replace("i̇", "i")


def _word_like(w: str) -> bool:
    """A sensible vowel ratio and no 5 consonants in a row: looks like a real word (even in another language).
    ("Baglantllar": consonants can end up next to each other when OCR reads 'ı' as 'l'.)"""
    letters_ = [c for c in w if c.isalpha()]
    if len(letters_) < 3:
        return False
    ratio_ = sum(1 for c in letters_ if c in _VOWELS) / len(letters_)
    return ratio_ >= 0.25 and not re.search(r"[^aeıioöuüAEIİOÖUÜ\W\d_]{5,}", w)


def junk_count(text: str) -> int:
    return sum(1 for w in normalize_text(text).split() if _JUNK.search(w))


def ends_with_punct(text: str) -> bool:
    t = normalize_text(text).rstrip("\"')]» ")
    return t.endswith((".", "!", "?", "…"))


# ---------------------------------------------------------------- speaker name and sentences

_SPEAKER = re.compile(r"^\s*([A-Z][A-Za-z'’.\-]*(?:\s+[A-Z][A-Za-z'’.\-]*){0,2})\s*:\s+(\S.*)$")
_CLOCK = re.compile(r"^\d{1,2}:\d{2}")


def split_speaker(text: str) -> tuple[str | None, str]:
    """'JASON: Gidelim.' -> ('JASON', 'Gidelim.'). The name is at most three words, each capitalized."""
    text = normalize_text(text)
    if _CLOCK.match(text):
        return None, text
    m = _SPEAKER.match(text)
    if not m:
        return None, text
    name = m.group(1).strip()
    if len(name) > 24:
        return None, text
    return name, m.group(2).strip()


_ABBREV = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|vs|Lt|Sgt|Capt|Det|Off)\.$", re.IGNORECASE)


def split_sentences(text: str) -> list[str]:
    """Splits a subtitle into sentences (the cache is kept per sentence). Does not split after abbreviations or before a
    lower case letter after '...'. Dialogue dashes ('- Where? - Home.') count as separate sentences."""
    text = normalize_text(text)
    if not text:
        return []
    parts: list[str] = []
    start = 0
    for m in re.finditer(r"(?<=[.!?…])[\"')\]]*\s+(?=[\-–—]?\s*[\"'(]?[A-Z0-9¿¡])", text):
        piece = text[start:m.end()].strip()
        if _ABBREV.search(piece):
            continue
        parts.append(piece)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    # A dialogue dash in the middle of a sentence: "Where? - Home."
    out: list[str] = []
    for p in parts:
        out.extend(s.strip() for s in re.split(r"(?<=[.!?…])\s+(?=[\-–—]\s)", p) if s.strip())
    return out


def chunk_sentences(sentences: list[str], minimum: int = 3) -> list[str]:
    """Joins one- or two-word sentences with their neighbors: "Girl. Stop. Rent is coming." is translated as one piece
    (so a small model does not translate "Kız. | Dur." separately). Filled from the left; a chunk closes once it
    reaches minimum words, and a one- or two-word leftover at the end joins the previous chunk. Normal sentences stay
    separate: they are found one by one in the cache."""
    chunks: list[list[str]] = []
    number = 0
    for c in sentences:
        if chunks and number < minimum:
            chunks[-1].append(c)
        else:
            chunks.append([c])
            number = 0
        number += len(words_(c))
    if len(chunks) > 1 and sum(len(words_(c)) for c in chunks[-1]) <= 2:
        chunks[-2].extend(chunks.pop())
    return [" ".join(o) for o in chunks]


def new_part(old: str, new: str) -> str | None:
    """If new starts with old's last sentences (the game kept the previous line on top and added a new one below), only
    the added sentences; None if there is no overlap. "A. B." -> "A. B. C." => "C."; "A. B." -> "B. C." => "C."."""
    se = split_sentences(split_speaker(old)[1])
    sy = split_sentences(split_speaker(new)[1])
    for j in range(len(se)):
        common = se[j:]
        if len(common) >= len(sy):
            continue
        if all(similarity(a, b) >= 0.8 for a, b in zip(common, sy)):
            return " ".join(sy[len(common):])
    return None
