"""Nightly pool pack, run on our own PC with a GPU (the Legion): `Dilmac.exe --nightly` (or py pc\\nightly.py).

1. Downloads the current packs (so lines already in a pack are known) and the pool's waiting lines (admin token).
2. Per game: lines a pack already has are marked done; OCR garbage is dropped (too many junk words, or the model
   answers "-"); the rest are translated with this PC's engine (Gemma through Ollama: the game's glossary and the
   pack's similar lines go into the prompt, like when playing). Nothing from the Google fallback goes into a pack:
   such lines wait for the next night.
3. The translations are added to the game's pool pack (pool-<game>, version up) and published; the lines are marked.

--install-nightly registers a Windows scheduled task that runs this every EVERY_MINUTES minutes (2026-11-27: not only
at night; a line played in the afternoon is in the pool pack minutes after the game is closed, and the app fetches it at
the next start). With an empty pool a run is two small requests and writes nothing to the log (--quiet).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pack_server as S
import paths
import textutil as M
from pack import Pack
from translation import Glossary, Translator

TASK_NAME = "Dilmac gece paketi"
MAX_PER_GAME = 4000   # lines a run per game (GTA-sized games keep growing run by run)
EVERY_MINUTES = 10    # how often the scheduled task looks at the pool


def _log_default(msg: str) -> None:
    print(msg, flush=True)


def game_title(game_key: str) -> str:
    for g in Glossary.all_glossaries():
        if M.text_key(g.game_title) == game_key or any(M.text_key(a) == game_key for a in g.names):
            return g.game_title
    return game_key.title()


def junk(en: str) -> bool:
    words = M.normalize_text(en).split()
    return not words or M.junk_count(en) > max(1, len(words) // 3) or len(M.text_key(en)) < 3


_LINK = re.compile(r"(https?://|www\.|\b[\w.-]+@[\w-]+\.[a-z]{2,}|\b[\w-]+\.(com|net|org|io|gg|tr|ru|xyz)\b)", re.I)


def safe_output(en: str, tr: str) -> bool:
    """A translation of a pool line (text anyone could send) must not bring anything new: no links or addresses the
    English did not have, no control characters, no runaway length."""
    if len(tr) > 3 * len(en) + 60 or re.search(r"[\x00-\x1f\x7f‪-‮⁦-⁩]", tr):
        return False
    return not any(m.group(0).lower() not in en.lower() for m in _LINK.finditer(tr))


def _wait_engine(cfg: dict, log) -> bool:
    """Gemma needs Ollama: started if it is not running."""
    if cfg.get("engine") != "local":
        return True
    import checks as B

    url, model = cfg.get("local_url", "http://127.0.0.1:11434"), cfg.get("local_model", "")
    if B.check_ollama(url, model).level == "ok":
        return True
    B.start_ollama()
    for _ in range(60):
        time.sleep(2)
        if B.check_ollama(url, model).level == "ok":
            return True
    log(f"Ollama ya da '{model}' modeli hazır değil; gece çevirisi yapılamadı.")
    return False


def run(cfg: dict, log=_log_default, max_per_game: int = MAX_PER_GAME, quiet: bool = False) -> dict:
    """One run's work; returns a summary per game. quiet: nothing logged when the pool is empty."""
    summary: dict[str, dict] = {}
    try:
        S.sync_packs(cfg, log, force=True)
    except S.ServerError as e:
        log(f"Paketler indirilemedi ({e}); yine de devam ediliyor.")
    waiting = S.admin_lines(cfg, limit=50_000)
    if not waiting:
        if not quiet:
            log("Havuzda bekleyen satır yok.")
        return summary
    log(f"Havuzda {len(waiting)} satır bekliyor; çeviri başladı.")
    if not _wait_engine(cfg, log):
        return summary
    by_game: dict[str, list[dict]] = {}
    for line in waiting:
        by_game.setdefault(line["game"], []).append(line)
    for gkey, lines in by_game.items():
        title = game_title(gkey)
        glossary = Glossary.load(title)
        if glossary:
            title = glossary.game_title
        pack_ = Pack.load(title, glossary.names if glossary else None)
        # The pack's own lines come first; the model sees no Google detour (a late model reply is waited for).
        tr = Translator({**cfg, "local_hedge_ms": 120_000, "hedge_ms": 60_000, "opus_hedge_ms": 60_000},
                        glossary, log=log, cache_data=None, pack_=pack_)
        pid = S.pack_id_for(title, "pool-")
        current_file = paths.SERVER_PACKS / f"{pid}.json"
        try:
            current = json.loads(current_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        out_lines: dict[str, str] = dict(current.get("lines") or {})
        known, junk_ids, done_ids, translated = [], [], [], 0
        lines.sort(key=lambda x: (-int(x.get("installs") or 1), int(x["id"])))  # lines more players saw first
        t0 = time.monotonic()
        for line in lines[:max_per_game]:
            # "JASON: ..." -> the line without the name (the app looks packs up without it).
            en = M.split_speaker(M.normalize_text(line["en"]))[1]
            if pack_ is not None and pack_.lookup(en):
                known.append(line["id"])
                continue
            if junk(en):
                junk_ids.append(line["id"])
                continue
            try:
                text, source = tr.translate(en)
            except Exception as e:
                log(f"Çevrilemedi ({e}): {en[:60]}")
                continue
            if source == "Google":
                continue  # the model was not reached: try again next night
            if text == "-":
                junk_ids.append(line["id"])
                continue
            if not safe_output(en, text):  # e.g. a web address the line itself did not have (prompt injection)
                junk_ids.append(line["id"])
                continue
            out_lines[en] = text
            done_ids.append(line["id"])
            translated += 1
        if translated:
            data = {"game": title, "source": "pool", "updated": time.strftime("%Y-%m-%d"), "lines": out_lines}
            pack = S.publish_pack(cfg, pid, data, title=f"{title} (havuz)", auto=True)
            log(f"{title}: {translated} yeni satır çevrildi, havuz paketi {len(out_lines)} satır "
                f"(sürüm {pack.get('version')}).")
            current_file.parent.mkdir(parents=True, exist_ok=True)
            current_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if known or done_ids:
            S.admin_set_state(cfg, known + done_ids, 1)
        if junk_ids:
            S.admin_set_state(cfg, junk_ids, 2)
        summary[title] = {"translated": translated, "known": len(known), "junk": len(junk_ids),
                          "left": max(0, len(lines) - max_per_game), "seconds": round(time.monotonic() - t0)}
        log(f"{title}: pakette zaten olan {len(known)}, çöp {len(junk_ids)}, çevrilen {translated}.")
    return summary


def task_script(exe: Path, script: Path | None, every_minutes: int = EVERY_MINUTES) -> str:
    """PowerShell that registers the task: from now on every every_minutes minutes, with no end (no
    -RepetitionDuration); a run still going when the next is due makes Windows skip that one (IgnoreNew)."""
    args = "--nightly --quiet" if script is None else f'"{script}" --nightly --quiet'
    return (f"$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '{args}' -WorkingDirectory '{exe.parent}'; "
            f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
            f"-RepetitionInterval (New-TimeSpan -Minutes {int(every_minutes)}); "
            "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 4) "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew; "
            f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null")


def install_task(exe: Path | None = None) -> bool:
    """Windows scheduled task for this user: every EVERY_MINUTES minutes; runs later if the PC was off."""
    exe = exe or Path(sys.executable)
    script = None if getattr(sys, "frozen", False) else Path(__file__).resolve()
    if script is not None and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").is_file():
        exe = exe.with_name("pythonw.exe")  # no console window popping up every few minutes (the log goes to log.txt)
    r = subprocess.run(["powershell", "-NoProfile", "-Command", task_script(exe, script)], capture_output=True,
                       text=True, creationflags=0x08000000)
    return r.returncode == 0


# Shared voice profiles. A network's vectors for a character count only if their average is this close to our own
# profile of that character (and closer to it than to any other): party chat or a wrong label does not get in.
VOICE_ACCEPT = 0.6
VOICE_MARGIN = 0.05
VOICE_MIN_ROW = 3     # a network's row needs this many clips
VOICE_CAP = 50        # and counts as at most this many, so one network cannot make a profile its own


def _unit(v):
    import numpy as np

    a = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(a))
    return a / n if n > 0 else None


def merge_voices(cfg: dict, log=_log_default) -> dict:
    """Our published profiles (voices-<game>, from the main PC's clips) plus the pool's checked rows ->
    voices-pool-<game>. The app uses, per character, the profile with more clips, so the merged one wins once it has
    more. Published only when it changed."""
    import audio

    rows = S.admin_voices(cfg)
    by_game: dict[str, list[dict]] = {}
    for r in rows:
        by_game.setdefault(r["game"], []).append(r)
    summary = {}
    for gkey, rs in by_game.items():
        title = game_title(gkey)
        glossary = Glossary.load(title)
        if glossary:
            title = glossary.game_title
        try:
            base = json.loads((paths.SERVER_PACKS / f"{S.pack_id_for(title, 'voices-')}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # no profiles of ours yet: nothing to check the pool against
        base_voices = base.get("voices") or {}
        dirs = {k: _unit(p["sum"]) for k, p in base_voices.items() if p.get("n", 0) >= audio.MIN_PROFILE_SAMPLES}
        dirs = {k: d for k, d in dirs.items() if d is not None}
        merged = {k: {"sum": list(map(float, p["sum"])), "n": int(p["n"])} for k, p in base_voices.items()}
        taken = refused = 0
        for r in rs:
            d = dirs.get(r["speaker"])
            m = _unit(r["sum"]) if d is not None and len(r["sum"]) == len(d) else None
            if m is None or int(r["n"]) < VOICE_MIN_ROW:
                continue
            own = float(m @ d)
            other = max((float(m @ x) for k, x in dirs.items() if k != r["speaker"] and len(x) == len(m)), default=0.0)
            if own < VOICE_ACCEPT or own - other < VOICE_MARGIN:
                refused += 1
                continue
            w = min(1.0, VOICE_CAP / int(r["n"]))
            p = merged[r["speaker"]]
            p["sum"] = [a + w * float(b) for a, b in zip(p["sum"], r["sum"])]
            p["n"] += max(1, round(w * int(r["n"])))
            taken += 1
        pid = S.pack_id_for(title, "voices-pool-")
        current_file = paths.SERVER_PACKS / f"{pid}.json"
        try:
            current = json.loads(current_file.read_text(encoding="utf-8")).get("voices") or {}
        except (OSError, ValueError):
            current = {}
        counts = {k: p["n"] for k, p in merged.items()}
        summary[title] = {"rows": len(rs), "taken": taken, "refused": refused}
        if not taken or counts == {k: p.get("n") for k, p in current.items()}:
            continue
        voices = {k: {"sum": [round(x, 6) for x in p["sum"]], "n": p["n"]} for k, p in merged.items()}
        data = {"game": title, "source": "voices", "lines": {}, "voices": voices}
        S.publish_pack(cfg, pid, data, title=f"{title} (ortak ses profilleri)", auto=True)
        current_file.parent.mkdir(parents=True, exist_ok=True)
        current_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        log(f"{title}: ortak ses profilleri yayınlandı ({taken} ağ satırı alındı, {refused} reddedildi).")
    return summary


def main(argv: list[str] | None = None) -> None:
    import subtitler as A

    argv = sys.argv[1:] if argv is None else argv
    if "--install-nightly" in argv:
        ok = install_task()
        A.log(f"Havuz görevi {'kuruldu' if ok else 'kurulamadı'}: '{TASK_NAME}', {EVERY_MINUTES} dakikada bir.")
        return
    quiet = "--quiet" in argv
    cfg = A.load_config()
    if not quiet:
        A.log("Gece paketi başladı.")
    try:
        result = run(cfg, A.log, quiet=quiet)
        if result or not quiet:
            A.log(f"Gece paketi bitti: {json.dumps(result, ensure_ascii=False)}")
    except S.ServerError as e:
        A.log(f"Gece paketi yapılamadı: {e}")
    try:
        voices = merge_voices(cfg, A.log)
        if not quiet:
            A.log(f"Ses havuzu: {json.dumps(voices, ensure_ascii=False)}")
    except S.ServerError as e:
        if not quiet:
            A.log(f"Ses havuzuna bakılamadı: {e}")


if __name__ == "__main__":
    main()
