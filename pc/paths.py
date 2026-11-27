"""File locations. In development: the pc/ folder. As an .exe: the pc/ folder next to the exe (our own PCs, a
"portable" copy), otherwise %LOCALAPPDATA%\\Dilmac (installed with Dilmac-Setup.exe: the program folder is not for data,
and settings survive a reinstall).

BUNDLE:  read-only files shipped with the app (glossary, packs, APK). In the .exe, PyInstaller's temp folder.
DATA:    config.json, caches, logs and the user-editable glossary/ and packs/ folders.
PROJECT: the top folder with the README and the TV APK.

Data written by older (Turkish-named) versions is moved and converted once on import, see migrate_old_data().
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    PROJECT = Path(sys.executable).resolve().parent
    BUNDLE = Path(getattr(sys, "_MEIPASS", PROJECT))
    if (PROJECT / "pc").is_dir():
        DATA = PROJECT / "pc"
    elif (PROJECT / "config.json").is_file():
        DATA = PROJECT  # an older copy that kept its data next to the exe
    else:
        DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Dilmac"
        DATA.mkdir(parents=True, exist_ok=True)
else:
    BUNDLE = DATA = Path(__file__).resolve().parent
    PROJECT = BUNDLE.parent

CONFIG = DATA / "config.json"
CACHE_FILE = DATA / "translation-cache.json"
LOG_FILE = DATA / "log.txt"
PAIRING_FILE = DATA / "ps5-pairing.json"     # PSN account id and Remote Play keys: private, never commit
LG_FILE = DATA / "lg-tv.json"                 # LG developer mode SSH key and passphrase: private, never commit
FRAMES = DATA / "frames"                      # recorded subtitle regions (save_frames), for OCR tuning
LINES_DIR = DATA / "lines"                    # every session's translated lines (input for the pack)
VOICES = DATA / "voices"                      # speaker recognition: model/, clips/, clips.jsonl, profiles.json
CLIPS = VOICES / "clips"
CLIP_LIST = "clips.jsonl"
# The user's folder first (editable), then the copy shipped with the app.
GLOSSARY_DIRS = [d for d in dict.fromkeys([DATA / "glossary", BUNDLE / "glossary"]) if d.is_dir()]
PACK_DIRS = [d for d in dict.fromkeys([DATA / "packs", BUNDLE / "packs"]) if d.is_dir()]
# Packs downloaded from the pack server (pack_server.py); between the user's own folder and the shipped copy.
SERVER_PACKS = DATA / "packs-server"


def pack_dirs() -> list[Path]:
    """Pack folders, the first wins for a line: the user's own, the server's, the one shipped with the app."""
    return [d for d in dict.fromkeys([DATA / "packs", SERVER_PACKS, BUNDLE / "packs"]) if d.is_dir()]


def apk() -> Path | None:
    for p in (PROJECT / "dilmac-tv.apk", BUNDLE / "dilmac-tv.apk"):
        if p.is_file():
            return p
    return None


def tv_web_dir() -> Path | None:
    """The TV web app's files (LG / Samsung): the project's tv-web/ folder, or the copy inside the exe."""
    for p in (PROJECT / "tv-web", BUNDLE / "tv-web"):
        if (p / "index.html").is_file():
            return p
    return None


# ---------------------------------------------------------------- one-time migration of old data

_RENAMES = [  # (old, new) relative to DATA; folders before the files inside them
    ("sozluk", "glossary"), ("paket", "packs"), ("kareler", "frames"), ("kayit", "lines"), ("sesler", "voices"),
    ("voices/kayit", "voices/clips"), ("voices/sesler.jsonl", "voices/clips.jsonl"),
    ("voices/profiller.json", "voices/profiles.json"), ("gunluk.txt", "log.txt"), ("gunluk.eski.txt", "log.old.txt"),
    ("ceviri-onbellek.json", "translation-cache.json"), ("ceviri-onbellek.eski.json", "translation-cache.old.json"),
    ("ps5-eslesme.json", "ps5-pairing.json"), ("frames/okumalar.jsonl", "frames/readings.jsonl"),
    ("frames/ceviriler.jsonl", "frames/translations.jsonl"), ("lines/bekleyen.json", "lines/pending.json"),
    ("kalibrasyon.png", "calibration.png"), ("kalibrasyon-bolge.png", "calibration-region.png"),
    ("kalibrasyon-beyaz.png", "calibration-white.png"),
]
LABELS = {"yaya": "pedestrian", "polis": "police", "karakter": "character", "teyze": "aunt", "anne": "mother"}
CONFIG_KEYS = {
    "kare_kaydet": "save_frames", "ses_tanima": "voice_recognition", "kutu_opaklik": "box_opacity",
    "gecis_ms": "fade_ms", "yazi_punto": "font_px", "yazi_olcek": "font_scale", "genislik": "box_width",
    "eski_satir_sn": "old_line_seconds", "gorevleri_goster": "show_objectives", "orta_tol": "center_tol",
    "gorev_y": "objective_y", "bloklari_ayir": "split_blocks", "blok_adim": "block_step",
    "konusan_rengi": "speaker_colors", "baglanti": "connection", "ps5_cozunurluk": "ps5_resolution",
}
_SCREEN_KEYS = {"blok_adim": "block_step", "orta_tol": "center_tol", "gorev_y": "objective_y"}
_GLOSSARY_KEYS = {
    "oyun": "game", "takma_adlar": "aliases", "ekran": "screen", "renkler": "colors", "lakaplar": "nicknames",
    "uslup": "style", "koru": "keep", "hitap": "address_terms", "terimler": "terms", "satirlar": "lines",
}
_PACK_KEYS = {"oyun": "game", "kaynak": "source", "satirlar": "lines", "guncelleme": "updated",
              "konusanlar": "speakers"}
_CACHE_KEYS = {"surum": "version", "istem": "prompt", "cumleler": "sentences"}
_RECORD_KEYS = {  # jsonl records: line logs, frame logs, voice clips
    "oyun": "game", "akis": "track", "parca": "part", "kaynak": "source", "dosya": "file", "zaman": "time",
    "okuma": "reading", "gorev": "objective", "hazirlik": "prep", "ham": "raw", "kutu": "box",
    "gorev_kutu": "objective_box", "sureler": "timings", "ceviri_ms": "translate_ms", "toplam_ms": "total_ms",
    "kim": "speaker", "guc": "loudness", "ilk": "first", "bas": "lead", "son": "end", "ses_kim": "voice_speaker",
    "benzerlik": "similarity", "etiket": "label",
}
_RECORD_VALUES = {"konusma": "speech", "gorev": "objective", "ham": "raw", "beyaz": "white", **LABELS}


def _rename_keys(d: dict, keys: dict) -> dict:
    return {keys.get(k, k): v for k, v in d.items()}


def _record(r: dict) -> dict:
    r = _rename_keys(r, _RECORD_KEYS)
    for k in ("track", "prep", "speaker", "voice_speaker", "label"):
        if isinstance(r.get(k), str):
            r[k] = _RECORD_VALUES.get(r[k], r[k])
    if isinstance(r.get("timings"), dict):
        r["timings"] = _rename_keys(r["timings"], {"ham": "raw", "beyaz": "white"})
    return r


def _convert_json(path: Path, fn) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        new = fn(data)
        if new != data:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(new, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            tmp.replace(path)
    except (OSError, ValueError):
        pass


def _convert_jsonl(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    out = []
    for line in lines:
        try:
            out.append(json.dumps(_record(json.loads(line)), ensure_ascii=False))
        except ValueError:
            out.append(line)
    if out != lines:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
        tmp.replace(path)


def convert_config(cfg: dict) -> dict:
    """Old (Turkish) setting names and values -> current ones. Already converted settings are returned as is."""
    cfg = _rename_keys(cfg, CONFIG_KEYS)
    cfg["connection"] = {"pencere": "window", "dogrudan": "direct"}.get(cfg.get("connection"), cfg.get("connection"))
    if cfg["connection"] is None:
        del cfg["connection"]
    return cfg


def convert_glossary(g: dict) -> dict:
    g = _rename_keys(g, _GLOSSARY_KEYS)
    if isinstance(g.get("screen"), dict):
        g["screen"] = _rename_keys(g["screen"], _SCREEN_KEYS)
    if isinstance(g.get("colors"), dict):
        g["colors"] = {LABELS.get(k, k): v for k, v in g["colors"].items()}
    return g


def convert_pack(p: dict) -> dict:
    p = _rename_keys(p, _PACK_KEYS)
    if isinstance(p.get("speakers"), dict):
        p["speakers"] = {en: LABELS.get(k, k) for en, k in p["speakers"].items()}
    return p


def _convert_pairing(e: dict) -> dict:
    e = _rename_keys(e, {"hesap": "account"})
    if isinstance(e.get("account"), dict):
        e["account"] = _rename_keys(e["account"], {"ad": "name", "kimlik": "id"})
    if isinstance(e.get("ps5"), dict):
        e["ps5"] = {mac: _rename_keys(v, {"ad": "name", "tarih": "date"}) for mac, v in e["ps5"].items()}
    return e


def _convert_profiles(p: dict) -> dict:
    return {game: {LABELS.get(s, s): _rename_keys(v, {"toplam": "sum"}) for s, v in speakers.items()}
            for game, speakers in p.items()}


def migrate_old_data(root: Path | None = None) -> list[str]:
    """Moves and converts data left by the Turkish-named version. Runs once; returns what was done."""
    root = root or DATA
    done = []
    for old, new in _RENAMES:
        o, n = root / old, root / new
        if o.exists() and not n.exists():
            try:
                o.rename(n)
                done.append(f"{old} -> {new}")
            except OSError:
                pass
    marker = root / ".migrated-en"
    if not done and marker.exists():
        return done
    if (root / "config.json").is_file():
        _convert_json(root / "config.json", convert_config)
    for d in (root / "glossary", root / "packs"):
        for f in d.glob("*.json") if d.is_dir() else []:
            _convert_json(f, convert_glossary if d.name == "glossary" else convert_pack)
    for f, fn in ((root / "ps5-pairing.json", _convert_pairing), (root / "voices" / "profiles.json", _convert_profiles)):
        if f.is_file():
            _convert_json(f, fn)
    for f in (root / "translation-cache.json", root / "translation-cache.old.json"):
        if f.is_file():
            _convert_json(f, lambda c: _rename_keys(c, _CACHE_KEYS) if isinstance(c, dict) else c)
    for d in (root / "lines", root / "frames", root / "voices"):
        for f in d.glob("*.jsonl") if d.is_dir() else []:
            _convert_jsonl(f)
    try:
        marker.write_text("1\n", encoding="utf-8")
    except OSError:
        pass
    return done


try:
    migrate_old_data()
except Exception:  # migration must never stop the app from starting
    pass
