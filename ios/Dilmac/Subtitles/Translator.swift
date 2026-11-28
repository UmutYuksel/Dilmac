import DilmacCore
import Foundation
import Translation

/// Port of pc/translation.py Translator.translate for iOS: glossary line -> game pack -> per sentence chunk (pack or
/// cache) -> Apple Translation (on device, offline) for what is still missing.
actor Translator {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    static let source = Locale.Language(identifier: "en")
    static let target = Locale.Language(identifier: "tr")

    private let pack: Pack?
    private let glossary: Glossary?
    private var cache: [String: String] = [:]
    private var session: TranslationSession?

    init(pack: Pack?, glossary: Glossary?) {
        self.pack = pack
        self.glossary = glossary
    }

    /// Is the English -> Turkish model on the phone (otherwise it has to be downloaded once, see ContentView).
    static func languageInstalled() async -> Bool {
        await LanguageAvailability().status(from: source, to: target) == .installed
    }

    /// (translation, source). Throws if Apple Translation is needed but not available.
    func translate(_ text: String) async throws -> (String, String) {
        let (name, body) = TextUtil.splitSpeaker(text)
        func label(_ tr: String) -> String { name.map { "\($0): \(tr)" } ?? tr }

        if let ready = glossary?.ready(body) { return (label(ready), "sözlük") }
        if let ready = pack?.lookup(body) { return (label(ready), "paket") }

        var chunks = TextUtil.chunkSentences(TextUtil.splitSentences(body))
        if chunks.isEmpty { chunks = [body] }
        var outcome = chunks.map { pack?.lookup($0) ?? cache[TextUtil.textKey($0)] }
        var source = outcome.allSatisfy({ $0 != nil }) ? "önbellek" : "Apple"
        if chunks.allSatisfy({ pack?.lookup($0) != nil }) { source = "paket" }
        for i in outcome.indices where outcome[i] == nil {
            let tr = try await apple(chunks[i])
            cache[TextUtil.textKey(chunks[i])] = tr
            outcome[i] = tr
        }
        let merged = TextUtil.normalize(outcome.compactMap { $0 }.filter { $0.trimmingCharacters(in: .whitespaces) != "-" }
            .joined(separator: " "))
        return (label(merged.isEmpty ? "-" : merged), source)
    }

    private func apple(_ text: String) async throws -> String {
        if session == nil {
            guard await Self.languageInstalled() else {
                throw Failure(message: "Türkçe çeviri dili telefonda yok. Ayarlar bölümünden indir.")
            }
            if #available(iOS 26.4, macOS 26.4, *) {
                session = TranslationSession(installedSource: Self.source, target: Self.target, preferredStrategy: .lowLatency)
            } else {
                session = TranslationSession(installedSource: Self.source, target: Self.target)
            }
        }
        do {
            return try await session!.translate(text).targetText
        } catch {
            session = nil // a fresh session next time
            throw error
        }
    }
}
