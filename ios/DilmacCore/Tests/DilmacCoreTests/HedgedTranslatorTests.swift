// HedgedTranslator: the order of the sources, the prompt's context, the race against Apple Translation, a down
// engine, duplicate requests and the disk cache (pc/translation.py Translator).
@testable import DilmacCore
import Foundation
import XCTest

/// Records the primary's calls; answers "TR<i>" per sentence after a delay, or throws.
private actor FakeLLM {
    var prompts: [String] = []
    var delay: Duration = .zero
    var error: Error?

    func set(delay: Duration = .zero, error: Error? = nil) { self.delay = delay; self.error = error }

    func call(_ prompt: String, _ n: Int) async throws -> [String] {
        prompts.append(prompt)
        if delay > .zero { try await Task.sleep(for: delay) }
        if let error { throw error }
        return (0..<n).map { "TR\($0)" }
    }
}

private actor StateBox {
    var value: OllamaStatus
    init(_ v: OllamaStatus) { value = v }
    func set(_ v: OllamaStatus) { value = v }
}

private actor FakeApple {
    var texts: [String] = []
    func call(_ text: String) async throws -> String {
        texts.append(text)
        try await Task.sleep(for: .milliseconds(100))
        return "APPLE(\(text))"
    }
}

final class HedgedTranslatorTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("dilmac-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: dir) }

    private func make(_ llm: FakeLLM?, _ apple: FakeApple, cache: TranslationCache = TranslationCache(file: nil),
                      pack: Pack? = nil, glossary: Glossary? = nil, hedge: Duration = .milliseconds(1500),
                      recheck: Duration = .seconds(30)) -> HedgedTranslator {
        HedgedTranslator(pack: pack, glossary: glossary, game: "Test Game", cache: cache,
                         primary: llm.map { l in { @Sendable p, n in try await l.call(p, n) } },
                         fallback: { t in try await apple.call(t) }, hedge: hedge, recheck: recheck)
    }

    func testOrderGlossaryPackCacheLLM() async throws {
        let llm = FakeLLM(), apple = FakeApple()
        let cache = TranslationCache(file: nil)
        cache.set(["We go right now."], ["Hemen gidiyoruz."])
        let t = make(llm, apple, cache: cache,
                     pack: Pack(gameTitle: "x", lines: [("Meet me at the trailer.", "Karavanda buluşalım.")]),
                     glossary: try Glossary(data: Data(#"{"game": "x", "lines": {"WASTED": "ÖLDÜN"}}"#.utf8)))
        var r = try await t.translate("WASTED")
        XCTAssertEqual(r.text, "ÖLDÜN"); XCTAssertEqual(r.source, "sözlük")
        r = try await t.translate("Meet me at the trailer.")
        XCTAssertEqual(r.text, "Karavanda buluşalım."); XCTAssertEqual(r.source, "paket")
        r = try await t.translate("We go right now.")
        XCTAssertEqual(r.text, "Hemen gidiyoruz."); XCTAssertEqual(r.source, "önbellek")
        r = try await t.translate("We go right now. Where is the boat?")
        XCTAssertEqual(r.text, "Hemen gidiyoruz. TR0"); XCTAssertEqual(r.source, "LLM")
        let prompts = await llm.prompts
        XCTAssertEqual(prompts.count, 1)
        XCTAssertTrue(prompts[0].hasSuffix("Translate:\n[\"Where is the boat?\"]"))
        let used = await apple.texts
        XCTAssertEqual(used, [])
    }

    func testPromptCarriesHistoryAndSpeaker() async throws {
        let llm = FakeLLM()
        let t = make(llm, FakeApple())
        _ = try await t.translate("Where is the boat?")
        _ = try await t.translate("It sank last night.")
        let r = try await t.translate("JASON: Move it now.")
        XCTAssertEqual(r.text, "JASON: TR0")
        let last = await llm.prompts.last!
        XCTAssertTrue(last.contains("Previous subtitles, for context only (do not translate them):\n"
            + "EN: Where is the boat?\nTR: TR0\nEN: It sank last night.\nTR: TR0"), last)
        XCTAssertTrue(last.contains("Speaker: JASON"))
        _ = try await t.translate("Jason, where are you going?")
        let named = await llm.prompts.last!
        XCTAssertTrue(named.contains("Keep exactly as written (names): Jason"), named)
    }

    func testHedgeFallbackThenLateLLMCached() async throws {
        let llm = FakeLLM()
        await llm.set(delay: .milliseconds(600))
        let t = make(llm, FakeApple(), hedge: .milliseconds(100))
        let first = try await t.translate("Where is the boat?")
        XCTAssertEqual(first.text, "APPLE(Where is the boat?)"); XCTAssertEqual(first.source, "Apple")
        try await Task.sleep(for: .milliseconds(800))
        let second = try await t.translate("Where is the boat?")
        XCTAssertEqual(second.text, "TR0"); XCTAssertEqual(second.source, "önbellek")
        let calls = await llm.prompts.count
        XCTAssertEqual(calls, 1)
    }

    func testFastLLMWinsBeforeHedge() async throws {
        let apple = FakeApple()
        let t = make(FakeLLM(), apple, hedge: .milliseconds(300))
        let r = try await t.translate("Where is the boat?")
        XCTAssertEqual(r.source, "LLM")
        let used = await apple.texts
        XCTAssertEqual(used, [])
    }

    func testInvalidReplyFallsBack() async throws {
        let llm = FakeLLM()
        await llm.set(error: LLMPrompt.ReplyError(description: "2 öğe beklenirken 1 geldi"))
        let t = make(llm, FakeApple(), hedge: .seconds(5))
        let started = ContinuousClock.now
        let r = try await t.translate("Where is the boat?")
        XCTAssertEqual(r.source, "Apple")
        XCTAssertLessThan(ContinuousClock.now - started, .seconds(1)) // right away, not after the hedge
        _ = try await t.translate("It sank last night.")
        let calls = await llm.prompts.count
        XCTAssertEqual(calls, 2) // a bad reply does not take the engine down
        let state = await t.engineState
        XCTAssertEqual(state, .ready)
    }

    func testDownEngineSkipsLLMUntilRecheck() async throws {
        let llm = FakeLLM()
        await llm.set(error: URLError(.cannotConnectToHost))
        let t = make(llm, FakeApple(), recheck: .seconds(1)) // the 4 Apple lines take 0.4 s
        var r = try await t.translate("Where is the boat?")
        XCTAssertEqual(r.source, "Apple")
        let state = await t.engineState
        XCTAssertEqual(state, .down)
        for line in ["It sank last night.", "We swim back then.", "That is a long way."] {
            r = try await t.translate(line)
            XCTAssertEqual(r.source, "Apple")
        }
        var calls = await llm.prompts.count
        XCTAssertEqual(calls, 1)
        try await Task.sleep(for: .milliseconds(700))
        await llm.set()
        r = try await t.translate("Ollama is back now.")
        XCTAssertEqual(r.source, "LLM")
        calls = await llm.prompts.count
        XCTAssertEqual(calls, 2)
        let back = await t.engineState
        XCTAssertEqual(back, .ready)
    }

    func testStatusDecidesWhenDown() async throws {
        let llm = FakeLLM()
        let t = HedgedTranslator(pack: nil, glossary: nil, game: "", cache: TranslationCache(file: nil),
                                 primary: { p, n in try await llm.call(p, n) }, fallback: { "A:" + $0 },
                                 status: { .modelMissing("qwen3:14b") }, recheck: .seconds(30))
        await t.warmUp()
        let state = await t.engineState
        XCTAssertEqual(state, .modelMissing("qwen3:14b"))
        let r = try await t.translate("Where is the boat?")
        XCTAssertEqual(r.source, "Apple")
        let calls = await llm.prompts.count
        XCTAssertEqual(calls, 0)
    }

    func testRefreshFollowsTheStatus() async throws {
        let llm = FakeLLM()
        let state = StateBox(.down)
        let t = HedgedTranslator(pack: nil, glossary: nil, game: "", cache: TranslationCache(file: nil),
                                 primary: { p, n in try await llm.call(p, n) }, fallback: { "A:" + $0 },
                                 status: { await state.value }, recheck: .seconds(30))
        await t.refresh()
        var now = await t.engineState
        XCTAssertEqual(now, .down)
        var r = try await t.translate("Where is the boat?")
        XCTAssertEqual(r.source, "Apple") // down: not asked
        await state.set(.ready) // Ollama started: the next refresh lets the LLM in at once, not after 30 s
        await t.refresh()
        now = await t.engineState
        XCTAssertEqual(now, .ready)
        r = try await t.translate("It sank last night.")
        XCTAssertEqual(r.source, "LLM")
    }

    func testWarmUpLoadsTheModel() async {
        let llm = FakeLLM()
        let t = make(llm, FakeApple())
        await t.warmUp()
        let prompts = await llm.prompts
        XCTAssertEqual(prompts.count, 1)
        XCTAssertTrue(prompts[0].hasSuffix("Translate:\n[\"Hello.\"]"))
    }

    func testConcurrentSameTextOneRequest() async throws {
        let llm = FakeLLM()
        await llm.set(delay: .milliseconds(200))
        let t = make(llm, FakeApple())
        async let a = t.translate("Where is the boat?")
        async let b = t.translate("Where is the boat?")
        let (ra, rb) = try await (a, b)
        XCTAssertEqual(ra.text, rb.text)
        XCTAssertEqual(ra.text, "TR0")
        let calls = await llm.prompts.count
        XCTAssertEqual(calls, 1)
    }

    func testCacheOnDisk() async throws {
        let file = dir.appendingPathComponent("cache.json")
        let cache = TranslationCache(file: file)
        for i in 0..<4 { cache.set(["Line number \(i) here."], ["Satır \(i)."]) }
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path)) // written every 5
        cache.set(["Line number 4 here."], ["Satır 4."])
        let saved = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as! [String: Any]
        XCTAssertEqual(saved["version"] as? Int, 2)
        XCTAssertEqual(saved["prompt"] as? Int, 2)
        XCTAssertEqual((saved["sentences"] as? [String: String])?[TextUtil.textKey("Line number 4 here.")], "Satır 4.")
        cache.set(["One more line here."], ["Bir satır daha."])
        cache.save()
        let again = TranslationCache(file: file)
        XCTAssertEqual(again[TextUtil.textKey("One more line here.")], "Bir satır daha.")
        XCTAssertEqual(again[TextUtil.textKey("Line number 0 here.")], "Satır 0.")
    }

    func testOldOrBrokenCacheStartsEmpty() throws {
        for (name, content) in [("old.json", #"{"version": 2, "prompt": 1, "sentences": {"x": "y"}}"#),
                                ("broken.json", "{")] {
            let file = dir.appendingPathComponent(name)
            try Data(content.utf8).write(to: file)
            let cache = TranslationCache(file: file)
            XCTAssertNil(cache["x"])
            let backup = dir.appendingPathComponent(name.replacingOccurrences(of: ".json", with: ".eski.json"))
            XCTAssertEqual(try String(contentsOf: backup, encoding: .utf8), content)
            XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        }
    }

    func testAppleOnlyMode() async throws {
        let apple = FakeApple()
        let t = make(nil, apple)
        var r = try await t.translate("Where is the boat? It sank last night.")
        XCTAssertEqual(r.text, "APPLE(Where is the boat?) APPLE(It sank last night.)")
        XCTAssertEqual(r.source, "Apple")
        r = try await t.translate("Where is the boat?")
        XCTAssertEqual(r.source, "önbellek")
        let history = await t.history
        XCTAssertTrue(history.isEmpty)
        let used = await apple.texts
        XCTAssertEqual(used.count, 2)
    }
}
