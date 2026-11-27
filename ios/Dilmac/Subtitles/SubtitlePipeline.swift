import CoreVideo
import DilmacCore
import Foundation
import QuartzCore

/// Port of the main loop of pc/subtitler.py (single speech box + objective box; split_blocks off, see ios/README.md):
/// frame -> OCR -> stabilizer -> translation -> TV. Frames are offered from chiaki's video thread; at most one is read
/// at a time, at most every `interval` seconds, the rest are dropped.
final class SubtitlePipeline: @unchecked Sendable {
    enum Event {
        case english(String, correction: Bool)
        case turkish(String, source: String, translateMs: Double, totalMs: Double)
        case cleared
        case log(String)
    }

    // pc/subtitler.py defaults
    static let interval = 0.2       // 5 reads a second
    static let maxShow = 10.0       // max_show_seconds
    static let linger = 2.5         // linger_seconds
    static let fontPx = 32.0        // BASE_FONT_PX; objective text is 70% of it
    static let style: [String: Any] = [
        "size": 30, "bottom": 60, "opacity": 0.97, "scale": 1.0, "max_width": 0.8, "old_secs": 3.0, "fade_ms": 180,
    ]

    /// A text tracked on its own on screen (speech subtitle or objective), with its own stabilizer and TV box.
    private final class Track {
        let name: String
        let stabilizer: Stabilizer
        var gen = 0, sentGen = 0
        var shownTr = "", shownAt = 0.0
        var recentTr: [String: String] = [:]
        var recentOrder: [String] = []
        var bbox: Box?
        var lineCount = 1
        var signature: [Double]?
        var shownEn = "", baseEn = ""
        var color: String?
        init(_ name: String, _ lexicon: Lexicon) { self.name = name; stabilizer = Stabilizer(lexicon: lexicon) }

        func remember(_ en: String, _ tr: String) {
            if recentTr[en] == nil { recentOrder.append(en) }
            recentTr[en] = tr
            if recentOrder.count > 50 { recentTr[recentOrder.removeFirst()] = nil }
        }
    }

    /// Called on the pipeline's queue.
    var onEvent: ((Event) -> Void)?
    var tv: TVLink? {
        get { lock.withLock { _tv } }
        set { lock.withLock { _tv = newValue } }
    }

    private let reader: SubtitleReader
    private let translator: Translator
    private let pack: Pack?
    private let colors: [String: String]
    private let queue = DispatchQueue(label: "dilmac.subtitles", qos: .userInitiated)
    private let lock = NSLock()
    private var _tv: TVLink?
    private var busy = false
    private var lastStart = 0.0
    private var lastTurkish = ""
    private let speech: Track
    private let objective: Track?

    init(lexicon: Lexicon, glossary: Glossary?, pack: Pack?) {
        reader = SubtitleReader(lexicon: lexicon, glossary: glossary)
        translator = Translator(pack: pack, glossary: glossary)
        self.pack = pack
        colors = glossary?.colors ?? [:]
        speech = Track("speech", lexicon)
        objective = reader.layout.objectiveY > 0 ? Track("objective", lexicon) : nil
    }

    /// A decoded frame from the stream; dropped unless it is time for the next read. Safe from any thread.
    func offer(_ frame: CVPixelBuffer) {
        let now = CACurrentMediaTime()
        let take = lock.withLock { () -> Bool in
            guard !busy, now - lastStart >= Self.interval else { return false }
            busy = true
            lastStart = now
            return true
        }
        guard take else { return }
        queue.async {
            self.step(frame)
            self.lock.withLock { self.busy = false }
        }
    }

    /// Removes everything from the TV (stream stopped).
    func stop() {
        queue.async { self.tv?.send(["type": "clear"]) }
    }

    // MARK: per frame

    private func step(_ frame: CVPixelBuffer) {
        guard let (reading, region) = reader.read(frame) else { return }
        if reading.turkish, reading.raw != lastTurkish {
            lastTurkish = reading.raw
            emit(.log("[Türkçe yazı, atlandı] \(reading.raw.prefix(80))"))
        }
        var tracks: [(Track, String, Box?, Int)] = [(speech, reading.speech, reading.speechBox, reading.speechLines)]
        if let objective { tracks.append((objective, reading.objective, reading.objectiveBox, reading.objectiveLines)) }
        for (track, text, box, lines) in tracks {
            let unchanged: Bool
            if !text.isEmpty {
                track.bbox = box
                track.lineCount = max(1, lines)
                track.signature = box.flatMap { region.signature(of: $0, layout: reader.layout) }
                unchanged = false
            } else {
                let now = track.bbox.flatMap { region.signature(of: $0, layout: reader.layout) }
                unchanged = track.bbox != nil && RegionImage.textInPlace(now, track.signature)
            }
            process(track, track.stabilizer.feed(text, unchanged: unchanged), reading)
        }
    }

    private func process(_ track: Track, _ d: Stabilizer.Decision, _ reading: SubtitleReader.Reading) {
        switch d.kind {
        case .send, .correct:
            track.gen += 1
            let full = d.text
            let toTranslate: String
            let isNew: Bool
            if d.kind == .send {
                if !track.shownEn.isEmpty, let part = TextUtil.newPart(old: track.shownEn, new: full), !part.isEmpty {
                    // The game kept the previous line on top and added one below: only the new one is translated.
                    (toTranslate, isNew, track.baseEn) = (part, true, track.shownEn)
                } else if !track.shownEn.isEmpty, track.stabilizer.isExtension(full, of: track.shownEn) {
                    (toTranslate, isNew, track.baseEn) = (full, false, "") // the sentence got longer: written in place
                } else {
                    (toTranslate, isNew, track.baseEn) = (full, true, "")
                }
            } else {
                let part = track.baseEn.isEmpty ? nil : TextUtil.newPart(old: track.baseEn, new: full)
                (toTranslate, isNew) = (part ?? full, false)
            }
            track.shownEn = full
            let label = track.name == "speech" ? (d.kind == .send ? "[EN]" : "[EN düzeltme]") : "[Görev]"
            emit(.log("\(label) \(toTranslate)   (\(reading.prep) \(Int(reading.ms)) ms, \(Int(d.waitMs)) ms bekledi)"))
            if track.name == "speech" { emit(.english(full, correction: d.kind == .correct)) }
            start(track, key: full, text: toTranslate, isNew: isNew, waitMs: d.waitMs)

        case .back:
            track.gen += 1
            track.shownEn = d.text
            track.baseEn = ""
            if let tr = track.recentTr[d.text] {
                if tr != "-" {
                    track.sentGen = track.gen
                    track.color = speakerColor(track, d.text)
                    show(track, tr, Self.maxShow)
                    emit(.log("[geri geldi] \(tr)"))
                }
            } else {
                start(track, key: d.text, text: d.text, isNew: false, waitMs: 0)
            }

        case .clear:
            track.gen += 1
            track.shownEn = ""
            track.baseEn = ""
            if track.name == "speech" { emit(.cleared) }
            if !track.shownTr.isEmpty {
                // Not cleared at once: kept a little longer so the translation can be read.
                let remain = readingTime(track.shownTr) - (CACurrentMediaTime() - track.shownAt)
                show(track, track.shownTr, max(Self.linger, remain))
                track.shownTr = ""
            }

        case .none:
            if !track.shownTr.isEmpty, track.stabilizer.sentText != nil,
               CACurrentMediaTime() - track.shownAt > Self.maxShow / 2 {
                show(track, track.shownTr, Self.maxShow) // text still on screen: refresh the TV's timer
            }
        }
    }

    private func start(_ track: Track, key: String, text: String, isNew: Bool, waitMs: Double) {
        let gen = track.gen
        let decided = CACurrentMediaTime()
        let translator = self.translator
        Task {
            do {
                let (tr, source) = try await translator.translate(text)
                self.queue.async { self.finish(track, gen, key, text, tr, source, isNew, decided, waitMs) }
            } catch {
                self.queue.async {
                    self.emit(.log("Çeviri hatası: \(error.localizedDescription)"))
                    if gen == track.gen { track.stabilizer.forget() } // retried a bit later while the line stays
                }
            }
        }
    }

    private func finish(_ track: Track, _ gen: Int, _ key: String, _ text: String, _ tr: String, _ source: String,
                        _ isNew: Bool, _ decided: Double, _ waitMs: Double) {
        track.remember(key, tr)
        if gen <= track.sentGen { return } // a newer text's translation is already shown
        if tr.trimmingCharacters(in: .whitespaces) == "-" { emit(.log("[atlandı] \(text)")); return }
        if TextUtil.textKey(tr) == TextUtil.textKey(text) {
            emit(.log("[aynı kaldı, gösterilmedi] \(text)")) // a place name, an exclamation
            return
        }
        let translateMs = (CACurrentMediaTime() - decided) * 1000
        let total = waitMs + translateMs
        track.sentGen = gen
        let current = gen == track.gen && track.stabilizer.sentText != nil
        track.color = speakerColor(track, text)
        show(track, tr, current ? Self.maxShow : readingTime(tr), isNew: isNew)
        let label = track.name == "speech" ? (current ? "[TR]" : "[TR geç]") : "[Görev TR]"
        emit(.log("\(label) \(tr)   (çeviri \(Int(translateMs)) ms, ilk görülmeden \(Int(total)) ms, \(source))"))
        if track.name == "speech" { emit(.turkish(tr, source: source, translateMs: translateMs, totalMs: total)) }
    }

    // MARK: TV

    private func readingTime(_ tr: String) -> Double { min(Self.maxShow, max(Self.linger, Double(tr.count) / 15)) }

    private func show(_ track: Track, _ tr: String, _ duration: Double, isNew: Bool = false) {
        track.shownTr = tr
        track.shownAt = CACurrentMediaTime()
        var msg = Self.style
        msg["type"] = "show"
        msg["id"] = track.name
        msg["text"] = tr
        msg["ttl"] = (duration * 100).rounded() / 100
        msg["new"] = isNew
        if let b = track.bbox {
            msg["box"] = b.array.map { ($0 * 10000).rounded() / 10000 }
            msg["lines"] = track.lineCount
            let px = track.name == "objective" ? Self.fontPx * 0.7 : Self.fontPx
            msg["lineh"] = (px / 1080 * 10000).rounded() / 10000
        }
        if let c = track.color { msg["color"] = c }
        tv?.send(msg)
    }

    /// Who says the line (from the pack) and their color; nil (white) if unknown.
    private func speakerColor(_ track: Track, _ text: String) -> String? {
        guard track.name == "speech", !colors.isEmpty, let speaker = pack?.speakerBySentence(text) else { return nil }
        return colors[speaker] ?? colors["character"]
    }

    private func emit(_ e: Event) { onEvent?(e) }
}
