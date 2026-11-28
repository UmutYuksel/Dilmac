import DilmacCore
import Foundation
import Translation

/// The app's translation entry: DilmacCore's HedgedTranslator (glossary -> pack -> cache -> engine) with the engine
/// from the settings. `llm` (the Mac's default) asks the local model through Ollama, with Apple Translation racing it
/// after 1.5 s and filling in while Ollama is down; `apple` (the iPhone's default) is Apple Translation only, per
/// sentence chunk, as before.
final class Translator: Sendable {
    enum Engine: String, CaseIterable, Sendable {
        case apple, llm
    }

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    static let source = Locale.Language(identifier: "en")
    static let target = Locale.Language(identifier: "tr")
    static let hedge = Duration.milliseconds(1500) // local_hedge_ms

    let engine: Engine
    let model: String
    let ollamaURL: URL
    private let core: HedgedTranslator
    private let cache: TranslationCache

    /// cache: the LLM's translations (the app keeps one for its whole life; on disk on the Mac). Not used by `apple`.
    init(pack: Pack?, glossary: Glossary?, engine: Engine, model: String, ollamaURL: URL, cache: TranslationCache) {
        self.engine = engine
        self.model = model
        self.ollamaURL = ollamaURL
        let apple = AppleTranslation()
        let client = engine == .llm ? OllamaClient(base: ollamaURL, model: model, session: Self.session) : nil
        self.cache = cache
        core = HedgedTranslator(
            pack: pack, glossary: glossary, game: glossary?.gameTitle ?? pack?.gameTitle ?? "", cache: cache,
            primary: client.map { c in { @Sendable prompt, n in try await c.translate(prompt: prompt, count: n) } },
            fallback: { @Sendable text in try await apple.translate(text) }, hedge: Self.hedge,
            status: client.map { c in { @Sendable in await c.check() } })
    }

    /// One kept-alive connection to Ollama; nothing cached on disk.
    private static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.httpMaximumConnectionsPerHost = 4
        return URLSession(configuration: c)
    }()

    /// Is the English -> Turkish model on the device (otherwise it has to be downloaded once, see ContentView).
    static func languageInstalled() async -> Bool {
        await LanguageAvailability().status(from: source, to: target) == .installed
    }

    /// (translation, source). Throws if Apple Translation is needed but not available.
    func translate(_ text: String) async throws -> (String, String) {
        let r = try await core.translate(text)
        return (r.text, r.source)
    }

    /// Loads the model before the first line; returns how long it took (nil: not the LLM engine or it failed).
    func warmUp() async -> Duration? {
        guard engine == .llm else { return nil }
        let started = ContinuousClock.now
        await core.warmUp()
        return await core.engineState == .ready ? ContinuousClock.now - started : nil
    }

    func refresh() async { await core.refresh() }

    var engineState: OllamaStatus { get async { await core.engineState } }

    /// Writes the LLM's cache (the session stopped).
    func save() { cache.save() }
}

/// Apple Translation (on device, offline) with one session kept open.
actor AppleTranslation {
    private var session: TranslationSession?

    func translate(_ text: String) async throws -> String {
        if session == nil {
            guard await Translator.languageInstalled() else {
                throw Translator.Failure(message: "Türkçe çeviri dili cihazda yok. Ayarlar bölümünden indir.")
            }
            #if os(iOS)
            if #available(iOS 26.4, *) {
                session = TranslationSession(installedSource: Translator.source, target: Translator.target,
                                             preferredStrategy: .lowLatency)
            } else {
                session = TranslationSession(installedSource: Translator.source, target: Translator.target)
            }
            #else
            // The Mac has no low latency model for the pair ("notInstalled"); the default model works.
            session = TranslationSession(installedSource: Translator.source, target: Translator.target)
            #endif
        }
        do {
            return try await session!.translate(text).targetText
        } catch {
            session = nil // a fresh session next time
            throw error
        }
    }
}
