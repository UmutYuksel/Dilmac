import Foundation

/// Thin NSRegularExpression wrapper so the Python `re` patterns of pc/ can be ported as they are.
/// ICU (NSRegularExpression) and Python agree on what the patterns used here mean (\w, \s and \d are Unicode aware
/// in both).
struct Rx: @unchecked Sendable {
    let re: NSRegularExpression

    init(_ pattern: String, _ options: NSRegularExpression.Options = []) {
        re = try! NSRegularExpression(pattern: pattern, options: options)
    }

    private func range(_ s: String) -> NSRange { NSRange(s.startIndex..., in: s) }

    /// re.search(...) is not None
    func test(_ s: String) -> Bool { re.firstMatch(in: s, range: range(s)) != nil }

    /// re.match / re.search: the first match's groups (index 0 = whole match); nil for a group that did not take part.
    func firstGroups(_ s: String) -> [String?]? {
        guard let m = re.firstMatch(in: s, range: range(s)) else { return nil }
        return (0..<m.numberOfRanges).map { i in Range(m.range(at: i), in: s).map { String(s[$0]) } }
    }

    /// re.sub
    func sub(_ s: String, _ template: String) -> String {
        re.stringByReplacingMatches(in: s, range: range(s), withTemplate: NSRegularExpression.escapedTemplate(for: template))
    }

    /// re.findall (whole matches)
    func findAll(_ s: String) -> [String] {
        re.matches(in: s, range: range(s)).compactMap { Range($0.range, in: s).map { String(s[$0]) } }
    }

    /// Ranges of all matches (for re.finditer / re.split ports).
    func ranges(_ s: String) -> [Range<String.Index>] {
        re.matches(in: s, range: range(s)).compactMap { Range($0.range, in: s) }
    }

    /// re.split (no capture groups in the pattern).
    func split(_ s: String) -> [String] {
        var parts: [String] = []
        var start = s.startIndex
        for r in ranges(s) {
            parts.append(String(s[start..<r.lowerBound]))
            start = r.upperBound
        }
        parts.append(String(s[start...]))
        return parts
    }
}

extension String {
    /// Python str.casefold() for the text handled here (lower case, Unicode aware).
    var casefolded: String { lowercased() }
}
