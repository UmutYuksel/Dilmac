import Foundation

/// The LLM's translations per sentence chunk, on disk (pc/translation.py's cache: the same format and versions). A
/// file made with another prompt, or a broken one (a crash mid-write), is moved aside as `<name>.eski.json` and the
/// cache starts empty. Written every 5 new entries and on `save()`.
public final class TranslationCache: @unchecked Sendable {
    public static let version = 2

    private let file: URL?
    private let lock = NSLock()
    private let writing = NSLock() // one save at a time (the temp file)
    private var sentences: [String: String] = [:]
    private var dirty = 0

    /// nil: memory only.
    public init(file: URL?) {
        self.file = file
        guard let file, let data = try? Data(contentsOf: file) else { return }
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        if let object, object["version"] as? Int == Self.version,
           (object["prompt"] as? Int ?? 1) == LLMPrompt.version {
            sentences = object["sentences"] as? [String: String] ?? [:]
            return
        }
        let backup = file.deletingPathExtension().appendingPathExtension("eski.json")
        try? FileManager.default.removeItem(at: backup)
        try? FileManager.default.moveItem(at: file, to: backup)
    }

    public var count: Int { lock.withLock { sentences.count } }

    /// Looked up by `TextUtil.textKey` of the chunk.
    public subscript(key: String) -> String? { lock.withLock { sentences[key] } }

    public func set(_ chunks: [String], _ translations: [String]) {
        let save: Bool = lock.withLock {
            for (c, t) in zip(chunks, translations) where !t.isEmpty { sentences[TextUtil.textKey(c)] = t }
            dirty += 1
            return dirty >= 5
        }
        if save { self.save() }
    }

    public func save() {
        let snapshot: [String: String] = lock.withLock {
            dirty = 0
            return sentences
        }
        guard let file else { return }
        writing.lock()
        defer { writing.unlock() }
        let object: [String: Any] = ["version": Self.version, "prompt": LLMPrompt.version, "sentences": snapshot]
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
        try? FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        let tmp = file.deletingPathExtension().appendingPathExtension("tmp")
        do {
            try data.write(to: tmp)
            if FileManager.default.fileExists(atPath: file.path) {
                _ = try FileManager.default.replaceItemAt(file, withItemAt: tmp)
            } else {
                try FileManager.default.moveItem(at: tmp, to: file)
            }
        } catch {
            try? FileManager.default.removeItem(at: tmp)
        }
    }
}
