import Foundation

/// Port of pc/translation.py Translator.translate with the local model as the main engine and Apple Translation as
/// the fallback (the PC's Google):
/// glossary line -> game pack -> per sentence chunk (pack or cache) -> the LLM for the missing chunks, with the
/// game, names, terms, speaker, similar pack lines and the last subtitles in the prompt. If the LLM has not answered
/// after `hedge`, Apple Translation races it and the first answer is shown; a late LLM answer is still cached. An
/// engine that is down is skipped for `recheck`.
///
/// Without a primary (the iPhone today) every missing chunk goes to Apple Translation on its own, as before.
public actor HedgedTranslator {
    public typealias Primary = @Sendable (String, Int) async throws -> [String]
    public typealias Fallback = @Sendable (String) async throws -> String

    private let pack: Pack?
    private let glossary: Glossary?
    private let game: String
    private let cache: TranslationCache
    private let primary: Primary?
    private let fallback: Fallback
    private let hedge: Duration
    private let status: (@Sendable () async -> OllamaStatus)?
    private let recheck: Duration
    private let contextLines = 3

    /// The last subtitles with their translations (context for the prompt).
    private(set) var history: [(en: String, tr: String)] = []
    /// Names learned from speaker labels ("JASON: ..." -> "Jason").
    private(set) var learnedNames: [String] = []
    public private(set) var engineState: OllamaStatus = .ready
    private var downUntil: ContinuousClock.Instant?
    private var inFlight: [String: Task<[String], Error>] = [:]
    private var appleMemory: [String: String] = [:] // Apple-only mode: per chunk, never on disk

    public init(pack: Pack?, glossary: Glossary?, game: String, cache: TranslationCache, primary: Primary?,
                fallback: @escaping Fallback, hedge: Duration = .milliseconds(1500),
                status: (@Sendable () async -> OllamaStatus)? = nil, recheck: Duration = .seconds(30)) {
        self.pack = pack
        self.glossary = glossary
        self.game = game
        self.cache = cache
        self.primary = primary
        self.fallback = fallback
        self.hedge = hedge
        self.status = status
        self.recheck = recheck
    }

    /// (translation, source); source is "sözlük", "paket", "önbellek", "LLM" or "Apple". A translation of "-" means
    /// the line was taken for junk.
    public func translate(_ text: String) async throws -> (text: String, source: String) {
        let (name, body) = TextUtil.splitSpeaker(text)
        if let name, !learnedNames.contains(name), !["you", "me"].contains(name.casefolded) {
            learnedNames.append(name == name.uppercased() && name != name.lowercased() ? name.capitalized : name)
        }
        func label(_ tr: String) -> String { name.map { tr != "-" ? "\($0): \(tr)" : tr } ?? tr }

        if let ready = glossary?.ready(body) { return (label(ready), "sözlük") }
        if let ready = pack?.lookup(body) {
            remember(body, ready)
            return (label(ready), "paket")
        }
        var chunks = TextUtil.chunkSentences(TextUtil.splitSentences(body))
        if chunks.isEmpty { chunks = [body] }
        guard primary != nil else { return try await appleOnly(chunks, label) }

        var outcome = chunks.map { pack?.lookup($0) ?? cache[TextUtil.textKey($0)] }
        let context = history
        var source = chunks.allSatisfy({ pack?.lookup($0) != nil }) ? "paket" : "önbellek"
        let missing = zip(chunks, outcome).compactMap { $1 == nil ? $0 : nil }
        if !missing.isEmpty {
            let (translations, from) = try await hedged(missing, name, context, body)
            source = from
            if from == "Apple" {
                outcome = [translations[0]] // the whole subtitle in one piece, as the PC does with Google
            } else {
                var it = translations.makeIterator()
                outcome = outcome.map { $0 ?? it.next() }
            }
        }
        let tr = Self.merge(outcome.map { $0 ?? "" })
        if tr != "-" { remember(body, tr) }
        return (label(tr), source)
    }

    /// Loads the model before the first line (and finds out whether it is there at all).
    public func warmUp() async {
        guard let primary else { return }
        if let status {
            engineState = await status()
            guard engineState == .ready else {
                downUntil = .now + recheck
                return
            }
        }
        let prompt = LLMPrompt.user(sentences: ["Hello."], speaker: nil, history: [], game: game, glossary: glossary,
                                    learnedNames: [], pack: pack)
        do {
            _ = try await primary(prompt, 1)
        } catch {
            await failed(error)
        }
    }

    // MARK: - the race

    private enum Outcome: Sendable {
        case primary([String])
        case primaryFailed(Error)
        case fallback(String)
        case fallbackFailed(Error)
        case timeout
    }

    private func hedged(_ sentences: [String], _ speaker: String?, _ context: [(en: String, tr: String)],
                        _ body: String) async throws -> ([String], String) {
        guard let primary, await engineUp() else { return ([try await fallback(body)], "Apple") }
        let key = sentences.map(TextUtil.textKey).joined(separator: "\n")
        let task: Task<[String], Error>
        if let running = inFlight[key] {
            task = running
        } else {
            let prompt = LLMPrompt.user(sentences: sentences, speaker: speaker, history: context, game: game,
                                        glossary: glossary, learnedNames: learnedNames, pack: pack)
            let n = sentences.count, cache = cache
            task = Task {
                let out = try await primary(prompt, n)
                cache.set(sentences, out) // also when it comes too late to be shown
                return out
            }
            inFlight[key] = task
            Task { _ = try? await task.value; self.finished(key, task) }
        }

        switch await Self.first([Self.wait(task), Self.sleep(hedge)]) {
        case .primary(let out):
            succeeded()
            return (out, "LLM")
        case .primaryFailed(let error):
            await failed(error)
            return ([try await fallback(body)], "Apple")
        default:
            break // late: Apple Translation races it
        }
        let fallback = fallback
        switch await Self.first([Self.wait(task), { do { return .fallback(try await fallback(body)) } catch {
            return .fallbackFailed(error) } }], successOnly: true) {
        case .primary(let out):
            succeeded()
            return (out, "LLM")
        case .fallback(let tr):
            return ([tr], "Apple")
        case .fallbackFailed(let error), .primaryFailed(let error):
            await failed(error)
            throw error
        case .timeout:
            fatalError("no timeout in this race")
        }
    }

    private func finished(_ key: String, _ task: Task<[String], Error>) {
        if inFlight[key] == task { inFlight[key] = nil }
    }

    /// May the LLM be asked now? After `recheck` a down engine is tried again (asking `status` first if there is one).
    private func engineUp() async -> Bool {
        guard let until = downUntil else { return true }
        if ContinuousClock.now < until { return false }
        downUntil = nil
        if let status {
            engineState = await status()
            if engineState != .ready {
                downUntil = .now + recheck
                return false
            }
        }
        return true
    }

    private func succeeded() {
        engineState = .ready
        downUntil = nil
    }

    /// A reply that could not be read only costs that line; a dead connection or a missing model takes the engine
    /// out until the next check.
    private func failed(_ error: Error) async {
        if error is LLMPrompt.ReplyError || error is CancellationError { return }
        if let status {
            engineState = await status()
        } else if error is URLError {
            engineState = .down
        } else {
            return
        }
        if engineState != .ready { downUntil = .now + recheck }
    }

    private static func wait(_ task: Task<[String], Error>) -> @Sendable () async -> Outcome {
        { do { return .primary(try await task.value) } catch { return .primaryFailed(error) } }
    }

    private static func sleep(_ d: Duration) -> @Sendable () async -> Outcome {
        { try? await Task.sleep(for: d); return .timeout }
    }

    /// The first racer to finish (with successOnly: the first success, or the last failure). The others keep
    /// running unwatched: an unstructured Task's value cannot be cancelled by its waiter.
    private static func first(_ racers: [@Sendable () async -> Outcome], successOnly: Bool = false) async -> Outcome {
        let box = Once(remaining: racers.count)
        return await withCheckedContinuation { (cont: CheckedContinuation<Outcome, Never>) in
            box.set(cont)
            for racer in racers {
                Task {
                    let out = await racer()
                    let failure: Bool
                    switch out {
                    case .primaryFailed, .fallbackFailed: failure = true
                    default: failure = false
                    }
                    box.offer(out, failure: successOnly && failure)
                }
            }
        }
    }

    private final class Once: @unchecked Sendable {
        private let lock = NSLock()
        private var cont: CheckedContinuation<Outcome, Never>?
        private var remaining: Int

        init(remaining: Int) { self.remaining = remaining }

        func set(_ c: CheckedContinuation<Outcome, Never>) { lock.withLock { cont = c } }

        /// A failure is only taken when nothing else is left to wait for.
        func offer(_ out: Outcome, failure: Bool) {
            let c: CheckedContinuation<Outcome, Never>? = lock.withLock {
                remaining -= 1
                guard let c = cont, !failure || remaining == 0 else { return nil }
                cont = nil
                return c
            }
            c?.resume(returning: out)
        }
    }

    // MARK: - helpers

    private func remember(_ en: String, _ tr: String) {
        history.append((en, tr))
        if history.count > contextLines { history.removeFirst(history.count - contextLines) }
    }

    /// The iPhone's way: each missing chunk through Apple Translation on its own, kept in memory only.
    private func appleOnly(_ chunks: [String], _ label: (String) -> String) async throws -> (text: String, source: String) {
        var outcome = chunks.map { pack?.lookup($0) ?? appleMemory[TextUtil.textKey($0)] }
        var source = outcome.allSatisfy({ $0 != nil }) ? "önbellek" : "Apple"
        if chunks.allSatisfy({ pack?.lookup($0) != nil }) { source = "paket" }
        for i in outcome.indices where outcome[i] == nil {
            let tr = try await fallback(chunks[i])
            appleMemory[TextUtil.textKey(chunks[i])] = tr
            outcome[i] = tr
        }
        return (label(Self.merge(outcome.map { $0 ?? "" })), source)
    }

    /// merge() in pc/translation.py: "-" pieces dropped, white space collapsed; "-" if nothing is left.
    static func merge(_ pieces: [String]) -> String {
        let kept = pieces.filter { !$0.isEmpty && $0.trimmingCharacters(in: .whitespaces) != "-" }
        let text = kept.joined(separator: " ").split(whereSeparator: \.isWhitespace).joined(separator: " ")
        return text.isEmpty ? "-" : text
    }
}
