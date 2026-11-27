"""Dilmaç - the PC side.

Reads the subtitle region of the game picture (Windows OCR) from the PS Remote Play window or straight from the PS5,
translates it into Turkish (local model / Gemini / Google) and sends it over the local network to the "Dilmaç"
app on the Google TV.

Parts:
    ocr.py         reads the region with a few image preparations, sorts lines into speech / objective / dropped
    stabilizer.py  decides when to translate and when to clear (time based)
    translation.py per-sentence cache, game glossary (glossary/*.json), context, engines
    textutil.py    text helpers

Usage:
    python subtitler.py                       # run
    python subtitler.py --windows             # list open window titles
    python subtitler.py --calibrate           # save calibration images and run OCR once
    python subtitler.py --image FRAME.png     # read a saved screenshot (test)
    python subtitler.py --translate "Hello there."   # translate one line (test)
    python subtitler.py --tv-test "Merhaba"   # send a test caption to the TV
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import socket
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import textutil as M
import ocr as O
import paths
from translation import Translator, Glossary
from stabilizer import Stabilizer

HERE = paths.DATA
CONFIG_PATH = paths.CONFIG
FRAME_DIR = paths.FRAMES
CLIP_BEFORE, CLIP_AFTER = 0.5, 2.5  # clip starts this long before a line is first seen and ends this long after (s)
# Speaker recognition: RECOGNIZE_AFTER s after a line is first seen, the audio of [first - 0.1, first + RECOGNIZE_AFTER]
# is checked (cut where the next line started, otherwise two speakers' voices get mixed).
RECOGNIZE_AFTER = 1.6
LOG_FILE = paths.LOG_FILE


class ConfigError(Exception):
    """Missing setting or install that prevents starting (the message is shown to the user)."""

DEFAULTS = {
    "tv_ip": "",
    "tv_port": 8787,
    # TV kind: "android" (the Android/Google TV app draws over the HDMI input; the PC connects to it on tv_port), "lg"
    # or "samsung" (the web app in tv-web/ shows the HDMI input itself and connects to this PC on tv_web_port).
    # tv_hdmi: the TV's HDMI input the PS5 is on (LG / Samsung); 0 = automatic.
    "tv_type": "android",
    "tv_web_port": 8788,
    "tv_hdmi": 0,
    "gemini_api_key": "",
    "gemini_model": "gemini-3.5-flash-lite",
    # The first matching title is used (case insensitive, "contains" search).
    "window_titles": ["PS Remote Play", "chiaki"],
    # Subtitle region inside the game picture, as 0-1 ratios.
    "region": {"left": 0.08, "top": 0.70, "width": 0.84, "height": 0.27},
    # From the start of one read to the start of the next (ms). Capture + OCR takes ~60 ms.
    "interval_ms": 100,
    # At most this many Gemini requests per minute (free tier limit); above it Google Translate is used.
    "gemini_rpm": 12,
    # true: Google Translate only.
    "google_only": False,
    # Translation engine: "opus" (OPUS-MT on this computer: free, no key, no GPU; the default), "local" (Ollama, an
    # LLM on the GPU), "gemini" or "google".
    "engine": "opus",
    "local_model": "gemma4:e4b-it-qat",
    "local_url": "http://127.0.0.1:11434",
    # If the local model has not finished within this many ms, Google Translate is tried in parallel.
    "local_hedge_ms": 1000,
    # If Gemini has not answered within this many ms, Google Translate is tried in parallel.
    "hedge_ms": 900,
    # Shortest time a translation stays on screen after the original subtitle is gone (s).
    "linger_seconds": 2.5,
    # 0 = automatic from the window size; set by hand e.g. 1.5
    "ocr_scale": 0,
    "font_size": 30,
    "bottom_margin": 60,
    "max_show_seconds": 10,
    "game": "",
    "context_lines": 3,
    "show_original_on_error": False,
    # true: the region image of every translated line is saved to frames/ (test data for OCR tuning).
    "save_frames": False,
    # With the direct PS5 connection the game audio is received too; a ~3 s clip of every line is saved under
    # pc\voices\clips\ (mono 16 kHz). Speaker voice profiles are built from these. Turn off if the TV loses sound.
    "voice_recognition": True,
    # The box on the TV sits on top of the English subtitle. opacity 0-1, width = widest box as a share of the screen,
    # old line: how long the previous subtitle stays dimmed above the next one (s).
    # font_px: fixed text size, pixels on a 1080-line screen (scaled up on a 4K TV); objective text is 70% of it.
    # null = from the old "font_scale" setting (the game's subtitle size x scale).
    # fade_ms: the box fades in and out, new text fades in when it changes (TV app 0.4); 0 = instant.
    "box_opacity": 0.97,
    "fade_ms": 180,
    "font_px": None,
    "font_scale": 1.0,
    "box_width": 0.8,
    "old_line_seconds": 3.0,
    # Should objective texts ("Go to the docks") be translated apart from speech and shown in their own place.
    "show_objectives": True,
    # Line sorting; null = the "screen" setting of the game's glossary. center_tol: a speech line's center may be at most
    # this far from the picture's center (phones, signs are dropped). objective_y: lines below it are objectives.
    "center_tol": None,
    "objective_y": None,
    # Should overlapping separate subtitles (an NPC shouting, the next line) be translated separately, each in its own TV
    # box on top of its own English. block_step: if two rows' top edges are further apart than this, they are separate
    # subtitles (null = the glossary's "screen" setting; without it nothing is split).
    "split_blocks": True,
    "block_step": None,
    # If the speaker is known (the pack's "speakers"), the text on the TV is in their color: Lucia, Jason, other
    # characters, pedestrians/police. Colors are in the game's glossary ("colors").
    "speaker_colors": True,
    # Video source: "window" (the PS Remote Play app's window) or "direct" (connect to the PS5 without the app,
    # needs a one-time pairing; ps5_direct.py). ps5_resolution: resolution of the direct stream (720p / 1080p).
    # Pack server (pack_server.py, server/): new packs come down; lines no pack had go to the shared pool when
    # share_lines is on; with share_voices also the voice vectors (192 numbers, not audio) of lines whose speaker the
    # pack knew, for the shared voice profiles. pool_admin_token: only on our own PCs (nightly.py, pack_tool).
    "pack_server": "",
    "pack_secret": "",
    "share_lines": True,
    "share_voices": True,
    "pool_admin_token": "",
    "connection": "window",
    "ps5_resolution": "1080p",
}

user32 = ctypes.windll.user32


_log_hook = None


def log_hook(fn) -> None:
    """Send log lines to fn instead of print (GUI)."""
    global _log_hook
    _log_hook = fn


_log_lock = threading.Lock()


def _write_log(line: str) -> None:
    """The log is also written to pc\\log.txt (to review latency and translations after a session); past 2 MB it
    becomes log.old.txt."""
    try:
        with _log_lock:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
                LOG_FILE.replace(LOG_FILE.with_name("log.old.txt"))
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d ") + line + "\n")
    except OSError:
        pass


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    _write_log(line)
    if _log_hook is not None:
        _log_hook(line)
    else:
        print(line, flush=True)


# Settings that are secrets: kept encrypted in config.json ("dpapi:...", secure_store) and plain only in memory.
SECRET_KEYS = ("gemini_api_key", "pool_admin_token", "pack_secret")


def user_config() -> dict:
    """config.json itself (without the defaults added; secrets still encrypted)."""
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    return {}


def save_config(user: dict) -> None:
    import secure_store

    user = dict(user)
    for k in SECRET_KEYS:
        if isinstance(user.get(k), str) and user[k]:
            try:
                user[k] = secure_store.protect_text(user[k])
            except secure_store.SecretError:
                pass  # no DPAPI (not Windows): kept as it is
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(user, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def load_config() -> dict:
    import secure_store

    user = user_config()
    if any(isinstance(user.get(k), str) and user[k] and not secure_store.is_protected(user[k]) for k in SECRET_KEYS):
        try:
            save_config(user)  # an older config with plain secrets: encrypted now
        except OSError:
            pass
    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    for k in SECRET_KEYS:
        cfg[k] = secure_store.unprotect_text(cfg.get(k))
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        cfg["gemini_api_key"] = env_key
    return cfg


# ---------------------------------------------------------------- window and capture

def set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def list_windows() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(proc(cb), 0)
    return out


def find_window(titles: list[str]) -> tuple[int, str] | None:
    wins = list_windows()
    for wanted in titles:
        for hwnd, title in wins:
            if wanted.lower() in title.lower() and "altyazi" not in title.lower():
                return hwnd, title
    return None


def video_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Coordinates (x, y, w, h) of the 16:9 game picture inside the window's client area."""
    if user32.IsIconic(hwnd):
        return None
    r = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        return None
    cw, ch = r.right, r.bottom
    if cw < 100 or ch < 100:
        return None
    return fit_16_9(cw, ch)


def fit_16_9(cw: int, ch: int) -> tuple[int, int, int, int]:
    """Drop black bars (letterbox): fit a 16:9 picture into the area."""
    if cw / ch > 16 / 9:
        vh = ch
        vw = int(ch * 16 / 9)
    else:
        vw = cw
        vh = int(cw * 9 / 16)
    return (cw - vw) // 2, (ch - vh) // 2, vw, vh


def subtitle_box(vr: tuple[int, int, int, int], region: dict) -> dict:
    x, y, w, h = vr
    return {
        "left": x + int(w * region["left"]),
        "top": y + int(h * region["top"]),
        "width": max(1, int(w * region["width"])),
        "height": max(1, int(h * region["height"])),
    }


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def window_image(hwnd: int):
    """Captures the window's client area even if another window is on top of it (PrintWindow)."""
    from PIL import Image

    gdi32 = ctypes.windll.gdi32
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        # 1 = client area only, 2 = also draw DirectX/GPU content
        user32.PrintWindow(hwnd, mem, 3)
        bi = _BMIH(ctypes.sizeof(_BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


def grab(hwnd: int, box: dict):
    img = window_image(hwnd)
    if img is None:
        raise RuntimeError("pencere görüntüsü alınamadı")
    return img.crop((box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"]))


# ---------------------------------------------------------------- kurulum

def make_ocr_engine():
    engine = O.ocr_engine()
    if engine is None:
        raise ConfigError(
            "İngilizce yazı tanıma paketi kurulu değil. Yönetici PowerShell'de şunu çalıştır:\n"
            "  Add-WindowsCapability -Online -Name \"Language.OCR~~~en-US~0.0.1.0\""
        )
    return engine


def make_parts(cfg: dict):
    """(glossary, lexicon). If the game has a glossary its names are added to the English lexicon (so a name is not
    taken for a garbled word)."""
    glossary = Glossary.load(cfg.get("game") or "")
    extras = (glossary.keep_names + glossary.names + list(glossary.nicknames)) if glossary else []
    if cfg.get("game"):
        extras.append(cfg["game"])
    return glossary, M.Lexicon(extras)


def make_pack(cfg: dict, glossary: Glossary | None):
    """The game's pack (pc/packs/*.json), or None."""
    from pack import Pack

    game_title = glossary.game_title if glossary else (cfg.get("game") or "")
    return Pack.load(game_title, glossary.names if glossary else None)


def make_layout(cfg: dict, glossary: Glossary | None) -> O.Layout:
    """Subtitle region + line sorting thresholds (from the glossary's "screen" section unless set in the config)."""
    screen = glossary.screen if glossary else {}

    def val(name: str) -> float:
        v = cfg.get(name)
        return float(screen.get(name, 0) if v is None else v)

    block = val("block_step") if cfg.get("split_blocks", True) else 0.0
    return O.Layout.from_region(cfg["region"], val("center_tol"), val("objective_y"), block)


# A single-row GTA VI subtitle is ~0.030 of the frame (~32 px at 1080p); objective text ~0.0205. The row height OCR
# measures varies 15-20% with the letters, so the TV gets this fixed size and text no longer grows and shrinks.
BASE_FONT_PX = 32
OBJECTIVE_RATIO = 0.7


def font_px(cfg: dict) -> int:
    p = cfg.get("font_px")
    if not p:
        p = BASE_FONT_PX * float(cfg.get("font_scale") or 1.0)
    return max(12, min(96, round(float(p))))


def line_height(cfg: dict, track: str = "speech") -> float:
    """The "lineh" sent to the TV: text size as a share of the screen height."""
    return round(font_px(cfg) * (OBJECTIVE_RATIO if track == "objective" else 1.0) / 1080, 4)


# ---------------------------------------------------------------- TV link

class TvLink:
    def __init__(self, host: str, port: int, notify=None):
        self.host, self.port = host, port
        self.sock: socket.socket | None = None
        self.warned = False
        self.notify = notify or (lambda *a, **k: None)

    def send(self, msg: dict) -> bool:
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        for _ in range(2):
            try:
                if self.sock is None:
                    self.sock = socket.create_connection((self.host, self.port), timeout=2)
                    self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # small messages must not wait
                    log(f"TV'ye bağlandı: {self.host}:{self.port}")
                    self.notify("tv", connected=True)
                    self.warned = False
                self.sock.sendall(line)
                return True
            except OSError as e:
                self.close()
                if not self.warned:
                    log(f"TV'ye ulaşılamadı ({e}). TV'de 'Dilmaç' uygulaması açık mı?")
                    self.notify("tv", connected=False)
                    self.warned = True
        return False

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


WEB_TVS = ("lg", "samsung")


def web_hub(cfg: dict):
    """The server the TV web app connects to (started once per process)."""
    import tv_web

    return tv_web.hub(int(cfg.get("tv_web_port") or 8788), log, lambda: {"hdmi": int(cfg.get("tv_hdmi") or 0)},
                      token=tv_web.tv_token())


def open_web_tv_app(cfg: dict) -> bool:
    """Opens the TV web app on the TV with this PC's address (over the TV's developer mode)."""
    port, hdmi = int(cfg.get("tv_web_port") or 8788), int(cfg.get("tv_hdmi") or 0)
    if cfg.get("tv_type") == "lg":
        import lg_tv

        return lg_tv.open_app(cfg["tv_ip"], port, hdmi, log)
    if cfg.get("tv_type") == "samsung":
        import samsung_tv

        return samsung_tv.open_app(cfg["tv_ip"], port, hdmi, log)
    return False


def make_tv_link(cfg: dict, notify=None):
    """The link the subtitles go over: TCP to the Android app, or the web server the LG app connects to."""
    if cfg.get("tv_type", "android") not in WEB_TVS:
        return TvLink(cfg["tv_ip"], int(cfg["tv_port"]), notify)
    import tv_web

    try:
        hub_ = web_hub(cfg)
    except OSError as e:
        raise ConfigError(f"TV için {cfg.get('tv_web_port', 8788)} portu açılamadı ({e}). Uygulama iki kez mi açık?")
    link = tv_web.WebTvLink(hub_, notify, log)
    if hub_.clients():
        (notify or (lambda *a, **k: None))("tv", connected=True)
    threading.Thread(target=open_web_tv_app, args=(cfg,), daemon=True, name="tv-open").start()
    return link


# ---------------------------------------------------------------- main loop

class Track:
    """A text tracked on its own on screen: a speech subtitle or an objective. Each has its own stabilizer and its own
    box on the TV."""

    def __init__(self, name: str, lexicon: M.Lexicon, tv_id: str | None = None):
        self.name = name
        self.tv_id = tv_id or name  # name of the box on the TV
        self.block = tv_id is not None  # block mode: this track follows one speech block
        self.clear_above = True     # block mode: no other subtitle above it (the previous line fits dimmed above)
        self.last_reading = ""      # block mode: this block's last read English (to match blocks between frames)
        self.end_time = 0.0         # when the box on the TV removes itself (monotonic)
        self.hidden = False       # block mode: hidden on the TV because another subtitle took its place (not read)
        self.color: str | None = None  # text color on the TV: the speaker's (from the pack or the voice), white if unknown
        self.stabilizer = Stabilizer(lexicon)
        self.gen = 0             # grows with every new decision / clear
        self.sent_gen = 0        # gen of the translation sent to the TV last
        self.shown_tr = ""       # translation shown on the TV
        self.shown_at = 0.0
        self.recent_tr: dict[str, str] = {}  # sent line -> its translation shown on the TV (for a line that comes back)
        self.bbox = None         # where the text was read last (as a share of the picture)
        self.line_count = 1
        self.line_h = 0.0       # height of one row (text size on the TV)
        self.signature = None         # that box's signature: is the text still in place when OCR misses it
        self.shown_en = ""  # the English of the translation on the TV (the full line on screen)
        self.base_en = ""       # part counted as the "old line" at the last send (to find the new part when correcting)


async def run(cfg: dict, notify=None) -> None:
    """Main loop. notify(event, **data): event notifications to the GUI (en, tr, clear, window, tv, ready)."""
    notify = notify or (lambda *a, **k: None)
    if not cfg["tv_ip"]:
        raise ConfigError("TV IP adresi boş. TV uygulamasının ekranında yazan IP adresini gir.")
    if cfg.get("engine") == "gemini" and not cfg["gemini_api_key"] and not cfg.get("google_only"):
        raise ConfigError("Gemini seçili ama API anahtarı yok. Anahtarı gir ya da yerel modeli seç.")

    glossary, lexicon = make_parts(cfg)
    layout = make_layout(cfg, glossary)
    reader = O.Reader(make_ocr_engine(), lexicon, float(cfg.get("ocr_scale") or 0), layout)
    import pack_server

    try:  # new packs from the pack server first (every start; nothing if no server is set)
        await asyncio.to_thread(pack_server.sync_packs, cfg, log)
    except Exception as e:
        log(f"Paket sunucusuna bakılamadı ({e}); eldeki paketlerle devam ediliyor.")
    line_pool = pack_server.LinePool(cfg, log)
    pack_ = make_pack(cfg, glossary)
    game_name = glossary.game_title if glossary else (cfg.get("game") or "")
    audio_buffer = None   # game audio in direct mode (audio.AudioBuffer)
    recognizer = None       # speaker recognition by voice (audio.SpeakerRecognizer; if the model and sherpa-onnx are there)
    translator = Translator(cfg, glossary, log=log, pack_=pack_)
    log(f"Oyun sözlüğü: {glossary.game_title} ({len(glossary.keep_names)} ad)" if glossary else "Oyun sözlüğü yok (ayarlarda 'Oyun').")
    if pack_:
        log(f"Oyun paketi: {len(pack_)} satır (önce paketten bakılır, benzerleri modele örnek olur).")
    if layout.center_tol > 0:
        log("Yalnız ortadaki altyazı okunur (telefon, tabela, semt adı atlanır)"
            + ("; görev yazıları ayrı gösterilir." if layout.objective_y > 0 and cfg.get("show_objectives") else "."))
    block_mode = layout.block_step > 0
    if block_mode:
        log("Üst üste binen altyazılar (NPC sözleri, sıradaki replik) ayrı çevrilir, her biri kendi kutusunda.")
    await asyncio.to_thread(translator.warmup)
    tv = make_tv_link(cfg, notify)
    notify("ready")
    style_ = {"size": cfg["font_size"], "bottom": cfg["bottom_margin"], "opacity": float(cfg.get("box_opacity", 0.9)),
            "scale": 1.0, "max_width": float(cfg.get("box_width", 0.8)),
            "old_secs": float(cfg.get("old_line_seconds", 3.0)), "fade_ms": int(cfg.get("fade_ms", 180))}
    ttl = float(cfg["max_show_seconds"])
    linger = float(cfg["linger_seconds"])
    save = bool(cfg.get("save_frames"))
    if save:
        FRAME_DIR.mkdir(exist_ok=True)

    speech = Track("speech", lexicon)
    objective = Track("objective", lexicon) if layout.objective_y > 0 and cfg.get("show_objectives", True) else None
    last_turkish = ""    # do not repeat in the log

    def reading_time(tr: str) -> float:
        return min(ttl, max(linger, len(tr) / 15))

    def show(track: Track, tr: str, duration: float, new: bool = False, moving: bool = False) -> None:
        """moving: only the box's place changed (the game scrolled the subtitle up); the display time is not reset."""
        if not moving:
            track.shown_tr, track.shown_at = tr, time.monotonic()
        track.end_time = time.monotonic() + duration
        msg = {"type": "show", "id": track.tv_id, "text": tr, "ttl": round(duration, 2), "new": new, **style_}
        if track.bbox is not None:
            msg["box"] = [round(v, 4) for v in track.bbox]
            msg["lines"] = track.line_count
            msg["lineh"] = line_height(cfg, track.name)
        if track.color:
            msg["color"] = track.color
        tv.send(msg)

    colors = (glossary.colors if glossary else {}) if cfg.get("speaker_colors", True) else {}

    def speaker_color(track: Track, text: str) -> str | None:
        """Who says the line (from the pack; the first known sentence counts) and their color. None (white) if unknown."""
        if not colors or pack_ is None or track.name != "speech":
            return None
        speaker = pack_.speaker_by_sentence(text)
        if not speaker:
            return None
        return colors.get(speaker) or colors.get("character")

    def append_record(filename: str, data_: dict, folder: Path = FRAME_DIR) -> None:
        try:
            folder.mkdir(exist_ok=True)
            with open(folder / filename, "a", encoding="utf-8") as f:
                f.write(json.dumps(data_, ensure_ascii=False) + "\n")
        except OSError as e:
            log(f"Kayıt yazılamadı: {e}")

    def log_line(track: Track, full_: str, piece_: str, tr: str, source: str, ms: float) -> None:
        """Every translated line goes to pc/lines/<day>.jsonl (the game pack is made from it; independent of save_frames,
        always on)."""
        append_record(time.strftime("%Y-%m-%d") + ".jsonl",
              {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "game": glossary.game_title if glossary else cfg.get("game", ""),
               "track": track.name, "en": full_, "part": piece_, "tr": tr, "source": source, "ms": round(ms)},
              paths.LINES_DIR)
        if tr != "-":  # lines no pack had go to the shared pool (if on)
            line_pool.add(glossary.game_title if glossary else cfg.get("game", ""), full_, track.name, source)

    def save_frames(img, reading: O.Reading) -> None:
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        try:
            img.save(FRAME_DIR / f"{name}.png")
        except OSError as e:
            log(f"Kare kaydedilemedi: {e}")
            return
        append_record("readings.jsonl", {"file": f"{name}.png", "reading": reading.txt, "objective": reading.objective,
                                 "prep": reading.prep, "raw": reading.raw_, "box": reading.bbox,
                                 "objective_box": reading.objective_box, "timings": reading.timings})

    async def translate_and_send(track: Track, my_gen: int, en_key: str, text: str, new: bool,
                                 t_decision: float, wait_ms: float) -> None:
        t0 = time.perf_counter()
        try:
            tr, src = await asyncio.to_thread(translator.translate, text)
        except Exception as e:
            log(f"Çeviri hatası: {e}")
            if my_gen == track.gen:
                track.stabilizer.forget()  # retried a bit later while the line stays on screen
            if not cfg["show_original_on_error"]:
                return
            tr, src = text, "orijinal"
        track.recent_tr[en_key] = tr
        if len(track.recent_tr) > 50:
            track.recent_tr.pop(next(iter(track.recent_tr)))
        log_line(track, en_key, text, tr, src, (time.perf_counter() - t0) * 1000)
        if my_gen <= track.sent_gen:
            return  # a newer text's translation is already shown
        if tr.strip() == "-":
            log(f"[atlandı] {text}")
            notify("skipped", txt=text, track=track.name)
            return
        if M.text_key(tr) == M.text_key(text):
            log(f"[aynı kaldı, gösterilmedi] {text}")  # place name, exclamation: the box must not show the same English
            return
        now = time.perf_counter()
        ms = (now - t0) * 1000
        total = wait_ms + (now - t_decision) * 1000
        track.sent_gen = my_gen
        current = my_gen == track.gen and track.stabilizer.sent is not None
        track.color = speaker_color(track, text)
        show(track, tr, ttl if current else reading_time(tr), new)
        label = ("[TR]" if current else "[TR geç]") if track.name == "speech" else "[Görev TR]"
        if track.block:
            label = label[:-1] + f" {track.tv_id}]"
        log(f"{label} {tr}   (çeviri {ms:.0f} ms, ilk görülmeden {total:.0f} ms, {src})")
        notify("tr", txt=tr, ms=round(ms), source=src, track=track.name)
        if save:
            append_record("translations.jsonl", {"time": time.strftime("%H:%M:%S"), "track": track.name, "en": text, "tr": tr,
                                      "source": src, "translate_ms": round(ms), "total_ms": round(total)})

    def start(track: Track, en_key: str, text: str, new: bool, wait_ms: float = 0.0) -> None:
        task = asyncio.create_task(translate_and_send(track, track.gen, en_key, text, new,
                                                      time.perf_counter(), wait_ms))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def process(track: Track, decision, reading: O.Reading, img) -> None:
        if decision.kind in ("send", "correct"):
            track.gen += 1
            full_ = decision.txt
            if decision.kind == "send":
                part = M.new_part(track.shown_en, full_) if track.shown_en else None
                if part:
                    # The game kept the previous line on top and added a new one below: only the new one is translated, the previous
                    # translation stays dimmed above on the TV.
                    to_translate, new, track.base_en = part, True, track.shown_en
                elif track.shown_en and track.stabilizer.is_extension(full_, track.shown_en):
                    to_translate, new, track.base_en = full_, False, ""  # the same sentence got longer: written in its place
                else:
                    to_translate, new, track.base_en = full_, True, ""
                if track.block and (not track.clear_above or track.end_time <= time.monotonic()):
                    # The previous line stays dimmed only while the box is still visible and no other subtitle is above it.
                    new = False
            else:
                part = M.new_part(track.base_en, full_) if track.base_en else None
                to_translate, new = part or full_, False
            track.shown_en = full_
            if track.name == "speech":
                label = "[EN]" if decision.kind == "send" else "[EN düzeltme]"
                if to_translate != full_:
                    label += " +"
                if track.block:
                    label = label.replace("]", f" {track.tv_id}]", 1)
                notify("en", txt=full_, correction=decision.kind == "correct")
            else:
                label = "[Görev]"
            log(f"{label} {to_translate}   ({reading.prep} {reading.timings}, {decision.wait_ms:.0f} ms bekledi)")
            if save:
                save_frames(img, reading)
            if audio_buffer is not None and track.name == "speech" and decision.kind == "send":
                audio_clip(track, to_translate, time.monotonic() - decision.wait_ms / 1000)
            start(track, full_, to_translate, new, decision.wait_ms)
        elif decision.kind == "back":
            track.gen += 1
            track.shown_en, track.base_en = decision.txt, ""
            tr = track.recent_tr.get(decision.txt)
            if tr and tr != "-":
                track.sent_gen = track.gen
                log(f"[geri geldi] {tr}")
                track.color = speaker_color(track, decision.txt)
                show(track, tr, ttl)
                notify("tr", txt=tr, ms=0, source="geri geldi", track=track.name)
            elif tr is None:
                start(track, decision.txt, decision.txt, False)
        elif decision.kind == "clear":
            track.gen += 1
            track.shown_en = track.base_en = ""
            if track is speech:
                notify("clear")  # in block mode process_blocks notifies once all are gone
            if track.shown_tr and not track.hidden:
                # Do not clear at once: keep it a little longer so the translation can be read.
                remain = reading_time(track.shown_tr) - (time.monotonic() - track.shown_at)
                show(track, track.shown_tr, max(linger, remain))
                track.shown_tr = ""
        elif (track.shown_tr and track.stabilizer.sent is not None
              and time.monotonic() - track.shown_at > ttl / 2):
            show(track, track.shown_tr, ttl)  # text still on screen: refresh so the TV timer does not run out

    # ---- separate subtitle mode: every subtitle block on screen has its own slot (stabilizer) and its own TV box.
    slots: list[Track] = []
    prev_active = [False]

    def y_overlap(a, b) -> float:
        """Vertical overlap of two boxes, as a share of the shorter one's height."""
        top, bottom = max(a[1], b[1]), min(a[1] + a[3], b[1] + b[3])
        return max(0.0, bottom - top) / max(1e-6, min(a[3], b[3]))

    def slot_text(slot: Track) -> str:
        g = slot.stabilizer.sent
        return g.txt if g is not None else slot.last_reading

    def same_subtitle(slot: Track, txt: str) -> bool:
        ref = slot_text(slot)
        d = slot.stabilizer
        # Strict similarity: "Final warning, stop your vehicle!" and "This is your final warning! Stop!" are separate
        # subtitles. A garbled read is caught by the position match.
        return bool(ref) and (d.is_same(txt, ref) or d.is_extension(txt, ref))

    def split_by_known(blocks_: list) -> list:
        """Two separate subtitles whose gap was measured just under the threshold and taken for one (the measurement
        varies ±0.003 per frame; wrapped row ~0.044, separate subtitle ~0.053): if the upper or lower part is a
        subtitle already tracked on screen, it is split there. Otherwise the same line was translated and shown twice,
        in its own box and as the "continuation" of the one above."""
        if not slots:
            return blocks_
        limit = layout.block_step - 0.003

        def split_rows(rows_: list) -> list:
            for k in range(1, len(rows_)):
                if rows_[k].y - rows_[k - 1].y <= limit:
                    continue
                top, bottom = O.joined(rows_[:k]), O.joined(rows_[k:])
                if any(same_subtitle(slot, top) or same_subtitle(slot, bottom) for slot in slots):
                    return [O.Block(top, O.bounding(rows_[:k]), k, rows_[:k]), *split_rows(rows_[k:])]
            return [O.Block(O.joined(rows_), O.bounding(rows_), len(rows_), rows_)]

        return [p for b in blocks_ for p in (split_rows(b.rows) if len(b.rows) >= 2 else [b])]

    def match_blocks(blocks_: list) -> dict:
        """block index -> slot. First by text (found even if the game scrolled the subtitle up), then the slot in the
        same place (the text was read garbled or a new one replaced it)."""
        pairs = [(M.similarity(b.txt, slot_text(slot)), i, slot)
                   for i, b in enumerate(blocks_) for slot in slots if same_subtitle(slot, b.txt)]
        assigned: dict = {}
        for _, i, slot in sorted(pairs, key=lambda c: -c[0]):
            if i not in assigned and all(z is not slot for z in assigned.values()):
                assigned[i] = slot
        for i, b in enumerate(blocks_):
            if i in assigned:
                continue
            for slot in slots:
                if (all(z is not slot for z in assigned.values()) and slot.bbox is not None
                        and y_overlap(b.bbox, slot.bbox) >= 0.5):
                    assigned[i] = slot
                    break
        return assigned

    def new_slot() -> Track | None:
        taken = {slot.tv_id for slot in slots}
        for n in range(6):
            if f"k{n}" not in taken:
                slot = Track("speech", lexicon, f"k{n}")
                slots.append(slot)
                return slot
        return None

    def release_slot(slot: Track) -> None:
        slot.gen += 1
        slot.sent_gen = 10 ** 9  # its translation in flight must no longer be shown (the slot may move to another block)
        slots.remove(slot)

    def process_blocks(reading: O.Reading, img) -> None:
        blocks_ = split_by_known(reading.blocks_) if reading.txt else []
        assigned = match_blocks(blocks_)
        now_ = time.monotonic()
        active: list[Track] = []
        for i, b in enumerate(blocks_):
            slot = assigned.get(i) or new_slot()
            if slot is None:
                continue
            old = slot.bbox
            slot.bbox, slot.line_count, slot.last_reading = b.bbox, max(1, b.line_count), b.txt
            slot.signature = O.box_signature(img, b.bbox, layout)
            active.append(slot)
            if slot.hidden:
                # It was hidden because it was not read for a frame; its text is still in place (or moved): show it again.
                slot.hidden = False
                if slot.stabilizer.sent is not None and slot.shown_tr:
                    show(slot, slot.shown_tr, ttl, moving=True)
            elif old is not None and slot.end_time > now_ and abs((old[1] + old[3]) - (b.bbox[1] + b.bbox[3])) > 0.01:
                # The game scrolled the subtitle (a new one came below): move its TV box with it.
                txt = slot.shown_tr or slot.recent_tr.get(slot.shown_en, "")
                if txt and txt != "-":
                    show(slot, txt, slot.end_time - now_, moving=True)
        visible = [z for z in slots if z.bbox is not None
                   and (any(z is a for a in active) or (z.end_time > now_ and not z.hidden))]
        for slot in active:
            slot.clear_above = not any(o is not slot and o.bbox[1] < slot.bbox[1] for o in visible)
        for slot in active:
            process(slot, slot.stabilizer.feed(slot.last_reading, False), reading, img)
        for slot in [z for z in slots if all(z is not a for a in active)]:
            unchanged = slot.bbox is not None and O.text_in_place(O.box_signature(img, slot.bbox, layout), slot.signature)
            process(slot, slot.stabilizer.feed("", unchanged), reading, img)
        for slot in [z for z in slots if all(z is not a for a in active)]:
            if slot.bbox is not None and slot.end_time > now_ and any(y_overlap(slot.bbox, a.bbox) >= 0.3 for a in active):
                # Another subtitle is in its place now: hide the old box at once. (If it was a one-frame miss the slot comes back
                # when its text is read again; otherwise its stabilizer clears it shortly.)
                tv.send({"type": "clear", "id": slot.tv_id})
                slot.end_time, slot.hidden = now_, True
            if slot.stabilizer.sent is None and slot.end_time <= now_:
                release_slot(slot)  # its text is gone and its TV box has expired
        if prev_active[0] and not active:
            notify("clear")
        prev_active[0] = bool(active)

    pending: set[asyncio.Task] = set()
    audio_state = {"geldi": False, "uyarildi": False}

    first_seen: list[float] = []  # when the latest speech lines were first seen (to cut the audio window)

    def audio_window_end(t_first: float, end_time: float) -> float:
        """Until the start of the next line that started within [t_first, end_time]."""
        following = [t for t in first_seen if t_first + 0.3 < t < end_time]
        return min(following) - 0.1 if following else end_time

    def audio_clip(track: Track, en: str, t_first: float) -> None:
        """The line's audio: after RECOGNIZE_AFTER the speaker is recognized by voice (if the pack knows the speaker, their
        profile learns from it; if not and the voice is close enough to a known character, the box turns that
        character's color). A bit later the clip from shortly before the line was first seen until CLIP_AFTER is
        saved (to rebuild the profiles)."""
        import audio as S

        first_seen.append(t_first)
        del first_seen[:-30]
        known = pack_.speaker_by_sentence(en) if pack_ is not None else None

        async def recognize(until: float):
            """Voice vector of [t_first - 0.1, until] (cut where the next line started); None if unusable."""
            pcm, channels, rate = audio_buffer.segment(t_first - 0.1, audio_window_end(t_first, until))
            mono = await asyncio.to_thread(S.mono16k, pcm, channels, rate)
            try:
                return await asyncio.to_thread(recognizer.vector, mono)
            except Exception as e:
                log(f"Ses vektörü çıkarılamadı: {e}")
                return None

        def paint(who: str, sim: float, margin: float, late: bool) -> None:
            log(f"[ses {track.tv_id or track.name}{' geç' if late else ''}] {who} ({sim:.2f}, fark {margin:.2f}) {en}")
            color = colors.get(who) or colors.get("character")
            if (color and en in track.shown_en and track.shown_tr and not track.hidden
                    and track.color is None and track.end_time > time.monotonic()):
                track.color = color
                show(track, track.shown_tr, track.end_time - time.monotonic(), moving=True)

        async def job() -> None:
            voice_speaker, similarity = None, 0.0
            if recognizer is not None:
                await asyncio.sleep(max(0.0, t_first + RECOGNIZE_AFTER - time.monotonic()) + 0.1)
                v = await recognize(t_first + RECOGNIZE_AFTER)
                if v is not None and known:
                    if recognizer.learn(known, v):
                        line_pool.add_voice(glossary.game_title if glossary else cfg.get("game", ""), known, v)
                elif v is not None:
                    voice_speaker, similarity, margin = recognizer.speaker(v)
                    if voice_speaker:
                        paint(voice_speaker, similarity, margin, False)
            await asyncio.sleep(max(0.0, t_first + CLIP_AFTER - time.monotonic()) + 0.3)
            if recognizer is not None and not known and not voice_speaker:
                # Second chance with the longer clip (more speech, fewer undecided lines); the color comes later.
                v = await recognize(t_first + CLIP_AFTER)
                if v is not None:
                    voice_speaker, similarity, margin = recognizer.speaker(v)
                    if voice_speaker:
                        paint(voice_speaker, similarity, margin, True)
            pcm, channels, rate = audio_buffer.segment(t_first - CLIP_BEFORE, t_first + CLIP_AFTER)
            mono = await asyncio.to_thread(S.mono16k, pcm, channels, rate)
            if len(mono) < (CLIP_BEFORE + CLIP_AFTER) * S.TARGET_RATE:  # less than half of what was expected: no audio arrives
                if not audio_state["geldi"] and not audio_state["uyarildi"]:
                    audio_state["uyarildi"] = True
                    log("PS5'ten ses gelmiyor; konuşanı sesten tanıma çalışmayacak.")
                return
            if not audio_state["geldi"]:
                audio_state["geldi"] = True
                log(f"Oyunun sesi de alınıyor ({rate} Hz, {channels} kanal); replik başına ses parçası kaydediliyor. "
                    "TV'nin sesi kesildiyse Ayarlar'dan 'Sesten tanıma'yı kapat.")
            name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.wav"
            try:
                await asyncio.to_thread(S.write_wav, paths.CLIPS / name, mono)
            except OSError as e:
                log(f"Ses parçası kaydedilemedi: {e}")
                return
            now_ = time.monotonic()
            record = {"file": name, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "game": game_name, "en": en,
                     "speaker": known, "loudness": round(S.loudness(mono)),
                     # clip start = CLIP_BEFORE before first seen; how long after first seen the next line started
                     "first": round(time.time() - (now_ - t_first), 3), "lead": CLIP_BEFORE,
                     "end": round(audio_window_end(t_first, t_first + CLIP_AFTER) - t_first, 3)}
            if voice_speaker:
                record.update(voice_speaker=voice_speaker, similarity=round(similarity, 3))
            append_record(paths.CLIP_LIST, record, paths.VOICES)

        task = asyncio.create_task(job())
        pending.add(task)
        task.add_done_callback(pending.discard)

    hwnd = None
    ps5 = None          # direct PS5 stream (instead of the Remote Play window)
    ps5_status = ""
    if cfg.get("connection") == "direct":
        if not cfg.get("ps5_ip"):
            raise ConfigError("Doğrudan bağlantı için PS5 IP adresi gerekli (Ayarlar > PS5 IP adresi).")
        from ps5_direct import PS5Stream, is_paired, attach_log

        attach_log(log)
        if not is_paired():
            raise ConfigError("PS5 henüz eşlenmedi. Bağlantılar'daki PS5 satırında 'Eşle' ile eşle ya da Ayarlar'dan "
                             "görüntü kaynağını 'Remote Play penceresi' yap.")
        if cfg.get("voice_recognition", True):
            from audio import AudioBuffer

            audio_buffer = AudioBuffer()
            try:
                from audio import SpeakerRecognizer

                recognizer = SpeakerRecognizer.open(paths.VOICES, game_name)
            except Exception as e:
                log(f"Sesten tanıma açılamadı: {e}")
            if recognizer is not None:
                log("Konuşanı sesinden tanıma açık; ses profilleri: " + (recognizer.summary() or "henüz yok (paketteki "
                    "etiketli repliklerden öğrenilecek)") + ".")
        ps5 = PS5Stream(cfg["ps5_ip"], str(cfg.get("ps5_resolution") or "1080p"), log=log, audio=audio_buffer)
        ps5.start()
        log("PS5'e doğrudan bağlanılıyor (Remote Play uygulaması gerekmez; açıksa kapat)...")
    log("Çalışıyor." if _log_hook else "Çalışıyor. Durdurmak için Ctrl+C.")
    interval = cfg["interval_ms"] / 1000
    try:
        while True:
            started = time.perf_counter()
            if ps5 is not None:
                # Direct PS5: the frame comes from its own stream (no window).
                if ps5.status != ps5_status:
                    ps5_status = ps5.status
                    notify("window", was_found=ps5_status == "connected", name="PS5 (doğrudan)", direct=True,
                           status=ps5_status, err=ps5.err)
                full_ = await asyncio.to_thread(ps5.latest_frame)
                if full_ is None:
                    await asyncio.sleep(0.3)
                    continue
                vr = fit_16_9(*full_.size)
                bbox = subtitle_box(vr, cfg["region"])
                img = full_.crop((bbox["left"], bbox["top"], bbox["left"] + bbox["width"], bbox["top"] + bbox["height"]))
            else:
                if hwnd is None or not user32.IsWindow(hwnd):
                    found = find_window(cfg["window_titles"])
                    if not found:
                        if hwnd is not False:
                            log("Remote Play penceresi bulunamadı; açılmasını bekliyorum...")
                            notify("window", was_found=False)
                        hwnd = False
                        await asyncio.sleep(2)
                        continue
                    hwnd = found[0]
                    log(f"Pencere bulundu: {found[1]}")
                    notify("window", was_found=True, name=found[1])

                vr = video_rect(hwnd)
                if vr is None:
                    await asyncio.sleep(0.5)
                    continue  # minimized
                try:
                    img = grab(hwnd, subtitle_box(vr, cfg["region"]))
                except Exception as e:
                    log(f"Okuma hatası: {e}")
                    await asyncio.sleep(1)
                    continue
            try:
                reading = await reader.read(img, vr[3])
            except Exception as e:
                log(f"Okuma hatası: {e}")
                await asyncio.sleep(1)
                continue

            if reading.turkish and reading.raw_ != last_turkish:
                last_turkish = reading.raw_
                log(f"[Türkçe yazı, atlandı] {reading.raw_[:80]}")

            tracks = [] if block_mode else [(speech, reading.txt, reading.bbox, reading.line_count, reading.line_h)]
            if block_mode:
                process_blocks(reading, img)
            if objective is not None:
                tracks.append((objective, reading.objective, reading.objective_box, reading.objective_line, reading.objective_line_h))
            for track, txt, bbox, line_count, line_h in tracks:
                if txt:
                    track.bbox, track.line_count, track.line_h = bbox, max(1, line_count), line_h
                    track.signature = O.box_signature(img, bbox, layout)
                    unchanged = False
                else:
                    unchanged = track.bbox is not None and O.text_in_place(O.box_signature(img, track.bbox, layout),
                                                                         track.signature)
                process(track, track.stabilizer.feed(txt, unchanged), reading, img)

            await asyncio.sleep(max(0.01, interval - (time.perf_counter() - started)))
    finally:
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if ps5 is not None:
            await asyncio.to_thread(ps5.stop)
        if recognizer is not None:
            try:
                recognizer.save()  # voice profiles learned while playing
            except OSError as e:
                log(f"Ses profilleri kaydedilemedi: {e}")
        tv.close()
        translator.save()
        await asyncio.to_thread(line_pool.close)


# ---------------------------------------------------------------- test commands

def calibrate(cfg: dict) -> None:
    found = find_window(cfg["window_titles"])
    if not found:
        sys.exit("Remote Play penceresi bulunamadı. --pencereler ile başlıkları kontrol et.")
    vr = video_rect(found[0])
    if vr is None:
        sys.exit("Pencere simge durumunda; aç ve tekrar dene.")
    hwnd = found[0]
    x, y, w, h = vr
    full = grab(hwnd, {"left": x, "top": y, "width": w, "height": h})
    print(f"Pencere: {found[1]}  görüntü: {w}x{h}")
    read_image(cfg, full, save=True)


def read_image(cfg: dict, full, save: bool = False) -> None:
    """Reads the subtitle region of a 16:9 game frame with both preparations and prints the chosen one."""
    from PIL import ImageDraw

    x, y, w, h = fit_16_9(*full.size)
    full = full.convert("RGB").crop((x, y, x + w, y + h))
    box = subtitle_box((0, 0, w, h), cfg["region"])
    crop = full.crop((box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"]))
    glossary, lexicon = make_parts(cfg)
    layout = make_layout(cfg, glossary)
    reader = O.Reader(make_ocr_engine(), lexicon, float(cfg.get("ocr_scale") or 0), layout)
    if save:
        marked = full.copy()
        ImageDraw.Draw(marked).rectangle(
            [box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"]],
            outline=(255, 0, 0), width=4)
        marked.save(HERE / "calibration.png")
        reader.prepare(crop, "raw", h).save(HERE / "calibration-region.png")
        reader.prepare(crop, "white", h).save(HERE / "calibration-white.png")
        print("calibration.png (kırmızı kutu = okunan bölge), calibration-region.png ve calibration-white.png kaydedildi.")

    async def both():
        for hz in ("raw", "white"):
            lines_ = await O.ocr_lines(reader.engine, reader.prepare(crop, hz, h))
            print(f"[{hz}] satırlar: {[s.txt for s in lines_]}")
            ay = O.classify(lines_, layout)
            print(f"[{hz}] konuşma: {O.joined(ay.speech) or '(boş)'}   görev: {O.joined(ay.objective) or '-'}"
                  f"   atılan: {[s.txt for s in ay.dropped]}")
        r = await reader.read(crop, h)
        if r.turkish:
            status = "Türkçe yazı, atlanır"
        elif not r.txt:
            status = "altyazı yok"
        else:
            status = "clean" if lexicon.is_clean(r.txt) else "garbled" if lexicon.is_garbled(r.txt) else "kısmen temiz"
        print(f"Seçilen [{r.prep}]: {r.txt or '(boş)'}   ({status}, {r.timings} ms)")
        if r.objective:
            print(f"Görev: {r.objective}")

    asyncio.run(both())


def try_translate(cfg: dict, text: str) -> None:
    glossary, _ = make_parts(cfg)
    c = Translator(cfg, glossary, log=log, pack_=make_pack(cfg, glossary))
    c.warmup()
    t0 = time.perf_counter()
    tr, src = c.translate(text)
    print(f"{tr}   ({(time.perf_counter() - t0) * 1000:.0f} ms, {src})")
    c.save()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    set_dpi_aware()
    p = argparse.ArgumentParser(description="Oyun altyazılarını Türkçeye çevirip TV'de gösterir.")
    p.add_argument("--windows", action="store_true", help="açık pencere başlıklarını listele")
    p.add_argument("--calibrate", action="store_true", help="okunan bölgenin görüntüsünü kaydet")
    p.add_argument("--image", metavar="FILE", help="kaydedilmiş bir oyun karesini oku (16:9 ekran görüntüsü)")
    p.add_argument("--translate", metavar="TEXT", help="bir İngilizce satırı çevir")
    p.add_argument("--tv-test", metavar="TEXT", help="TV'ye deneme yazısı gönder")
    args = p.parse_args()
    cfg = load_config()

    if args.windows:
        for _, title in list_windows():
            print(title)
    elif args.calibrate:
        calibrate(cfg)
    elif args.image:
        from PIL import Image

        read_image(cfg, Image.open(args.image))
    elif args.translate is not None:
        try_translate(cfg, args.translate)
    elif args.tv_test is not None:
        if not cfg["tv_ip"]:
            sys.exit("config.json içinde tv_ip boş.")
        from checks import send_tv_text

        ok = send_tv_text(cfg["tv_ip"], args.tv_test, cfg)
        print("Gönderildi, TV'ye bak." if ok else "Gönderilemedi.")
    else:
        try:
            asyncio.run(run(cfg))
        except KeyboardInterrupt:
            print("Durduruldu.")
        except ConfigError as e:
            sys.exit(str(e))


if __name__ == "__main__":
    try:
        main()
    except ConfigError as e:
        sys.exit(str(e))
