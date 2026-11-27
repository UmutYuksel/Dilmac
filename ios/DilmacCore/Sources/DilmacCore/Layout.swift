import Foundation

/// One OCR line; box as a share (0-1) of the region or of the game picture, origin top left.
public struct OcrLine: Equatable, Sendable {
    public var text: String
    public var x: Double, y: Double, w: Double, h: Double

    public init(_ text: String, _ x: Double, _ y: Double, _ w: Double, _ h: Double) {
        self.text = text; self.x = x; self.y = y; self.w = w; self.h = h
    }
}

/// (x, y, w, h) as a share of the game picture.
public struct Box: Equatable, Sendable {
    public var x: Double, y: Double, w: Double, h: Double
    public init(_ x: Double, _ y: Double, _ w: Double, _ h: Double) { self.x = x; self.y = y; self.w = w; self.h = h }
    public var array: [Double] { [x, y, w, h] }
}

/// Port of the line sorting part of pc/ocr.py: where the subtitle region is in the game picture and how OCR rows are
/// sorted into speech / objective / dropped (phones, signs, district names, button hints).
public struct Layout: Sendable {
    public var left = 0.0, top = 0.0, width = 1.0, height = 1.0
    /// > 0: a row's center may be at most this far from the picture's center; rows further off are dropped.
    public var centerTol = 0.0
    /// > 0: a row whose top edge is below this height is objective text.
    public var objectiveY = 0.0

    public init(left: Double = 0, top: Double = 0, width: Double = 1, height: Double = 1, centerTol: Double = 0,
                objectiveY: Double = 0) {
        self.left = left; self.top = top; self.width = width; self.height = height
        self.centerTol = centerTol; self.objectiveY = objectiveY
    }

    /// Line box relative to the region -> relative to the picture.
    public func toFrame(_ s: OcrLine) -> OcrLine {
        OcrLine(s.text, left + s.x * width, top + s.y * height, s.w * width, s.h * height)
    }

    public func toRegion(_ b: Box) -> Box {
        Box((b.x - left) / width, (b.y - top) / height, b.w / width, b.h / height)
    }

    public struct Split: Sendable {
        public var speech: [OcrLine] = []
        public var objective: [OcrLine] = []
        public var dropped: [OcrLine] = []
    }

    /// Aspect ratio of the game picture: to compare horizontal and vertical shares.
    static let aspect = 16.0 / 9.0

    /// Sorts region-relative OCR lines into speech / objective / dropped (boxes relative to the picture).
    public func classify(_ lines: [OcrLine]) -> Split {
        var a = Split()
        for var group in Self.rowGroups(lines.map(toFrame)) {
            if centerTol > 0 {
                guard let (i, j) = Self.centeredPart(group, centerTol) else {
                    a.dropped += group // not centered: phone, sign, district name, button hint
                    continue
                }
                a.dropped += Array(group[..<i]) + Array(group[j...])
                group = Array(group[i..<j])
            }
            let g = Self.merge(group)
            if !Self.hasLetters(g.text) {
                a.dropped.append(g) // HUD leftovers, numbers, single letters
                continue
            }
            if centerTol <= 0 {
                let r = toRegion(Box(g.x, g.y, g.w, g.h))
                if r.w < 0.35 && !(0.2...0.8).contains(r.x + r.w / 2) {
                    a.dropped.append(g) // short line stuck to the edge
                    continue
                }
            }
            if objectiveY > 0 && g.y >= objectiveY { a.objective.append(g) } else { a.speech.append(g) }
        }
        return a
    }

    // MARK: helpers (static, as in ocr.py)

    public static func joined(_ lines: [OcrLine]) -> String {
        TextUtil.normalize(lines.map(\.text).joined(separator: " "))
    }

    public static func bounding(_ lines: [OcrLine]) -> Box? {
        guard !lines.isEmpty else { return nil }
        let x0 = lines.map(\.x).min()!, y0 = lines.map(\.y).min()!
        let x1 = lines.map { $0.x + $0.w }.max()!, y1 = lines.map { $0.y + $0.h }.max()!
        return Box(x0, y0, x1 - x0, y1 - y0)
    }

    static func merge(_ pieces: [OcrLine]) -> OcrLine {
        let b = bounding(pieces)!
        let text = pieces.map { $0.text.trimmingCharacters(in: .whitespaces) }.joined(separator: " ")
        return OcrLine(text, b.x, b.y, b.w, b.h)
    }

    static func hasLetters(_ t: String) -> Bool {
        let compact = t.replacingOccurrences(of: " ", with: "")
        let letters = compact.filter(\.isLetter).count
        return letters >= 3 && Double(letters) / Double(max(1, compact.count)) >= 0.6
    }

    /// OCR pieces at the same height (left to right), groups top to bottom.
    static func rowGroups(_ lines: [OcrLine]) -> [[OcrLine]] {
        var groups: [[OcrLine]] = []
        let sorted = lines.filter { !$0.text.trimmingCharacters(in: .whitespaces).isEmpty }
            .sorted { $0.y + $0.h / 2 < $1.y + $1.h / 2 }
        outer: for s in sorted {
            for gi in groups.indices where groups[gi].contains(where: { abs((s.y + s.h / 2) - ($0.y + $0.h / 2)) <= 0.5 * max(s.h, $0.h) }) {
                groups[gi].append(s)
                continue outer
            }
            groups.append([s])
        }
        return groups.map { $0.sorted { $0.x < $1.x } }
            .sorted { $0.map { $0.y + $0.h / 2 }.min()! < $1.map { $0.y + $0.h / 2 }.min()! }
    }

    /// The widest run of adjacent pieces [i, j) whose center is at the picture's center.
    static func centeredPart(_ group: [OcrLine], _ tol: Double) -> (Int, Int)? {
        var best: (Int, Int)?, bestWidth = -1.0
        for i in group.indices {
            for j in (i + 1)...group.count {
                if j - i > 1 {
                    let a = group[j - 2], b = group[j - 1]
                    if (b.x - (a.x + a.w)) * aspect > 1.2 * max(a.h, b.h) { break } // separate texts
                }
                let k = merge(Array(group[i..<j]))
                if abs(k.x + k.w / 2 - 0.5) <= tol && k.w > bestWidth { best = (i, j); bestWidth = k.w }
            }
        }
        return best
    }
}
