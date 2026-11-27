/// Port of Python's difflib.SequenceMatcher with isjunk=None and autojunk=False (how pc/textutil.py uses it), over
/// Unicode scalars so results match Python's str comparison exactly.
struct SequenceMatcher {
    struct Match: Comparable {
        let a: Int, b: Int, size: Int
        static func < (l: Match, r: Match) -> Bool { (l.a, l.b, l.size) < (r.a, r.b, r.size) }
    }

    private let a: [Unicode.Scalar]
    private let b: [Unicode.Scalar]
    private let b2j: [Unicode.Scalar: [Int]]

    init(_ a: String, _ b: String) {
        self.a = Array(a.unicodeScalars)
        self.b = Array(b.unicodeScalars)
        var index: [Unicode.Scalar: [Int]] = [:]
        for (j, c) in self.b.enumerated() { index[c, default: []].append(j) }
        b2j = index
    }

    func findLongestMatch(_ alo: Int, _ ahi: Int, _ blo: Int, _ bhi: Int) -> Match {
        var besti = alo, bestj = blo, bestsize = 0
        var j2len: [Int: Int] = [:]
        for i in alo..<ahi {
            var newj2len: [Int: Int] = [:]
            for j in b2j[a[i]] ?? [] {
                if j < blo { continue }
                if j >= bhi { break }
                let k = (j2len[j - 1] ?? 0) + 1
                newj2len[j] = k
                if k > bestsize { besti = i - k + 1; bestj = j - k + 1; bestsize = k }
            }
            j2len = newj2len
        }
        // No junk: the extension loops of difflib cannot grow the match further, kept for fidelity.
        while besti > alo, bestj > blo, a[besti - 1] == b[bestj - 1] { besti -= 1; bestj -= 1; bestsize += 1 }
        while besti + bestsize < ahi, bestj + bestsize < bhi, a[besti + bestsize] == b[bestj + bestsize] { bestsize += 1 }
        return Match(a: besti, b: bestj, size: bestsize)
    }

    func matchingBlocks() -> [Match] {
        var queue = [(0, a.count, 0, b.count)]
        var blocks: [Match] = []
        while let (alo, ahi, blo, bhi) = queue.popLast() {
            let m = findLongestMatch(alo, ahi, blo, bhi)
            if m.size > 0 {
                blocks.append(m)
                if alo < m.a, blo < m.b { queue.append((alo, m.a, blo, m.b)) }
                if m.a + m.size < ahi, m.b + m.size < bhi { queue.append((m.a + m.size, ahi, m.b + m.size, bhi)) }
            }
        }
        return blocks.sorted()
    }

    func ratio() -> Double {
        let matches = matchingBlocks().reduce(0) { $0 + $1.size }
        let total = a.count + b.count
        return total > 0 ? 2.0 * Double(matches) / Double(total) : 1.0
    }
}
