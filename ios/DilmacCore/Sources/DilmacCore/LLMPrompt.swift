import Foundation

/// Port of the LLM part of pc/translation.py: the system prompt, the per-request prompt (prompt_text) and the reply
/// parsing (reply_seq). The output must be the same as the PC's for the same inputs (checked against fixtures made
/// from the Python code, ios/scripts/make-prompt-fixtures.py), so both share caches and the measured prompt quality.
public enum LLMPrompt {
    /// PROMPT_VERSION in pc/translation.py: caches made with another prompt are dropped.
    public static let version = 2

    /// SYSTEM in pc/translation.py, verbatim.
    public static let system = """
        You are an experienced video game localizer translating English in-game subtitles into Turkish.
        - Write natural, spoken Turkish, the way people actually talk. Match the tone: slang stays slang and swearing stays swearing at the same strength; never soften or censor.
        - Translate the meaning, not word by word. An English idiom becomes a Turkish idiom or its plain meaning, never a literal translation. Never leave English words in the Turkish text (names excepted).
        - Characters talk to each other with informal "sen". Use "siz" only for clearly formal address (police, officials, a polite stranger) or when talking to more than one person.
        - Keep names and nicknames of people, places, brands and organizations exactly as written.
        - Interjections and sounds (Argh, Uhh, Hmm, Pssp) stay as they are or become the Turkish equivalent (Ah, Şey, Hımm, Pisi pisi).
        - The text was read from the screen by OCR: letters may be misread (0/O, l/I, rn/m) or words merged. Translate what the speaker obviously meant.
        - A string that starts in lowercase or with a connector (and, but, so) continues the previous subtitle; translate it as that continuation.
        - Short labels without a subject (objectives, menu items) are instructions to the player: use short informal imperatives ("Arabaya bin", not "Arabaya binin").
        - If a string is not real language (OCR garbage, random letters, button prompts), return "-" for it.
        Examples of the expected style:
        "Oh, shit. Look at you." -> "Vay anasını. Şu haline bak."
        "Sucks to suck, right?" -> "Kendi düşen ağlamaz, değil mi?"
        "You're hot as fuck." -> "Çok seksisin lan."
        "Who are you two?" -> "Siz ikiniz kimsiniz?"
        "Livin' the dream, huh?" -> "Keyfin yerinde bakıyorum, ha?"
        "Don't sweat it." -> "Kafana takma."
        "You holdin'?" -> "Üstünde mal var mı?"
        "What a waste of space." -> "Tam bir boş adam."
        "Get the fuck outta here!" -> "Siktir git buradan!"
        Input is a JSON array of strings. Reply with only a JSON array containing the same number of Turkish strings in the same order.
        """

    /// prompt_text(): game, style notes, names to keep (glossary and learned from speaker labels), nicknames,
    /// preferred wording, speaker, similar pack lines, the previous subtitles, then the sentences as a JSON array.
    public static func user(sentences: [String], speaker: String?, history: [(en: String, tr: String)], game: String,
                            glossary: Glossary?, learnedNames: [String], pack: Pack?) -> String {
        var parts: [String] = []
        if !game.isEmpty { parts.append("Game: \(game)") }
        if let g = glossary, !g.style.isEmpty { parts.append("Style notes (Turkish): \(g.style)") }
        let txt = sentences.joined(separator: " ")
        var kept: [String] = []
        var terms: [(en: String, tr: String)] = []
        if let g = glossary { (kept, terms) = g.relevant(txt) }
        kept += learnedNames.filter { a in !kept.contains(a) && Glossary.occurs(a, in: txt) }
        if !kept.isEmpty { parts.append("Keep exactly as written (names): " + kept.joined(separator: ", ")) }
        let nicknames = glossary?.nicknameMap(txt) ?? []
        if !nicknames.isEmpty {
            parts.append("Nicknames, keep them exactly as written: "
                + nicknames.map { "\($0.nick) = nickname of \($0.name)" }.joined(separator: "; "))
        }
        if !terms.isEmpty {
            parts.append("Preferred Turkish wording: " + terms.map { "\($0.en) → \($0.tr)" }.joined(separator: "; "))
        }
        if let speaker, !speaker.isEmpty { parts.append("Speaker: \(speaker)") }
        let samples = pack?.similar(txt) ?? []
        if !samples.isEmpty {
            parts.append("Similar lines from this game, already translated well (follow their tone and wording):\n"
                + samples.map { "EN: \($0.en)\nTR: \($0.tr)" }.joined(separator: "\n"))
        }
        if !history.isEmpty {
            parts.append("Previous subtitles, for context only (do not translate them):\n"
                + history.map { "EN: \($0.en)\nTR: \($0.tr)" }.joined(separator: "\n"))
        }
        parts.append("Translate:\n" + pythonJSON(sentences))
        return parts.joined(separator: "\n\n")
    }

    /// The reply could not be read as exactly n strings.
    public struct ReplyError: Error, CustomStringConvertible {
        public let description: String
    }

    private static let fence = Rx(#"^```(?:json)?\s*|\s*```$"#)
    private static let anyArray = Rx(#"\[.*\]"#, [.dotMatchesLineSeparators])

    /// reply_seq(): an array of n strings from the model's reply. A code fence, an object wrapper, prose around the
    /// array or (for n = 1) a bare string are accepted too.
    public static func replySeq(_ raw: String, count n: Int) throws -> [String] {
        let t = fence.sub(raw.trimmingCharacters(in: .whitespacesAndNewlines), "")
        var data: Any? = parse(t)
        if data == nil {
            if let r = anyArray.ranges(t).first { data = parse(String(t[r])) }
            if data == nil {
                if n == 1, !t.isEmpty, !t.hasPrefix("["), !t.hasPrefix("{") {
                    return [stripQuotes(t.trimmingCharacters(in: .whitespacesAndNewlines))]
                }
                throw ReplyError(description: "JSON dizisi okunamadı: \(raw.prefix(120))")
            }
        }
        if let object = data as? [String: Any] {
            // Python dicts keep their order: the first list value, else all values. (An object always comes from the
            // whole reply: the array found inside prose is never one.)
            let keys = OrderedJSON.topLevelKeys(Data(t.utf8)).filter { object[$0] != nil }
            let values = keys.count == object.count ? keys.map { object[$0]! } : Array(object.values)
            data = values.first { $0 is [Any] } ?? values
        }
        if let s = data as? String { data = [s] }
        guard let list = data as? [Any], list.count == n else {
            throw ReplyError(description: "\(n) öğe beklenirken \((data as? [Any])?.count.description ?? "?") geldi")
        }
        return list.map { pythonStr($0).trimmingCharacters(in: .whitespacesAndNewlines) }
    }

    private static func parse(_ s: String) -> Any? {
        guard !s.isEmpty else { return nil }
        return try? JSONSerialization.jsonObject(with: Data(s.utf8), options: .fragmentsAllowed)
    }

    private static func stripQuotes(_ s: String) -> String {
        var t = Substring(s)
        while t.first == "\"" { t.removeFirst() }
        while t.last == "\"" { t.removeLast() }
        return String(t)
    }

    /// str(x) of a JSON value as Python prints it, for the odd non-string item.
    private static func pythonStr(_ x: Any) -> String {
        switch x {
        case let s as String: return s
        case is NSNull: return "None"
        case let n as NSNumber:
            if CFGetTypeID(n) == CFBooleanGetTypeID() { return n.boolValue ? "True" : "False" }
            return n.stringValue
        default: return String(describing: x)
        }
    }

    /// json.dumps(list_of_str, ensure_ascii=False): ", " between items, only quotes, backslashes and control
    /// characters escaped (not "/" and not non-ASCII).
    static func pythonJSON(_ items: [String]) -> String {
        "[" + items.map { s -> String in
            var out = "\""
            for u in s.unicodeScalars {
                switch u {
                case "\"": out += "\\\""
                case "\\": out += "\\\\"
                case "\n": out += "\\n"
                case "\r": out += "\\r"
                case "\t": out += "\\t"
                case "\u{08}": out += "\\b"
                case "\u{0C}": out += "\\f"
                case _ where u.value < 0x20: out += String(format: "\\u%04x", u.value)
                default: out.unicodeScalars.append(u)
                }
            }
            return out + "\""
        }.joined(separator: ", ") + "]"
    }
}
