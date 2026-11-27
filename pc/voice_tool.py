"""Voice profiles (to recognize the speaker by voice). After the pack is updated and the speakers are labeled:

  py pc\\voice_tool.py status       how many clips there are, how many have a known speaker, the profiles
  py pc\\voice_tool.py profile      rebuilds the profiles from the labeled clips (pc\\voices\\profiles.json)
  py pc\\voice_tool.py validate     tries every labeled clip against profiles built from the others: accuracy, coverage
  py pc\\voice_tool.py list [DAY]   labeling help: each clip's pitch (male/female) and its most similar profile
  py pc\\voice_tool.py publish      puts this PC's profiles on the pack server ("voices-<game>", signed) so every
                                   app has them from the start (needs pool_admin_token and the signing key)

Speaker: if the pack knew it when the clip was saved, that; otherwise the pack's current "speakers" label (old clips
get labels too once the pack is updated). Group labels such as "pedestrian" and "police" are not used for profiles.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

import audio as S
import paths


def read_records(root: Path) -> list[dict]:
    entries = root / paths.CLIP_LIST
    if not entries.exists():
        return []
    outcome = []
    for line_count in entries.read_text(encoding="utf-8").splitlines():
        try:
            k = json.loads(line_count)
        except ValueError:
            continue
        if not (root / "clips" / k.get("file", "")).is_file():
            continue
        if "first" not in k:  # first-version records: the file name is the save time, the line was first seen ~2.8 s earlier
            try:
                t = time.mktime(time.strptime(k["file"][:15], "%Y%m%d-%H%M%S")) + int(k["file"][16:19]) / 1000
            except (ValueError, KeyError):
                continue
            k["first"] = t - 2.8
        k.setdefault("lead", 0.5)
        outcome.append(k)
    outcome.sort(key=lambda k: k["first"])
    for a, b in zip(outcome, outcome[1:]):  # if the next line started early, the clip ends there
        if "end" not in a:
            a["end"] = min(2.5, max(0.0, b["first"] - a["first"] - 0.1))
    if outcome:
        outcome[-1].setdefault("end", 2.5)
    return outcome


def segment(pcm16k: bytes, k: dict, end_time: float) -> bytes:
    """From the clip: [first - 0.1, first + min(end_time, end)] (s, relative to first seen)."""
    started = max(0, int((k["lead"] - 0.1) * S.TARGET_RATE))
    last = int((k["lead"] + min(end_time, k.get("end", end_time))) * S.TARGET_RATE)
    return pcm16k[started * 2: last * 2]


def pitch(pcm16k: bytes) -> float:
    """Median fundamental frequency of the speech (Hz, autocorrelation): male ~85-155, female ~165-255. 0 if not found."""
    import numpy as np

    a = np.frombuffer(pcm16k[: len(pcm16k) // 2 * 2], dtype=np.int16).astype(np.float32)
    frame_len, step = 640, 320  # 40 ms window, 20 ms step
    if len(a) < frame_len * 2:
        return 0.0
    threshold = np.percentile(np.abs(a), 90) * 0.3
    lo, hi = S.TARGET_RATE // 350, S.TARGET_RATE // 70
    vals = []
    for i in range(0, len(a) - frame_len, step):
        x = a[i:i + frame_len]
        if np.abs(x).mean() < threshold:
            continue
        x = x - x.mean()
        r = np.correlate(x, x, "full")[frame_len - 1:]
        k = lo + int(np.argmax(r[lo:hi]))
        if r[k] > 0.45 * r[0]:
            vals.append(S.TARGET_RATE / k)
    return float(np.median(vals)) if len(vals) >= 5 else 0.0


def speaker_of(k: dict, pack_) -> str | None:
    return k.get("label") or (pack_.speaker_by_sentence(k["en"]) if pack_ is not None else None) or k.get("speaker")


def prepare():
    import subtitler as A

    cfg = {**A.DEFAULTS, **A.load_config()}
    glossary = A.make_parts(cfg)[0]
    game_title = glossary.game_title if glossary else (cfg.get("game") or "")
    return game_title, A.make_pack(cfg, glossary)


PUBLISH_MIN = 5  # clips a profile needs to be shared (the app decides only with MIN_PROFILE_SAMPLES or more)


def publish(game_title: str, profiles: dict[str, dict]) -> int:
    import pack_server
    import subtitler as A

    voices = {k: {"sum": [round(float(x), 6) for x in p["sum"]], "n": int(p["n"])}
              for k, p in sorted(profiles.items()) if p["n"] >= PUBLISH_MIN and k not in S.GROUP_LABELS}
    if not voices:
        print("Paylaşılacak profil yok.")
        return 1
    pid = pack_server.pack_id_for(game_title, "voices-")
    data = {"game": game_title, "source": "voices", "lines": {}, "voices": voices}
    result = pack_server.publish_pack(A.load_config(), pid, data, title=f"{game_title} (ses profilleri)", auto=True)
    print(f"Yüklendi: {pid} ({len(voices)} profil: " + ", ".join(f"{k} {v['n']}" for k, v in voices.items())
          + f"), sürüm {result.get('version')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "profile", "validate", "list", "publish"])
    ap.add_argument("day", nargs="?", default="", help="liste: yalnız bu günün parçaları (ör. 2026-11-24)")
    a = ap.parse_args(argv)
    root = paths.VOICES
    game_title, pack_ = prepare()
    records = [k for k in read_records(root) if k.get("game", game_title) == game_title]
    labeled = [(k, speaker_of(k, pack_)) for k in records]
    counter = collections.Counter(speaker for _, speaker in labeled if speaker)
    print(f"{game_title}: {len(records)} ses parçası, {sum(counter.values())} tanesinin söyleyeni belli.")
    if counter:
        print("  " + ", ".join(f"{k} {n}" for k, n in counter.most_common()))

    recognizer = S.SpeakerRecognizer.open(root, game_title)
    if recognizer is None:
        print(f"Ses modeli ya da sherpa-onnx yok ({S.model_path(root)}).")
        return 1
    if a.cmd == "status":
        print("Profiller: " + (recognizer.summary() or "yok"))
        return 0
    if a.cmd == "publish":
        return publish(game_title, recognizer.profiles)
    if a.cmd == "list":
        import numpy as np

        profiles = {speaker: np.asarray(p["sum"], dtype=np.float32) for speaker, p in recognizer.profiles.items()}
        profiles = {speaker: v / np.linalg.norm(v) for speaker, v in profiles.items() if np.linalg.norm(v) > 0}
        for k, speaker in labeled:
            if a.day and not k.get("time", "").startswith(a.day):
                continue
            pcm = segment(S.read_wav(root / "clips" / k["file"]), k, 2.5)
            v = recognizer.vector(pcm)
            sims = sorted(((float(v @ p), name) for name, p in profiles.items()), reverse=True) if v is not None else []
            nearest = f"{sims[0][1]} {sims[0][0]:.2f}" if sims else "-"
            print(f"{k.get('time', '')[11:19]} perde {pitch(pcm):4.0f} | {nearest:12s} | {speaker or '':7s} | {k['en'][:70]}")
        return 0

    vectors = []
    for k, speaker in labeled:
        if not speaker or speaker in S.GROUP_LABELS:
            continue
        pcm = S.read_wav(root / "clips" / k["file"])
        long_vec = recognizer.vector(segment(pcm, k, 2.5))
        live = recognizer.vector(segment(pcm, k, 1.6))  # in the game the decision is made with this much audio
        if long_vec is not None:
            vectors.append((speaker, long_vec, live, k))

    if a.cmd == "profile":
        recognizer.reset()
        for speaker, long_vec, _, _ in vectors:
            recognizer.learn(speaker, long_vec)
        recognizer.save()
        print(f"Profiller yeniden çıkarıldı ({len(vectors)} parça): {recognizer.summary() or 'yok'}")
        return 0

    # validate: each clip against profiles built from all the others
    correct = wrong = undecided = 0
    confused = collections.Counter()
    for i, (speaker, _, live, k) in enumerate(vectors):
        if live is None:
            undecided += 1
            continue
        t = S.SpeakerRecognizer(root, game_title + " (deneme)", recognizer._extractor)
        t.profiles = {}
        for j, (speaker2, long_vec2, _, _) in enumerate(vectors):
            if j != i:
                t.learn(speaker2, long_vec2)
        guess, b, margin = t.speaker(live)
        if guess is None:
            undecided += 1
        elif guess == speaker:
            correct += 1
        else:
            wrong += 1
            confused[(speaker, guess)] += 1
    total = correct + wrong + undecided
    if not total:
        print("Denenecek etiketli parça yok.")
        return 0
    print(f"Doğrulama ({total} parça, eşik {S.THRESHOLD}, fark {S.MARGIN}): doğru {correct}, yanlış {wrong}, "
          f"karar yok {undecided}. Karar verilenlerde doğruluk "
          f"{correct / max(1, correct + wrong):.0%}, kapsama {(correct + wrong) / total:.0%}.")
    for (g, t_), n in confused.most_common(8):
        print(f"  {g} -> {t_}: {n}")

    # False alarm: a pedestrian/police line (voices without a profile) assigned to a character
    t = S.SpeakerRecognizer(root, game_title + " (deneme)", recognizer._extractor)
    t.profiles = {}
    for speaker2, long_vec2, _, _ in vectors:
        t.learn(speaker2, long_vec2)
    group_ = [k for k, speaker in labeled if speaker in S.GROUP_LABELS]
    false_alarms = collections.Counter()
    tried = 0
    for k in group_:
        v = recognizer.vector(segment(S.read_wav(root / "clips" / k["file"]), k, 1.6))
        if v is None:
            continue
        tried += 1
        guess = t.speaker(v)[0]
        if guess:
            false_alarms[guess] += 1
    if tried:
        print(f"Yaya/polis replikleri ({tried} parça): {sum(false_alarms.values())} tanesi yanlışlıkla bir "
              f"karaktere atandı" + (" (" + ", ".join(f"{k} {n}" for k, n in false_alarms.items()) + ")"
                                     if false_alarms else "") + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
