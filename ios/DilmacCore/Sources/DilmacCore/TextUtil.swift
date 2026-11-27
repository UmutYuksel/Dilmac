import Foundation

/// Port of pc/textutil.py: pure helpers for the read subtitle text (keys, OCR-tolerant similarity, speaker name,
/// sentences). Behavior is checked against the Python tests (Tests/DilmacCoreTests/TextUtilTests.swift).
///
/// Python's `[\W_]` (not a letter or digit) is written `[^\p{L}\p{N}]` here: ICU's \w also counts combining marks
/// as word characters, Python's does not.
public enum TextUtil {
    // MARK: basic form

    private static let quotes: [Character: Character] = [
        "’": "'", "‘": "'", "`": "'", "´": "'", "“": "\"", "”": "\"", "„": "\"",
    ]
    private static let spaces = Rx(#"\s+"#)

    /// NFKC, straight quotes, single spaces.
    public static func normalize(_ text: String) -> String {
        let nfkc = text.precomposedStringWithCompatibilityMapping
        let straight = String(nfkc.map { quotes[$0] ?? $0 })
        return spaces.sub(straight, " ").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static let nonAlnum = Rx(#"[^\p{L}\p{N}]+"#)

    /// Cache key: lower case, letters and digits only (Turkish letters kept).
    public static func textKey(_ text: String) -> String {
        nonAlnum.sub(normalize(text).casefolded, "")
    }

    private static let nonAlnumPipe = Rx(#"[^\p{L}\p{N}_|]+"#)
    private static let ocrPairs = [("rn", "m"), ("vv", "w"), ("cl", "d")]
    private static let ocrChars: [Character: Character] = [
        "l": "i", "1": "i", "|": "i", "j": "i", "ı": "i", "0": "o", "5": "s", "8": "b",
    ]

    /// Letters OCR confuses with each other folded into one form (only for similarity).
    public static func ocrKey(_ text: String) -> String {
        var k = nonAlnumPipe.sub(normalize(text).casefolded, "")
        for (a, b) in ocrPairs { k = k.replacingOccurrences(of: a, with: b) }
        return nonAlnum.sub(String(k.map { ocrChars[$0] ?? $0 }), "")
    }

    public static func similarity(_ a: String, _ b: String) -> Double {
        let ka = ocrKey(a), kb = ocrKey(b)
        if ka.isEmpty || kb.isEmpty { return ka == kb ? 1 : 0 }
        return SequenceMatcher(ka, kb).ratio()
    }

    /// How much of short's letters (in order) are inside long: for a partial read or a continuation line.
    public static func coverage(_ short: String, _ long: String) -> Double {
        let ka = ocrKey(short), kl = ocrKey(long)
        if ka.isEmpty { return 0 }
        let matched = SequenceMatcher(ka, kl).matchingBlocks().reduce(0) { $0 + $1.size }
        return Double(matched) / Double(ka.unicodeScalars.count)
    }

    // MARK: words

    private static let wordRx = Rx(#"\p{L}+(?:'\p{L}+)*"#)

    public static func words(_ text: String) -> [String] { wordRx.findAll(normalize(text)) }

    private static let junkRx = Rx(#"[@#$%^&*_=|\\{}<>~]|[a-zA-Z]\d|\d[a-zA-Z]|[a-z][A-Z]"#)

    public static func junkCount(_ text: String) -> Int {
        normalize(text).split(separator: " ").filter { junkRx.test(String($0)) }.count
    }

    public static func endsWithPunct(_ text: String) -> Bool {
        var t = normalize(text)
        while let last = t.last, "\"')]» ".contains(last) { t.removeLast() }
        return t.hasSuffix(".") || t.hasSuffix("!") || t.hasSuffix("?") || t.hasSuffix("…")
    }

    // MARK: speaker name and sentences

    private static let speakerRx = Rx(#"^\s*([A-Z][A-Za-z'’.\-]*(?:\s+[A-Z][A-Za-z'’.\-]*){0,2})\s*:\s+(\S.*)$"#)
    private static let clockRx = Rx(#"^\d{1,2}:\d{2}"#)

    /// "JASON: Gidelim." -> ("JASON", "Gidelim."). The name is at most three words, each capitalized.
    public static func splitSpeaker(_ text: String) -> (name: String?, body: String) {
        let text = normalize(text)
        if clockRx.test(text) { return (nil, text) }
        guard let g = speakerRx.firstGroups(text), let rawName = g[1], let body = g[2] else { return (nil, text) }
        let name = rawName.trimmingCharacters(in: .whitespaces)
        if name.count > 24 { return (nil, text) }
        return (name, body.trimmingCharacters(in: .whitespaces))
    }

    private static let sentenceGap = Rx(#"(?<=[.!?…])["')\]]*\s+(?=[\-–—]?\s*["'(]?[A-Z0-9¿¡])"#)
    private static let abbrevRx = Rx(#"\b(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|vs|Lt|Sgt|Capt|Det|Off)\.$"#, [.caseInsensitive])
    private static let dashGap = Rx(#"(?<=[.!?…])\s+(?=[\-–—]\s)"#)

    /// Splits a subtitle into sentences; not after abbreviations or before a lower case letter after "...".
    /// Dialogue dashes ("- Where? - Home.") count as separate sentences.
    public static func splitSentences(_ text: String) -> [String] {
        let text = normalize(text)
        if text.isEmpty { return [] }
        var parts: [String] = []
        var start = text.startIndex
        for r in sentenceGap.ranges(text) {
            let piece = text[start..<r.upperBound].trimmingCharacters(in: .whitespaces)
            if abbrevRx.test(piece) { continue }
            parts.append(piece)
            start = r.upperBound
        }
        let tail = text[start...].trimmingCharacters(in: .whitespaces)
        if !tail.isEmpty { parts.append(tail) }
        return parts.flatMap { p in
            dashGap.split(p).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        }
    }

    /// Joins one- or two-word sentences with their neighbors ("Girl. Stop. Rent is coming." stays one piece).
    public static func chunkSentences(_ sentences: [String], minimum: Int = 3) -> [String] {
        var chunks: [[String]] = []
        var number = 0
        for c in sentences {
            if !chunks.isEmpty, number < minimum {
                chunks[chunks.count - 1].append(c)
            } else {
                chunks.append([c])
                number = 0
            }
            number += words(c).count
        }
        if chunks.count > 1, chunks[chunks.count - 1].reduce(0, { $0 + words($1).count }) <= 2 {
            let last = chunks.removeLast()
            chunks[chunks.count - 1].append(contentsOf: last)
        }
        return chunks.map { $0.joined(separator: " ") }
    }

    /// If new starts with old's last sentences (the previous line kept on top, a new one added below), only the added
    /// sentences; nil if there is no overlap.
    public static func newPart(old: String, new: String) -> String? {
        let se = splitSentences(splitSpeaker(old).body)
        let sy = splitSentences(splitSpeaker(new).body)
        for j in 0..<se.count {
            let common = Array(se[j...])
            if common.count >= sy.count { continue }
            if zip(common, sy).allSatisfy({ similarity($0, $1) >= 0.8 }) {
                return sy[common.count...].joined(separator: " ")
            }
        }
        return nil
    }
}

// MARK: - Lexicon

/// English dictionary (pyspellchecker's word list, the same as the Windows app) + names known for the game.
public final class Lexicon: @unchecked Sendable {
    /// Every word of the dictionary, lower case. Loaded once (~160k words) and shared.
    private static let dictionary: Set<String> = {
        guard let url = Bundle.module.url(forResource: "en-words", withExtension: "deflate"),
              let packed = try? Data(contentsOf: url),
              let data = try? (packed as NSData).decompressed(using: .zlib) as Data else {
            assertionFailure("en-words.deflate missing")
            return []
        }
        return Set(String(decoding: data, as: UTF8.self).split(separator: "\n").map(String.init))
    }()

    static let shortWords: Set<String> = [
        "a", "i", "o", "ah", "am", "an", "as", "at", "aw", "ay", "be", "by", "do", "eh", "go", "ha", "he", "hi", "hm",
        "if", "in", "is", "it", "me", "mm", "my", "no", "of", "oh", "ok", "on", "or", "ow", "so", "to", "uh", "um", "up",
        "us", "we", "ya", "yo", "ye", "em", "mr", "ms", "dr", "st", "tv", "id", "ad", "ex", "ox", "pa", "ma",
    ]
    static let slang: Set<String> = [
        "pssp", "scuse", "ey", "bro", "asswipe", "meds", "doofus", "oughta", "dumbass", "ahh", "uhh", "hmm", "mmm",
        "shh", "aww", "argh", "ugh", "whoa", "jeez", "gonna", "wanna", "gotta", "kinda", "sorta", "outta", "lemme",
        "gimme", "dunno", "nah", "yep", "nope", "sup", "finna", "tryna", "cuz", "lotta", "hafta", "homie", "homies",
        "y'know", "y'all", "c'mon", "ain't", "em", "ya", "yo", "da", "mf", "motherfucker", "motherfuckers", "badass",
        "bullshit", "dipshit", "jackass", "asshole", "assholes", "fuckers", "fucker", "dickhead", "shitty",
    ]
    /// Words common in the PS5's Turkish interface (Turkish letters flattened, as English OCR reads them).
    static let turkishWords: Set<String> = [
        "iptal", "tamam", "ayarlar", "ayar", "geri", "sec", "secin", "oyna", "oyun", "oyunu", "oyunlar", "magaza",
        "kitaplik", "bildirim", "bildirimler", "ana", "ekran", "ekrani", "lutfen", "bekleyin", "diger", "baglantilar",
        "baglanti", "arkadas", "arkadaslar", "etkinlik", "etkinligi", "etkinlikler", "konsol", "depolama", "alani",
        "haberler", "hikaye", "guc", "tasarrufu", "gorunumu", "gorunum", "evet", "hayir", "devam", "kaydet", "yukle",
        "yukleniyor", "cikis", "icin", "ile", "veya", "simdi", "daha", "fazla", "bilgi", "goruntule", "ayrintilar",
        "uygulamalar", "uygulama", "kupalar", "kupa", "oturum", "kapat", "yeniden", "baslat", "download", "indiriliyor",
        "guncelleme", "sesli", "sohbet", "parti", "medya", "galeri", "yakala", "paylas", "kontrol", "merkezi",
    ]

    private var extra: Set<String> = []
    private let lock = NSLock()

    public init(_ extras: [String] = []) {
        _ = Self.dictionary
        add(extras)
    }

    public func add(_ names: [String]) {
        lock.withLock {
            for name in names { for w in TextUtil.words(name) { extra.insert(w.casefolded) } }
        }
    }

    private static let possessive = Rx(#"'(s|ll|re|ve|d|m|t)$"#)

    public func isKnown(_ word: String) -> Bool {
        let k = word.casefolded
        if lock.withLock({ extra.contains(k) }) { return true }
        if k.count <= 2 { return Self.shortWords.contains(k) }
        if Self.dictionary.contains(k) || Self.slang.contains(k) { return true }
        // Street speech: "fuckin'", "holdin'" (the trailing apostrophe is not part of the word)
        if k.count >= 4, k.hasSuffix("in"), Self.dictionary.contains(k + "g") { return true }
        // Possessive and contraction endings: "Jason's", "Lucia'll"
        let base = Self.possessive.sub(k, "")
        return base != k && (lock.withLock { extra.contains(base) } || Self.dictionary.contains(base))
    }

    /// (known words, all words)
    public func tally(_ text: String) -> (known: Int, total: Int) {
        let ws = TextUtil.words(text)
        return (ws.filter(isKnown).count, ws.count)
    }

    /// How much a read looks like "real English"; to pick the best of a few reads.
    public func score(_ text: String) -> Double {
        let (known, total) = tally(text)
        let junk = TextUtil.junkCount(text)
        return Double(known) - 1.5 * Double(total - known) - 2 * Double(junk) + 0.001 * Double(text.count)
    }

    private static let turkishChars = Rx("[ğĞışŞİ]")

    /// Is the text on screen already Turkish (PS5 menu, notification): then it must not go to the translator.
    public func isTurkish(_ text: String) -> Bool {
        if Self.turkishChars.test(text) { return true }
        let ws = TextUtil.words(text).filter { $0.count >= 3 }
        if ws.isEmpty { return false }
        let tr = ws.filter { Self.turkishWords.contains(Self.fold($0)) }.count
        if ws.count == 1 { return tr == 1 }
        let known = ws.filter { isKnown($0) && !Self.turkishWords.contains(Self.fold($0)) }.count
        var foreign = ws.filter { !isKnown($0) && (Self.turkishWords.contains(Self.fold($0)) || Self.wordLike($0)) }.count
        foreign = max(foreign, tr)
        return foreign >= 2 && foreign >= known && (tr >= 1 || Double(known) / Double(ws.count) < 0.34)
    }

    /// Does it look like OCR junk (an in-between state read while the text fades in/out).
    public func isGarbled(_ text: String) -> Bool {
        let ws = TextUtil.words(text)
        if ws.isEmpty || TextUtil.junkCount(text) > 0 { return true }
        let unknown = ws.filter { !isKnown($0) }
        // An unknown capitalized word is mostly a name; on its own it does not count as garbled.
        let suspicious = unknown.filter { !Self.looksLikeName($0) }
        if suspicious.contains(where: { $0.count >= 12 }) { return true }
        return suspicious.count >= 2 || Double(suspicious.count) / Double(ws.count) > 0.25
    }

    /// Every word is in the dictionary or looks like a name; no junk marks.
    public func isClean(_ text: String) -> Bool {
        let ws = TextUtil.words(text)
        if ws.isEmpty || TextUtil.junkCount(text) > 0 { return false }
        return ws.allSatisfy { isKnown($0) || Self.looksLikeName($0) }
    }

    /// Python: w[0].isupper() and len(w) >= 3 and w[1:].islower()
    static func looksLikeName(_ w: String) -> Bool {
        guard w.count >= 3, let first = w.first, first.isUppercase else { return false }
        return pythonIsLower(w.dropFirst())
    }

    /// Python str.islower(): at least one cased character and no upper case one.
    static func pythonIsLower<S: StringProtocol>(_ s: S) -> Bool {
        var cased = false
        for c in s {
            if c.isUppercase { return false }
            if c.isLowercase { cased = true }
        }
        return cased
    }

    private static let foldMap: [Character: Character] = [
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i",
    ]

    static func fold(_ w: String) -> String {
        String(w.casefolded.map { foldMap[$0] ?? $0 }).replacingOccurrences(of: "i\u{307}", with: "i")
    }

    private static let vowels = Set("aeıioöuüAEIİOÖUÜ")
    private static let consonantRun = Rx(#"[^aeıioöuüAEIİOÖUÜ\W\d_]{5,}"#)

    /// A sensible vowel ratio and no 5 consonants in a row: looks like a real word (even in another language).
    static func wordLike(_ w: String) -> Bool {
        let letters = w.filter(\.isLetter)
        if letters.count < 3 { return false }
        let ratio = Double(letters.filter { vowels.contains($0) }.count) / Double(letters.count)
        return ratio >= 0.25 && !consonantRun.test(w)
    }
}
