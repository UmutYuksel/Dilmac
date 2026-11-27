"""Reading the subtitle region with Windows OCR: a few image preparations, a line position filter, picking the best read.

Preparations:
- raw:   grayscale + auto contrast. Good for colored words and dark backgrounds.
- white: only "white text + a dark shadow/edge next to it" pixels are kept and read as black text on white.
         With GTA-style subtitles (white text, black shadow) a bright background (sky, shirt, pavement) is erased.
Raw is read first; if it gives clean English that is enough (most frames need a single read). Otherwise white is read
too and the one that looks more like English wins.

Line sorting (Layout): OCR lines are first collected into rows by joining pieces at the same height (a line split in
two by a colored word, "Pick up | Dr. Bill", becomes one row). If the game's layout is known (GTA VI):
- a row whose center is not at the picture's center is dropped: phone messages, signs, district names, button hints;
- a centered row below objective_y is objective text ("Go to the docks"), read and translated apart from speech.
Without a layout the old rule applies: short lines stuck to the region's edge are dropped.
Boxes are given as shares of the game picture (0-1): the Turkish box on the TV sits exactly on the English text.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

import textutil as M


@dataclass
class OcrLine:
    txt: str
    x: float
    y: float
    w: float
    h: float


Box = tuple  # (x, y, w, h): as a share of the game picture


@dataclass
class Layout:
    """Where the subtitle region is in the game picture, and the line sorting thresholds (all as shares, 0-1)."""
    left: float = 0.0
    top: float = 0.0
    gen: float = 1.0
    height: float = 1.0
    center_tol: float = 0.0   # >0: a row's center may be at most this far from the picture's center; rows further off are dropped
    objective_y: float = 0.0    # >0: a row whose top edge is below this height is objective text
    block_step: float = 0.0  # >0: if two speech rows' top edges are further apart than this, they are separate subtitles

    @classmethod
    def from_region(cls, region: dict, center_tol: float = 0.0, objective_y: float = 0.0, block_step: float = 0.0) -> "Layout":
        return cls(region["left"], region["top"], region["width"], region["height"], center_tol, objective_y, block_step)

    def to_frame(self, s: OcrLine) -> OcrLine:
        """Line box relative to the region -> relative to the picture."""
        return OcrLine(s.txt, self.left + s.x * self.gen, self.top + s.y * self.height, s.w * self.gen, s.h * self.height)

    def to_region(self, k: Box) -> Box:
        x, y, w, h = k
        return ((x - self.left) / self.gen, (y - self.top) / self.height, w / self.gen, h / self.height)


@dataclass
class LineSplit:
    """The rows of one read, sorted (relative to the picture, top to bottom)."""
    speech: list = field(default_factory=list)
    objective: list = field(default_factory=list)
    dropped: list = field(default_factory=list)


@dataclass
class Block:
    """One subtitle (one speaker's line; a long one may wrap over a few rows)."""
    txt: str
    bbox: Box
    line_count: int = 1
    rows: list = field(default_factory=list)  # OcrLines (top to bottom)


@dataclass
class Reading:
    txt: str = ""             # speech subtitle
    prep: str = ""
    turkish: bool = False        # the text on screen is Turkish (PS5 menu etc.), ignored
    raw_: str = ""               # before filtering, for the log
    timings: dict = field(default_factory=dict)
    bbox: Box | None = None    # area covered by the speech
    line_count: int = 0              # how many rows the speech has
    line_h: float = 0.0        # average height of one row (text size on the TV follows it)
    objective: str = ""             # objective text (if the layout is known)
    objective_box: Box | None = None
    objective_line: int = 0
    objective_line_h: float = 0.0
    blocks_: list = field(default_factory=list)  # speech split into separate subtitles (top to bottom)


# ---------------------------------------------------------------- preparations

def _scaled(img: Image.Image, scale: float) -> Image.Image:
    if scale and abs(scale - 1) > 0.01:
        return img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.BILINEAR)
    return img


def prepare_raw(img: Image.Image, scale: float = 1.0) -> Image.Image:
    return _scaled(ImageOps.autocontrast(ImageOps.grayscale(img)), scale)


def _channels(img: Image.Image):
    r, g, b = img.convert("RGB").split()
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return mn, mx


def auto_level(img: Image.Image) -> Image.Image:
    """The brightest 0.5% of the region is pulled to white: in dark/dim scenes a white subtitle can stay at 150-200."""
    g = ImageOps.grayscale(img)
    hist = g.histogram()
    dest = g.width * g.height * 0.005
    total, peak = 0, 255
    for v in range(255, -1, -1):
        total += hist[v]
        if total >= dest:
            peak = v
            break
    if peak >= 235 or peak < 100:
        return img
    k = 255 / peak
    return img.point(lambda p: min(255, int(p * k)))


def shadow_k(frame_height: int) -> int:
    """Half the shadow search window: grows with the text size (1 at 720p, 2 at 1080p-1440p, 3 at 4K)."""
    return max(1, round(frame_height / 680))


def white_mask(img: Image.Image, bright: int = 185, color: int = 60, dark: int = 90, shadow: int = 2) -> Image.Image:
    """255 = white text pixel. If shadow>0, white pixels without a dark pixel (shadow/edge) in the [0, 2*shadow] window to
    their lower right are dropped. The shadow is only looked for to the lower right: bright background to the upper
    left of the letters does not mix into the text."""
    mn, mx = _channels(img)
    white = mn.point(lambda p: 255 if p >= bright else 0)
    gray_ = ImageChops.subtract(mx, mn).point(lambda p: 255 if p <= color else 0)
    mask = ImageChops.multiply(white, gray_)
    if shadow:
        k = shadow
        dark_m = mx.point(lambda p: 255 if p <= dark else 0).filter(ImageFilter.MaxFilter(2 * k + 1))
        dark_m = ImageChops.offset(dark_m, -k, -k)  # (x, y) -> window [x, x+2k] x [y, y+2k]
        mask = ImageChops.multiply(mask, dark_m)
    return mask


def prepare_white(img: Image.Image, scale: float = 1.0, k: int = 2) -> Image.Image:
    # Black text on white. Thickening was tried: letters stick together and reading gets worse.
    return _scaled(ImageOps.invert(white_mask(auto_level(img), shadow=k)), scale)


def auto_scale(frame_height: int) -> float:
    """In a small window the text stays small; it is scaled up before OCR (frames of 1300 px and more are read as they
    are)."""
    return max(1.0, min(2.0, 1300 / max(1, frame_height)))


def signature(img: Image.Image) -> Image.Image:
    """A small signature of the region (white text mask): to tell whether the text is still there when OCR misses it."""
    return white_mask(img, shadow=0).resize((128, 32), Image.BOX)


def box_signature(img: Image.Image, bbox: Box | None, layout: "Layout") -> Image.Image | None:
    """Signature of only the box where the text was read last (a phone or sign lighting up at the edge does not matter)."""
    if bbox is None:
        return None
    x, y, w, h = layout.to_region(bbox)
    px, py = 0.01, 0.25 * h  # a bit outside the box: letter shadows and OCR's tight box
    x0, y0 = max(0, int((x - px) * img.width)), max(0, int((y - py) * img.height))
    x1, y1 = min(img.width, int((x + w + px) * img.width) + 1), min(img.height, int((y + h + py) * img.height) + 1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return signature(img.crop((x0, y0, x1, y1)))


def text_in_place(now_: Image.Image | None, last: Image.Image | None, ratio_: float = 0.6) -> bool:
    """Is at least `ratio_` of the white text pixels of the last read frame still there in this frame.
    The mean difference cannot be used: the text is a small part of the region, the mean barely changes when it goes."""
    if now_ is None or last is None or now_.size != last.size:
        return False
    total = ImageStat.Stat(last).sum[0]
    if total < 255 * 20:  # the signature has too little white to hold text
        return False
    return ImageStat.Stat(ImageChops.darker(now_, last)).sum[0] / total >= ratio_


# ---------------------------------------------------------------- OCR

def ocr_engine():
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    return OcrEngine.try_create_from_language(Language("en-US"))


async def ocr_lines(engine, img: Image.Image) -> list[OcrLine]:
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    rgba = img.convert("RGBA")
    w, h = rgba.size
    writer = DataWriter()
    writer.write_bytes(rgba.tobytes("raw", "BGRA"))
    bmp = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED
    )
    result = await engine.recognize_async(bmp)
    out = []
    for line in result.lines:
        rects = [wd.bounding_rect for wd in line.words]
        if not rects:
            continue
        x0 = min(r.x for r in rects)
        y0 = min(r.y for r in rects)
        x1 = max(r.x + r.width for r in rects)
        y1 = max(r.y + r.height for r in rects)
        out.append(OcrLine(line.text, x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h))
    return out


def _has_letters(t: str) -> bool:
    compact = t.replace(" ", "")
    letters = sum(c.isalpha() for c in compact)
    return letters >= 3 and letters / max(1, len(compact)) >= 0.6


ASPECT = 16 / 9  # aspect ratio of the game picture: to compare horizontal and vertical shares


def _merge(pieces: list[OcrLine]) -> OcrLine:
    x0, y0 = min(p.x for p in pieces), min(p.y for p in pieces)
    x1, y1 = max(p.x + p.w for p in pieces), max(p.y + p.h for p in pieces)
    return OcrLine(" ".join(p.txt.strip() for p in pieces), x0, y0, x1 - x0, y1 - y0)


def row_groups(lines_: list[OcrLine]) -> list[list[OcrLine]]:
    """OCR pieces at the same height (left to right), groups top to bottom. Boxes must be relative to the picture."""
    groups: list[list[OcrLine]] = []
    for s in sorted((s for s in lines_ if s.txt.strip()), key=lambda s: s.y + s.h / 2):
        for group_ in groups:
            if any(abs((s.y + s.h / 2) - (p.y + p.h / 2)) <= 0.5 * max(s.h, p.h) for p in group_):
                group_.append(s)
                break
        else:
            groups.append([s])
    for group_ in groups:
        group_.sort(key=lambda p: p.x)
    groups.sort(key=lambda g: min(p.y + p.h / 2 for p in g))
    return groups


def rows(lines_: list[OcrLine]) -> list[OcrLine]:
    """The pieces at each height as one row."""
    return [_merge(g) for g in row_groups(lines_)]


def _centered_part(group_: list[OcrLine], tol: float) -> tuple[int, int] | None:
    """The widest run of adjacent pieces [i, j) whose center is at the picture's center. A line split by a colored word
    ("Pick up" + "Dr. Bill") is centered as a whole; a sign stuck next to it ("... money. | PlüWASSER") stays out."""
    best, box_width = None, -1.0
    for i in range(len(group_)):
        for j in range(i + 1, len(group_) + 1):
            if j - i > 1:
                a, b = group_[j - 2], group_[j - 1]
                if (b.x - (a.x + a.w)) * ASPECT > 1.2 * max(a.h, b.h):
                    break  # a gap wider than a word space in between: separate texts
            k = _merge(group_[i:j])
            if abs(k.x + k.w / 2 - 0.5) <= tol and k.w > box_width:
                best, box_width = (i, j), k.w
    return best


def classify(lines_: list[OcrLine], layout: Layout) -> LineSplit:
    """Sorts region-relative OCR lines into speech / objective / dropped (boxes relative to the picture)."""
    a = LineSplit()
    for group_ in row_groups([layout.to_frame(s) for s in lines_]):
        if layout.center_tol > 0:
            ij = _centered_part(group_, layout.center_tol)
            if ij is None:
                a.dropped.extend(group_)  # not centered: phone, sign, district name, button hint, partial read
                continue
            a.dropped.extend(group_[:ij[0]] + group_[ij[1]:])
            group_ = group_[ij[0]:ij[1]]
        g = _merge(group_)
        if not _has_letters(g.txt):
            a.dropped.append(g)  # HUD leftovers, numbers, single letters
            continue
        if layout.center_tol <= 0:
            bx, _, bw, _ = layout.to_region((g.x, g.y, g.w, g.h))
            if bw < 0.35 and not (0.2 <= bx + bw / 2 <= 0.8):
                a.dropped.append(g)  # short line stuck to the edge: button hint, corner notification
                continue
        (a.objective if layout.objective_y > 0 and g.y >= layout.objective_y else a.speech).append(g)
    return a


def joined(lines_: list[OcrLine]) -> str:
    return re.sub(r"\s+", " ", " ".join(s.txt for s in lines_)).strip()


def bounding(lines_: list[OcrLine]) -> Box | None:
    if not lines_:
        return None
    x0, y0 = min(s.x for s in lines_), min(s.y for s in lines_)
    x1, y1 = max(s.x + s.w for s in lines_), max(s.y + s.h for s in lines_)
    return (x0, y0, x1 - x0, y1 - y0)


def split_into_blocks(rows_: list[OcrLine], step: float) -> list[Block]:
    """Splits speech rows into separate subtitles. In GTA VI the wrapped rows of one long subtitle sit close together (top
    edges ~0.044 of the frame apart); separate overlapping subtitles have a wider gap (~0.053): an NPC shouting, the
    next line. The top edge is used; the middle moves with descending letters."""
    if not rows_:
        return []
    groups = [[rows_[0]]]
    for prev, s in zip(rows_, rows_[1:]):
        if step > 0 and s.y - prev.y > step:
            groups.append([s])
        else:
            groups[-1].append(s)
    return [Block(joined(g), bounding(g), len(g), g) for g in groups]


def filter_lines(lines_: list[OcrLine], layout: Layout | None = None) -> str:
    """Speech text (for the test commands)."""
    return joined(classify(lines_, layout or Layout()).speech)


class Reader:
    """scale <= 0: automatic from the frame height (auto_scale). layout: the region's place and line sorting thresholds."""

    def __init__(self, engine, lexicon: M.Lexicon, scale: float = 0.0, layout: Layout | None = None):
        self.engine = engine
        self.lexicon = lexicon
        self.scale = scale
        self.layout = layout or Layout()

    def prepare(self, img: Image.Image, prep: str, frame_height: int) -> Image.Image:
        scale = self.scale if self.scale > 0 else auto_scale(frame_height)
        if prep == "raw":
            return prepare_raw(img, scale)
        return prepare_white(img, scale, shadow_k(frame_height))

    async def _read(self, img, prep: str, frame_height: int, timings: dict) -> tuple[LineSplit, list[OcrLine]]:
        t0 = time.perf_counter()
        lines_ = await ocr_lines(self.engine, self.prepare(img, prep, frame_height))
        timings[prep] = round((time.perf_counter() - t0) * 1000)
        return classify(lines_, self.layout), lines_

    def _clean(self, split: LineSplit) -> bool:
        texts = [t for t in (joined(split.speech), joined(split.objective)) if t]
        return bool(texts) and all(self.lexicon.is_clean(t) for t in texts)

    async def read(self, img: Image.Image, frame_height: int = 1080) -> Reading:
        """img: the subtitle region; frame_height: height of the whole game picture (to estimate the text size).
        Raw is read first; if speech or objective is not clean, white is read too and the better read is picked for
        each."""
        timings: dict = {}
        a, raw_a = await self._read(img, "raw", frame_height, timings)
        candidates = [("raw", a)]
        raw_lines = raw_a
        if not self._clean(a):
            b, raw_b = await self._read(img, "white", frame_height, timings)
            candidates.append(("white", b))
            raw_lines = raw_a + raw_b

        def choose(row: str) -> tuple[str, str, list]:
            option = [(joined(getattr(ay, row)), name, getattr(ay, row)) for name, ay in candidates]
            option = [x for x in option if x[0]]
            if not option:
                return "", "", []
            return max(option, key=lambda x: self.lexicon.score(x[0]))

        k_text, k_name, speech_rows = choose("speech")
        g_text, g_name, g_idx = choose("objective")
        outcome = Reading(raw_=" | ".join(s.txt for s in raw_lines), timings=timings, prep=k_name or g_name)
        if k_text and self.lexicon.is_turkish(k_text):
            outcome.turkish = True
        elif k_text:
            outcome.txt, outcome.bbox, outcome.line_count = k_text, bounding(speech_rows), len(speech_rows)
            outcome.line_h = sum(s.h for s in speech_rows) / len(speech_rows)
            outcome.blocks_ = split_into_blocks(speech_rows, self.layout.block_step)
        if g_text and not self.lexicon.is_turkish(g_text):
            outcome.objective, outcome.objective_box, outcome.objective_line = g_text, bounding(g_idx), len(g_idx)
            outcome.objective_line_h = sum(s.h for s in g_idx) / len(g_idx)
        return outcome
