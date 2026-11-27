import CoreImage
import DilmacCore
import Foundation
import QuartzCore
import Vision

/// Port of pc/ocr.py Reader for iOS: reads the subtitle region of a frame with Vision (accurate, en-US).
/// The region as it is is read first; if speech or objective is not clean English, the "white text" preparation is
/// read too and the read that looks more like English wins for each.
final class SubtitleReader {
    struct Reading {
        var speech = ""
        var speechBox: Box?
        var speechLines = 0
        var objective = ""
        var objectiveBox: Box?
        var objectiveLines = 0
        /// The text on screen is Turkish (PS5 menu, notification): ignored.
        var turkish = false
        var raw = ""
        var prep = ""
        var ms = 0.0
    }

    /// Region of the subtitles in the game picture (pc/subtitler.py default "region").
    static let defaultRegion = (left: 0.08, top: 0.70, width: 0.84, height: 0.27)

    let layout: Layout
    private let lexicon: Lexicon
    private let customWords: [String]
    private let context = CIContext(options: [.cacheIntermediates: false])

    init(lexicon: Lexicon, glossary: Glossary?) {
        let r = Self.defaultRegion
        layout = Layout(left: r.left, top: r.top, width: r.width, height: r.height,
                        centerTol: glossary?.screen["center_tol"] ?? 0, objectiveY: glossary?.screen["objective_y"] ?? 0)
        self.lexicon = lexicon
        customWords = glossary?.lexiconExtras ?? []
    }

    /// Reads one frame. Returns the reading and the region's pixels (for the "text still in place" signatures).
    func read(_ pixelBuffer: CVPixelBuffer) -> (Reading, RegionImage)? {
        let started = CACurrentMediaTime()
        let frame = CIImage(cvPixelBuffer: pixelBuffer)
        let fw = frame.extent.width, fh = frame.extent.height
        let rect = CGRect(x: (layout.left * fw).rounded(), y: (layout.top * fh).rounded(),
                          width: (layout.width * fw).rounded(), height: (layout.height * fh).rounded())
        guard let region = RegionImage.render(frame, rect: rect, context: context),
              let rawImage = region.cgImage() else { return nil }

        let raw = layout.classify(recognize(rawImage))
        var candidates = [("raw", raw)]
        if !isClean(raw), let white = region.whiteTextImage(frameHeight: Int(fh)) {
            candidates.append(("white", layout.classify(recognize(white))))
        }

        func choose(_ rows: (Layout.Split) -> [OcrLine]) -> (text: String, prep: String, rows: [OcrLine]) {
            let options = candidates.map { (Layout.joined(rows($0.1)), $0.0, rows($0.1)) }.filter { !$0.0.isEmpty }
            return options.max { lexicon.score($0.0) < lexicon.score($1.0) } ?? ("", "", [])
        }
        let speech = choose(\.speech)
        let objective = choose(\.objective)

        var r = Reading()
        r.raw = candidates.flatMap { ($0.1.speech + $0.1.objective).map(\.text) }.joined(separator: " | ")
        r.prep = speech.prep.isEmpty ? objective.prep : speech.prep
        if !speech.text.isEmpty {
            if lexicon.isTurkish(speech.text) {
                r.turkish = true
            } else {
                r.speech = speech.text
                r.speechBox = Layout.bounding(speech.rows)
                r.speechLines = speech.rows.count
            }
        }
        if !objective.text.isEmpty, !lexicon.isTurkish(objective.text) {
            r.objective = objective.text
            r.objectiveBox = Layout.bounding(objective.rows)
            r.objectiveLines = objective.rows.count
        }
        r.ms = (CACurrentMediaTime() - started) * 1000
        return (r, region)
    }

    private func isClean(_ split: Layout.Split) -> Bool {
        let texts = [Layout.joined(split.speech), Layout.joined(split.objective)].filter { !$0.isEmpty }
        return !texts.isEmpty && texts.allSatisfy(lexicon.isClean)
    }

    /// Vision text lines, boxes relative to the image (top-left origin).
    private func recognize(_ image: CGImage) -> [OcrLine] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = true
        request.customWords = customWords
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        do { try handler.perform([request]) } catch { return [] }
        return (request.results ?? []).compactMap { o in
            guard let text = o.topCandidates(1).first?.string else { return nil }
            let b = o.boundingBox
            return OcrLine(text, b.minX, 1 - b.maxY, b.width, b.height)
        }
    }
}
