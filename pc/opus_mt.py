"""Translation on this computer without a key or a GPU: OPUS-MT English→Turkish (Helsinki-NLP, opus-mt-tc-big-en-tr,
CC-BY 4.0) run with CTranslate2 (int8 on the CPU; the GPU if CUDA works).

The model is the CTranslate2 conversion of the same model CouchSubs uses: one zip from a fixed Hugging Face revision,
checked with SHA-256 before it is unpacked into pc/models/opus-mt-en-tr/ (downloaded once, ~475 MB).

Input sentences are cut into SentencePiece pieces with the model's source.spm and end with "</s>"; the output pieces
are joined back with target.spm.
"""
from __future__ import annotations

import hashlib
import shutil
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import paths

ARCHIVE_URL = ("https://huggingface.co/Unbakedcookie/live-translator-models/resolve/"
               "50473df5681764ebdef107dd78f7d21da66c28a7/opus-mt-en-tr.zip")
ARCHIVE_SIZE = 474_595_937
ARCHIVE_SHA256 = "acabcdc3becfbf4d740c3012d6dc4e8a1ba6ed41a62a125c37f6c228667ff9b9"
MODEL_DIR = paths.DATA / "models" / "opus-mt-en-tr"
REQUIRED = ("model.bin", "config.json", "source.spm", "target.spm")
SIZE_TEXT = "475 MB"


class OpusError(Exception):
    """Turkish message for the user."""


def installed(model_dir: Path | None = None) -> bool:
    d = model_dir or MODEL_DIR
    return all((d / n).is_file() for n in REQUIRED)


def available() -> bool:
    """The Python packages are there (they are in the exe)."""
    try:
        import ctranslate2  # noqa: F401
        import sentencepiece  # noqa: F401
    except ImportError:
        return False
    return True


def _find_model_root(folder: Path) -> Path | None:
    for p in [folder, *sorted(x for x in folder.rglob("*") if x.is_dir())]:
        if installed(p):
            return p
    return None


def download(progress=None, stop: threading.Event | None = None, url: str = ARCHIVE_URL,
             sha256: str = ARCHIVE_SHA256, size: int = ARCHIVE_SIZE, model_dir: Path | None = None) -> None:
    """Downloads, checks and unpacks the model. progress(done_bytes, total_bytes)."""
    model_dir = model_dir or MODEL_DIR
    model_dir.parent.mkdir(parents=True, exist_ok=True)
    part = model_dir.parent / (model_dir.name + ".zip.part")
    h = hashlib.sha256()
    done = 0
    last = 0.0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Dilmac"})
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or size)
            while True:
                if stop is not None and stop.is_set():
                    raise OpusError("İndirme iptal edildi.")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress and (time.monotonic() - last > 0.4):
                    last = time.monotonic()
                    progress(done, total)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise OpusError(f"Çeviri modeli indirilemedi: {e}")
    if h.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise OpusError("İndirilen çeviri modeli bozuk (özet tutmuyor); yeniden dene.")
    try:
        install_archive(part, model_dir)
    finally:
        part.unlink(missing_ok=True)
    if progress:
        progress(done, done)


def install_archive(archive: Path, model_dir: Path | None = None, sha256: str | None = None) -> None:
    """Unpacks a (checked) model zip into model_dir; the old model is replaced only when the new one is complete."""
    model_dir = model_dir or MODEL_DIR
    if sha256:
        h = hashlib.sha256()
        with open(archive, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != sha256:
            raise OpusError("Çeviri modeli arşivi bozuk (özet tutmuyor).")
    model_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = model_dir.parent / (model_dir.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
        root = _find_model_root(tmp)
        if root is None:
            raise OpusError("Çeviri modeli arşivinde beklenen dosyalar yok.")
        shutil.rmtree(model_dir, ignore_errors=True)
        shutil.move(str(root), str(model_dir))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class OpusMT:
    """The loaded model. translate() is thread safe (CTranslate2 serializes the batches itself)."""

    def __init__(self, model_dir: Path | None = None, device: str = "auto", threads: int = 4):
        import ctranslate2
        import sentencepiece as spm

        d = model_dir or MODEL_DIR
        if not installed(d):
            raise OpusError("Çeviri modeli indirilmedi.")
        self.device = "cpu"
        if device in ("auto", "cuda") and ctranslate2.get_cuda_device_count() > 0:
            try:
                self.model = ctranslate2.Translator(str(d), device="cuda", compute_type="int8_float16")
                self.device = "cuda"
            except Exception:
                self.model = None
        if self.device == "cpu":
            self.model = ctranslate2.Translator(str(d), device="cpu", compute_type="int8", intra_threads=threads)
        # Read in Python: SentencePiece's own file reading fails on non-ASCII paths on Windows ("...\Masaüstü\...").
        self.src = spm.SentencePieceProcessor(model_proto=(d / "source.spm").read_bytes())
        self.tgt = spm.SentencePieceProcessor(model_proto=(d / "target.spm").read_bytes())
        self.lock = threading.Lock()
        try:  # the first translation also checks that the GPU really works (CUDA DLLs)
            self.translate(["Hello."])
        except Exception:
            if self.device != "cuda":
                raise
            self.model = ctranslate2.Translator(str(d), device="cpu", compute_type="int8", intra_threads=threads)
            self.device = "cpu"
            self.translate(["Hello."])

    def translate(self, sentences: list[str]) -> list[str]:
        if not sentences:
            return []
        batch = [self.src.encode(s, out_type=str) + ["</s>"] for s in sentences]
        with self.lock:
            results = self.model.translate_batch(batch, beam_size=4, max_decoding_length=256,
                                                 repetition_penalty=1.1)
        return [self.tgt.decode(r.hypotheses[0]).strip() for r in results]


_model: OpusMT | None = None
_model_lock = threading.Lock()


def model() -> OpusMT:
    """The process's model, loaded on first use."""
    global _model
    with _model_lock:
        if _model is None:
            _model = OpusMT()
        return _model
