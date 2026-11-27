"""Tool that grows the game pack from played sessions.

Flow ("update the pack"; step by step for Claude: PACKS.md in the top folder):
1. py pc\\pack_tool.py pending [--pool]
   Lists, scene by scene, the lines from pc\\lines\\*.jsonl (written automatically every session) that are not in the
   hand-made pack and writes them to pc\\lines\\pending.json. --pool adds the pool's lines: the pool packs (with the
   Legion's Gemma translation as "draft") and the lines still waiting there (needs pool_admin_token). Reads of the same line that differ by OCR errors are merged and the
   cleanest one is kept. Junk reads and Turkish text are skipped. Lines are split into the same pieces as in live
   translation so the pack is always found.
2. The pending lines are translated with the scene's context (Claude) and written to a {"English": "Türkçe"} file.
3. py pc\\pack_tool.py add TRANSLATIONS.json
   Adds the translations to pc\\packs\\<game>.json (replacing the translation of an existing line). The app uses the
   pack from its next start.

--frames: for sessions before the line log, pc\\frames\\ (the frame log) is read again.
publish NAME: uploads pc\\packs\\NAME.json to the pack server (needs pack_server and pool_admin_token in config.json),
so other PCs get it with their next pack update.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import subtitler as A
import textutil as M
import ocr as O
import paths
from pack import Pack

SCENE_GAP_SECONDS = 45  # a gap without lines this long starts a new scene
PENDING = paths.LINES_DIR / "pending.json"


def _time(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def from_records(game_title: str) -> list[dict]:
    """[{t, track, en}] from pc/lines/*.jsonl, by time."""
    out = []
    for f in sorted(paths.LINES_DIR.glob("*.jsonl")) if paths.LINES_DIR.is_dir() else []:
        for line_count in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line_count)
            except ValueError:
                continue
            if game_title and r.get("game") and M.text_key(r["game"]) != M.text_key(game_title):
                continue
            out.append({"t": _time(r["t"]), "track": r.get("track", "speech"), "en": r["en"]})
    return sorted(out, key=lambda r: r["t"])


def from_frames(cfg: dict) -> list[dict]:
    """Re-reads the frame log images with today's line sorting (when there is no line log)."""
    glossary, lexicon = A.make_parts(cfg)
    reader = O.Reader(A.make_ocr_engine(), lexicon, float(cfg.get("ocr_scale") or 0), A.make_layout(cfg, glossary))
    record = [json.loads(s) for s in (A.FRAME_DIR / "readings.jsonl").read_text(encoding="utf-8").splitlines()]
    height = round(cfg["region"]["height"] and 1 / cfg["region"]["height"] or 1)

    async def read():
        from PIL import Image
        out = []
        for r in record:
            img = Image.open(A.FRAME_DIR / r["file"]).convert("RGB")
            o = await reader.read(img, round(img.height * height))
            t = datetime.strptime(r["file"][:15], "%Y%m%d-%H%M%S").timestamp()
            if o.txt:
                out.append({"t": t, "track": "speech", "en": o.txt})
            if o.objective:
                out.append({"t": t, "track": "objective", "en": o.objective})
        return out

    return asyncio.run(read())


POOL_SCENE = 40  # pool lines have no time: they come in groups of this many, in the order players saw them


def hand_pack(game_title: str, names: list[str] | None) -> Pack | None:
    """The hand-made packs only (pc/packs and the shipped ones), not the server's pool packs: a line Gemma translated
    is still pending here, so it gets a proper translation."""
    return Pack.load(game_title, names, [d for d in paths.pack_dirs() if d != paths.SERVER_PACKS])


def skipped_keys(game_title: str, names: list[str] | None = None) -> set[str]:
    """Lines a review left out on purpose (junk reads, two lines run together, song lyrics, test lines): the
    "skipped" list of the game's hand-made pack files, so they are not offered again."""
    dest = {M.text_key(a) for a in [game_title, *(names or [])] if a}
    out: set[str] = set()
    for f in Pack.files([d for d in paths.pack_dirs() if d != paths.SERVER_PACKS]):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if M.text_key(data.get("game", "")) in dest:
            out |= {M.text_key(s) for s in data.get("skipped") or []}
    return out


def from_pool(cfg: dict, game_title: str, log=print) -> list[dict]:
    """[{en, track, draft?}] from the server: the game's pool packs (draft = the Legion's translation) and the lines
    still waiting in the pool, most seen first."""
    import pack_server as S

    try:
        S.sync_packs(cfg, log, force=True)
    except S.ServerError as e:
        log(f"Paketler indirilemedi ({e}); eldeki havuz paketleri kullanılıyor.")
    out, game = [], M.text_key(game_title)
    for f in sorted(paths.SERVER_PACKS.glob("pool-*.json")) if paths.SERVER_PACKS.is_dir() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if M.text_key(data.get("game", "")) == game:
            out += [{"en": en, "track": "speech", "draft": tr} for en, tr in (data.get("lines") or {}).items()]
    try:
        # One sender is enough here (a friend playing a game alone): every line is read in the review. The Legion's
        # automatic pool pack still needs pool_min_sources (2) networks.
        waiting = S.admin_lines(cfg, game=game, limit=20_000, min_sources=1)
    except S.ServerError as e:
        log(f"Havuzda bekleyen satırlar okunamadı ({e}).")
        waiting = []
    waiting.sort(key=lambda x: (-int(x.get("installs") or 1), int(x.get("id") or 0)))
    out += [{"en": M.split_speaker(M.normalize_text(w["en"]))[1], "track": w.get("track") or "speech"} for w in waiting]
    return out


def pool_scenes(entries: list[dict], pack_: Pack | None, lexicon: M.Lexicon, seen: set[str],
                skipped: set[str] = frozenset()) -> list[list[dict]]:
    """The pool's lines not in the hand-made pack (and not already pending from the sessions, nor skipped), in
    groups."""
    chosen: dict[str, dict] = {}
    for r in entries:
        en = M.normalize_text(r["en"])
        k = M.ocr_key(en)
        if len(M.text_key(en)) < 2 or k in seen or M.text_key(en) in skipped:
            continue
        if lexicon.is_garbled(en) or lexicon.is_turkish(en):
            continue
        if pack_ is not None and pack_.lookup(en):
            continue
        prev = chosen.get(k)
        if prev is None:
            chosen[k] = {"en": en, "track": r.get("track", "speech"), "from": "havuz"}
            if r.get("draft"):
                chosen[k]["draft"] = r["draft"]
        elif r.get("draft") and "draft" not in prev:
            prev["draft"] = r["draft"]
    items = list(chosen.values())
    return [items[i:i + POOL_SCENE] for i in range(0, len(items), POOL_SCENE)]


def prune_pool(cfg: dict, game_title: str, names: list[str] | None, drop: list[str], log=print) -> list[str]:
    """Republishes the game's pool pack without the lines the hand-made pack now has and the dropped ones
    (junk, test lines). Returns what was published."""
    import pack_server as S

    S.sync_packs(cfg, log, force=True)
    pack_ = hand_pack(game_title, names)
    drop_keys = {M.text_key(d) for d in drop} | skipped_keys(game_title, names)
    done = []
    for f in sorted(paths.SERVER_PACKS.glob("pool-*.json")) if paths.SERVER_PACKS.is_dir() else []:
        data = json.loads(f.read_text(encoding="utf-8"))
        if M.text_key(data.get("game", "")) != M.text_key(game_title):
            continue
        lines_ = data.get("lines") or {}
        keep = {en: tr for en, tr in lines_.items()
                if M.text_key(en) not in drop_keys and not (pack_ is not None and pack_.lookup(en))}
        if len(keep) == len(lines_):
            continue
        data = {**data, "lines": keep, "updated": time.strftime("%Y-%m-%d")}
        result = S.publish_pack(cfg, f.stem, data, title=f"{data.get('game', game_title)} (havuz)", auto=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        done.append(f"{f.stem}: {len(lines_)} -> {len(keep)} satır (sürüm {result.get('version')})")
    return done


def pending_lines(lines_: list[dict], pack_: Pack | None, lexicon: M.Lexicon,
                  skipped: set[str] = frozenset()) -> list[list[dict]]:
    """Pieces not in the pack (nor skipped), scene by scene: [[{"en", "track"}]]."""
    chosen: dict[str, dict] = {}   # ocr key -> {"en", "track", "scene", "order", "score"}
    scene, last_t = 0, None
    for i, r in enumerate(lines_):
        if last_t is not None and r["t"] - last_t > SCENE_GAP_SECONDS:
            scene += 1
        last_t = r["t"]
        _, body = M.split_speaker(r["en"])
        for piece_ in M.chunk_sentences(M.split_sentences(body)) or [body]:
            if len(M.text_key(piece_)) < 2 or lexicon.is_garbled(piece_) or lexicon.is_turkish(piece_):
                continue
            if pack_ is not None and pack_.lookup(piece_):
                continue
            if M.text_key(piece_) in skipped:
                continue
            k = M.ocr_key(piece_)
            score = lexicon.score(piece_)
            prev = chosen.get(k)
            if prev is None:
                chosen[k] = {"en": piece_, "track": r["track"], "scene": scene, "order": i, "score": score}
            elif score > prev["score"]:
                prev.update(en=piece_, score=score)
    scenes: dict[int, list[dict]] = {}
    for s in sorted(chosen.values(), key=lambda s: s["order"]):
        scenes.setdefault(s["scene"], []).append({"en": s["en"], "track": s["track"]})
    return [scenes[k] for k in sorted(scenes)]


def add(filename: Path, game_title: str, names: list[str]) -> Path:
    """file: {"English": "Türkçe"} or {"lines": {...}, "speakers": {"English": "Lucia"}, "drop": ["English", ...]}
    (the speaker labels may also come on their own; "drop": lines left out on purpose, kept in the pack's "skipped"
    list so they are not offered again)."""
    new = json.loads(filename.read_text(encoding="utf-8"))
    speakers: dict = {}
    drop: list = []
    if isinstance(new, dict) and ("lines" in new or "speakers" in new or "drop" in new):
        speakers = new.get("speakers") or {}
        drop = new.get("drop") or []
        new = new.get("lines") or {}
    dest = paths.DATA / "packs" / (re.sub(r"[^a-z0-9]+", "-", M.text_key(names[0] if names else game_title)) + ".json")
    dest.parent.mkdir(exist_ok=True)
    data = {"game": game_title, "source": "Oynanan oturumların satırları, sahne bağlamıyla Claude çevirisi.", "lines": {}}
    if dest.exists():
        data = json.loads(dest.read_text(encoding="utf-8"))
    data["lines"].update({M.normalize_text(en): tr.strip() for en, tr in new.items() if tr and tr.strip()})
    if speakers:
        data.setdefault("speakers", {}).update({M.normalize_text(en): k.strip() for en, k in speakers.items() if k and k.strip()})
    if drop:
        have = {M.text_key(s) for s in data.get("skipped") or []} | {M.text_key(en) for en in data["lines"]}
        for en in drop:
            en = M.normalize_text(str(en))
            if M.text_key(en) and M.text_key(en) not in have:
                data.setdefault("skipped", []).append(en)
                have.add(M.text_key(en))
    data["updated"] = time.strftime("%Y-%m-%d %H:%M")
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(dest)
    return dest


def admin_token(name: str) -> None:
    """A new admin token for this PC: put into the Worker as the secret ADMIN_TOKEN_<NAME> (through wrangler's stdin)
    and into config.json (encrypted with DPAPI). The token is never printed. Deleting that secret shuts this PC out."""
    import secrets
    import shutil
    import subprocess

    name = name.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,32}", name):
        sys.exit("Ad yalnız harf ve rakam olmalı (ör. LEGION).")
    server = Path(__file__).resolve().parent.parent / "server"
    wrangler = server / "node_modules" / "wrangler" / "bin" / "wrangler.js"
    node = shutil.which("node")
    if not (node and wrangler.exists()):
        sys.exit("node ya da server\\node_modules bulunamadı: server klasöründe `npm ci` çalıştır.")
    token = secrets.token_urlsafe(32)
    r = subprocess.run([node, str(wrangler), "secret", "put", f"ADMIN_TOKEN_{name}"], input=token + "\n", text=True,
                       encoding="utf-8", errors="replace", capture_output=True, cwd=server, timeout=180)
    if r.returncode != 0:
        sys.exit("Sunucuya yüklenemedi (wrangler login yapıldı mı?):\n" + (r.stderr or r.stdout)[-800:])
    user = A.user_config()
    user["pool_admin_token"] = token
    A.save_config(user)
    print(f"Yönetici anahtarı hazır: sunucuda ADMIN_TOKEN_{name}, bu bilgisayarın config.json'unda şifreli. Birkaç saniye içinde geçerli olur.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("pending", help="pakette olmayan satırları sahne sahne listele")
    b.add_argument("--frames", action="store_true", help="satır kaydı yerine kare kaydını yeniden oku")
    b.add_argument("--pool", action="store_true", help="sunucudaki havuz satırlarını da ekle (Gemma taslağıyla)")
    e = sub.add_parser("add", help='{"English": "Türkçe"} dosyasını pakete ekle')
    e.add_argument("file", type=Path)
    pp = sub.add_parser("prune-pool", help="havuz paketinden elle pakete giren ve atılan satırları çıkarıp yeniden yayınla")
    pp.add_argument("file", type=Path, nargs="?", help='çeviri dosyası; içindeki "drop": [İngilizce satırlar] atılır')
    pb = sub.add_parser("publish", help="pc/packs/<ad>.json'u paket sunucusuna yükle (yönetici anahtarıyla)")
    pb.add_argument("name", help="paket dosyasının adı, ör. gta6")
    sub.add_parser("keygen", help="bu bilgisayarın paket imza anahtarını üret (yoksa) ve açık anahtarını yaz")
    at = sub.add_parser("admin-token", help="bu bilgisayara kendi yönetici anahtarını üret: sunucuya ADMIN_TOKEN_<AD> "
                                            "olarak yükler, config'e şifreli yazar (wrangler login gerekir)")
    at.add_argument("name", help="bilgisayarın adı, ör. LEGION")
    args = ap.parse_args()
    if args.cmd == "admin-token":
        admin_token(args.name)
        return
    if args.cmd == "keygen":
        import pack_server

        key = pack_server.signing_key(create=True)
        kid, pub = pack_server.public_key_text(key)
        print(f"Paket imza anahtarı hazır (bu Windows kullanıcısı için şifreli: pc\\{pack_server.SIGNING_KEY_FILE}).")
        print(f'pack_server.PUBLISHER_KEYS içine ekle:  "{kid}": "{pub}",')
        return

    cfg = A.load_config()
    glossary, lexicon = A.make_parts(cfg)
    game_title = glossary.game_title if glossary else cfg.get("game", "")
    if args.cmd == "publish":
        import pack_server

        src = paths.DATA / "packs" / f"{args.name}.json"
        data = json.loads(src.read_text(encoding="utf-8"))
        result = pack_server.publish_pack(cfg, args.name, data, title=data.get("game", args.name))
        print(f"Yüklendi: {args.name} ({len(data.get('lines', {}))} satır), sürüm {result.get('version')}")
        return
    if args.cmd == "add":
        dest = add(args.file, game_title, glossary.names[1:] if glossary else [])
        print(f"Eklendi: {dest}  ({len(json.loads(dest.read_text(encoding='utf-8'))['lines'])} satır)")
        return
    names = glossary.names if glossary else None
    if args.cmd == "prune-pool":
        drop = []
        if args.file:
            data = json.loads(args.file.read_text(encoding="utf-8"))
            drop = list(data.get("drop") or []) if isinstance(data, dict) else []
        done = prune_pool(cfg, game_title, names, drop)
        print("\n".join(done) if done else "Havuz paketinde çıkarılacak satır yok.")
        return
    pack_ = hand_pack(game_title, names)
    skipped = skipped_keys(game_title, names)
    lines_ = from_frames(cfg) if args.frames else from_records(game_title)
    scenes = pending_lines(lines_, pack_, lexicon, skipped)
    if args.pool:
        seen = {M.ocr_key(r["en"]) for s in scenes for r in s}
        scenes += pool_scenes(from_pool(cfg, game_title), pack_, lexicon, seen, skipped)
    PENDING.parent.mkdir(exist_ok=True)
    PENDING.write_text(json.dumps({"game": game_title, "scenes": scenes}, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    n = sum(len(s) for s in scenes)
    for i, s in enumerate(scenes, 1):
        print(f"\n== Sahne {i}" + (" (havuz)" if s and s[0].get("from") == "havuz" else ""))
        for r in s:
            print(("[görev] " if r["track"] == "objective" else "") + r["en"]
                  + (f"   [Gemma: {r['draft']}]" if r.get("draft") else ""))
    print(f"\n{n} bekleyen satır, {len(scenes)} sahne -> {PENDING}", file=sys.stderr)


if __name__ == "__main__":
    main()
