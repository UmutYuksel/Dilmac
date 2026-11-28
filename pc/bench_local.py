"""Compares local models (Ollama) on real game lines with slang: latency and the translations side by side, with
the pack's hand-checked Turkish as the reference. The prompt is the one the PC and the Mac app send
(translation.Translator.prompt_text), and the line itself is never among its pack examples.

    pc/.venv/bin/python pc/bench_local.py --models gemma4:e4b-it-qat,gemma4:12b --n 50

The report has the pack's English lines: it is written to ios/build/bench/ (git-ignored) and must never be committed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

import pack as P  # noqa: E402
import textutil as M  # noqa: E402
import translation as T  # noqa: E402

SLANG = re.compile(r"\b(fuck\w*|shit\w*|homie|bro|damn|ass\w*|hell|man|yo|bitch\w*|dude|ain't|gonna|gotta|y'all)\b",
                   re.IGNORECASE)
OUT_DIR = ROOT / "ios/build/bench"


def pick_lines(lines: dict[str, str], n: int, seed: int) -> list[tuple[str, str]]:
    """Mostly lines with slang, the rest at random; 4 to 30 words, so each is a real subtitle."""
    pool = [(en, tr) for en, tr in lines.items() if 4 <= len(M.words_(en)) <= 30 and tr.strip()]
    rnd = random.Random(seed)
    slang = [p for p in pool if SLANG.search(p[0])]
    rnd.shuffle(slang)
    chosen = slang[: max(1, n * 4 // 5)]
    rest = [p for p in pool if p not in chosen]
    rnd.shuffle(rest)
    return chosen + rest[: n - len(chosen)]


def translate(t: T.Translator, en: str) -> tuple[str, float, bool]:
    """The model's Turkish for one line (chunked as the app does), its latency in ms and whether the reply was
    valid."""
    chunks = M.chunk_sentences(M.split_sentences(en)) or [en]
    prompt = t.prompt_text(chunks, None, [])
    started = time.perf_counter()
    try:
        out = t._local(prompt, len(chunks))
        ok = True
    except Exception as e:  # noqa: BLE001 - a bad reply is a result here
        out, ok = [f"(geçersiz: {str(e)[:60]})"], False
    return T.merge(out), (time.perf_counter() - started) * 1000, ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pack", default=str(ROOT / "pc/packs/gta6.json"))
    ap.add_argument("--models", default="gemma4:e4b-it-qat,gemma4:12b")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    a = ap.parse_args(argv)

    data = json.loads(Path(a.pack).read_text(encoding="utf-8"))
    game = data.get("game", "")
    pk = P.Pack(game, data.get("lines", {}), data.get("speakers"))
    glossary = T.Glossary.load(game)
    lines = pick_lines(data.get("lines", {}), a.n, a.seed)
    models = [m.strip() for m in a.models.split(",") if m.strip()]

    results: dict[str, list[tuple[str, float, bool]]] = {}
    for model in models:
        cfg = {"game": game, "engine": "local", "local_model": model, "local_url": a.url, "context_lines": 3}
        t = T.Translator(cfg, glossary, log=lambda *_: None, cache_data=None, pack_=pk)
        started = time.perf_counter()
        t._local(t.prompt_text(["Hello."], None, []), 1)  # load the model; not counted
        print(f"{model}: yüklendi ({(time.perf_counter() - started):.1f} s)", flush=True)
        results[model] = []
        for i, (en, _) in enumerate(lines, 1):
            results[model].append(translate(t, en))
            print(f"  {i}/{len(lines)} {results[model][-1][1]:.0f} ms", end="\r", flush=True)
        print()
        t.pool.shutdown()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{dt.datetime.now():%Y%m%d-%H%M}.md"
    rows = ["# Yerel model karşılaştırması", "",
            f"{len(lines)} satır ({game}), argo ağırlıklı. Süre: satır başına, model yüklüyken.", "",
            "| Model | Medyan ms | p90 ms | Geçersiz cevap |", "|---|---:|---:|---:|"]
    for model in models:
        ms = sorted(r[1] for r in results[model])
        p90 = ms[min(len(ms) - 1, int(round(0.9 * (len(ms) - 1))))]
        bad = sum(not r[2] for r in results[model])
        rows.append(f"| {model} | {statistics.median(ms):.0f} | {p90:.0f} | {bad} |")
    rows += ["", "| # | EN | Paket (elle) | " + " | ".join(models) + " |", "|---|---|---|" + "---|" * len(models)]
    cell = lambda s: s.replace("|", "\\|").replace("\n", " ")  # noqa: E731
    for i, (en, tr) in enumerate(lines):
        rows.append(f"| {i + 1} | {cell(en)} | {cell(tr)} | " + " | ".join(cell(results[m][i][0]) for m in models) + " |")
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"yazıldı: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
