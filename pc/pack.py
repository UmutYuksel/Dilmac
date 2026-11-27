"""Game pack: translations of played lines made by a strong model (Claude) with the scene's context + hand-written
style examples.

pc/packs/*.json format:
    {"game": "Grand Theft Auto VI", "source": "...", "lines": {"English line.": "Türkçesi."},
     "speakers": {"English line.": "Lucia"}}
speakers is optional: who says the line (Lucia, Jason, pedestrian, police or another character's name). On the TV the
text is shown in the speaker's color (the colors are in the game's glossary, "colors").
All files of the same game are merged; the one in the user's folder (pc/packs) wins over the one shipped with the app.

It does two jobs:
- Lookup (lookup): checked before live translation. Tolerant to OCR errors: "Bills¯" and "Bill." find the same line.
- Examples (similar): when a new line is translated, the few most similar lines from the pack are added to the prompt
  as examples. That is how the small local model learns the game's voice, forms of address and terms; as the pack
  grows, lines seen for the first time get better too.

For the line log (pc/lines/*.jsonl) and the "pending lines", see pack_tool.py.

Hashed packs (2026-11-27, for a public release): the packs the server gives every app hold no English text, only
hashes (as CouchSubs does): {"format": "hashed-v1", "h": {H(text_key): Turkish}, "o": {H(ocr_key): H(text_key)},
"m": {H(text_key): "<ocr_key length>:<base64 MinHash>"}, "s": {H(text_key): speaker}}. Exact and OCR-key lookups hash
the read line; a line with a letter or two misread is found by the MinHash of its letter trigrams (an estimate of how
many trigrams two lines share, without the text). Hashed lines give no prompt examples (similar() needs the English).
Our own PCs get the full packs (pack_server, admin token) and keep working with the text.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import textutil as M
import paths

# Common words not counted when measuring similarity.
PADDING = {
    "the", "and", "you", "your", "that", "this", "with", "for", "are", "was", "were", "have", "has", "had", "not",
    "but", "what", "who", "how", "why", "when", "where", "all", "just", "can", "get", "got", "she", "her", "him",
    "his", "they", "them", "their", "our", "out", "about", "there", "here", "from", "will", "would", "could",
    "should", "been", "then", "than", "now", "yeah", "okay", "it's", "i'm", "you're", "don't", "gonna", "like",
    "know", "one", "some", "any", "into", "too", "very", "really", "right", "come", "let's", "hey",
}


def content_words(text: str) -> set[str]:
    """Content words for similarity (lower case, 3+ letters, filler words excluded)."""
    return {w for w in (x.casefold() for x in M.words_(text)) if len(w) >= 3 and w not in PADDING}


def _punctuation(source: str, tr: str) -> str:
    """Makes the trailing punctuation of the pack's translation follow the read line (OCR may have missed the dot)."""
    last = re.search(r"[.!?…]+$", M.normalize_text(source))
    if last is None:
        return tr
    return tr.rstrip(".!?…") + last.group(0) if not tr.endswith(last.group(0)) else tr


# ---------------------------------------------------------------- hashed packs

HASHED = "hashed-v1"
_HASH_PREFIX = "dilmac-line-v1\n"
MINHASH_N = 32           # MinHash values per line (16 bits each)
MINHASH_BANDS = 8        # LSH: lines sharing all values of one band are candidates
MINHASH_MIN_LEN = 12     # OCR keys shorter than this are not matched loosely (same as the text fuzzy match)
MINHASH_ACCEPT = 0.6     # share of equal MinHash values (about the trigram overlap) to accept a candidate
_P61 = (1 << 61) - 1
_COEF = [(int.from_bytes(hashlib.sha256(f"dilmac-mh-a{i}".encode()).digest()[:8], "big") % (_P61 - 1) + 1,
          int.from_bytes(hashlib.sha256(f"dilmac-mh-b{i}".encode()).digest()[:8], "big") % _P61)
         for i in range(MINHASH_N)]


def line_hash(key: str) -> str:
    """80-bit hash of a normalized key (text_key or ocr_key); the pack's English never leaves our PCs."""
    return hashlib.sha256((_HASH_PREFIX + key).encode("utf-8")).hexdigest()[:20]


def _trigrams(ocr_k: str) -> set[int]:
    s = f"^{ocr_k}$"
    return {int.from_bytes(hashlib.blake2b(s[i:i + 3].encode("utf-8"), digest_size=8).digest(), "big")
            for i in range(len(s) - 2)}


def minhash(ocr_k: str) -> bytes:
    """MinHash of the key's letter trigrams: MINHASH_N values of 16 bits. Two lines share about as many values as
    they share trigrams (Jaccard), so a line with a misread letter or two stays close."""
    grams = _trigrams(ocr_k)
    out = bytearray()
    for a, b in _COEF:
        v = min((a * g + b) % _P61 for g in grams) if grams else 0
        out += (v & 0xFFFF).to_bytes(2, "big")
    return bytes(out)


def _bands(mh: bytes) -> list[bytes]:
    step = len(mh) // MINHASH_BANDS
    return [bytes([j]) + mh[j * step:(j + 1) * step] for j in range(MINHASH_BANDS)]


def _same_values(a: bytes, b: bytes) -> float:
    n = len(a) // 2
    return sum(a[2 * i:2 * i + 2] == b[2 * i:2 * i + 2] for i in range(n)) / n if n else 0.0


def to_hashed(data: dict) -> dict:
    """A pack file without its English: what the server gives every app."""
    h, o, m, s = {}, {}, {}, {}
    speakers = {M.text_key(en): who for en, who in (data.get("speakers") or {}).items()}
    for en, tr in (data.get("lines") or {}).items():
        en, tr = M.normalize_text(en), (tr or "").strip()
        k = M.text_key(en)
        if not k or not tr:
            continue
        hk = line_hash(k)
        if hk in h:
            continue
        h[hk] = tr
        ok = M.ocr_key(en)
        if len(ok) >= 8 and ok != k:
            o.setdefault(line_hash(ok), hk)
        if len(ok) >= MINHASH_MIN_LEN:
            m[hk] = f"{len(ok)}:{base64.b64encode(minhash(ok)).decode('ascii')}"
        if speakers.get(k):
            s[hk] = speakers[k]
    out = {k: v for k, v in data.items() if k not in ("lines", "speakers")}
    out.update({"format": HASHED, "lines": {}, "h": h, "o": o, "m": m, "s": s})
    return out


class Pack:
    def __init__(self, game_title: str = "", lines_: dict[str, str] | None = None,
                 speaker_labels: dict[str, str] | None = None):
        self.game_title = game_title
        self.full_: dict[str, int] = {}     # key -> pair index
        self.ocr: dict[str, int] = {}     # ocr_key -> pair index
        self.pairs: list[tuple[str | None, str, frozenset]] = []  # en is None for a hashed line
        self.speakers: dict[int, str] = {}  # pair index -> speaker ("Lucia", "Jason", "pedestrian", "police", another name)
        self.word_index: dict[str, list[int]] = defaultdict(list)
        self.hashed: dict[str, int] = {}    # line_hash(text_key) -> pair index (every line, so files dedupe)
        self.hashed_ocr: dict[str, int] = {}
        self.minhashes: dict[int, tuple[int, bytes]] = {}  # pair index -> (ocr_key length, MinHash)
        self.lsh: dict[bytes, list[int]] = defaultdict(list)
        self.add(lines_ or {}, speaker_labels)

    def __len__(self) -> int:
        return len(self.pairs)

    def add(self, lines_: dict[str, str], speaker_labels: dict[str, str] | None = None) -> None:
        """speakers: {"English line.": "Lucia"}; the line may be in this file or in a file added earlier."""
        for en, tr in lines_.items():
            en, tr = M.normalize_text(en), (tr or "").strip()
            k = M.text_key(en)
            if not k or not tr or k in self.full_:
                continue  # the file that comes first (the user's) wins
            hk = line_hash(k)
            if hk in self.hashed:
                continue  # a hashed file that came first has it
            i = len(self.pairs)
            self.full_[k] = i
            self.hashed[hk] = i
            self.ocr.setdefault(M.ocr_key(en), i)
            word_set = frozenset(content_words(en))
            self.pairs.append((en, tr, word_set))
            for w in word_set:
                self.word_index[w].append(i)
        for en, speaker in (speaker_labels or {}).items():
            i = self.full_.get(M.text_key(en))
            if i is not None and speaker and i not in self.speakers:
                self.speakers[i] = speaker.strip()

    def add_hashed(self, data: dict) -> None:
        """A hashed pack file (to_hashed); lines a file before it had are skipped."""
        new: dict[str, int] = {}
        for hk, tr in (data.get("h") or {}).items():
            tr = (tr or "").strip()
            if not tr or hk in self.hashed:
                continue
            i = len(self.pairs)
            self.hashed[hk] = new[hk] = i
            self.pairs.append((None, tr, frozenset()))
        for ho, hk in (data.get("o") or {}).items():
            if hk in new:
                self.hashed_ocr.setdefault(ho, new[hk])
        for hk, v in (data.get("m") or {}).items():
            i = new.get(hk)
            try:
                n, b64 = str(v).split(":", 1)
                mh = base64.b64decode(b64)
            except ValueError:
                continue
            if i is None or len(mh) != 2 * MINHASH_N:
                continue
            self.minhashes[i] = (int(n), mh)
            for band in _bands(mh):
                self.lsh[band].append(i)
        for hk, who in (data.get("s") or {}).items():
            i = new.get(hk)
            if i is not None and who and i not in self.speakers:
                self.speakers[i] = str(who).strip()

    @classmethod
    def files(cls, dirs: list[Path] | None = None) -> list[Path]:
        return [f for d in (paths.pack_dirs() if dirs is None else dirs) for f in sorted(d.glob("*.json"))]

    @classmethod
    def load(cls, game_title: str, names: list[str] | None = None, dirs: list[Path] | None = None) -> "Pack | None":
        """All pack files of the game (matching its name or one of its aliases), merged. dirs: only these folders."""
        if not game_title:
            return None
        dest = {M.text_key(a) for a in [game_title, *(names or [])] if a}
        p = cls(game_title)
        for f in cls.files(dirs):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if M.text_key(data.get("game", "")) in dest:
                if data.get("format") == HASHED:
                    p.add_hashed(data)
                else:
                    p.add(data.get("lines", {}), data.get("speakers"))
        return p if len(p) else None

    def _index_of(self, text: str) -> int | None:
        k = M.text_key(text)
        i = self.full_.get(k)
        if i is None and len(self.hashed) > len(self.full_):  # there are hashed-only lines
            i = self.hashed.get(line_hash(k))
        if i is None:
            ocr_k = M.ocr_key(text)
            if len(ocr_k) >= 8:  # on a short piece the OCR match could find the wrong line
                i = self.ocr.get(ocr_k)
                if i is None and self.hashed_ocr:
                    i = self.hashed_ocr.get(line_hash(ocr_k))
                if i is None:
                    i = self._fuzzy(text, ocr_k)
                if i is None and self.minhashes:
                    i = self._fuzzy_hashed(ocr_k)
        return i

    def _fuzzy_hashed(self, ocr_k: str) -> int | None:
        """_fuzzy for hashed lines: candidates from the MinHash bands, the one sharing the most values (>=
        MINHASH_ACCEPT) with a similar length."""
        if len(ocr_k) < MINHASH_MIN_LEN:
            return None
        mh = minhash(ocr_k)
        seen: set[int] = set()
        best, score = None, MINHASH_ACCEPT
        for band in _bands(mh):
            for i in self.lsh.get(band, ()):
                if i in seen:
                    continue
                seen.add(i)
                n, other = self.minhashes[i]
                if abs(n - len(ocr_k)) > 0.15 * len(ocr_k):
                    continue
                s = _same_values(mh, other)
                if s >= score:
                    best, score = i, s
        return best

    def lookup(self, text: str) -> str | None:
        i = self._index_of(text)
        return _punctuation(text, self.pairs[i][1]) if i is not None else None

    def speaker(self, text: str) -> str | None:
        """Who says the line (worked out from the scene when translating the pack); None if unknown."""
        i = self._index_of(text)
        return self.speakers.get(i) if i is not None else None

    def speaker_by_sentence(self, text: str) -> str | None:
        """speaker(); if the whole line is not in the pack (the game showed two sentences in one subtitle, the pack has them
        separately) it is looked up sentence by sentence, the first known speaker. A speaker name like "JASON:" is
        dropped from the start."""
        speaker = self.speaker(text)
        if speaker is None:
            _, body = M.split_speaker(text)
            for piece_ in M.chunk_sentences(M.split_sentences(body)) or [body]:
                speaker = self.speaker(piece_)
                if speaker:
                    break
        return speaker

    def _fuzzy(self, text: str, ocr_k: str) -> int | None:
        """A line with a letter or two misread ("Bills ̄", "doh't"): of the candidates with the same content words, the one
        with letter similarity >= 0.9 and a similar length."""
        if len(ocr_k) < 12:
            return None
        counter: dict[int, int] = defaultdict(int)
        for w in content_words(text):
            for i in self.word_index.get(w, ()):
                counter[i] += 1
        best, score = None, 0.9
        for i, _ in sorted(counter.items(), key=lambda x: -x[1])[:30]:
            en, _, _ = self.pairs[i]
            if en is None:
                continue
            k_old = M.ocr_key(en)
            if abs(len(k_old) - len(ocr_k)) > 0.15 * len(ocr_k):
                continue
            b = M.similarity(text, en)
            if b >= score:
                best, score = i, b
        return best

    def similar(self, text: str, n: int = 3, threshold: float = 0.3) -> list[tuple[str, str]]:
        """The n most similar pairs (content word overlap, cosine); the same line excluded."""
        word_set = content_words(text)
        if not word_set:
            return []
        counter: dict[int, int] = defaultdict(int)
        for w in word_set:
            for i in self.word_index.get(w, ()):
                counter[i] += 1
        k = M.text_key(text)
        scored = []
        for i, common in counter.items():
            en, tr, s = self.pairs[i]
            if en is None:
                continue
            score = common / math.sqrt(len(word_set) * len(s))
            if score >= threshold and M.text_key(en) != k:
                scored.append((score, -i, en, tr))
        scored.sort(reverse=True)
        return [(en, tr) for _, _, en, tr in scored[:n]]
