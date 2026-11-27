"""Subtitle stabilizer: decides "translate now / correct / bring back / clear" from the OCR reads arriving frame by
frame.

Decisions depend on the clock, not on frame counts (behavior stays the same if the read rate changes). Rules:
- A clean line that ends with punctuation is sent on the first frame (no delay).
- A clean line without punctuation goes once it has read the same for STABLE_MS (the text may still be appearing).
- A garbled read (like OCR junk) is not sent unless it stays the same for GARBLED_STABLE_MS: the in-between states
  read while a subtitle fades in/out never reach the translator.
- A partial or slightly different read of the sent line counts as the same line and is not translated again.
- If words are added to the end of the sent line (the speech got longer), it is a new line.
- If a more "real English" read with the same number of words comes repeatedly, it is translated once more as a
  correction.
- When the region stays empty for CLEAR_MS the subtitle counts as gone. If the region's picture has not changed (OCR
  missed the text), it waits up to HOLD_IF_UNCHANGED_MS.
- If a cleared line comes back within COMEBACK_MS it is not translated again; its old translation is shown.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import textutil as M


@dataclass
class Decision:
    kind: str = "yok"  # "" | send | correct | back | clear
    txt: str = ""
    wait_ms: float = 0.0  # time from the frame where the text was first seen to this decision (latency measurement)


NOTHING = Decision()


@dataclass
class _Line:
    txt: str
    first: float
    last: float
    corrected: bool = False
    same_count: int = 1


@dataclass
class _Candidate:
    txt: str
    first: float
    number: int = 1
    score: float = 0.0
    seen: list[str] = field(default_factory=list)


class Stabilizer:
    CLEAR_MS = 800
    HOLD_IF_UNCHANGED_MS = 4000
    COMEBACK_MS = 1500
    STABLE_MS = 250
    GARBLED_STABLE_MS = 700
    RETRY_MS = 1000

    def __init__(self, lexicon: M.Lexicon, clock=time.monotonic):
        self.lexicon = lexicon
        self._clock = clock
        self.sent: _Line | None = None
        self._aday: _Candidate | None = None
        self._correction: tuple[str, int] | None = None  # (ocr key, how many times in a row)
        self._history: deque[tuple[str, float]] = deque(maxlen=5)  # (text, time it was cleared)
        self._blocked: tuple[str, float] | None = None  # (text, no retry so far)

    def _now(self) -> float:
        return self._clock() * 1000.0

    # -------------------------------------------------------- comparisons

    @staticmethod
    def _body(text: str) -> str:
        return M.split_speaker(text)[1]

    def is_extension(self, new: str, old: str) -> bool:
        """Is new old with words added to its end (the speech got longer, a second row appeared)."""
        k_new, k_old = M.ocr_key(self._body(new)), M.ocr_key(self._body(old))
        return len(k_new) >= len(k_old) + 4 and M.coverage(self._body(old), self._body(new)) >= 0.85

    def is_same(self, reading: str, line_count: str) -> bool:
        """Is the read a re-read of the line (noisy, partial or without the name)."""
        a, b = self._body(reading), self._body(line_count)
        if self.is_extension(reading, line_count):
            return False
        if M.similarity(a, b) >= 0.8:
            return True
        ka, kb = M.ocr_key(a), M.ocr_key(b)
        # Partial read: a piece of the line was read (text fading / on a bright background)
        return len(ka) < len(kb) and len(ka) >= 0.4 * len(kb) and M.coverage(a, b) >= 0.85

    # -------------------------------------------------------- main entry

    def feed(self, reading: str, unchanged: bool = False) -> Decision:
        """The (cleaned) read of one frame. An empty read = no subtitle in the region (or it could not be read).
        unchanged: has the region's picture stayed the same since the frame where the line was read last."""
        t = self._now()
        reading = M.normalize_text(reading)
        if not reading:
            return self._empty(t, unchanged)

        g = self.sent
        if g is not None and self.is_same(reading, g.txt):
            g.last = t
            self._aday = None
            return self._check_correction(reading, g)

        self._correction = None
        a = self._aday
        if a is not None and not self.is_extension(reading, a.txt) and M.similarity(reading, a.txt) >= 0.8:
            a.number += 1
            p = self.lexicon.score(reading)
            if p > a.score:
                a.txt, a.score = reading, p
        else:
            a = self._aday = _Candidate(reading, t, score=self.lexicon.score(reading))

        back = self._returning(reading, t)
        if back is not None:
            self._sent(back, t)
            return Decision("back", back)

        if self._blocked and t < self._blocked[1] and M.similarity(reading, self._blocked[0]) >= 0.8:
            return NOTHING

        duration = t - a.first
        clean = self.lexicon.is_clean(a.txt)
        if clean and M.ends_with_punct(a.txt):
            send_line = True
        elif clean:
            send_line = a.number >= 2 and duration >= self.STABLE_MS
        elif not self.lexicon.is_garbled(a.txt):
            send_line = a.number >= 2 and duration >= self.STABLE_MS
        else:
            send_line = a.number >= 3 and duration >= self.GARBLED_STABLE_MS
        if not send_line:
            return NOTHING
        self._sent(a.txt, t)
        return Decision("send", a.txt, t - a.first)

    # -------------------------------------------------------- helpers

    def _empty(self, t: float, unchanged: bool) -> Decision:
        self._aday = None
        self._correction = None
        g = self.sent
        if g is None:
            return NOTHING
        elapsed = t - g.last
        if unchanged and elapsed < self.HOLD_IF_UNCHANGED_MS:
            return NOTHING
        if elapsed >= self.CLEAR_MS:
            self._history.append((g.txt, t))
            self.sent = None
            return Decision("clear")
        return NOTHING

    def _sent(self, text: str, t: float) -> None:
        if self.sent is not None:
            self._history.append((self.sent.txt, t))
        self.sent = _Line(text, t, t)
        self._aday = None
        self._correction = None

    def _returning(self, reading: str, t: float) -> str | None:
        for i in range(len(self._history) - 1, -1, -1):
            text, cleared_at = self._history[i]
            if t - cleared_at <= self.COMEBACK_MS and self.is_same(reading, text) and not self.is_extension(reading, text):
                del self._history[i]
                return text
        return None

    def _check_correction(self, reading: str, g: _Line) -> Decision:
        if M.ocr_key(reading) == M.ocr_key(g.txt) or M.text_key(reading) == M.text_key(g.txt):
            g.same_count += 1
            self._correction = None
            return NOTHING
        if g.corrected or len(M.words_(reading)) != len(M.words_(g.txt)):
            return NOTHING
        if self.lexicon.score(reading) <= self.lexicon.score(g.txt):
            return NOTHING
        k = M.text_key(reading)
        number = self._correction[1] + 1 if self._correction and self._correction[0] == k else 1
        self._correction = (k, number)
        if number >= 2 and (self._repairs(g.txt, reading) or number > g.same_count):
            g.txt, g.corrected = reading, True
            self._correction = None
            return Decision("correct", reading)
        return NOTHING

    def _repairs(self, old: str, new: str) -> bool:
        """Is every word of new that differs from old a repaired version of it (the old word not in the dictionary or cut)."""
        words_old, words_new = M.words_(old), M.words_(new)
        for a, b in zip(words_old, words_new):
            if a.casefold() == b.casefold():
                continue
            if self.lexicon.is_known(a) and not (len(a) < len(b) and a.casefold() in b.casefold()):
                return False
        return True

    def forget(self) -> None:
        """The translation of the sent line failed: it is retried after RETRY_MS while the line stays on screen."""
        if self.sent is not None:
            self._blocked = (self.sent.txt, self._now() + self.RETRY_MS)
            self.sent = None
