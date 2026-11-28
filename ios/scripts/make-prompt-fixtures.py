"""Writes ios/DilmacCore/Tests/DilmacCoreTests/Resources/prompt-fixtures.json: prompts and reply parses made by
pc/translation.py, so DilmacCore's LLMPrompt is checked to be byte for byte the same as the PC's.

The lines here are made up (no game text from the private packs). Run from the repo root:
    pc/.venv/bin/python ios/scripts/make-prompt-fixtures.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pc"))

import pack  # noqa: E402
import translation  # noqa: E402

OUT = ROOT / "ios/DilmacCore/Tests/DilmacCoreTests/Resources/prompt-fixtures.json"
GAME = "Test Game"

GLOSSARY = {
    "game": GAME,
    "aliases": ["TG"],
    "style": "Argo argo kalsın; küfür yumuşatılmasın.",
    "keep": ["Cal", "Cal Hampton", "Lucia", "Vice City"],
    "address_terms": {"bro": "kardeş", "man": "lan"},
    "terms": {"homie": "kanka", "bro": "kanka", "the heat": "aynasızlar"},
    "nicknames": {"Lu": "Lucia", "Jay": "Jason"},
}
PACK = [
    ["Cal, get the damn car.", "Cal, şu lanet arabayı getir."],
    ["Get in the car, homie.", "Arabaya bin kanka."],
    ["Lucia, we gotta go now.", "Lucia, hemen gitmemiz lazım."],
    ["This boat is fucked.", "Bu tekne sıçtı."],
    ["Okay.", "Tamam."],
]
PROMPTS = [
    {"sentences": ["Hello."]},
    {"sentences": ["Hello."], "glossary": False, "pack": False, "game": ""},
    {"sentences": ["Cal Hampton told Lucia to wait in Vice City."]},
    {"sentences": ["Lu, get in the car."], "speaker": "JASON"},
    {"sentences": ["Yo bro, where's my homie?", "The heat is coming, man."], "speaker": "Jay"},
    {"sentences": ["Move it."], "history": [["We gotta go.", "Gitmemiz lazım."], ["Now?", "Şimdi mi?"]]},
    {"sentences": ["Brian, this boat is fucked."], "names": ["Brian", "Cal"]},
    {"sentences": ['He said "run" / now\\.', "Çabuk!\tŞimdi."], "pack": False},
]
REPLIES = [
    ('["Bir", "İki"]', 2),
    ('```json\n["Bir", "İki"]\n```', 2),
    ('```\n["Tek"]\n```', 1),
    ('{"translations": ["Bir", "İki"]}', 2),
    ('{"a": "Bir", "b": "İki"}', 2),
    ('"Tek satır"', 1),
    ("Tek satır, tırnaksız", 1),
    ('Sure! Here it is: ["Bir", "İki"] Hope it helps.', 2),
    ('["  boşluklu  ", "-"]', 2),
    ('["Bir"]', 2),
    ('["Bir", "İki", "Üç"]', 2),
    ("no json at all", 2),
    ("", 1),
    ("{broken", 1),
    ("42", 1),
]


def main() -> None:
    glossary = translation.Glossary(GLOSSARY)
    pk = pack.Pack(GAME, dict(PACK))
    prompts = []
    for case in PROMPTS:
        cfg = {"game": case.get("game", GAME), "context_lines": 3}
        t = translation.Translator(cfg, glossary if case.get("glossary", True) else None, log=lambda *_: None,
                                   cache_data=None, pack_=pk if case.get("pack", True) else None)
        t.names = list(case.get("names", []))
        history = [tuple(h) for h in case.get("history", [])]
        prompts.append({
            "sentences": case["sentences"], "speaker": case.get("speaker"), "history": case.get("history", []),
            "names": t.names, "game": cfg["game"], "glossary": case.get("glossary", True),
            "pack": case.get("pack", True),
            "prompt": t.prompt_text(case["sentences"], case.get("speaker"), history),
        })
        t.pool.shutdown()
    replies = []
    for raw, n in REPLIES:
        try:
            replies.append({"raw": raw, "n": n, "out": translation.reply_seq(raw, n)})
        except ValueError:
            replies.append({"raw": raw, "n": n, "error": True})
    data = {"system": translation.SYSTEM, "prompt_version": translation.PROMPT_VERSION,
            "glossary_json": json.dumps(GLOSSARY, ensure_ascii=False), "pack": PACK, "prompts": prompts,
            "replies": replies}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {len(prompts)} prompts, {len(replies)} replies")


if __name__ == "__main__":
    main()
