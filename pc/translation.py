"""Subtitle translation: per-sentence cache, game glossary, speaker name, context and engines (OPUS-MT on this
computer / local model / Gemini / Google).

Flow (Translator.translate):
1. The speaker name is split off ("JASON: ..."): the name is not translated and is added to the name list.
2. A short line with a ready translation in the glossary ("WASTED") does not go to the model.
3. The subtitle is split into sentences and short sentences are chunked with their neighbors ("Girl. Stop. Rent is
   coming." is one chunk); chunks missing from the cache go in one request as a JSON array, and the reply is expected
   as a JSON array in the same order. Each chunk is cached on its own.
4. The request carries the game name, only the names/nicknames/terms from the glossary that occur in this line, and
   the last few lines (with their translations) as context. The system prompt holds fixed slang examples (the local
   model keeps this common prefix in its cache).
5. If the main engine is late, Google Translate is tried in parallel; if Google wins, the main engine's late reply is
   still cached (a good translation is ready next time). Google translations are not cached.
"""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import re
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path

import textutil as M
import paths

CACHE_PATH = paths.CACHE_FILE
CACHE_VERSION = 2
# When the prompt changes, old translations (made with a weaker prompt) are backed up and the cache starts empty.
PROMPT_VERSION = 2

SYSTEM = (
    "You are an experienced video game localizer translating English in-game subtitles into Turkish.\n"
    "- Write natural, spoken Turkish, the way people actually talk. Match the tone: slang stays slang and swearing "
    "stays swearing at the same strength; never soften or censor.\n"
    "- Translate the meaning, not word by word. An English idiom becomes a Turkish idiom or its plain meaning, "
    "never a literal translation. Never leave English words in the Turkish text (names excepted).\n"
    "- Characters talk to each other with informal \"sen\". Use \"siz\" only for clearly formal address (police, "
    "officials, a polite stranger) or when talking to more than one person.\n"
    "- Keep names and nicknames of people, places, brands and organizations exactly as written.\n"
    "- Interjections and sounds (Argh, Uhh, Hmm, Pssp) stay as they are or become the Turkish equivalent (Ah, Şey, "
    "Hımm, Pisi pisi).\n"
    "- The text was read from the screen by OCR: letters may be misread (0/O, l/I, rn/m) or words merged. "
    "Translate what the speaker obviously meant.\n"
    "- A string that starts in lowercase or with a connector (and, but, so) continues the previous subtitle; "
    "translate it as that continuation.\n"
    "- Short labels without a subject (objectives, menu items) are instructions to the player: use short informal "
    "imperatives (\"Arabaya bin\", not \"Arabaya binin\").\n"
    "- If a string is not real language (OCR garbage, random letters, button prompts), return \"-\" for it.\n"
    "Examples of the expected style:\n"
    "\"Oh, shit. Look at you.\" -> \"Vay anasını. Şu haline bak.\"\n"
    "\"Sucks to suck, right?\" -> \"Kendi düşen ağlamaz, değil mi?\"\n"
    "\"You're hot as fuck.\" -> \"Çok seksisin lan.\"\n"
    "\"Who are you two?\" -> \"Siz ikiniz kimsiniz?\"\n"
    "\"Livin' the dream, huh?\" -> \"Keyfin yerinde bakıyorum, ha?\"\n"
    "\"Don't sweat it.\" -> \"Kafana takma.\"\n"
    "\"You holdin'?\" -> \"Üstünde mal var mı?\"\n"
    "\"What a waste of space.\" -> \"Tam bir boş adam.\"\n"
    "\"Get the fuck outta here!\" -> \"Siktir git buradan!\"\n"
    "Input is a JSON array of strings. Reply with only a JSON array containing the same number of Turkish strings "
    "in the same order."
)


# ---------------------------------------------------------------- glossary

class Glossary:
    def __init__(self, data: dict):
        self.game_title: str = data.get("game", "")
        self.style_examples: str = data.get("style", "")
        self.keep_names: list[str] = list(data.get("keep", []))
        self.address_terms: dict[str, str] = dict(data.get("address_terms", {}))
        self.terms: dict[str, str] = dict(data.get("terms", {}))
        self.lines_: dict[str, str] = {M.text_key(k): v for k, v in data.get("lines", {}).items()}
        self.names = [self.game_title, *data.get("aliases", [])]
        # Character nicknames: {"Lu": "Lucia"}. Sent to the model as "Lu = Lucia's nickname, leave it as is".
        self.nicknames: dict[str, str] = dict(data.get("nicknames", {}))
        # Screen layout: {"center_tol": 0.02, "objective_y": 0.925} (ocr.Layout)
        self.screen: dict = dict(data.get("screen", {}))
        # Speaker colors: {"Lucia": "#FF9AD5", "character": "#FFE28A", "pedestrian": "#B4B4B4"} (the pack's "speakers").
        self.colors: dict[str, str] = dict(data.get("colors", {}))

    @classmethod
    def all_glossaries(cls) -> list["Glossary"]:
        """All glossaries (for the same game the copy in the user's folder wins over the one shipped with the app)."""
        out: dict[str, Glossary] = {}
        for d in paths.GLOSSARY_DIRS:
            for f in sorted(d.glob("*.json")):
                try:
                    s = cls(json.loads(f.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
                out.setdefault(M.text_key(s.game_title), s)
        return list(out.values())

    @classmethod
    def load(cls, game_title: str) -> "Glossary | None":
        if not game_title:
            return None
        dest = M.text_key(game_title)
        for s in cls.all_glossaries():
            if dest in {M.text_key(a) for a in s.names if a}:
                return s
        return None

    @staticmethod
    def _occurs(phrase: str, text: str) -> bool:
        return re.search(r"(?<![\w'])" + re.escape(phrase) + r"(?![\w'])", text, re.IGNORECASE) is not None

    def relevant(self, text: str) -> tuple[list[str], dict[str, str]]:
        """Names to keep and preferred translations that occur in this text."""
        names = [a for a in self.keep_names if self._occurs(a, text)]
        # If a long name is present, do not repeat a short name that is part of it ("Cal" when "Cal Hampton" is there).
        names = [a for a in names if not any(a != b and a in b for b in names)]
        term = {en: tr for en, tr in {**self.address_terms, **self.terms}.items() if self._occurs(en, text)}
        return names, term

    def nickname_map(self, text: str) -> dict[str, str]:
        """Nicknames that occur in this text (must be capitalized: "Lu", not "lu")."""
        return {l: name for l, name in self.nicknames.items()
                if re.search(r"(?<![\w'])" + re.escape(l) + r"(?![\w'])", text)}

    def ready(self, text: str) -> str | None:
        """A ready line from the glossary; the trailing punctuation follows the source ("Lose the cops." -> "Polisleri ekte.")."""
        tr = self.lines_.get(M.text_key(text))
        if tr is None:
            return None
        last = re.search(r"[.!?…]+$", M.normalize_text(text))
        return tr.rstrip(".!?…") + (last.group(0) if last else "")


# ---------------------------------------------------------------- helpers

def reply_seq(raw: str, n: int) -> list[str]:
    """An array of n strings from the model's reply. A code fence, an object wrapper or a single string reply are accepted
    too."""
    t = raw.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        data = json.loads(t)
    except ValueError:
        m = re.search(r"\[.*\]", t, re.DOTALL)
        data = None
        if m:
            try:
                data = json.loads(m.group(0))
            except ValueError:
                data = None
        if data is None:
            if n == 1 and t and not t.startswith(("[", "{")):
                return [t.strip().strip('"')]
            raise ValueError(f"JSON dizisi okunamadı: {raw[:120]!r}")
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if lists else list(data.values())
    if isinstance(data, str):
        data = [data]
    if not isinstance(data, list) or len(data) != n:
        raise ValueError(f"{n} öğe beklenirken {len(data) if isinstance(data, list) else '?'} geldi")
    return [str(x).strip() for x in data]


def merge(pieces: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(p for p in pieces if p and p.strip() != "-")).strip()


# ---------------------------------------------------------------- translator

class Translator:
    def __init__(self, cfg: dict, glossary: Glossary | None = None, log=print, cache_data: Path | None = CACHE_PATH,
                 pack_=None):
        self.log = log
        self.pack_ = pack_  # pack.Pack: looked up first; its similar lines become examples in the prompt
        self.cache_path = cache_data  # None: memory only (tests)
        self.key = cfg.get("gemini_api_key") or ""
        self.model = cfg.get("gemini_model") or "gemini-3.5-flash-lite"
        self.game = cfg.get("game") or ""
        self.glossary = glossary
        self.history: deque[tuple[str, str]] = deque(maxlen=int(cfg.get("context_lines", 3)))
        self.names: list[str] = []  # names learned from speaker labels
        self.lock = threading.Lock()
        self.cache: dict[str, str] = self._load_cache()
        self._dirty = 0
        self.hedge = float(cfg.get("hedge_ms", 900)) / 1000
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        self.local = threading.local()  # each thread's own open (keep-alive) connection
        self.rpm = int(cfg.get("gemini_rpm", 12))
        self.calls: deque[float] = deque()
        self.cooldown_until = 0.0
        self.engine = str(cfg.get("engine") or "local").lower()
        if cfg.get("google_only"):
            self.engine = "google"
        self.local_model = cfg.get("local_model") or "gemma4:e4b-it-qat"
        u = urllib.parse.urlsplit(cfg.get("local_url") or "http://127.0.0.1:11434")
        self.local_host, self.local_port = u.hostname or "127.0.0.1", u.port or 11434
        self.local_hedge = float(cfg.get("local_hedge_ms", 1500)) / 1000
        self.opus_hedge = float(cfg.get("opus_hedge_ms", 2500)) / 1000
        self.local_think = True  # falls back to False if the model does not support the "think" option

    # -------------------------------------------------------- cache

    def _load_cache(self) -> dict[str, str]:
        if self.cache_path is None:
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if (isinstance(data, dict) and data.get("version") == CACHE_VERSION
                and data.get("prompt", 1) == PROMPT_VERSION):
            return dict(data.get("sentences", {}))
        # Old format or translations made with an old prompt: backed up and started empty.
        fallback = self.cache_path.with_name(self.cache_path.stem + ".eski.json")
        try:
            self.cache_path.replace(fallback)
            self.log(f"Eski çeviri önbelleği {fallback.name} olarak yedeklendi; yeni istemle sıfırdan başlanıyor.")
        except OSError:
            pass
        return {}

    def save(self) -> None:
        with self.lock:
            data = {"version": CACHE_VERSION, "prompt": PROMPT_VERSION, "sentences": dict(self.cache)}
            self._dirty = 0
        if self.cache_path is None:
            return
        try:
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            pass

    def _to_cache(self, sentences: list[str], translations: list[str]) -> None:
        with self.lock:
            for c, t in zip(sentences, translations):
                if t:
                    self.cache[M.text_key(c)] = t
            self._dirty += 1
            save = self._dirty >= 5
        if save:
            self.save()

    # -------------------------------------------------------- prompt

    def prompt_text(self, sentences: list[str], name: str | None, history: list[tuple[str, str]]) -> str:
        parts = []
        if self.game:
            parts.append(f"Game: {self.game}")
        if self.glossary and self.glossary.style_examples:
            parts.append(f"Style notes (Turkish): {self.glossary.style_examples}")
        txt = " ".join(sentences)
        kept: list[str] = []
        term: dict[str, str] = {}
        if self.glossary:
            kept, term = self.glossary.relevant(txt)
        kept += [a for a in self.names if a not in kept and Glossary._occurs(a, txt)]
        if kept:
            parts.append("Keep exactly as written (names): " + ", ".join(kept))
        nickname = self.glossary.nickname_map(txt) if self.glossary else {}
        if nickname:
            parts.append("Nicknames, keep them exactly as written: "
                         + "; ".join(f"{l} = nickname of {name}" for l, name in nickname.items()))
        if term:
            parts.append("Preferred Turkish wording: " + "; ".join(f"{en} → {tr}" for en, tr in term.items()))
        if name:
            parts.append(f"Speaker: {name}")
        samples = self.pack_.similar(txt) if self.pack_ else []
        if samples:
            parts.append("Similar lines from this game, already translated well (follow their tone and wording):\n"
                         + "\n".join(f"EN: {e}\nTR: {t}" for e, t in samples))
        if history:
            ctx = "\n".join(f"EN: {e}\nTR: {t}" for e, t in history)
            parts.append("Previous subtitles, for context only (do not translate them):\n" + ctx)
        parts.append("Translate:\n" + json.dumps(sentences, ensure_ascii=False))
        return "\n\n".join(parts)

    # -------------------------------------------------------- main entry

    last_source = ""

    def translate(self, text: str) -> tuple[str, str]:
        """(translation, source). A translation of '-' means the line was taken for junk."""
        name, body = M.split_speaker(text)
        if name and name not in self.names and name.casefold() not in {"you", "me"}:
            self.names.append(name.title() if name.isupper() else name)

        def label_for(tr: str) -> str:
            return f"{name}: {tr}" if name and tr != "-" else tr

        if self.glossary and (ready := self.glossary.ready(body)):
            return label_for(ready), "sözlük"
        if self.pack_ and (ready := self.pack_.lookup(body)):
            with self.lock:
                self.history.append((body, ready))
            return label_for(ready), "pack"

        sentences = M.chunk_sentences(M.split_sentences(body)) or [body]
        with self.lock:
            outcome: list[str | None] = [(self.pack_.lookup(c) if self.pack_ else None) or self.cache.get(M.text_key(c))
                                       for c in sentences]
            history = list(self.history)
        missing = [c for c, t in zip(sentences, outcome) if t is None]
        source = "pack" if self.pack_ and all(self.pack_.lookup(c) for c in sentences) else "önbellek"
        if missing:
            translations, source = self._hedged(missing, name, history, body)
            if source == "Google":
                # Google cannot be matched sentence by sentence: the translation of the whole subtitle is used as one piece.
                outcome = [translations[0]]
            else:
                it = iter(translations)
                outcome = [t if t is not None else next(it) for t in outcome]
        tr = merge([s or "" for s in outcome]) or "-"
        if tr != "-":
            with self.lock:
                self.history.append((body, tr))
        return label_for(tr), source

    def _hedged(self, sentences: list[str], name: str | None, history, body: str) -> tuple[list[str], str]:
        """The main engine (local model or Gemini); if it fails or is late, Google Translate is tried too."""
        prompt = self.prompt_text(sentences, name, history)
        n = len(sentences)
        keep = self.engine != "opus"  # OPUS-MT is fast and the same every time: not cached (a better engine fills it)
        if self.engine == "opus":
            primary = self.pool.submit(self._opus, sentences)
            name_, timeout = "Bilgisayar", self.opus_hedge
        elif self.engine == "local":
            primary = self.pool.submit(self._local, prompt, n)
            name_, timeout = "Yerel", self.local_hedge
        elif self.engine == "gemini" and self._gemini_allowed():
            primary = self.pool.submit(self._gemini_safe, prompt, n)
            name_, timeout = "Gemini", self.hedge
        else:
            return [self._google(body)], "Google"

        def late(f: concurrent.futures.Future) -> None:
            if keep and not f.cancelled() and f.exception() is None:
                self._to_cache(sentences, f.result())

        try:
            out = primary.result(timeout=timeout)
            if keep:
                self._to_cache(sentences, out)
            return out, name_
        except concurrent.futures.TimeoutError:
            pass
        except Exception as e:
            self.log(f"{name_} çevirisi başarısız ({e}); Google Çeviri deneniyor.")
            return [self._google(body)], "Google"
        fallback = self.pool.submit(self._google, body)
        done, _ = concurrent.futures.wait([primary, fallback], return_when=concurrent.futures.FIRST_COMPLETED)
        if primary in done and primary.exception() is None:
            out = primary.result()
            if keep:
                self._to_cache(sentences, out)
            return out, name_
        primary.add_done_callback(late)
        try:
            return [fallback.result()], "Google"
        except Exception:
            out = primary.result()  # Google failed too: wait for the main engine
            return out, name_

    # -------------------------------------------------------- motorlar

    def warmup(self) -> None:
        """Load the model in advance (so the first line does not wait)."""
        if self.engine == "opus":
            try:
                import opus_mt

                t0 = time.perf_counter()
                m = opus_mt.model()
                where = "ekran kartında" if m.device == "cuda" else "işlemcide"
                self.log(f"Çeviri modeli hazır: OPUS-MT, {where} ({int((time.perf_counter() - t0) * 1000)} ms)")
            except Exception as e:
                self.log(f"Çeviri modeli açılamadı ({e}); Google Çeviri kullanılacak.")
            return
        if self.engine != "local":
            return
        try:
            t0 = time.perf_counter()
            self._local(self.prompt_text(["Hello."], None, []), 1)
            self.log(f"Yerel model hazır: {self.local_model} ({int((time.perf_counter() - t0) * 1000)} ms)")
        except Exception as e:
            self.log(f"Yerel model açılamadı ({e}); Google Çeviri kullanılacak.")

    def _local(self, prompt: str, n: int) -> list[str]:
        """Local model through Ollama (runs on the GPU, no quota). The reply is forced with a JSON schema."""
        msg = {
            "model": self.local_model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "stream": False,
            "keep_alive": "60m",
            "format": {"type": "array", "items": {"type": "string"}, "minItems": n, "maxItems": n},
            "options": {"temperature": 0, "num_predict": 160 + 120 * n},
        }
        for attempt in range(3):
            if self.local_think:
                msg["think"] = False
            else:
                msg.pop("think", None)
            conn = getattr(self.local, "lconn", None)
            if conn is None:
                conn = self.local.lconn = http.client.HTTPConnection(self.local_host, self.local_port, timeout=180)
            try:
                conn.request("POST", "/api/chat", json.dumps(msg).encode("utf-8"), {"Content-Type": "application/json"})
                resp = conn.getresponse()
                raw = resp.read()
            except (OSError, http.client.HTTPException):
                conn.close()
                self.local.lconn = None
                if attempt:
                    raise
                continue
            if resp.status == 400 and self.local_think and b"think" in raw:
                self.local_think = False
                continue
            break
        if resp.status != 200:
            raise RuntimeError(f"Yerel model HTTP {resp.status}: {raw.decode('utf-8', 'replace')[:200]}")
        out = json.loads(raw.decode("utf-8")).get("message", {}).get("content", "")
        return reply_seq(out, n)

    def _opus(self, chunks: list[str]) -> list[str]:
        """OPUS-MT on this computer (opus_mt.py): no key, no quota, no GPU needed.

        Every sentence goes on its own (one batch), then the chunk is joined again: given "Ugh, fuck, fine! Asshole..."
        or "Yeah! What?" in one piece the model dropped a sentence (18 of 120 multi-sentence lines on 2026-11-26;
        sentence by sentence none, and chrF against the pack 55.4 -> 57.6, Gemma 56.3)."""
        import opus_mt

        pieces = [M.split_sentences(c) or [c] for c in chunks]
        flat = opus_mt.model().translate([s for p in pieces for s in p])
        out, i = [], 0
        for p in pieces:
            out.append(" ".join(t for t in flat[i:i + len(p)] if t))
            i += len(p)
        return out

    def _gemini_allowed(self) -> bool:
        if not self.key:
            return False
        now = time.monotonic()
        with self.lock:
            if now < self.cooldown_until:
                return False
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= self.rpm:
                return False
            self.calls.append(now)
            return True

    def _gemini_safe(self, prompt: str, n: int) -> list[str]:
        try:
            return self._gemini(prompt, n)
        except RuntimeError as e:
            if "HTTP 429" in str(e):
                with self.lock:
                    self.cooldown_until = time.monotonic() + 30
                self.log("Gemini dakikalık sınıra takıldı; 30 sn Google Çeviri kullanılacak.")
            raise

    def _gemini(self, prompt: str, n: int) -> list[str]:
        path = f"/v1beta/models/{self.model}:generateContent"
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 300 + 200 * n,
                "responseMimeType": "application/json",
                "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.key}
        for attempt in range(2):
            conn = getattr(self.local, "conn", None)
            if conn is None:
                conn = self.local.conn = http.client.HTTPSConnection("generativelanguage.googleapis.com", timeout=15)
            try:
                conn.request("POST", path, body, headers)
                resp = conn.getresponse()
                raw = resp.read()
                break
            except (OSError, http.client.HTTPException):
                # The server may have closed an idle connection: reconnect and try once more.
                conn.close()
                self.local.conn = None
                if attempt:
                    raise
        if resp.status != 200:
            raise RuntimeError(f"Gemini HTTP {resp.status}: {raw.decode('utf-8', 'replace')[:300]}")
        data = json.loads(raw.decode("utf-8"))
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return reply_seq(text, n)

    def _google(self, text: str) -> str:
        """Google Translate's free web endpoint (needs no key). Fallback only."""
        q = urllib.parse.urlencode({"client": "gtx", "sl": "en", "tl": "tr", "dt": "t", "q": text})
        for attempt in range(2):
            conn = getattr(self.local, "gconn", None)
            if conn is None:
                conn = self.local.gconn = http.client.HTTPSConnection("translate.googleapis.com", timeout=10)
            try:
                conn.request("GET", "/translate_a/single?" + q, headers={"User-Agent": "Mozilla/5.0"})
                resp = conn.getresponse()
                raw = resp.read()
                break
            except (OSError, http.client.HTTPException):
                conn.close()
                self.local.gconn = None
                if attempt:
                    raise
        if resp.status != 200:
            raise RuntimeError(f"Google Çeviri HTTP {resp.status}")
        data = json.loads(raw.decode("utf-8"))
        out = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
        if not out:
            raise RuntimeError("Google Çeviri boş yanıt verdi")
        return out
