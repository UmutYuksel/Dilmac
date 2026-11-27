"""Game audio (from chiaki over the direct PS5 connection): a buffer of the last few seconds and a clip per line.

For recognizing the speaker by voice: for every line a clip starting shortly before it was first seen on screen and
lasting a few seconds is saved as mono 16 kHz WAV under pc\\voices\\clips\\ (clips.jsonl: which file is which line).
Once the pack is updated and the speakers are labeled, the characters' voice profiles are built from these clips.
"""
from __future__ import annotations

import collections
import threading
import time
import wave
from pathlib import Path

TARGET_RATE = 16000  # speaker models want 16 kHz mono


class AudioBuffer:
    """PCM chunks from chiaki (int16, interleaved channels) with their arrival times; the last `seconds` are kept."""

    def __init__(self, seconds: float = 15.0):
        self.seconds = seconds
        self._chunks: collections.deque = collections.deque()  # (arrival time, bytes, samples)
        self._lock = threading.Lock()
        self.channels = 0
        self.rate = 0
        self.total_samples = 0

    def add(self, pcm: bytes, samples: int, channels: int, rate: int, ts: float | None = None) -> None:
        ts = time.monotonic() if ts is None else ts
        with self._lock:
            if (channels, rate) != (self.channels, self.rate):
                self._chunks.clear()  # format changed: old chunks must not get mixed in
                self.channels, self.rate = channels, rate
            self._chunks.append((ts, pcm, samples))
            self.total_samples += samples
            while self._chunks and ts - self._chunks[0][0] > self.seconds:
                self._chunks.popleft()

    def segment(self, t0: float, t1: float) -> tuple[bytes, int, int]:
        """Audio falling in [t0, t1] (monotonic seconds): (pcm, channels, rate). Rounded to chunk boundaries."""
        with self._lock:
            channels, rate = self.channels, self.rate
            chosen = [pcm for ts, pcm, samples in self._chunks if rate and ts >= t0 and ts - samples / rate <= t1]
        return b"".join(chosen), channels, rate

    def last_ts(self) -> float:
        with self._lock:
            return self._chunks[-1][0] if self._chunks else 0.0


def mono16k(pcm: bytes, channels: int, rate: int) -> bytes:
    """int16 multichannel PCM -> int16 mono 16 kHz."""
    if not pcm or not channels or not rate:
        return b""
    try:
        import numpy as np

        a = np.frombuffer(pcm[: len(pcm) // (2 * channels) * 2 * channels], dtype=np.int16).reshape(-1, channels)
        m = a.astype(np.float32).mean(axis=1)
        if rate != TARGET_RATE:
            n = int(len(m) * TARGET_RATE / rate)
            if rate % TARGET_RATE == 0:  # 48 kHz: mean of 3 samples (simple low-pass + decimation)
                k = rate // TARGET_RATE
                m = m[: len(m) // k * k].reshape(-1, k).mean(axis=1)
            else:
                m = np.interp(np.linspace(0, len(m) - 1, n), np.arange(len(m)), m)
        return np.clip(m, -32768, 32767).astype(np.int16).tobytes()
    except ImportError:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop  # present in Python 3.12; fallback without numpy

        if channels == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        elif channels > 2:
            return b""
        return audioop.ratecv(pcm, 2, 1, rate, TARGET_RATE, None)[0] if rate != TARGET_RATE else pcm


def write_wav(path_: Path, pcm16k: bytes) -> None:
    path_.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path_), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_RATE)
        w.writeframes(pcm16k)


def read_wav(path_: Path) -> bytes:
    with wave.open(str(path_), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != TARGET_RATE:
            raise ValueError(f"{path_.name}: mono 16 kHz int16 değil")
        return w.readframes(w.getnframes())


def loudness(pcm16k: bytes) -> float:
    """RMS amplitude of the clip (int16 scale); ~0 if silent."""
    if len(pcm16k) < 2:
        return 0.0
    try:
        import numpy as np

        a = np.frombuffer(pcm16k[: len(pcm16k) // 2 * 2], dtype=np.int16).astype(np.float64)
        return float(np.sqrt((a * a).mean()))
    except ImportError:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop

        return float(audioop.rms(pcm16k[: len(pcm16k) // 2 * 2], 2))


# ---------------------------------------------------------------- recognizing the speaker by voice

MODEL_NAME = "nemo_en_titanet_small.onnx"  # NVIDIA TitaNet-small (ONNX published by sherpa-onnx), 192 dimensions
# Labels not used for learning profiles: they stand for many different people, not one voice.
GROUP_LABELS = {"pedestrian", "police", "character"}
THRESHOLD = 0.42  # cosine similarity to the nearest profile (0.55 left most noisy-scene lines undecided)
MARGIN = 0.08  # and at least this far ahead of the second nearest (no decision between two voices)
# A profile takes part in decisions only from this many clips on. A side character heard in a single noisy scene (15
# clips of Wyman at the mud park) learns the scene's crowd and music as much as the voice, and then claims the
# pedestrians of that scene: 13 false alarms out of 143 pedestrian clips instead of 5. Its clips are still collected.
MIN_PROFILE_SAMPLES = 20
MIN_SECONDS = 0.8    # no decision from a clip shorter than this
MIN_LOUDNESS = 150   # no decision from a clip quieter than this (RMS)


def model_path(root: Path) -> Path:
    return root / "model" / MODEL_NAME


class SpeakerRecognizer:
    """Voice vector (TitaNet via sherpa-onnx) and voice profiles of the game's characters.

    Profile = the sum of the unit vectors of the character's labeled lines (its direction is the average voice). File:
    voices/profiles.json  {game: {speaker: {"sum": [...], "n": count}}}.
    """

    def __init__(self, root: Path, game_title: str, extractor=None):
        self.root, self.game_title = root, game_title
        self.profile_file = root / "profiles.json"
        self._extractor = extractor
        self._lock = threading.Lock()
        self.profiles: dict[str, dict] = {}
        self.shared: dict[str, dict] = {}  # from the pack server ("voices-<game>" packs); never written to our file
        self.changed = False
        self._added: dict[str, dict] = {}  # learned since load: saved as increments, not over the file
        self._replace = False             # reset() was called: save() writes the profiles as a whole
        self._load()

    @classmethod
    def open(cls, root: Path, game_title: str) -> "SpeakerRecognizer | None":
        """A SpeakerRecognizer if the model and sherpa-onnx are there; otherwise None (the feature stays quietly off)."""
        path_ = model_path(root)
        if not path_.is_file():
            return None
        try:
            import sherpa_onnx
        except ImportError:
            return None
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path_), num_threads=2, debug=False,
                                                          provider="cpu")
        rec = cls(root, game_title, sherpa_onnx.SpeakerEmbeddingExtractor(cfg))
        import paths

        rec.load_shared(paths.SERVER_PACKS)
        return rec

    def load_shared(self, folder: Path) -> None:
        """Voice profiles other players' clips built, from the server's signed "voices-*" packs (pack_server checked
        them). Used beside our own: for each character the profile with more clips counts (ours may be built from
        the same clips, so the two are never added up)."""
        import json

        out: dict[str, dict] = {}
        for f in sorted(folder.glob("voices-*.json")) if folder.is_dir() else []:
            try:
                data_ = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data_.get("game") != self.game_title:
                continue
            for k, v in (data_.get("voices") or {}).items():
                if v.get("n") and (k not in out or v["n"] > out[k]["n"]):
                    out[k] = {"sum": list(map(float, v["sum"])), "n": int(v["n"])}
        with self._lock:
            self.shared = out

    def _decision_profiles(self) -> dict[str, dict]:
        """Ours and the shared ones; for a character the one with more clips. Call with the lock held."""
        out = dict(self.profiles)
        for k, p in self.shared.items():
            mine = out.get(k)
            if mine is None or (p["n"] > mine["n"] and len(p["sum"]) == len(mine["sum"])):
                out[k] = p
        return out

    # -------------------------------------------------------- profiles

    def _load(self) -> None:
        import json

        try:
            data_ = json.loads(self.profile_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data_ = {}
        self.profiles = {k: {"sum": list(map(float, v["sum"])), "n": int(v["n"])}
                          for k, v in (data_.get(self.game_title) or {}).items() if v.get("n")}

    def save(self) -> None:
        import json

        with self._lock:
            if not self.changed:
                return
            try:
                data_ = json.loads(self.profile_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data_ = {}
            if self._replace:  # rebuilt from all clips (voice_tool profile)
                new = {k: dict(v) for k, v in self.profiles.items()}
            else:
                # Add only what was learned in this session to what is in the file now. Writing the whole in-memory
                # copy would undo a rebuild done while the app was running.
                new = {k: {"sum": list(map(float, v["sum"])), "n": int(v["n"])}
                       for k, v in (data_.get(self.game_title) or {}).items()}
                for k, inc in self._added.items():
                    cur = new.setdefault(k, {"sum": [0.0] * len(inc["sum"]), "n": 0})
                    cur["sum"] = [a + b for a, b in zip(cur["sum"], inc["sum"])]
                    cur["n"] += inc["n"]
            data_[self.game_title] = {k: {"sum": [round(x, 6) for x in v["sum"]], "n": v["n"]}
                                      for k, v in sorted(new.items())}
            self.profile_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.profile_file.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(data_, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self.profile_file)
            self.changed = False
            self._added, self._replace = {}, False

    def learn(self, speaker: str, v) -> bool:
        """Adds the voice of a line with a known speaker to that character's profile (group labels excluded)."""
        if v is None or not speaker or speaker in GROUP_LABELS:
            return False
        with self._lock:
            for d in (self.profiles, self._added):
                p = d.setdefault(speaker, {"sum": [0.0] * len(v), "n": 0})
                p["sum"] = [a + float(b) for a, b in zip(p["sum"], v)]
                p["n"] += 1
            self.changed = True
        return True

    def reset(self) -> None:
        with self._lock:
            self.profiles, self._added = {}, {}
            self.changed = self._replace = True

    # -------------------------------------------------------- vector and decision

    def vector(self, pcm16k: bytes):
        """Unit-length voice vector (numpy); None if the clip is short or silent."""
        import numpy as np

        if len(pcm16k) < MIN_SECONDS * TARGET_RATE * 2 or loudness(pcm16k) < MIN_LOUDNESS:
            return None
        a = np.frombuffer(pcm16k[: len(pcm16k) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        st = self._extractor.create_stream()
        st.accept_waveform(TARGET_RATE, a)
        st.input_finished()
        v = np.asarray(self._extractor.compute(st), dtype=np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None

    def speaker(self, v, min_samples: int = MIN_PROFILE_SAMPLES) -> tuple[str | None, float, float]:
        """(character, similarity, margin over the second). Character is None if not sure. Profiles with fewer than
        min_samples clips are ignored."""
        import numpy as np

        if v is None:
            return None, 0.0, 0.0
        with self._lock:
            candidates = [(k, np.asarray(p["sum"], dtype=np.float32)) for k, p in self._decision_profiles().items()
                          if p["n"] >= min_samples and len(p["sum"]) == len(v)]
        score = []
        for k, t in candidates:
            n = float(np.linalg.norm(t))
            if n > 0:
                score.append((float(v @ (t / n)), k))
        if not score:
            return None, 0.0, 0.0
        score.sort(reverse=True)
        b1, k1 = score[0]
        margin = b1 - (score[1][0] if len(score) > 1 else 0.0)
        return (k1 if b1 >= THRESHOLD and margin >= MARGIN else None), b1, margin

    def summary(self) -> str:
        with self._lock:
            mine = ", ".join(f"{k} {p['n']}" for k, p in sorted(self.profiles.items(), key=lambda x: -x[1]["n"]))
            shared = ", ".join(f"{k} {p['n']}" for k, p in sorted(self.shared.items(), key=lambda x: -x[1]["n"]))
        return mine + (f" | sunucudan: {shared}" if shared else "")
