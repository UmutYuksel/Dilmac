import Foundation

/// Port of pc/stabilizer.py: decides "translate now / correct / bring back / clear" from the OCR reads arriving frame
/// by frame. Decisions depend on the clock, not on frame counts. See the Python module for the rules.
public final class Stabilizer {
    public struct Decision: Equatable {
        public enum Kind: Equatable { case none, send, correct, back, clear }
        public var kind: Kind = .none
        public var text: String = ""
        /// Time from the frame where the text was first seen to this decision (latency measurement).
        public var waitMs: Double = 0
        static let nothing = Decision()
    }

    final class Line {
        var text: String
        let first: Double
        var last: Double
        var corrected = false
        var sameCount = 1
        init(_ text: String, _ t: Double) { self.text = text; first = t; last = t }
    }

    private final class Candidate {
        var text: String
        let first: Double
        var number = 1
        var score: Double
        init(_ text: String, _ first: Double, score: Double) { self.text = text; self.first = first; self.score = score }
    }

    static let clearMs = 800.0
    static let holdIfUnchangedMs = 4000.0
    static let comebackMs = 1500.0
    static let stableMs = 250.0
    static let garbledStableMs = 700.0
    static let retryMs = 1000.0

    private let lexicon: Lexicon
    private let clock: () -> Double
    /// The line sent last (nil after it was cleared).
    public var sentText: String? { sent?.text }
    var sent: Line?
    private var candidate: Candidate?
    private var correction: (key: String, count: Int)?
    private var history: [(text: String, clearedAt: Double)] = []
    private var blocked: (text: String, until: Double)?

    /// clock: seconds (monotonic); tests pass their own.
    public init(lexicon: Lexicon, clock: @escaping () -> Double = { ProcessInfo.processInfo.systemUptime }) {
        self.lexicon = lexicon
        self.clock = clock
    }

    private func now() -> Double { clock() * 1000 }

    // MARK: comparisons

    private static func body(_ text: String) -> String { TextUtil.splitSpeaker(text).body }

    /// Is new old with words added to its end (the speech got longer, a second row appeared).
    public func isExtension(_ new: String, of old: String) -> Bool {
        let kNew = TextUtil.ocrKey(Self.body(new)), kOld = TextUtil.ocrKey(Self.body(old))
        return kNew.unicodeScalars.count >= kOld.unicodeScalars.count + 4
            && TextUtil.coverage(Self.body(old), Self.body(new)) >= 0.85
    }

    /// Is the read a re-read of the line (noisy, partial or without the name).
    public func isSame(_ reading: String, _ line: String) -> Bool {
        let a = Self.body(reading), b = Self.body(line)
        if isExtension(reading, of: line) { return false }
        if TextUtil.similarity(a, b) >= 0.8 { return true }
        let ka = TextUtil.ocrKey(a).unicodeScalars.count, kb = TextUtil.ocrKey(b).unicodeScalars.count
        return ka < kb && Double(ka) >= 0.4 * Double(kb) && TextUtil.coverage(a, b) >= 0.85
    }

    // MARK: main entry

    /// The read of one frame. An empty read = no subtitle in the region. unchanged: the region's picture stayed the
    /// same since the frame where the line was read last.
    public func feed(_ rawReading: String, unchanged: Bool = false) -> Decision {
        let t = now()
        let reading = TextUtil.normalize(rawReading)
        if reading.isEmpty { return empty(t, unchanged: unchanged) }

        if let g = sent, isSame(reading, g.text) {
            g.last = t
            candidate = nil
            return checkCorrection(reading, g)
        }

        correction = nil
        let a: Candidate
        if let c = candidate, !isExtension(reading, of: c.text), TextUtil.similarity(reading, c.text) >= 0.8 {
            c.number += 1
            let p = lexicon.score(reading)
            if p > c.score { c.text = reading; c.score = p }
            a = c
        } else {
            a = Candidate(reading, t, score: lexicon.score(reading))
            candidate = a
        }

        if let back = returning(reading, t) {
            setSent(back, t)
            return Decision(kind: .back, text: back)
        }

        if let b = blocked, t < b.until, TextUtil.similarity(reading, b.text) >= 0.8 { return .nothing }

        let duration = t - a.first
        let clean = lexicon.isClean(a.text)
        let send: Bool
        if clean && TextUtil.endsWithPunct(a.text) {
            send = true
        } else if clean || !lexicon.isGarbled(a.text) {
            send = a.number >= 2 && duration >= Self.stableMs
        } else {
            send = a.number >= 3 && duration >= Self.garbledStableMs
        }
        if !send { return .nothing }
        let text = a.text
        setSent(text, t)
        return Decision(kind: .send, text: text, waitMs: t - a.first)
    }

    /// The translation of the sent line failed: it is retried after retryMs while the line stays on screen.
    public func forget() {
        if let s = sent {
            blocked = (s.text, now() + Self.retryMs)
            sent = nil
        }
    }

    // MARK: helpers

    private func empty(_ t: Double, unchanged: Bool) -> Decision {
        candidate = nil
        correction = nil
        guard let g = sent else { return .nothing }
        let elapsed = t - g.last
        if unchanged && elapsed < Self.holdIfUnchangedMs { return .nothing }
        if elapsed >= Self.clearMs {
            pushHistory(g.text, t)
            sent = nil
            return Decision(kind: .clear)
        }
        return .nothing
    }

    private func pushHistory(_ text: String, _ t: Double) {
        history.append((text, t))
        if history.count > 5 { history.removeFirst(history.count - 5) }
    }

    private func setSent(_ text: String, _ t: Double) {
        if let s = sent { pushHistory(s.text, t) }
        sent = Line(text, t)
        candidate = nil
        correction = nil
    }

    private func returning(_ reading: String, _ t: Double) -> String? {
        for i in stride(from: history.count - 1, through: 0, by: -1) {
            let (text, clearedAt) = history[i]
            if t - clearedAt <= Self.comebackMs, isSame(reading, text), !isExtension(reading, of: text) {
                history.remove(at: i)
                return text
            }
        }
        return nil
    }

    private func checkCorrection(_ reading: String, _ g: Line) -> Decision {
        if TextUtil.ocrKey(reading) == TextUtil.ocrKey(g.text) || TextUtil.textKey(reading) == TextUtil.textKey(g.text) {
            g.sameCount += 1
            correction = nil
            return .nothing
        }
        if g.corrected || TextUtil.words(reading).count != TextUtil.words(g.text).count { return .nothing }
        if lexicon.score(reading) <= lexicon.score(g.text) { return .nothing }
        let k = TextUtil.textKey(reading)
        let number = (correction?.key == k ? correction!.count : 0) + 1
        correction = (k, number)
        if number >= 2 && (repairs(g.text, reading) || number > g.sameCount) {
            g.text = reading
            g.corrected = true
            correction = nil
            return Decision(kind: .correct, text: reading)
        }
        return .nothing
    }

    /// Is every word of new that differs from old a repaired version of it (old word not in the dictionary or cut).
    private func repairs(_ old: String, _ new: String) -> Bool {
        for (a, b) in zip(TextUtil.words(old), TextUtil.words(new)) {
            if a.casefolded == b.casefolded { continue }
            if lexicon.isKnown(a) && !(a.count < b.count && b.casefolded.contains(a.casefolded)) { return false }
        }
        return true
    }
}
