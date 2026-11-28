import Foundation

/// Port of pc/pack.py (lookup side): the game pack's ready translations, looked up before live translation and
/// tolerant to OCR errors. File format: {"game": "...", "lines": {EN: TR}, "speakers": {EN: speaker}}.
public final class Pack: @unchecked Sendable {
    /// Common words not counted when measuring similarity.
    static let padding: Set<String> = [
        "the", "and", "you", "your", "that", "this", "with", "for", "are", "was", "were", "have", "has", "had", "not",
        "but", "what", "who", "how", "why", "when", "where", "all", "just", "can", "get", "got", "she", "her", "him",
        "his", "they", "them", "their", "our", "out", "about", "there", "here", "from", "will", "would", "could",
        "should", "been", "then", "than", "now", "yeah", "okay", "it's", "i'm", "you're", "don't", "gonna", "like",
        "know", "one", "some", "any", "into", "too", "very", "really", "right", "come", "let's", "hey",
    ]

    public let gameTitle: String
    private var full: [String: Int] = [:]
    private var ocr: [String: Int] = [:]
    private var pairs: [(en: String, tr: String, words: Set<String>)] = []
    private var speakers: [Int: String] = [:]
    private var wordIndex: [String: [Int]] = [:]

    public var count: Int { full.count }

    public init(gameTitle: String = "", lines: [(String, String)] = [], speakers: [String: String] = [:]) {
        self.gameTitle = gameTitle
        add(lines: lines, speakers: speakers)
    }

    /// Lines are added in order; the first one with a key wins (the user's file comes first).
    public func add(lines: [(String, String)], speakers labels: [String: String] = [:]) {
        for (rawEn, rawTr) in lines {
            let en = TextUtil.normalize(rawEn), tr = rawTr.trimmingCharacters(in: .whitespacesAndNewlines)
            let k = TextUtil.textKey(en)
            if k.isEmpty || tr.isEmpty || full[k] != nil { continue }
            let i = pairs.count
            full[k] = i
            let ok = TextUtil.ocrKey(en)
            if ocr[ok] == nil { ocr[ok] = i }
            let ws = Self.contentWords(en)
            pairs.append((en, tr, ws))
            for w in ws { wordIndex[w, default: []].append(i) }
        }
        for (en, speaker) in labels {
            let s = speaker.trimmingCharacters(in: .whitespaces)
            if let i = full[TextUtil.textKey(en)], !s.isEmpty, speakers[i] == nil { speakers[i] = s }
        }
    }

    /// Reads a pack file (JSON). The order of "lines" in the file is kept.
    public func add(file data: Data) throws {
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
        let lines = (object["lines"] as? [String: String]) ?? [:]
        // JSONSerialization does not keep key order; sort for a stable "first wins" between duplicate keys.
        let ordered = lines.sorted { $0.key < $1.key }
        add(lines: ordered, speakers: (object["speakers"] as? [String: String]) ?? [:])
    }

    /// The "game" field of a pack file, to pick the files of one game.
    public static func gameTitle(ofFile data: Data) -> String? {
        ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["game"] as? String
    }

    static func contentWords(_ text: String) -> Set<String> {
        Set(TextUtil.words(text).map(\.casefolded).filter { $0.count >= 3 && !padding.contains($0) })
    }

    private func index(of text: String) -> Int? {
        if let i = full[TextUtil.textKey(text)] { return i }
        let k = TextUtil.ocrKey(text)
        guard k.unicodeScalars.count >= 8 else { return nil } // a short piece could find the wrong line
        return ocr[k] ?? fuzzy(text, k)
    }

    public func lookup(_ text: String) -> String? {
        index(of: text).map { Self.punctuation(source: text, tr: pairs[$0].tr) }
    }

    /// Who says the line; nil if unknown.
    public func speaker(_ text: String) -> String? {
        index(of: text).flatMap { speakers[$0] }
    }

    /// speaker(); if the whole line is not in the pack it is looked up sentence by sentence (first known speaker).
    public func speakerBySentence(_ text: String) -> String? {
        if let s = speaker(text) { return s }
        let body = TextUtil.splitSpeaker(text).body
        let chunks = TextUtil.chunkSentences(TextUtil.splitSentences(body))
        for piece in chunks.isEmpty ? [body] : chunks {
            if let s = speaker(piece) { return s }
        }
        return nil
    }

    /// A line with a letter or two misread: of the candidates sharing content words, the one with letter similarity
    /// >= 0.9 and a similar length.
    private func fuzzy(_ text: String, _ k: String) -> Int? {
        let n = k.unicodeScalars.count
        guard n >= 12 else { return nil }
        var counter: [Int: Int] = [:]
        for w in Self.contentWords(text) { for i in wordIndex[w] ?? [] { counter[i, default: 0] += 1 } }
        // Python: sorted by count (stable over insertion order), first 30.
        let order = counter.keys.sorted { counter[$0]! != counter[$1]! ? counter[$0]! > counter[$1]! : $0 < $1 }
        var best: Int?, score = 0.9
        for i in order.prefix(30) {
            let en = pairs[i].en
            if abs(Double(TextUtil.ocrKey(en).unicodeScalars.count - n)) > 0.15 * Double(n) { continue }
            let b = TextUtil.similarity(text, en)
            if b >= score { best = i; score = b }
        }
        return best
    }

    /// The n most similar pairs (content word overlap, cosine), the same line left out. Port of pc/pack.py
    /// similar(): ties go to the line added first. Hashed lines have no English and are never examples.
    public func similar(_ text: String, n: Int = 3, threshold: Double = 0.3) -> [(en: String, tr: String)] {
        let words = Self.contentWords(text)
        guard !words.isEmpty else { return [] }
        var counter: [Int: Int] = [:]
        for w in words { for i in wordIndex[w] ?? [] { counter[i, default: 0] += 1 } }
        let k = TextUtil.textKey(text)
        var scored: [(score: Double, i: Int)] = []
        for (i, common) in counter {
            let score = Double(common) / (Double(words.count) * Double(pairs[i].words.count)).squareRoot()
            if score >= threshold && TextUtil.textKey(pairs[i].en) != k { scored.append((score, i)) }
        }
        scored.sort { $0.score != $1.score ? $0.score > $1.score : $0.i < $1.i }
        return scored.prefix(n).map { (pairs[$0.i].en, pairs[$0.i].tr) }
    }

    private static let trailing = Rx(#"[.!?…]+$"#)

    /// The trailing punctuation of the pack's translation follows the read line.
    static func punctuation(source: String, tr: String) -> String {
        guard let last = trailing.firstGroups(TextUtil.normalize(source))?[0] else { return tr }
        if tr.hasSuffix(last) { return tr }
        var t = tr
        while let c = t.last, ".!?…".contains(c) { t.removeLast() }
        return t + last
    }
}

/// Port of the glossary part of pc/translation.py: per-game names, colors, screen layout and ready lines.
public struct Glossary: Sendable {
    public let gameTitle: String
    public let names: [String]
    public let keepNames: [String]
    public let nicknames: [String: String]
    public let colors: [String: String]
    public let screen: [String: Double]
    /// Style notes and examples for the LLM ("style").
    public let style: String
    /// "address_terms" and "terms" merged ("terms" wins), in file order as Python's {**address_terms, **terms}.
    private let termPairs: [(en: String, tr: String)]
    private let nicknamePairs: [(nick: String, name: String)]
    private let lines: [String: String]

    /// Preferred wordings: "address_terms" merged with "terms" ("terms" wins on a clash).
    public var terms: [String: String] {
        Dictionary(termPairs.map { ($0.en, $0.tr) }, uniquingKeysWith: { _, b in b })
    }

    public init(data: Data) throws {
        let o = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
        gameTitle = o["game"] as? String ?? ""
        names = [gameTitle] + (o["aliases"] as? [String] ?? [])
        keepNames = o["keep"] as? [String] ?? []
        nicknames = o["nicknames"] as? [String: String] ?? [:]
        colors = o["colors"] as? [String: String] ?? [:]
        screen = (o["screen"] as? [String: Any] ?? [:]).compactMapValues { ($0 as? NSNumber)?.doubleValue }
        style = o["style"] as? String ?? ""
        let order = OrderedJSON.memberKeys(data)
        func ordered(_ key: String) -> [(String, String)] {
            let dict = o[key] as? [String: String] ?? [:]
            let keys = (order[key] ?? []).filter { dict[$0] != nil }
            // Keys the scan missed (should not happen) go last, sorted, so nothing is lost.
            let rest = dict.keys.filter { !keys.contains($0) }.sorted()
            var seen = Set<String>()
            return (keys + rest).compactMap { k in seen.insert(k).inserted ? (k, dict[k]!) : nil }
        }
        var merged: [(en: String, tr: String)] = ordered("address_terms").map { ($0.0, $0.1) }
        for (en, tr) in ordered("terms") {
            if let at = merged.firstIndex(where: { $0.en == en }) { merged[at].tr = tr } else { merged.append((en, tr)) }
        }
        termPairs = merged
        nicknamePairs = ordered("nicknames").map { ($0.0, $0.1) }
        let raw = o["lines"] as? [String: String] ?? [:]
        lines = Dictionary(raw.map { (TextUtil.textKey($0.key), $0.value) }, uniquingKeysWith: { a, _ in a })
    }

    /// Words the English lexicon should know for this game (names, title, nicknames).
    public var lexiconExtras: [String] { keepNames + names + Array(nicknames.keys) }

    /// The phrase occurs as a whole word or words (not inside another word, not before an apostrophe), any case.
    public static func occurs(_ phrase: String, in text: String) -> Bool {
        wordRx(phrase, [.caseInsensitive]).test(text)
    }

    private static func wordRx(_ phrase: String, _ options: NSRegularExpression.Options) -> Rx {
        Rx(#"(?<![\w'])"# + NSRegularExpression.escapedPattern(for: phrase) + #"(?![\w'])"#, options)
    }

    /// Names to keep and preferred translations that occur in this text. A short name inside a longer one that is
    /// there too is left out ("Cal" when "Cal Hampton" is there).
    public func relevant(_ text: String) -> (names: [String], terms: [(en: String, tr: String)]) {
        let found = keepNames.filter { Self.occurs($0, in: text) }
        let names = found.filter { a in !found.contains { b in a != b && b.contains(a) } }
        return (names, termPairs.filter { Self.occurs($0.en, in: text) })
    }

    /// Nicknames that occur in this text; case matters ("Lu", not "lu").
    public func nicknameMap(_ text: String) -> [(nick: String, name: String)] {
        nicknamePairs.filter { Self.wordRx($0.nick, []).test(text) }
    }

    /// A ready line from the glossary; the trailing punctuation follows the source.
    public func ready(_ text: String) -> String? {
        guard let tr = lines[TextUtil.textKey(text)] else { return nil }
        let last = Rx(#"[.!?…]+$"#).firstGroups(TextUtil.normalize(text))?[0] ?? ""
        var t = tr
        while let c = t.last, ".!?…".contains(c) { t.removeLast() }
        return t + last
    }
}
