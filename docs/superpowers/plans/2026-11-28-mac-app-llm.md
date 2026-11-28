# Dilmaç for Mac, part 1: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The iPhone app also runs natively on the Mac. New lines are translated by a local LLM through Ollama, with
Apple Translation filling in when the LLM is late or down.

**Architecture:** The existing `Dilmac` Xcode target becomes multiplatform (iOS + macOS) over the same synchronized
`ios/Dilmac/` folder, with `#if os(macOS)` only where UIKit is used. All the logic that can be tested without a device
goes into `ios/DilmacCore` (SwiftPM, `swift test` on the Mac): the LLM prompt, reply parsing, `Pack.similar`, the
glossary fields the prompt needs, the Ollama client, the race against the fallback and the disk cache. chiaki-ng gets a
`macos` slice in the same `ChiakiBridge.xcframework`.

**Tech Stack:** Swift 6.4 / SwiftUI / Xcode 27.2, macOS 26 + iOS 26, SwiftPM, VideoToolbox, Vision, Apple Translation,
URLSession → Ollama HTTP API, CMake + Ninja (chiaki-ng v1.10.1, OpenSSL, opus).

**Spec:** `docs/superpowers/specs/2026-11-28-mac-app-llm-design.md`

## Global Constraints

- macOS 26 or later, arm64 only. iOS stays at 26.0, and the iPhone build must keep building and behaving the same
  (engine `apple`).
- Bundle ids: `com.dilmac.ios` (unchanged); on the Mac `com.dilmac.mac`. Team id only in the git-ignored
  `ios/Config/Local.xcconfig`.
- UI text is Turkish; code, identifiers, comments and docs are English. The repo is public: no secrets, IPs, account
  ids, English game lines or personal details in commits.
- Translation order: glossary → pack (exact, then fuzzy) → cache → LLM → Apple Translation.
- Ollama: `http://127.0.0.1:11434/api/chat`, `stream: false`, `keep_alive: "60m"`, `think: false` (dropped once
  the model rejects it with HTTP 400), `format` = `{"type":"array","items":{"type":"string"},"minItems":n,"maxItems":n}`,
  `options: {"temperature": 0, "num_predict": 160 + 120*n}`.
- Hedge: Apple Translation starts after **1.5 s** (`local_hedge_ms` 1500) without an LLM answer. A late LLM answer is
  still cached. Ollama is checked again every **30 s** while it is down.
- `context_lines` 3, pack examples: 3 pairs with score ≥ 0.3. `PROMPT_VERSION` 2, `CACHE_VERSION` 2 (as
  `pc/translation.py`). The cache is written every 5 new entries and on stop, to
  `~/Library/Application Support/Dilmac/cache.json`.
- Secrets stay in the Keychain (`kSecUseDataProtectionKeychain` on macOS). The Mac app is not sandboxed.
- Ask the user before installing anything (Ollama, models, Homebrew packages).
- Deviation from the spec, decided here: the spec names a separate target **DilmacMac**. This plan makes the existing
  target multiplatform instead (one synchronized folder, far fewer hand edits to `project.pbxproj`). Task 6 updates
  the spec to match.

## Review Focus

1. **Ollama answers with the wrong number of items or with prose** (small models do this under load): `replySeq`
   throws, and that line falls back to Apple instead of showing garbage or nothing. Pinned in Task 2
   (`testReplySeqWrongCountThrows`) and Task 4 (`testInvalidReplyFallsBack`).
2. **Ollama is running but the model is not pulled** (HTTP 404 `model not found`): the engine card says
   "Model yok: <name>", lines still get translated by Apple, and nothing retries every line. Pinned in Task 3
   (`testMissingModel`) and Task 4 (`testDownEngineSkipsLLMUntilRecheck`).
3. **The same line twice while the first LLM call is still running** (the stabilizer corrects a reading): only one
   request goes out, and both get the same answer. Pinned in Task 4 (`testConcurrentSameTextOneRequest`).
4. **Cache file from an older prompt or a broken file** (a crash mid-write): it is backed up as `cache.eski.json` and
   the cache starts empty. The app never crashes on it. Pinned in Task 4 (`testOldOrBrokenCacheStartsEmpty`).
5. **Hashed packs** (the builds handed out bundle only hashed packs, with no English): `similar()` returns `[]` and
   the prompt has no example section. It must not crash or produce an empty "Similar lines" header. Pinned in Task 1
   (`testSimilarOnHashedPackIsEmpty`).

---

### Task 1: Glossary fields and `Pack.similar` in DilmacCore

**Files:**
- Modify: `ios/DilmacCore/Sources/DilmacCore/Pack.swift` (struct `Glossary`, class `Pack`)
- Test: `ios/DilmacCore/Tests/DilmacCoreTests/PackTests.swift`

**Interfaces:**
- Produces, on `Glossary`:
  - `style: String` (JSON `"style"`)
  - `terms: [String: String]` (`address_terms` merged with `terms`; `terms` wins on a clash)
  - `func relevant(_ text: String) -> (names: [String], terms: [(en: String, tr: String)])`
  - `func nicknameMap(_ text: String) -> [(nick: String, name: String)]`
  - `static func occurs(_ phrase: String, in text: String) -> Bool`
- Produces, on `Pack`: `func similar(_ text: String, n: Int = 3, threshold: Double = 0.3) -> [(en: String, tr: String)]`
- Order matters: the prompt has to match the Python output byte for byte. `relevant` keeps `keepNames` order, and
  terms keep the JSON order (`address_terms` first, then `terms`). Read the JSON with an order-preserving decode, for
  example `JSONSerialization` plus the key order from a scan of the raw data, or the pairs as an array. Python dicts
  keep insertion order.

- [ ] **Step 1: Write failing tests** in `PackTests.swift`:
  - `testRelevantNamesAndTerms`: a glossary with `keep: ["Cal", "Cal Hampton", "Lucia"]` and
    `terms: {"homie": "kanka"}`. Then `relevant("Cal Hampton told the homie")` gives names `["Cal Hampton"]` and terms
    `[("homie", "kanka")]`, and `relevant("calhampton")` gives `([], [])`.
  - `testNicknameMapIsCaseSensitive`: `nicknames: {"Lu": "Lucia"}`. `nicknameMap("Lu, wait")` gives
    `[("Lu", "Lucia")]`, and `nicknameMap("lu wait")` gives `[]`.
  - `testSimilar`: a pack with the 5 `packLines`. `similar("A lot of fucking cops here, Bill.")` starts with
    `("A lot of fucking guys here, Bill.", "Burada bir sürü adam var lan, Bill.")`. `similar` of the exact pack line
    leaves that line out. `similar("zzz")` gives `[]`.
  - `testSimilarOnHashedPackIsEmpty`: a pack loaded from a `hashed-v1` file gives `similar("Meet me at the trailer.") == []`.
- [ ] **Step 2:** `cd ios/DilmacCore && swift test --filter PackTests`. Expected: FAIL, the members are missing.
- [ ] **Step 3: Implement** by porting `pc/translation.py` `Glossary._occurs` / `relevant` / `nickname_map` and
  `pc/pack.py` `similar`:
  - `occurs` uses the regex `(?<![\w'])` + escaped phrase + `(?![\w'])`, case-insensitive. In `nicknameMap` it is
    case-sensitive.
  - `similar` scores `common / sqrt(|words| * |pairWords|)`. Ties are broken by the higher score first, then the lower
    pair index (Python sorts `(score, -i)` in reverse).
- [ ] **Step 4:** `swift test`. Expected: all pass, the old tests too.
- [ ] **Step 5:** Commit `DilmacCore: glossary terms, nicknames and Pack.similar for the LLM prompt`.

### Task 2: `LLMPrompt` (system text, user prompt, reply parsing) with fixtures from Python

**Files:**
- Create: `ios/DilmacCore/Sources/DilmacCore/LLMPrompt.swift`
- Create: `ios/scripts/make-prompt-fixtures.py`
- Create: `ios/DilmacCore/Tests/DilmacCoreTests/Resources/prompt-fixtures.json` (generated, committed)
- Create: `ios/DilmacCore/Tests/DilmacCoreTests/LLMPromptTests.swift`
- Modify: `ios/DilmacCore/Package.swift` (test target `resources: [.copy("Resources/prompt-fixtures.json")]`)

**Interfaces:**
- Consumes: Task 1's `Glossary.relevant`, `nicknameMap`, `style`, and `Pack.similar`.
- Produces:
  - `public enum LLMPrompt`
  - `static let version = 2`
  - `static let system: String`, verbatim `SYSTEM` from `pc/translation.py`
  - `static func user(sentences: [String], speaker: String?, history: [(en: String, tr: String)], game: String,
    glossary: Glossary?, learnedNames: [String], pack: Pack?) -> String`
  - `static func replySeq(_ raw: String, count n: Int) throws -> [String]`
  - `struct ReplyError: Error`

- [ ] **Step 1: Write the fixture generator.** `make-prompt-fixtures.py` builds `translation.Translator(cfg,
  glossary, cache_data=None, pack_=pack)` from a small inline glossary and pack. No game text is copied from the
  private packs; use made-up lines like those in `PackTests`. It writes about 8 cases
  `{"sentences", "speaker", "history", "names", "prompt"}`, covering names, nicknames, terms, style, a speaker, history
  and pack examples, plus the empty case. It also writes `replySeq` cases `{"raw", "n", "out" | "error": true}`: a
  plain array, a ```json fence, an object wrapper, a bare string with n=1, prose around an array, a wrong count, and
  junk. Run it with `pc/.venv/bin/python ios/scripts/make-prompt-fixtures.py`.
- [ ] **Step 2: Write failing tests** in `LLMPromptTests.swift`:
  - `testPromptMatchesPython`: every fixture gives `LLMPrompt.user(...) == fixture.prompt`.
  - `testSystemMatchesPython`: `LLMPrompt.system == fixtures.system`. The generator dumps `translation.SYSTEM`.
  - `testReplySeqCases`: covers every `replySeq` case.
  - `testReplySeqWrongCountThrows`: `replySeq("[\"a\"]", count: 2)` throws.
- [ ] **Step 3:** `swift test --filter LLMPromptTests`. Expected: FAIL.
- [ ] **Step 4: Implement** `LLMPrompt` by porting `prompt_text` and `reply_seq` line by line. The JSON array of
  sentences must be encoded like Python's `json.dumps(..., ensure_ascii=False)`: `", "` separators, no escaping of
  non-ASCII, `/` not escaped.
- [ ] **Step 5:** `swift test`. Expected: all pass.
- [ ] **Step 6:** Commit `DilmacCore: LLM prompt and reply parsing, checked against pc/translation.py`.

### Task 3: `OllamaClient` in DilmacCore

**Files:**
- Create: `ios/DilmacCore/Sources/DilmacCore/OllamaClient.swift`
- Create: `ios/DilmacCore/Tests/DilmacCoreTests/OllamaClientTests.swift` (with a `URLProtocol` stub)

**Interfaces:**
- Produces:
  - `public actor OllamaClient`
  - `init(base: URL = URL(string: "http://127.0.0.1:11434")!, model: String, session: URLSession = .shared)`
  - `func translate(prompt: String, count n: Int) async throws -> [String]`: system plus user message, the body from
    Global Constraints, the reply through `LLMPrompt.replySeq`
  - `func check() async -> OllamaStatus`: `GET /api/tags`; the model is ready if its name, or its name plus
    `:latest`, is listed
  - `public enum OllamaStatus: Equatable { case ready, down, modelMissing(String) }`
  - `public struct OllamaError: Error { let status: Int; let body: String }`

- [ ] **Step 1: Write failing tests:**
  - `testRequestBody`: the stub captures the body. It has `model`, `stream == false`, `keep_alive == "60m"`,
    `think == false`, `format.minItems == 2`, `options.num_predict == 400` for n=2, and the messages
    `[system, user]` with `LLMPrompt.system`.
  - `testThinkRetriedWithout`: the first reply is 400 with a body containing `think`. A second request without
    `think` goes out, and later calls never send it again.
  - `testMissingModel`: `/api/tags` lists `["gemma4:e4b-it-qat"]`, so model `qwen3:14b` gives `.modelMissing("qwen3:14b")`,
    and a connection error gives `.down`.
  - `testNon200Throws`: a 500 throws `OllamaError(status: 500, ...)`.
- [ ] **Step 2:** `swift test --filter OllamaClientTests`. Expected: FAIL.
- [ ] **Step 3: Implement.** Use one `URLSession` with a request timeout of 180 s (the model may be loading). The
  hedge, not this timeout, keeps the subtitles moving.
- [ ] **Step 4:** `swift test`. Expected: all pass.
- [ ] **Step 5:** Commit `DilmacCore: Ollama client`.

### Task 4: `HedgedTranslator` core (history, names, cache, race, fallback)

**Files:**
- Create: `ios/DilmacCore/Sources/DilmacCore/HedgedTranslator.swift`
- Create: `ios/DilmacCore/Sources/DilmacCore/TranslationCache.swift`
- Create: `ios/DilmacCore/Tests/DilmacCoreTests/HedgedTranslatorTests.swift`

**Interfaces:**
- Consumes: `LLMPrompt` (Task 2) and `OllamaStatus` (Task 3).
- Produces:
  - `public final class TranslationCache: @unchecked Sendable`
    - `init(file: URL?)` (nil means memory only)
    - `subscript(key: String) -> String?`
    - `func set(_ sentences: [String], _ translations: [String])`
    - `func save()`
    - Format `{"version":2,"prompt":2,"sentences":{textKey: tr}}`. Writes every 5 `set`s through a temp file plus
      replace. An old version or a broken file is moved to `<name>.eski.json`.
  - `public actor HedgedTranslator`
    - `init(pack: Pack?, glossary: Glossary?, game: String, cache: TranslationCache, primary: (@Sendable (String, Int) async throws -> [String])?, fallback: @Sendable (String) async throws -> String, hedge: Duration = .milliseconds(1500), status: (@Sendable () async -> OllamaStatus)? = nil, recheck: Duration = .seconds(30))`
    - `func translate(_ text: String) async throws -> (text: String, source: String)`, where source is one of
      `"sözlük"`, `"paket"`, `"önbellek"`, `"LLM"`, `"Apple"`
    - `var engineState: OllamaStatus { get }`
    - `func warmUp() async`: translates `"Hello."` through `primary`, errors swallowed, sets `engineState`.
  - Port `Translator.translate` from `pc/translation.py`:
    - Split off the speaker and learn the name (not "you"/"me", title-cased if all caps).
    - Glossary, then pack. A pack hit is added to history.
    - Chunk the sentences, check pack or cache per chunk, and send only the missing chunks to the LLM with
      `LLMPrompt.user` (history = the last 3).
    - Join with `merge` semantics (drop `"-"` pieces, collapse whitespace). Add to history if not `"-"`.
    - With `primary == nil`, behave like today's iPhone `Translator`: Apple per chunk, source `"Apple"`.
  - Race: start `primary`. After `hedge` without an answer, also start `fallback(body)` and take the first success.
    If the primary fails, use the fallback right away. A late primary success is still written to the cache. The
    fallback result is not cached (as the PC does with Google).
  - Down handling: when `primary` throws a connection error or `status()` reports not `.ready`, skip the primary
    for `recheck` and go straight to the fallback.

- [ ] **Step 1: Write failing tests** (fake `primary`/`fallback` closures with controllable delays; a temp folder for
  the cache):
  - `testOrderGlossaryPackCacheLLM`: each source comes back with its label, and the LLM gets only the uncached chunks.
  - `testPromptCarriesHistoryAndSpeaker`: after two lines, the third LLM prompt contains `Previous subtitles` with
    both, and `Speaker: Jason` for `"JASON: Move."`.
  - `testHedgeFallbackThenLateLLMCached`: the primary takes 3 s, the fallback 0.1 s. The result is Apple's, and a
    second call for the same text returns `("<llm text>", "önbellek")`.
  - `testInvalidReplyFallsBack`: the primary throws `LLMPrompt.ReplyError`, so the result is Apple's immediately.
  - `testDownEngineSkipsLLMUntilRecheck`: the primary throws `URLError(.cannotConnectToHost)`. The next 3 lines inside
    the window never call the primary, and after `recheck` (set to 0.2 s in the test) it is tried again.
  - `testConcurrentSameTextOneRequest`: two concurrent `translate` calls with the same text give one primary call
    and equal results.
  - `testOldOrBrokenCacheStartsEmpty`: a file with `"prompt":1` and a file with `"{"`. Both are renamed to
    `.eski.json`, and lookups miss.
  - `testAppleOnlyMode`: `primary: nil` gives source `"Apple"` and no history in any call.
- [ ] **Step 2:** `swift test --filter HedgedTranslatorTests`. Expected: FAIL.
- [ ] **Step 3: Implement.** Deduplicate in-flight requests with a `[String: Task<[String], Error>]` keyed on the
  joined text keys of the missing chunks.
- [ ] **Step 4:** `swift test`. Expected: all pass.
- [ ] **Step 5:** Commit `DilmacCore: hedged LLM translation with Apple fallback and a disk cache`.

### Task 5: chiaki-ng macOS slice

**Files:**
- Modify: `ios/scripts/build-chiaki.sh`

**Interfaces:**
- Produces: `ios/build/ChiakiBridge.xcframework` with slices `ios-arm64`, `ios-arm64-simulator` and `macos-arm64`.

- [ ] **Step 1: Generalize `build_slice`.** Make the CMake system name a parameter (`iOS` or `Darwin`) and the
  deployment target a parameter, then add a call:
  `build_slice macos macosx darwin64-arm64-cc "-mmacosx-version-min=26.0"` with `Darwin` and `26.0`. Add the `macos`
  library to the `-create-xcframework` call.
- [ ] **Step 2: Run** `ios/scripts/build-chiaki.sh`. Expected: `built: .../ChiakiBridge.xcframework`. The build tools
  in `ios/.venv` are missing on this Mac right now; ask the user before creating it (cmake, ninja, protobuf,
  grpcio-tools via pip).
- [ ] **Step 3: Verify** with `plutil -p ios/build/ChiakiBridge.xcframework/Info.plist | grep -c LibraryIdentifier`
  (expected `3`) and with `lipo -info ios/build/macos/libChiakiBridge.a` (expected `arm64`).
- [ ] **Step 4:** Commit `iOS build: chiaki-ng macOS slice`.

### Task 6: Multiplatform target, platform splits, Mac build

**Files:**
- Modify: `ios/Dilmac.xcodeproj/project.pbxproj`. In both target configurations set:
  - `SUPPORTED_PLATFORMS = "iphoneos iphonesimulator macosx"`, `SDKROOT = auto`
  - `MACOSX_DEPLOYMENT_TARGET = 26.0`, `SUPPORTS_MACCATALYST = NO`
  - `"PRODUCT_BUNDLE_IDENTIFIER[sdk=macosx*]" = com.dilmac.mac`, `ENABLE_APP_SANDBOX = NO`
  - `"ARCHS[sdk=macosx*]" = arm64`, `INFOPLIST_KEY_LSApplicationCategoryType = "public.app-category.utilities"`
- Modify: `ios/Dilmac/Video/VideoView.swift`. On macOS, `VideoLayerView` is an `NSView` with `wantsLayer` and the
  `AVSampleBufferDisplayLayer` as its layer, and `VideoView` is an `NSViewRepresentable`. `FrameDisplay` and
  `FrameStore` are shared; on macOS `FrameStore.folder` is `~/Library/Application Support/Dilmac/frames`.
- Modify: `ios/Dilmac/PSN/PSNLogin.swift`. `PSNLoginView` is an `NSViewRepresentable` on macOS; the Coordinator is
  shared.
- Modify: `ios/Dilmac/DilmacApp.swift`. On macOS, `ProcessInfo.processInfo.beginActivity(options:
  [.idleSystemSleepDisabled, .userInitiated], reason: "Dilmaç altyazı")` runs while streaming; `AppModel` holds the
  token and ends it on disconnect. iOS keeps `isIdleTimerDisabled`.
- Modify: `ios/Dilmac/AppModel.swift`. The battery code is iOS-only; on macOS `deviceText()` shows only the heat.
- Modify: `ios/Dilmac/Storage/Keychain.swift`. Add `kSecUseDataProtectionKeychain: true` to `query` on macOS.
- Modify: `docs/superpowers/specs/2026-11-28-mac-app-llm-design.md`. Replace "New target **DilmacMac**" with the
  multiplatform target, keeping the same bundle id.

- [ ] **Step 1:** `xcodebuild -project ios/Dilmac.xcodeproj -scheme Dilmac -destination 'platform=macOS' build`.
  Expected before the change: FAIL (the macOS destination is not supported).
- [ ] **Step 2:** Make the build-setting and source changes above.
- [ ] **Step 3:** The same command. Expected: `** BUILD SUCCEEDED **`, with no warning about a missing macOS
  xcframework slice.
- [ ] **Step 4:** `xcodebuild -project ios/Dilmac.xcodeproj -scheme Dilmac -destination 'generic/platform=iOS' -allowProvisioningUpdates build`.
  Expected: `** BUILD SUCCEEDED **` (the iPhone is unchanged).
- [ ] **Step 5: Launch the Mac app** from `DerivedData/.../Build/Products/Debug/Dilmac.app` (the `run` skill or
  `open`), take a screenshot, and check that the window opens with the iPhone's form.
- [ ] **Step 6:** Commit `Dilmaç runs on macOS: multiplatform target, AppKit views, staying awake`.

### Task 7: The app uses the LLM engine; engine settings

**Files:**
- Modify: `ios/Dilmac/Subtitles/Translator.swift`. It becomes a thin wrapper: Apple Translation session handling
  (the existing `apple(_:)` code) plus a `HedgedTranslator` built from settings.
- Modify: `ios/Dilmac/Subtitles/SubtitlePipeline.swift`. Pass the settings in. `stop()` saves the cache. Add an
  `engineState` event.
- Modify: `ios/Dilmac/AppModel.swift`. Settings in `UserDefaults` (not secrets):
  - `engine`: `"llm"` on macOS, `"apple"` on iOS by default
  - `local_model`: default `"gemma4:e4b-it-qat"`
  - `local_url`: default `http://127.0.0.1:11434`
  - `@Published var engineInfo: String`, for example "LLM · gemma4:e4b-it-qat · hazır", "Ollama kapalı", "Model yok: x"
  - On start: `warmUp()`, then the 30 s status check.

**Interfaces:**
- Consumes: `HedgedTranslator`, `TranslationCache` (Task 4) and `OllamaClient`, `OllamaStatus` (Task 3).
- Produces:
  - `Translator.init(pack: Pack?, glossary: Glossary?, engine: Engine, model: String, ollamaURL: URL, cacheFile: URL?)`
    with `enum Engine: String { case apple, llm }`
  - `func translate(_ text: String) async throws -> (String, String)`, the same signature as today, so
    `SubtitlePipeline.start` does not change
  - `func save()`
  - `var engineState: OllamaStatus { get async }`

- [ ] **Step 1:** Implement the wrapper. The cache file is
  `~/Library/Application Support/Dilmac/cache.json` on macOS and `nil` on iOS (today's behavior).
- [ ] **Step 2:** Build for both platforms (Task 6's commands). Expected: both `** BUILD SUCCEEDED **`.
- [ ] **Step 3: Check on the Mac without Ollama.** Launch the app with no Ollama running. The engine card shows
  "Ollama kapalı", and a test caption ("Test altyazısı" / `testTV`) is translated by Apple.
- [ ] **Step 4:** Commit `Mac: local LLM translation in the app, Apple Translation as the fallback`.

### Task 8: Mac window layout

**Files:**
- Create: `ios/Dilmac/MacContentView.swift` (`#if os(macOS)`)
  - Left: the video with the subtitle region outlined. The box comes from the pipeline's current `Layout` region as
    normalized 0–1 coordinates, drawn as a `Rectangle().stroke` overlay.
  - Right: cards for PS5 (state, fps), TV (host, connected) and Çeviri (`engineInfo`), Başlat/Durdur, and the live
    list.
  - Settings sheet: TV IP, engine picker, model, Ollama address. If `translationInstalled == false`, the Çeviri card
    shows a "Türkçe modeli indir" button, which uses the same `.translationTask` download as `ContentView`.
  - PSN sign-in and pairing reuse the iPhone's sections as a sheet.
- Modify: `ios/Dilmac/DilmacApp.swift`. Use `MacContentView` on macOS and `ContentView` on iOS.
- Modify: `ios/Dilmac/AppModel.swift`.
  - `@Published var lines: [SubtitleLine]`, the last 50, newest first, where `struct SubtitleLine: Identifiable
    { id, time: Date, english: String, turkish: String, source: String, translateMs: Int }`. It is filled from the
    `.turkish` event.
  - `var regionBox: CGRect?`.

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Build for macOS and iOS. Expected: both succeed.
- [ ] **Step 3:** Launch on the Mac and take screenshots: the window at its default size and at 1100×700. No clipped
  cards, and the list scrolls.
- [ ] **Step 4:** Commit `Mac: window with picture, status cards and live subtitles`.

### Task 9: Saved-frame check and model benchmark

**Files:**
- Modify: `ios/Dilmac/AppModel.swift`. Add `func replayFrames(_ urls: [URL])` (macOS, a "Kareleri dene" button in
  settings). It loads the PNGs as `CVPixelBuffer`s and offers each one to the pipeline 8 times at the pipeline
  interval, so the stabilizer sees a steady line. The `.english` / `.turkish` events land in `lines`.
- Create: `pc/bench_local.py`.
  - Input: `--pack pc/packs/gta6.json --models m1,m2,... --n 50`.
  - It picks 50 lines with slang, measured as lines containing a word from a short list (fuck, shit, homie, bro,
    damn, ass, hell, man, yo), plus random lines up to 50.
  - Each line is translated with every model through `translation.Translator` (engine `local`, `cache_data=None`,
    the same prompt as the app).
  - It writes `ios/build/bench/<date>.md`, which is git-ignored and has English lines: a table of model,
    median ms, p90 ms and invalid replies, then one row per line with EN, the pack's TR and each model's TR.
  - Never commit the output.

- [ ] **Step 1: Ask the user** before installing anything. Installing Ollama (ollama.com Mac app) and pulling
  `gemma4:e4b-it-qat` plus one or two larger candidates (proposed: a 12B–14B instruct model) need a yes. The private
  `Dilmac-packs` repo also has to be cloned into `pc/packs`.
- [ ] **Step 2:** Run `pc/.venv/bin/python pc/bench_local.py --models ... --n 50`. Expected: the table is written.
  Show it to the user (`SendUserFile`) and set `local_model` to their pick.
- [ ] **Step 3:** In the Mac app, run "Kareleri dene" on 3–5 frames saved from the iPhone or Mac stream. Expected:
  the lines appear in the list with source `LLM` or `paket`, and the times are logged.
- [ ] **Step 4:** Commit `Mac: replay saved frames; local model benchmark script`. Commit `AppModel.swift` and
  `pc/bench_local.py`, never the files in `ios/build/bench/`.

### Task 10: Docs

**Files:**
- Modify: `ios/README.md` (a "Mac" section: build, Ollama, model setting, cache location, sleeping; the iPhone section
  unchanged).
- Modify: `README.md` and `README.tr.md` (one paragraph: Dilmaç also runs on the Mac, with local LLM translation).

- [ ] **Step 1:** Write the docs.
- [ ] **Step 2:** `cd ios/DilmacCore && swift test`, then both `xcodebuild` builds. Expected: all pass.
- [ ] **Step 3:** Commit `Docs: Dilmaç on the Mac`. Scan the diff for secrets and English game lines, then push.

**Not in this plan (the user does these with the PS5):** a real game session on the Mac, with per-line latency read
from the log, as the spec's last test.
