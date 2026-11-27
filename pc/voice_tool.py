"""Voice profiles (to recognize the speaker by voice). After the pack is updated and the speakers are labeled:

  py pc\\voice_tool.py status       how many clips there are, how many have a known speaker, the profiles
  py pc\\voice_tool.py profile      rebuilds the profiles from the labeled clips (pc\\voices\\profiles.json)
  py pc\\voice_tool.py validate     tries every labeled clip against profiles built from the others: accuracy, coverage
  py pc\\voice_tool.py list [DAY]   labeling help: each clip's pitch (male/female) and its most similar profile
  py pc\\voice_tool.py publish      puts this PC's profiles on the pack server ("voices-<game>", signed) so every
                                   app has them from the start (needs pool_admin_token and the signing key)
  py pc\\voice_tool.py upload       (main PC, once) sends the voice vectors of the clips already saved here to the
                                   server; the app sends every new clip's by itself (no audio either way)

Where the clips come from: on a PC with the voice model (pc\\voices\\model\\) and sherpa-onnx, the WAVs in
pc\\voices\\clips\\; on a PC without them (the Legion), or with --server, the voice vectors our PCs sent to the
pack server (kept in pc\\voices\\clip-vectors.jsonl, only new ones are fetched). sherpa-onnx is then never loaded.

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

SERVER_CACHE = "clip-vectors.jsonl"  # the server's clip vectors, kept here (pc\\voices\\)


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
    return S.window(pcm16k, k["lead"], k.get("end"), end_time)


pitch = S.pitch


def server_records(root: Path, cfg: dict, game_title: str, fetch: bool = True) -> list[dict]:
    """The clip vectors our PCs sent to the server, as records (with vec_long / vec_live / pitch instead of a WAV).
    New ones are fetched and added to the local copy; without a connection the local copy is used."""
    import pack_server

    gk = pack_server.game_key(game_title)
    cache = root / SERVER_CACHE
    known: dict[tuple[str, str], dict] = {}
    try:
        for line in cache.read_text(encoding="utf-8").splitlines():
            try:
                c = json.loads(line)
                known[(c["game"], c["file"])] = c
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    if fetch:
        after = max((int(c.get("id") or 0) for c in known.values() if c.get("game") == gk), default=0)
        try:
            new = pack_server.admin_clips(cfg, game_title, after=after)
        except pack_server.ServerError as e:
            print(f"Sunucudan ses vektörleri alınamadı ({e}); eldeki kopya kullanılıyor.")
            new = []
        new = [c for c in new if (c.get("game"), c.get("file")) not in known]
        if new:
            root.mkdir(parents=True, exist_ok=True)
            with open(cache, "a", encoding="utf-8") as f:
                for c in new:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")
                    known[(c["game"], c["file"])] = c
    out = [{**c, "game": game_title} for c in known.values() if c.get("game") == gk]
    out.sort(key=lambda k: (k.get("time", ""), int(k.get("id") or 0)))
    return out


def speaker_of(k: dict, pack_) -> str | None:
    return k.get("label") or (pack_.speaker_by_sentence(k["en"]) if pack_ is not None else None) or k.get("speaker")


def prepare():
    """(config, game title, pack)."""
    import subtitler as A

    cfg = {**A.DEFAULTS, **A.load_config()}
    glossary = A.make_parts(cfg)[0]
    game_title = glossary.game_title if glossary else (cfg.get("game") or "")
    return cfg, game_title, A.make_pack(cfg, glossary)


def upload(cfg: dict, root: Path, game_title: str, records: list[dict], recognizer) -> int:
    """The saved clips' voice vectors to the server (the same as the app sends for new clips); known ones are
    ignored there, so running it again does no harm."""
    import pack_server

    clips = []
    for i, k in enumerate(records, 1):
        try:
            c = S.clip_vectors(recognizer, S.read_wav(root / "clips" / k["file"]), k)
        except (OSError, ValueError) as e:
            print(f"  {k['file']}: okunamadı ({e})")
            continue
        if c is not None:
            clips.append(c)
        if i % 200 == 0:
            print(f"  {i}/{len(records)} parça hazırlandı")
    if not clips:
        print("Gönderilecek ses vektörü yok.")
        return 1
    added = 0
    for i in range(0, len(clips), 500):
        added += pack_server.post_clips(cfg, game_title, clips[i:i + 500])
        print(f"  {min(i + 500, len(clips))}/{len(clips)} gönderildi")
    print(f"Sunucuya gönderildi: {len(clips)} parçanın ses vektörü ({added} yeni, {len(clips) - added} zaten vardı; "
          f"{len(records) - len(clips)} parça kısa ya da sessiz). Ses dosyası gönderilmedi.")
    return 0


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
    ap.add_argument("cmd", choices=["status", "profile", "validate", "list", "publish", "upload"])
    ap.add_argument("day", nargs="?", default="", help="liste: yalnız bu günün parçaları (ör. 2026-11-24)")
    ap.add_argument("--server", action="store_true",
                    help="ses dosyaları yerine sunucudaki ses vektörleri (modeli olmayan bilgisayarda kendiliğinden)")
    a = ap.parse_args(argv)
    root = paths.VOICES
    cfg, game_title, pack_ = prepare()
    # Without the voice model the clips come from the server; sherpa-onnx is then never imported (on the Legion
    # Smart App Control blocks its DLL).
    from_server = a.server or (a.cmd != "upload" and not S.model_path(root).is_file())
    if a.cmd == "upload" and from_server:
        print("upload ses dosyalarından çalışır: ses modeli ve sherpa-onnx olan bilgisayarda (ana PC) çalıştır.")
        return 1
    if from_server:
        records = server_records(root, cfg, game_title)
    else:
        records = [k for k in read_records(root) if k.get("game", game_title) == game_title]
    labeled = [(k, speaker_of(k, pack_)) for k in records]
    counter = collections.Counter(speaker for _, speaker in labeled if speaker)
    print(f"{game_title}: {len(records)} ses parçası{' (sunucudaki ses vektörleri)' if from_server else ''}, "
          f"{sum(counter.values())} tanesinin söyleyeni belli.")
    if counter:
        print("  " + ", ".join(f"{k} {n}" for k, n in counter.most_common()))

    if from_server:
        recognizer = S.SpeakerRecognizer.offline(root, game_title)
    else:
        recognizer = S.SpeakerRecognizer.open(root, game_title)
        if recognizer is None:
            print(f"Ses modeli ya da sherpa-onnx yok ({S.model_path(root)}); sunucudaki ses vektörleriyle çalışmak "
                  "için --server ekle.")
            return 1
    if a.cmd == "status":
        print("Profiller: " + (recognizer.summary() or "yok"))
        return 0
    if a.cmd == "publish":
        return publish(game_title, recognizer.profiles)
    if a.cmd == "upload":
        return upload(cfg, root, game_title, records, recognizer)

    import numpy as np

    def vectors_of(k: dict, live_only: bool = False):
        """(long window vector, live window vector, pitch of the long window); a vector is None if unusable."""
        if from_server:
            def vec(x):
                return None if x is None else np.asarray(x, dtype=np.float32)
            return vec(None if live_only else k.get("vec_long")), vec(k.get("vec_live")), float(k.get("pitch") or 0)
        pcm = S.read_wav(root / "clips" / k["file"])
        long_pcm = segment(pcm, k, S.LONG_WINDOW)
        # in the game the decision is made with about LIVE_WINDOW s of audio
        live = recognizer.vector(segment(pcm, k, S.LIVE_WINDOW))
        if live_only:
            return None, live, 0.0
        return recognizer.vector(long_pcm), live, pitch(long_pcm)

    if a.cmd == "list":
        profiles = {speaker: np.asarray(p["sum"], dtype=np.float32) for speaker, p in recognizer.profiles.items()}
        profiles = {speaker: v / np.linalg.norm(v) for speaker, v in profiles.items() if np.linalg.norm(v) > 0}
        for k, speaker in labeled:
            if a.day and not k.get("time", "").startswith(a.day):
                continue
            v, _, hz = vectors_of(k)
            sims = sorted(((float(v @ p), name) for name, p in profiles.items()), reverse=True) if v is not None else []
            nearest = f"{sims[0][1]} {sims[0][0]:.2f}" if sims else "-"
            print(f"{k.get('time', '')[11:19]} perde {hz:4.0f} | {nearest:12s} | {speaker or '':7s} | {k['en'][:70]}")
        return 0

    vectors = []
    for k, speaker in labeled:
        if not speaker or speaker in S.GROUP_LABELS:
            continue
        long_vec, live, _ = vectors_of(k)
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
        v = vectors_of(k, live_only=True)[1]
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
