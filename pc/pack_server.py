"""The pack server (server/, a Cloudflare Worker): new packs come down, lines no pack had go up to the shared pool.

App side (requests signed with the shared app secret, like CouchSubs):
  sync_packs()   at every start (at most every SYNC_EVERY s): the server's index, changed packs downloaded,
                 checked with SHA-256 and unpacked into pc/packs-server/<id>.json (pack.py reads them after the
                 user's own pc/packs/).
  LinePool       translated lines that came from no pack (the model, the cache, Google) are queued and sent every
                 5 minutes and at the end: only the English text, its normalized key, the game and a random install
                 id. Off with "share_lines": false.
Nightly job and pack_tool (admin token, only on our own PCs):
  admin_lines(), admin_set_state(), publish_pack().

Settings (config.json): pack_server (base URL), pack_secret, share_lines, pool_admin_token (only on the PC that runs
the nightly job). Without pack_server / pack_secret nothing here does anything.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import paths
import textutil as M

DEFAULT_URL = "https://dilmac.dilmac-server.workers.dev"  # README: server/
# The app secret is not in git: the app gets it from config.json (pack_secret) or at build time. Our own PCs
# need none: their admin token (pool_admin_token) is accepted for the app calls as well.
try:  # written by build_exe.py from this PC's (encrypted) config just for the build, deleted after it
    from _build_config import APP_SECRET as DEFAULT_SECRET  # type: ignore
except ImportError:
    DEFAULT_SECRET = ""
STATE_FILE = "sync-state.dat"  # not *.json: pack.py reads every .json in the folder as a pack
QUEUE_FILE = paths.DATA / "pool-queue.json"
# 2026-11-27: was a day. The Legion now publishes pool packs minutes after a game is closed, so every start looks
# (the index is one small request; only changed packs are downloaded). The window only merges the GUI's check at
# launch with the one when translation starts.
SYNC_EVERY = 10 * 60
FLUSH_EVERY = 300
SKIP_SOURCES = {"pack", "paket", "sözlük"}
VOICES_PER_SESSION = 2000  # voice vectors one session sends at most (the server also has a daily limit)


class ServerError(Exception):
    pass


def settings(cfg: dict) -> tuple[str, str] | None:
    url = str(cfg.get("pack_server") or DEFAULT_URL).rstrip("/")
    secret = str(cfg.get("pack_secret") or DEFAULT_SECRET)
    if not (url and (secret or cfg.get("pool_admin_token"))):  # our PCs: the admin token is enough
        return None
    # HTTPS only (secrets and tokens travel in the headers); plain http only to this PC (local tests).
    host = urllib.parse.urlsplit(url).hostname or ""
    if not url.startswith("https://") and not (url.startswith("http://") and host in ("127.0.0.1", "localhost")):
        return None
    return url, secret


def _admin_header(cfg: dict) -> str | None:
    return str(cfg.get("pool_admin_token") or "") or None


def sign(secret: str, method: str, path: str, now: int | None = None) -> dict:
    t = str(int(time.time() if now is None else now))
    sig = hmac.new(secret.encode(), f"{method}\n{path}\n{t}".encode(), hashlib.sha256).hexdigest()
    return {"x-dilmac-time": t, "x-dilmac-signature": sig}


def request(base: str, method: str, path: str, *, secret: str | None = None, admin: str | None = None,
            body: bytes | None = None, headers: dict | None = None, timeout: float = 20) -> tuple[int, dict, bytes]:
    h = {"User-Agent": "Dilmac", **(headers or {})}
    if secret:
        h.update(sign(secret, method, urllib.parse.urlsplit(path).path))
    if admin:
        h["Authorization"] = f"Bearer {admin}"
    req = urllib.request.Request(base + path, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, OSError) as e:
        raise ServerError(f"Paket sunucusuna ulaşılamadı: {e}")


def game_key(title: str) -> str:
    return M.text_key(title)


def pack_id_for(title: str, prefix: str = "") -> str:
    ascii_ = M.normalize_text(title).translate(str.maketrans("ÇĞİIÖŞÜçğıöşü", "CGIIOSUcgiosu")).casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")[:50] or "oyun"
    return f"{prefix}{slug}"


# ---------------------------------------------------------------- pack signatures (Ed25519)
#
# Every published pack is signed with a publisher key that only our own PCs have (pack-signing-key, encrypted with
# DPAPI). The app accepts a pack only if one of PUBLISHER_KEYS signed "dilmac-pack-v1\n<id>\n<sha256 of the
# .json.gz>". Getting the Cloudflare account or the admin token is then not enough to put text on users' TVs.

# key id -> raw Ed25519 public key, base64 (`pack_tool.py keygen` prints a PC's line). The private keys never leave the
# PC that made them (encrypted with DPAPI); the Legion gets its own key for the nightly pool packs.
PUBLISHER_KEYS: dict[str, str] = {
    "c1a34893f549810f": "L2JqUcvY4ESVX0PvzJ9N+KuUT6TXRljdnSz/P7MVuCE=",  # main PC (hand-made packs), 2026-11-26
    "ca60e4793e00f8a5": "0V8cEpAnkhrtZGDE7LHyokGKMGlzEW3yJfNVzf6H2u8=",  # Legion (nightly pool packs), 2026-11-27
}
SIGNING_KEY_FILE = "pack-signing-key"
MAX_PACK_BYTES = 20 * 1024 * 1024      # compressed
MAX_UNPACKED = 80 * 1024 * 1024
MAX_LINES = 300_000
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f‪-‮⁦-⁩]")


def _message(pack_id: str, sha256_hex: str) -> bytes:
    return f"dilmac-pack-v1\n{pack_id}\n{sha256_hex}".encode("ascii")


HASHED = "hashed-v1"  # pack.HASHED
FULL = "#full"  # the full copy (with the English text) is signed as "<id>#full": it cannot pass for the app's copy


def key_id(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def trusted_keys(cfg: dict | None = None) -> dict[str, str]:
    keys = dict(PUBLISHER_KEYS)
    for item in (cfg or {}).get("pack_trusted_keys") or []:  # a key added by hand on this PC (tests, a new publisher)
        try:
            raw = base64.b64decode(str(item), validate=True)
            if len(raw) == 32:
                keys[key_id(raw)] = str(item)
        except ValueError:
            continue
    return keys


def verify_entry(entry: dict, keys: dict[str, str]) -> bool:
    """The index entry's signature over its id and sha256, by one of the trusted keys."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    kid, sig, sha, pid = (str(entry.get(k) or "") for k in ("key", "signature", "sha256", "id"))
    if kid not in keys or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(keys[kid])).verify(base64.b64decode(sig),
                                                                                _message(pid, sha))
        return True
    except (InvalidSignature, ValueError):
        return False


def signing_key(create: bool = False):
    """This PC's publisher key (encrypted for this Windows user); None if there is none."""
    import secure_store
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = paths.DATA / SIGNING_KEY_FILE
    try:
        return Ed25519PrivateKey.from_private_bytes(secure_store.read_bytes(path))
    except (OSError, ValueError, secure_store.SecretError):
        if not create:
            return None
    key = Ed25519PrivateKey.generate()
    secure_store.write_bytes(path, key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                     serialization.NoEncryption()))
    return key


def public_key_text(key) -> tuple[str, str]:
    """(key id, base64 public key) to put into PUBLISHER_KEYS."""
    from cryptography.hazmat.primitives import serialization

    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key_id(raw), base64.b64encode(raw).decode("ascii")


def sign_pack(key, pack_id: str, sha256_hex: str) -> str:
    return base64.b64encode(key.sign(_message(pack_id, sha256_hex))).decode("ascii")


def _gunzip(raw: bytes) -> bytes:
    """gzip with a size cap (a small file must not unpack into gigabytes)."""
    import zlib

    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(raw, MAX_UNPACKED)
    if d.unconsumed_tail or not d.eof:
        raise ValueError("paket çok büyük ya da yarım")
    return out


def clean_pack(data) -> dict:
    """Only the expected shape: string lines of sane length, no control or text-direction characters."""
    if not isinstance(data, dict) or not isinstance(data.get("lines"), dict):
        raise ValueError("lines yok")
    lines = {}
    for en, tr in list(data["lines"].items())[:MAX_LINES]:
        if isinstance(en, str) and isinstance(tr, str) and 0 < len(en) <= 400 and 0 < len(tr) <= 600:
            lines[_CONTROL.sub("", en)] = _CONTROL.sub("", tr)
    speakers = {}
    for en, who in (data.get("speakers") or {}).items() if isinstance(data.get("speakers"), dict) else []:
        if isinstance(en, str) and isinstance(who, str) and len(en) <= 400 and 0 < len(who) <= 40 and en in lines:
            speakers[en] = _CONTROL.sub("", who)
    out = {"game": _CONTROL.sub("", str(data.get("game") or ""))[:120], "source": str(data.get("source") or "")[:40],
           "lines": lines}
    if speakers:
        out["speakers"] = speakers
    if data.get("format") == HASHED:
        out.update(clean_hashed(data))
    voices = clean_voices(data.get("voices"))
    if voices:
        out["voices"] = voices
    return out


_HEX20 = re.compile(r"[0-9a-f]{20}")
_MINHASH = re.compile(r"\d{1,4}:[A-Za-z0-9+/]{86}==")


def clean_hashed(data: dict) -> dict:
    """The hashed part of a pack (pack.to_hashed): hex keys, sane translations, MinHash strings, speaker names."""
    def part(name):
        v = data.get(name)
        return v if isinstance(v, dict) else {}

    h = {}
    for k, tr in list(part("h").items())[:MAX_LINES]:
        if isinstance(k, str) and _HEX20.fullmatch(k) and isinstance(tr, str) and 0 < len(tr) <= 600:
            h[k] = _CONTROL.sub("", tr)
    o = {k: v for k, v in part("o").items() if isinstance(k, str) and _HEX20.fullmatch(k) and v in h}
    m = {k: v for k, v in part("m").items() if k in h and isinstance(v, str) and _MINHASH.fullmatch(v)}
    s = {k: _CONTROL.sub("", v) for k, v in part("s").items() if k in h and isinstance(v, str) and 0 < len(v) <= 40}
    return {"format": HASHED, "h": h, "o": o, "m": m, "s": s}


def pack_size(data: dict) -> int:
    """Lines of a pack, hashed or not."""
    return len(data.get("lines") or {}) or len(data.get("h") or {})


MAX_VOICE_DIM = 1024
# What clean_pack keeps. A pack saved by an older version may lack parts (before 2: "voices"): it is downloaded again
# even if its sha256 did not change.
CLEAN_VERSION = 3  # 3: hashed packs and the full copies


def clean_voices(raw) -> dict:
    """Voice profiles in a pack ("voices-<game>"): {speaker: {"sum": [floats], "n": count}}, the same shape as
    pc/voices/profiles.json. Only finite numbers of one length, sane names and counts."""
    import math

    out: dict = {}
    if not isinstance(raw, dict):
        return out
    dim = None
    for who, p in list(raw.items())[:200]:
        if not (isinstance(who, str) and 0 < len(who) <= 40 and isinstance(p, dict)):
            continue
        s, n = p.get("sum"), p.get("n")
        if not (isinstance(s, list) and 0 < len(s) <= MAX_VOICE_DIM and type(n) is int and 0 < n <= 10_000_000):
            continue
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in s):
            continue
        dim = dim or len(s)
        if len(s) != dim:
            continue
        out[_CONTROL.sub("", who)] = {"sum": [float(x) for x in s], "n": n}
    return out


# ---------------------------------------------------------------- packs down

def _state(folder: Path) -> dict:
    try:
        return json.loads((folder / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sync_packs(cfg: dict, log=print, folder: Path | None = None, force: bool = False) -> dict:
    """Downloads changed packs; {"updated": [...], "removed": [...], "packs": n} or {"skipped": reason}."""
    s = settings(cfg)
    if s is None:
        return {"skipped": "sunucu ayarlı değil"}
    base, secret = s
    folder = folder or paths.SERVER_PACKS
    folder.mkdir(parents=True, exist_ok=True)
    state = _state(folder)
    if not force and time.time() - float(state.get("_checked", 0)) < SYNC_EVERY and state.get("_ok"):
        return {"skipped": "az önce bakıldı"}
    status, _, body = request(base, "GET", "/v1/packs", secret=secret, admin=_admin_header(cfg), timeout=10)
    if status != 200:
        raise ServerError(f"Paket listesi alınamadı (HTTP {status})")
    packs = json.loads(body.decode("utf-8")).get("packs", [])
    admin = _admin_header(cfg)
    updated, removed = [], []
    for p in packs:
        pid, sha = str(p.get("id", "")), str(p.get("sha256", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", pid) or not sha:
            continue
        # Our own PCs (admin token) take the full copy with the English text: pack_tool, the nightly job and the
        # prompt examples need it. The apps get the hashed copy.
        full = p.get("full") if admin and isinstance(p.get("full"), dict) else None
        if full:
            entry, url, variant = {**full, "id": pid + FULL}, f"/v1/admin/packs/{pid}/full", "full"
            sha = str(full.get("sha256", ""))
        else:
            entry, url, variant = p, f"/v1/packs/{pid}", "app"
        st = state.get(pid, {})
        if (st.get("sha256") == sha and st.get("clean") == CLEAN_VERSION and st.get("variant", "app") == variant
                and (folder / f"{pid}.json").is_file()):
            continue
        # Only packs we signed: a server (or admin token) in the wrong hands cannot push text to anyone's TV.
        if not verify_entry(entry, trusted_keys(cfg)):
            log(f"İmzası doğrulanamayan paket atlandı: {pid}")
            continue
        status, headers, raw = request(base, "GET", url, secret=secret, admin=admin, timeout=60)
        if status != 200:
            log(f"Paket indirilemedi: {pid} (HTTP {status})")
            continue
        if len(raw) > MAX_PACK_BYTES or hashlib.sha256(raw).hexdigest() != sha:
            log(f"Paket bozuk geldi, atlandı: {pid}")
            continue
        try:
            data = clean_pack(json.loads(_gunzip(raw).decode("utf-8")))
        except (OSError, ValueError, EOFError) as e:
            log(f"Paket okunamadı, atlandı: {pid} ({e})")
            continue
        tmp = folder / f"{pid}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(folder / f"{pid}.json")
        state[pid] = {"sha256": sha, "version": p.get("version"), "entries": p.get("entries"), "clean": CLEAN_VERSION,
                      "variant": variant}
        updated.append(f"{p.get('title') or pid} (" + (f"{len(data['voices'])} ses profili" if data.get("voices")
                                                       else f"{pack_size(data)} satır") + ")")
    listed = {str(p.get("id")) for p in packs}
    for f in folder.glob("*.json"):
        if f.name != STATE_FILE and f.stem not in listed:
            f.unlink(missing_ok=True)
            state.pop(f.stem, None)
            removed.append(f.stem)
    state["_checked"], state["_ok"] = time.time(), True
    (folder / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    if updated:
        log("Sunucudan paket güncellendi: " + ", ".join(updated))
    return {"updated": updated, "removed": removed, "packs": len(packs)}


# ---------------------------------------------------------------- lines up (the pool)

def install_id() -> str:
    """A random id of this install (kept in the queue file's folder); the pool counts installs with it."""
    path = paths.DATA / "install-id.txt"
    try:
        v = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9-]{8,64}", v):
            return v
    except OSError:
        pass
    v = str(uuid.uuid4())
    try:
        path.write_text(v, encoding="utf-8")
    except OSError:
        pass
    return v


class LinePool:
    """Queues lines for the pool and sends them in the background (every FLUSH_EVERY s and on close())."""

    def __init__(self, cfg: dict, log=print, queue_file: Path | None = None, start: bool = True):
        self.cfg = cfg
        self.log = log
        self.on = settings(cfg) is not None and bool(cfg.get("share_lines", True))
        # Voice vectors of known speakers too (share_voices), but not from our own PCs: their profiles are published
        # whole (voice_tool publish), and sending them again would count the same clips twice.
        self.voices_on = self.on and bool(cfg.get("share_voices", True)) and not cfg.get("pool_admin_token")
        self.voices: list[dict] = []  # {"game", "speaker", "v"}: in memory only (a lost batch is no harm)
        self.queue_file = queue_file or QUEUE_FILE
        self.lock = threading.Lock()
        self.queue: list[dict] = self._load()
        self.sent: set[str] = set()
        self.stop = threading.Event()
        self.warned = False
        self.thread = None
        if self.on and start:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="line-pool")
            self.thread.start()

    def _load(self) -> list[dict]:
        try:
            q = json.loads(self.queue_file.read_text(encoding="utf-8"))
            return q if isinstance(q, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        try:
            self.queue_file.write_text(json.dumps(self.queue[-5000:], ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def add(self, game: str, en: str, track: str = "speech", source: str = "") -> None:
        if not self.on or not game or not en or source in SKIP_SOURCES:
            return
        key = M.text_key(en)
        gk = game_key(game)
        if len(key) < 3 or (gk, key) in self.sent:
            return
        with self.lock:
            self.sent.add((gk, key))
            self.queue.append({"game": gk, "key": key, "en": M.normalize_text(en)[:400],
                               "track": "objective" if track == "objective" else "speech"})

    def add_voice(self, game: str, speaker: str, v) -> None:
        """A voice vector the app learned for a line whose speaker the pack knew (group labels are not voices)."""
        if not self.voices_on or not game or not speaker or v is None:
            return
        try:
            vec = [round(float(x), 5) for x in v]
        except (TypeError, ValueError):
            return
        with self.lock:
            if len(self.voices) < VOICES_PER_SESSION:
                self.voices.append({"game": game_key(game), "speaker": speaker[:40], "v": vec})

    def flush_voices(self) -> int:
        if not self.voices_on:
            return 0
        with self.lock:
            batch, self.voices = self.voices, []
        if not batch:
            return 0
        base, secret = settings(self.cfg)
        by_game: dict[str, list[dict]] = {}
        for item in batch:
            by_game.setdefault(item["game"], []).append(item)
        sent = 0
        for game, items in by_game.items():
            for i in range(0, len(items), 50):
                part = items[i:i + 50]
                body = json.dumps({"game": game, "voices": [{"speaker": x["speaker"], "v": x["v"]} for x in part]})
                try:
                    status, _, _ = request(base, "POST", "/v1/voices", secret=secret, body=body.encode(),
                                           headers={"Content-Type": "application/json"}, timeout=15)
                except ServerError:
                    status = 0
                if status == 200:
                    sent += len(part)
        return sent

    def flush(self) -> int:
        """Sends what is queued; what could not be sent stays in the queue file for next time."""
        if not self.on:
            return 0
        try:
            self.flush_voices()
        except Exception:
            pass
        with self.lock:
            batch, self.queue = self.queue, []
        if not batch:
            return 0
        base, secret = settings(self.cfg)
        install = install_id()
        sent = 0
        by_game: dict[str, list[dict]] = {}
        for item in batch:
            by_game.setdefault(item["game"], []).append(item)
        failed: list[dict] = []
        for game, items in by_game.items():
            for i in range(0, len(items), 200):
                part = items[i:i + 200]
                body = json.dumps({"install": install, "game": game,
                                   "lines": [{k: x[k] for k in ("key", "en", "track")} for x in part]}).encode()
                try:
                    # Our own PCs also send the admin token: their lines count as trusted in the pool.
                    status, _, _ = request(base, "POST", "/v1/lines", secret=secret, body=body,
                                           admin=str(self.cfg.get("pool_admin_token") or "") or None,
                                           headers={"Content-Type": "application/json"}, timeout=15)
                except ServerError as e:
                    status = 0
                    if not self.warned:
                        self.warned = True
                        self.log(f"Ortak havuza gönderilemedi ({e}); sonra yeniden denenecek.")
                if status == 200:
                    sent += len(part)
                else:
                    failed += part
        with self.lock:
            self.queue = failed + self.queue
            self._save()
        return sent

    def _loop(self) -> None:
        while not self.stop.wait(FLUSH_EVERY):
            self.flush()

    def close(self) -> None:
        self.stop.set()
        try:
            self.flush()
        except Exception:
            with self.lock:
                self._save()


# ---------------------------------------------------------------- admin (nightly job, pack_tool publish)

def _admin(cfg: dict) -> tuple[str, str]:
    s = settings(cfg)
    token = str(cfg.get("pool_admin_token") or "")
    if s is None or not token:
        raise ServerError("Sunucu adresi ya da yönetici anahtarı (pool_admin_token) ayarlı değil.")
    return s[0], token


def admin_lines(cfg: dict, game: str = "", state: int = 0, limit: int = 4000, min_sources: int | None = None) -> list[dict]:
    """Waiting lines that several networks sent (pool_min_sources, default 2) or our own PCs sent."""
    base, token = _admin(cfg)
    min_sources = int(min_sources or cfg.get("pool_min_sources") or 2)
    out, after = [], 0
    while len(out) < limit:
        q = urllib.parse.urlencode({k: v for k, v in (("game", game), ("after", after), ("limit", 500),
                                                       ("state", state), ("min_sources", min_sources)) if v != ""})
        status, _, body = request(base, "GET", f"/v1/admin/lines?{q}", admin=token)
        if status != 200:
            raise ServerError(f"Havuz okunamadı (HTTP {status})")
        data = json.loads(body.decode("utf-8"))
        out += data.get("lines", [])
        if not data.get("next"):
            break
        after = data["next"]
    return out[:limit]


def admin_voices(cfg: dict, game: str = "") -> list[dict]:
    """The pool's voice rows: [{game, speaker, source, n, sum}] (one row per game, speaker and network)."""
    base, token = _admin(cfg)
    q = urllib.parse.urlencode({"game": game} if game else {})
    status, _, body = request(base, "GET", f"/v1/admin/voices?{q}", admin=token, timeout=60)
    if status != 200:
        raise ServerError(f"Ses havuzu okunamadı (HTTP {status})")
    return json.loads(body.decode("utf-8")).get("voices", [])


def admin_set_state(cfg: dict, ids: list[int], state: int) -> None:
    base, token = _admin(cfg)
    for i in range(0, len(ids), 1000):
        body = json.dumps({"ids": ids[i:i + 1000], "state": state}).encode()
        status, _, _ = request(base, "POST", "/v1/admin/lines/state", admin=token, body=body,
                               headers={"Content-Type": "application/json"})
        if status != 200:
            raise ServerError(f"Havuz güncellenemedi (HTTP {status})")


def publish_pack(cfg: dict, pack_id: str, data: dict, title: str = "", auto: bool = False) -> dict:
    """Signs the pack with this PC's publisher key and uploads it (admin token)."""
    base, token = _admin(cfg)
    key = signing_key()
    if key is None:
        raise ServerError("Bu bilgisayarda paket imza anahtarı yok: önce `pack_tool.py keygen`, sonra açık anahtarı "
                          "pack_server.PUBLISHER_KEYS'e ekle.")
    from pack import to_hashed

    def gz(d: dict) -> bytes:
        return gzip.compress(json.dumps(clean_pack(d), ensure_ascii=False).encode("utf-8"), mtime=0)

    # The apps get the copy without the English (the lines are Rockstar's, for example); our PCs the full one.
    app_raw = gz(to_hashed(data)) if data.get("lines") else gz(data)
    full_raw = gz(data)
    kid, _ = public_key_text(key)
    game = str(data.get("game", ""))
    pack = {}
    for raw, variant in ((app_raw, "app"), (full_raw, "full")):
        sha = hashlib.sha256(raw).hexdigest()
        headers = {"x-pack-game": urllib.parse.quote(game), "x-pack-title": urllib.parse.quote(title or game),
                   "x-pack-entries": str(len(data.get("lines") or {}) or len(data.get("voices") or {})),
                   "x-pack-auto": "1" if auto else "0", "x-pack-key": kid,
                   "x-pack-signature": sign_pack(key, pack_id + (FULL if variant == "full" else ""), sha),
                   "Content-Type": "application/gzip"}
        if variant == "full":
            headers["x-pack-variant"] = "full"
        status, _, body = request(base, "PUT", f"/v1/admin/packs/{pack_id}", admin=token, body=raw, headers=headers,
                                  timeout=120)
        if status != 200:
            raise ServerError(f"Paket yüklenemedi (HTTP {status}): {body[:200].decode('utf-8', 'replace')}")
        pack = json.loads(body.decode("utf-8")).get("pack", {})
    return pack
