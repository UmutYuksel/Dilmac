# Dilmaç for Mac, part 1: the Mac app and local LLM translation

Date: 2026-11-28. Status: approved design, not yet planned.

## Why

The iPhone app works in game, but the phone's battery drains fast (5 Vision "accurate" reads a second plus 30 fps
decoding). A Mac on mains power removes the battery problem, joins the Apple ecosystem, and, more importantly, an
Apple Silicon Mac can run a local LLM that translates slang and context far better than Apple Translation. Gemini is out (too slow to answer); slang must be kept.

## The Mac version in three parts

Each part gets its own spec, plan and implementation, in this order:

1. **Mac app and local LLM translation** (this document): everything the iPhone does, plus Ollama translation with
   Apple Translation as the fallback, plus the game picture in the Mac window.
2. **Pack download**: signed packs from the Cloudflare pack server (`ios/README.md`, "Pack server"), in `DilmacCore`
   so the iPhone gets it too.
3. **Speaker colors from the voice**: game audio through `kp_audio_cb`, TitaNet speaker embeddings on the Mac, the
   PC's voice profiles (`pc/audio.py`). If the Mac records game audio, it sends clip vectors with the same
   `POST /v1/admin/clips` format the PC uses (`audio.clip_vectors`, `pack_server.ClipUploader`).

Not in any part for now: LG / Samsung TV (`tv-web/`) on Apple platforms; to be discussed separately.

## Approach

A native macOS target in the existing Xcode project, sharing the Swift code with the iPhone app. Rejected: Mac
Catalyst (looks foreign on the Mac, chiaki still needs its own slice, later limits) and porting the Python PC app
(Windows OCR, WebView2 and packaging would all be rewritten, and a third code base would drift from the iPhone's).

## Architecture

- The existing **Dilmac** target in `ios/Dilmac.xcodeproj` becomes multiplatform (iOS + macOS over the same
  synchronized `ios/Dilmac/` folder; decided while planning, far fewer hand edits than a second target): macOS 26,
  arm64 only, bundle id `com.dilmac.mac` on the Mac, same team id from `ios/Config/Local.xcconfig`.
- Shared as is: `AppModel`, `Chiaki`, `PS5Discovery`, `Keychain`, `Models`, `GameData`, `RegionImage`,
  `SubtitlePipeline`, `SubtitleReader`, `Translator`, `TVLink`, `H264Decoder`, all of `DilmacCore`.
- Platform specific, split with `#if os(macOS)` (or a `+Mac.swift` file where the difference is larger than a few
  lines):
  - `VideoView`: an `NSView` with `wantsLayer` and the same `AVSampleBufferDisplayLayer`; `FrameDisplay` is shared.
  - `PSNLogin`: the `WKWebView` wrapper as `NSViewRepresentable`; the navigation logic (cancel Sony's redirect in
    `decidePolicyFor`, non-persistent data store) is shared.
  - Staying awake: `ProcessInfo.processInfo.beginActivity(options: [.idleSystemSleepDisabled, .userInitiated], …)`
    while a session runs, instead of `isIdleTimerDisabled`.
  - Local network: macOS needs no multicast entitlement for broadcast discovery outside the App Store sandbox; the
    app is not sandboxed for now (developer-signed, run from Xcode or a signed build), so PS5 discovery by broadcast
    should work. If it does not, the typed-in IP stays.
- `ios/scripts/build-chiaki.sh` builds a third slice, `macos` (`-DCMAKE_SYSTEM_NAME=Darwin`, `macosx` SDK, OpenSSL
  target `darwin64-arm64-cc`, `-mmacosx-version-min=26.0`), and adds it to the same `ChiakiBridge.xcframework`. The
  `ios_stubs.c` stubs (holepunch, AIA) are used on macOS too.
- Secrets stay in the Keychain (`AfterFirstUnlockThisDeviceOnly` is valid on macOS for the data protection keychain;
  use `kSecUseDataProtectionKeychain`), never in the repo. A build signed without a provisioning profile is refused
  the data protection keychain; the app then uses the login keychain.

## Translation

Order: glossary → pack (exact, then fuzzy) → cache → **Ollama LLM** → Apple Translation as the fallback.

- `DilmacCore` gets the platform independent parts, ported from `pc/translation.py` with tests:
  - `LLMPrompt`: the `SYSTEM` prompt (verbatim, with its style examples) and `prompt_text`: game title, glossary
    style notes, names to keep (glossary plus names learned from speaker labels), nicknames, preferred wording,
    speaker, the 3 most similar pack lines, the last `context_lines` (3) subtitles, the sentences as a JSON array.
    A `PROMPT_VERSION` constant shared with the PC, so caches made with another prompt are dropped.
  - `reply_seq`: parse the model's JSON array reply into exactly n strings.
  - `Pack.similar(_:n:threshold:)`: port of `pack.py`'s `similar` (content-word overlap, cosine, 3 pairs, ≥ 0.3,
    the same line excluded).
- App side, a new `LLMTranslator` (actor) talks to Ollama at `http://127.0.0.1:11434/api/chat`: `stream: false`,
  `keep_alive: "60m"`, `think: false` (dropped if the model rejects it), `format` = JSON schema of an array of exactly
  n strings, `temperature: 0`, `num_predict: 160 + 120·n`. One kept-alive `URLSession`.
- `Translator` becomes the Mac's and the iPhone's common entry with an engine setting: `apple` (the iPhone's
  default) or `llm` (the Mac's default). With `llm`:
  - The LLM and, after 1.5 s without an answer (`local_hedge_ms`), Apple Translation race; the first answer is shown.
    A late LLM answer still goes into the cache, so the next time that line comes it is the LLM's.
  - If Ollama is not reachable (connection refused, model missing), Apple Translation is used directly and the
    engine card shows the reason ("Ollama kapalı", "Model yok: <name>"); the app checks again every 30 s.
  - Warm-up at session start (translate "Hello." once) so the first real line does not wait for the model to load.
  - History and learned names as in the PC's `Translator`.
- The cache is kept on disk on the Mac (`~/Library/Application Support/Dilmac/cache.json`, format and versioning as
  `pc/translation.py`'s `CACHE_VERSION` / `PROMPT_VERSION`), written every 5 new entries and on stop.
- Model choice is measured, not guessed: once Ollama is installed (the user is asked before installing it), the
  PC's default `gemma4:e4b-it-qat` and one or two larger models are run over ~50 real GTA VI lines (with slang),
  and the user gets a table of latency (median, p90) and the translations side by side to pick from. The model is
  a setting (`local_model`).

## Interface

One window (SwiftUI, macOS):

- Left: the PS5 picture with the subtitle region outlined.
- Right: status cards for PS5, TV and the translation engine (engine, model, Ollama state), start / stop, and the
  live subtitle list with each line's source (sözlük, paket, önbellek, LLM, Apple) and time to translate.
- PSN sign-in and pairing: the iPhone's flow, in a sheet.
- Settings: TV IP, game, engine, model, Ollama URL.

The UI is Turkish; code and identifiers English.

## Reading

On the Mac there is no battery limit: 5 frames a second, Vision `.accurate`, as today. The iPhone's battery work is
separate and not changed by this part.

## Errors

- PS5 connection lost: the session stops, the PS5 card says why, start tries again (as the iPhone).
- TV not reachable: scan the /24 for port 8787 (as the iPhone); subtitles keep being read and translated meanwhile.
- Ollama down or slow: Apple Translation fallback as above; never a stopped pipeline.
- Apple's Turkish model missing: the engine card offers the download (as the iPhone).

## Testing

- `cd ios/DilmacCore && swift test`: new tests for `LLMPrompt` (the same prompt text as `pc/translation.py` for the
  same inputs; fixtures generated from the Python code), `reply_seq` (well formed, extra or missing items, junk),
  `Pack.similar` (the same pairs as `pack.py` on a fixture pack).
- `LLMTranslator` against a stub HTTP server: hedge timing, late answer cached, Ollama down → Apple.
- End to end on the Mac without the PS5: saved frames through OCR and translation.
- Finally a real game session; latency per line read from the log.

## Done when

- The Mac app connects to the PS5, shows the picture, reads subtitles and shows the Turkish on the Android TV.
- New lines are translated by the local LLM, with Apple Translation filling in when it is late or down.
- The iPhone app still builds and behaves as before (engine `apple`).
- `swift test` passes; the user has picked the model from the measured table.
