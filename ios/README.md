# Dilmaç for iPhone and Mac (v0.1, in development)

One app for both (the `Dilmac` target is multiplatform; see [Mac](#mac) for what differs). The iPhone connects to the PS5 directly (Remote Play protocol, chiaki-ng), reads the subtitles, translates them and
sends them to the TV overlay app the PC version uses. The TV side does not change.

```
PS5 ──(Remote Play, chiaki)──▶ iPhone ──(TCP 8787, JSON lines)──▶ Android TV app (tv/)
                                  └────(HTTP SSE, port 8788)────▶ LG / Samsung web app (tv-web/), later
```

## Build

- `ios/scripts/build-chiaki.sh` builds chiaki-ng and its dependencies into `ChiakiBridge.xcframework` (arm64);
  `pc/chiaki_bridge/bridge.c` / `bridge.h` is the thin C API both platforms use (`kp_version()` 3).
- `ios/Config/Base.xcconfig`; your Apple team id goes in `Local.xcconfig` (git-ignored, from the `.example`).
- `ios/DilmacCore`: Swift ports of textutil, stabilizer, pack lookup, glossary and the OCR line sorting, with tests
  (`cd ios/DilmacCore && swift test`). When those Python modules change, change the Swift ones too.
- English word list: pyspellchecker 0.9.1's (`ios/scripts/make-wordlist.py`).
- `pc/glossary/*.json` and the packs are bundled at build time (folder references). The packs live in the private
  `Dilmac-packs` repo (cloned into `pc/packs`): **a build that is handed out must bundle hashed copies only**
  (`pack.to_hashed`, see below), never the files with the English lines.

## Pipeline

1. VideoToolbox decodes the stream; 4–5 frames a second are cropped to the subtitle region.
2. Vision `VNRecognizeTextRequest` (accurate, en-US).
3. Translation: glossary, then pack (exact and fuzzy), else Apple Translation (on device, offline).
4. `show` / `clear` messages to the Android TV app on 8787:

```json
{"type":"show","id":"speech","text":"Türkçe satır","ttl":4.2,"new":true,"box":[x,y,w,h],"lines":2,"lineh":0.029,
 "opacity":0.97,"scale":1,"max_width":0.8,"old_secs":3,"color":"#FF9AD5","fade_ms":180,"size":32,"bottom":60}
{"type":"clear"}            {"type":"clear","id":"objective"}
```

`box` is where the English sits in the picture (0–1), the TV box covers it; each `id` is its own box; on `new` the
previous line stays faded for `old_secs`. Shown for `min(max_show, max(linger, len(tr)/15))` seconds. The TV's IP
changes with DHCP: scan the /24 for an open 8787.

GTA VI layout (measured): speech centered and white, objective at y ≈ 0.95, the bottom of the subtitle stack fixed at
y+h ≈ 0.914, line spacing 0.044–0.053; one box (splitting into blocks flickered); TV text 32 px.

## PSN sign-in and pairing

The flow is in `pc/ps5_direct.py` (`login_url`, `code_from_redirect`, `account_info`): open the Remote Play login
(the public client id chiaki-ng uses too); Sony redirects to `https://remoteplay.dl.playstation.net/remoteplay/redirect?code=…`;
POST the code to `oauth/token`, then GET `oauth/token/<access_token>` for `user_id` and `online_id`. The token is not
kept, only the account id (Keychain). `ASWebAuthenticationSession` cannot catch Sony's https redirect: use a
`WKWebView` with a non-persistent data store and cancel that navigation in `decidePolicyFor`. Pairing needs the
8-digit code from PS5 Settings → System → Remote Play → Pair Device. Keys stay in the Keychain
(`AfterFirstUnlockThisDeviceOnly`).

## Mac

The same target builds a native macOS 26 app (arm64, bundle id `com.dilmac.mac`, not sandboxed):
`xcodebuild -project ios/Dilmac.xcodeproj -scheme Dilmac -destination 'generic/platform=macOS' build`, or pick
"My Mac" in Xcode. `build-chiaki.sh` makes the `macos-arm64` slice too. The window has the PS5 picture with the
subtitle region outlined, status cards for the PS5, the TV and the translation engine, and the live subtitle list
(source and time to translate per line); PSN sign-in, pairing and settings are sheets.

Translation (`DilmacCore`: `LLMPrompt`, `OllamaClient`, `HedgedTranslator`, `TranslationCache`): glossary → pack →
cache → a **local LLM through [Ollama](https://ollama.com)** → Apple Translation. It is `pc/translation.py`'s flow:
the same prompt, checked byte for byte against the Python code (`ios/scripts/make-prompt-fixtures.py`), with the
game's names, nicknames, preferred slang wording, the speaker, the 3 most similar pack lines and the last 3
subtitles. If the model has not answered in 1.5 s, Apple Translation races it and the first answer is shown; the
model's late answer is still cached. While Ollama is down or the model is not pulled, Apple Translation is used and
the engine card says why; it is checked again every 30 s.

- Setup: install Ollama (the Mac app, from ollama.com) and pull the model, by default `gemma4:e4b-it-qat` (the PC's):
  `ollama pull gemma4:e4b-it-qat`. Settings (gear): engine (`Yerel LLM` / `Apple Çeviri`), model, Ollama address.
  The iPhone keeps Apple Translation (`engine` = `apple`).
- Cache: `~/Library/Application Support/Dilmac/cache.json` (the PC's format and prompt version), written every 5
  new lines, when a session stops and when the app quits.
- Saved frames: `~/Library/Application Support/Dilmac/frames`. Settings → "Kareleri dene..." runs PNG frames through
  the pipeline as if they came from the PS5.
- The Mac stays awake while a session runs (`beginActivity`, `.idleSystemSleepDisabled`).
- Secrets: the data protection keychain when the build has a provisioning profile, else the login keychain.
- Comparing models: `pc/.venv/bin/python pc/bench_local.py --models gemma4:e4b-it-qat,gemma4:12b --n 50` writes a
  latency table and the translations side by side to `ios/build/bench/` (git-ignored: it has the pack's English
  lines, never commit it).
- Developer checks without a PS5: `open Dilmac.app --args -trial "An English line."` translates one line at start;
  `--args -replay "/path/a.png,/path/b.png"` replays frames. Debug builds also print the log to stdout.

## iOS limits

- The app must stay in front (`isIdleTimerDisabled`), the phone on power; iOS stops it in the background.
- Finding the PS5 by broadcast needs the `com.apple.developer.networking.multicast` entitlement (ask Apple); until
  then the PS5's IP is typed in. `NSLocalNetworkUsageDescription` is required.
- Apple Translation's API differs by iOS version (SwiftUI `.translationTask` on iOS 18).

## Pack server (to do on iOS)

Today the app reads only the bundled packs. To download them like the PC:
- `GET /v1/packs` (index: `id, sha256, size, key, signature`), `GET /v1/packs/:id` → `.json.gz`; requests signed with
  `x-dilmac-time` (Unix seconds) and `x-dilmac-signature` = hex(HMAC-SHA256(APP_SECRET, METHOD + "\n" + path + "\n" +
  time)), ±5 min (`server/README.md`). The app secret is never in git: put it in at build time (a git-ignored
  xcconfig).
- Check the Ed25519 signature over `"dilmac-pack-v1\n<id>\n<sha256 hex of the .json.gz>"` with a key from
  `PUBLISHER_KEYS` in `pc/pack_server.py` (raw 32 bytes, base64; CryptoKit `Curve25519.Signing`).
- The apps' packs are hashed (`"format": "hashed-v1"`, `pc/pack.py`): `h` = {line_hash(text_key): Turkish},
  `o` = {line_hash(ocr_key): line_hash(text_key)}, `m` = {line_hash(text_key): "<ocr_key length>:<base64 MinHash>"},
  `s` = speakers. `line_hash(k)` = first 20 hex of SHA-256 of `"dilmac-line-v1\n" + k`; the MinHash is 32 × 16-bit
  values over the letter trigrams of `"^" + ocr_key + "$"` (blake2b-64 per trigram, `(a·h + b) mod (2^61−1)`, the
  coefficients from SHA-256 of `"dilmac-mh-a<i>"` / `"dilmac-mh-b<i>"`); a misread line matches when 8 bands of 4
  share one band and ≥ 60 % of the values agree, with a similar length. Port `pack.py`'s tests along.

## Open issue

Battery: 5 Vision "accurate" reads a second plus 30 fps decoding drain the phone. Next: read less often while there
is no text, a fast read first, a lower stream rate.
