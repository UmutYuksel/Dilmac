# Dilmaç

**English** · [Türkçe](README.tr.md)

*Dilmaç* is an old Turkish word for "interpreter". Live Turkish subtitles for PS5 games. While you play on the TV, a Windows PC reads the game's English subtitles,
translates them into Turkish and shows them on the TV, right on top of the English text.

```
PS5 ──HDMI──> TV (you play here)
 │                        ▲
 │                        │ Wi-Fi: Turkish text, drawn over the game (Android/Google TV overlay app)
 └──Remote Play──> PC: reads the subtitle (Windows OCR) → translates (OPUS-MT on the PC / Ollama / Gemini / Google)
```

The PC gets the picture either from the official PS Remote Play window or straight from the PS5 (no Remote Play
window, via [chiaki-ng](https://github.com/streetpea/chiaki-ng)'s library). The current tuning targets
Grand Theft Auto VI; other games need their own layout settings (see [Other games](#other-games)).

> Status: a working personal project, being prepared for open source. The UI and the translations are Turkish; the
> code and docs are English.

## Features

- **Translation without a key:** by default OPUS-MT (Helsinki-NLP, English→Turkish) runs on the PC's CPU with
  CTranslate2 (~50-100 ms per line, the ~475 MB model is downloaded once). Better with slang: an LLM through
  [Ollama](https://ollama.com) (~0.3-0.6 s per line on a mid-range GPU) or Gemini; Google Translate is the fallback.
  Lines in the game pack always come first.
- **Reads like a subtitle:** fixed text size, the Turkish box covers the English line, consecutive lines stack with
  the previous one dimmed above, boxes fade in and out.
- **Understands the screen:** speech, mission objectives, phone texts and signs are told apart by position;
  overlapping subtitles (an NPC shouting while someone talks) each get their own box.
- **Game packs:** lines already seen are translated once, in context, by a strong model and reused. A pack grows
  with every session and teaches the small local model the game's tone.
- **Speaker colors:** Lucia pink, Jason blue, other characters yellow, pedestrians/police gray. From the pack's
  labels, and for new lines from the speaker's **voice** (TitaNet speaker embeddings via
  [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)).
- **Direct PS5 connection:** pair once with a PIN; afterwards no Remote Play window is needed. The PS5 is woken from
  rest mode when needed. TV sound is not affected.

## Requirements

- Windows 10/11 PC on the same network as the PS5 and the TV.
- The English OCR language pack for Windows.
- Python 3.12 (for running from source) or the packaged `Dilmac.exe`.
- An Android TV / Google TV (tested on a Philips 65PUS8808, Android 14) for the overlay app, or an LG webOS TV
  (webOS 4+, 2018 and later), or a Samsung TV (Tizen 3.0+, 2017 and later). LG and Samsung are not yet tested on a
  real TV.
- Optional: an NVIDIA GPU for the local model (Ollama), a Gemini API key.
- For the direct connection: a PSN account (a second, free account is recommended) and the PS5's Remote Play enabled.

## Setup

### 1. PC

1. English OCR pack (in an administrator PowerShell):
   ```powershell
   Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
   ```
2. Python packages:
   ```powershell
   py -m pip install -r pc\requirements.txt
   ```
3. Translation engine: the default `opus` needs nothing but the one-time model download (the GUI's "Modeli indir"
   button, into `pc\models\`). For `local`, install Ollama and pull the model set in `local_model` (default
   `gemma4:e4b-it-qat`); for `gemini`, get a key from https://aistudio.google.com/apikey.
4. Copy `pc\config.example.json` to `pc\config.json` and fill in `tv_ip` and `game`.

The GUI (`Dilmac.exe`, or `py pc\gui.py`) is an HTML page (`pc\ui\`) in an Edge WebView2 window (pywebview;
`pc\gui_web.py` is its Python side). On first start a setup wizard walks through the computer checks, PS5 pairing, the
TV (found on the network, brand recognized, install steps with live checks), the subtitle area and a test caption.
The main screen has start/stop, PS5 / TV / translation / game cards, a to-do list with fix buttons and the live
subtitles. Without WebView2 the older Tkinter GUI (`pc\gui_tk.py`) opens; `py pc\gui.py --tk` forces it.

### 2. TV

1. Settings → System → About → click "Android TV OS build" 7 times to enable developer options.
2. Settings → System → Developer options → turn on USB debugging (and Wireless debugging if present).
3. Press "Kur" in the GUI's TV row (the exe ships adb, no Android SDK needed) or run
   `.\tv\install.ps1 -TvIp <TV_IP>`, and allow debugging on the TV when asked.

#### LG TV (webOS)

LG does not let apps draw over the HDMI input, so on LG the TV app (`tv-web/`, a web app) stays in front, shows the
PS5's HDMI input inside itself and draws the subtitles above it. It connects to the PC (port 8788), not the other way
round.

1. Create a free account at https://webostv.developer.lge.com.
2. On the TV, install **Developer Mode** from the LG Content Store, sign in, turn on **Dev Mode Status** (the TV
   restarts).
3. In the GUI's Settings pick **LG (webOS)** as the TV type and find the TV with "Ağda bul".
4. In the Developer Mode app turn on **Key Server**, press "Kur" in the GUI's TV row and type the 6-character
   passphrase shown on the TV. The PC fetches the TV's SSH key, builds the `.ipk`, installs it and opens it.

From then on the app opens by itself when translation starts, is updated when `tv-web/` changes, and the 1000-hour
developer mode session is renewed once a day. Allow the app in Windows Firewall when asked. If the PS5 switches the TV
to its own input and the app goes to the background, turn off SIMPLINK (HDMI-CEC). Left/right on the TV remote picks the
HDMI input; OK shows the connection info.

#### Samsung TV (Tizen)

Same idea as LG: the app shows the HDMI input inside itself (Tizen's TV window behind the page). Samsung only installs
a package signed for that TV, so the PC signs it with certificates it requests with your free Samsung account.

1. On the TV open the Apps screen, press **1 2 3 4 5** on the remote, turn **Developer mode** on and enter this PC's
   address as **Host PC IP** (the GUI's Samsung window shows it). Restart the TV fully: hold the power button until
   the Samsung logo appears.
2. In the GUI's Settings pick **Samsung (Tizen)** as the TV type and find the TV with "Ağda bul".
3. Press "Kur" in the GUI's TV row. The browser opens Samsung's sign-in page (only the first time); after you sign in
   the PC requests the author and distributor certificates for the TV's DUID (kept in `pc\samsung\`, not in git),
   builds and signs the `.wgt`, installs it over sdb and opens it.

The PC's address is written into the package; if it changes, the TV app searches the network for it, and the next
translation start reinstalls the package with the new address (no sign-in needed). Turn off Anynet+ (HDMI-CEC) and
automatic source switching if the TV keeps leaving the app for the PS5's input.

### 3. Direct PS5 connection (optional, recommended)

In the GUI, press **Eşle** (Pair) in the PS5 row:

1. **PSN ile giriş yap** opens Sony's own sign-in page in a small window (Microsoft Edge in app mode, with a fresh
   temporary profile). Sign in with your PSN account; the password goes to Sony only. The app catches Sony's redirect
   by itself and closes the window (`psn_login.py`). Without Edge or Chrome, the system browser opens and the
   redirect address is pasted instead.
2. On the PS5 open Settings → System → Remote Play → Link Device and type the 8-digit code into the app.

The video source switches to "Doğrudan PS5" (direct) by itself. The pairing is stored in `pc\ps5-pairing.json`
(private: it holds your account id and Remote Play keys). Close the official Remote Play app first: the PS5 accepts
one Remote Play connection at a time.

## How it works

| Part | Module | What it does |
|---|---|---|
| Capture | `subtitler.py`, `ps5_direct.py` | Remote Play window (PrintWindow) or the direct chiaki stream (H.264 decoded with PyAV) |
| Reading | `ocr.py` | Windows OCR on two preparations (raw / white text with dark shadow), picks the more English-looking read, sorts rows into speech / objective / dropped, splits overlapping subtitles |
| Timing | `stabilizer.py` | decides when a line is stable enough to translate, when it was corrected, when it is gone |
| Translation | `translation.py`, `pack.py` | per-sentence cache, game glossary (`glossary/*.json`), game pack (`packs/*.json`) with similar lines as examples, context of the last lines |
| Voice | `audio.py` | game audio from the direct stream, a clip per line, speaker profiles, cosine match |
| TV | `tv/` | an overlay service listening on TCP 8787 for JSON messages |
| GUI | `gui.py`, `gui_web.py`, `ui/`, `checks.py` | HTML in a WebView2 window (pywebview): setup wizard, main screen, settings, region picker; `gui_tk.py` is the Tkinter fallback |

The protocol between PC and TV is one JSON object per line, for example:

```json
{"type": "show", "id": "speech", "text": "Hadi gidelim.", "ttl": 10, "box": [0.33, 0.886, 0.34, 0.029],
 "lineh": 0.0296, "opacity": 0.9, "color": "#8CCBFF", "fade_ms": 180, "new": true, "old_secs": 3}
```

## Game packs

Every translated line is written to `pc\lines\<day>.jsonl`. After a session the pack is updated:

```powershell
py pc\pack_tool.py pending                 # lines not in the pack yet, scene by scene -> pc\lines\pending.json
# translate them with the scene's context, label the speakers, save {"lines": {...}, "speakers": {...}}
py pc\pack_tool.py add translations.json   # merged into pc\packs\gta6.json
py pc\voice_tool.py profile                # rebuild the voice profiles from the newly labeled clips
py pc\voice_tool.py validate               # leave-one-out accuracy of the voice profiles
```

Step by step (with the pool's lines too): `PACKS.md`.

> **Copyright note:** game subtitles belong to their publishers, so no pack with English text is published. The
> hand-made packs live in a separate private repo (cloned into `pc\packs`). The pack server and the exe give every
> app hashed copies only (`pack.to_hashed`): the Turkish lines under a hash of the normalized English, plus a MinHash
> of its letter trigrams so lines with a misread letter or two are still found (on the GTA VI pack: one misread
> letter, 727 of 800 found against 738 with the text, no wrong line). Hashed lines give the model no prompt examples.
> Our own PCs (admin token) download the full copies.

## Other games

The pipeline is game independent; the GTA VI specifics live in `pc\glossary\gta6.json`:
`screen` (`center_tol`, `objective_y`, `block_step`), names to keep, nicknames, terms, colors. A new game needs its
own glossary file and a subtitle region (picked in the GUI's "Altyazı alanı" tab). Voice profiles are kept per game.
`pc\glossary\hogwarts-legacy.json` (2026-11-27) is the second one: names and spells kept, the Turkish Harry Potter
books' terms (house = bina, goblin = cin), no `screen` measurements yet. Lines without a pack are translated live and
go to the pool, which the Legion translates and "update the packs" (`PACKS.md`) reviews.

## Development

```powershell
py -m unittest discover -s pc\tests        # 100+ tests, no PS5/TV needed
py pc\build_exe.py                          # one-file Dilmac.exe (PyInstaller)
.\tv\build.ps1                              # TV APK without Gradle (Android SDK + JDK 17)
```

The chiaki bridge (`pc\chiaki_bridge\bridge.c`) is built in an MSYS2 UCRT64 shell:

```bash
bash pc/chiaki_bridge/build.sh <chiaki-ng source> <build dir> pc/chiaki_bridge/bin
```

The speaker model (`nemo_en_titanet_small.onnx`, 40 MB, from the
[sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models)) goes into
`pc\voices\model\`; without it voice recognition stays off.

Data written by older versions of the app (Turkish file and field names) is migrated once on start
(`paths.migrate_old_data`).

## Privacy

- Only the read subtitle text leaves the PC, and only when Gemini or the Google fallback is used. With the local
  model nothing leaves the PC.
- No picture, audio, PSN data or usage statistics are sent anywhere. PC ↔ TV traffic stays on the home network.
- Private files are kept out of git (see `.gitignore`): `config.json`, `ps5-pairing.json`, logs, recorded frames,
  voice clips and profiles, the translation cache.

## License

The app links [libchiaki](https://github.com/streetpea/chiaki-ng) (AGPL-3.0), so it is distributed under the
**GNU Affero General Public License v3.0**.

## Acknowledgments

- [chiaki-ng](https://github.com/streetpea/chiaki-ng) for the PS5 Remote Play protocol implementation.
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) and NVIDIA's TitaNet for speaker embeddings.
- CouchSubs for the idea of community-built subtitle packs and for the on-computer OPUS-MT default (no code was taken
  from it).
- [OPUS-MT](https://github.com/Helsinki-NLP/Opus-MT) (Helsinki-NLP, `opus-mt-tc-big-en-tr`, CC-BY 4.0) and
  [CTranslate2](https://github.com/OpenNMT/CTranslate2) (MIT) for the translation on the PC.

Not affiliated with Sony Interactive Entertainment or any game publisher. "PlayStation" and "PS5" are trademarks of
Sony Interactive Entertainment Inc.
