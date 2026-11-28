import Foundation

/// JSONSerialization forgets the key order of objects; Python dicts keep it, and the LLM prompt lists terms and
/// nicknames in file order. This scan returns, for each member of the top-level object that is itself an object,
/// its keys in file order.
enum OrderedJSON {
    static func memberKeys(_ data: Data) -> [String: [String]] {
        var s = Scanner(bytes: [UInt8](data))
        return s.topLevel() ?? [:]
    }

    /// The keys of the top-level object, in file order ([] if it is not an object).
    static func topLevelKeys(_ data: Data) -> [String] {
        var s = Scanner(bytes: [UInt8](data))
        s.skipSpace()
        var keys: [String] = []
        guard s.object(each: { k, inner in keys.append(k); return inner.skipValue() }) else { return [] }
        return keys
    }

    private struct Scanner {
        let bytes: [UInt8]
        var i = 0

        init(bytes: [UInt8]) { self.bytes = bytes }

        mutating func skipSpace() {
            while i < bytes.count, [0x20, 0x09, 0x0A, 0x0D].contains(bytes[i]) { i += 1 }
        }

        mutating func topLevel() -> [String: [String]]? {
            skipSpace()
            guard i < bytes.count, bytes[i] == UInt8(ascii: "{") else { return nil }
            var out: [String: [String]] = [:]
            _ = object { key, s in
                s.skipSpace()
                if s.i < s.bytes.count, s.bytes[s.i] == UInt8(ascii: "{") {
                    var keys: [String] = []
                    guard s.object(each: { k, inner in keys.append(k); return inner.skipValue() }) else { return false }
                    if out[key] == nil { out[key] = keys }
                    return true
                }
                return s.skipValue()
            }
            return out
        }

        /// Walks an object; `each` gets the key and must consume the value.
        mutating func object(each: (String, inout Scanner) -> Bool) -> Bool {
            guard i < bytes.count, bytes[i] == UInt8(ascii: "{") else { return false }
            i += 1
            skipSpace()
            if i < bytes.count, bytes[i] == UInt8(ascii: "}") { i += 1; return true }
            while true {
                skipSpace()
                guard let key = string() else { return false }
                skipSpace()
                guard i < bytes.count, bytes[i] == UInt8(ascii: ":") else { return false }
                i += 1
                guard each(key, &self) else { return false }
                skipSpace()
                guard i < bytes.count else { return false }
                if bytes[i] == UInt8(ascii: ",") { i += 1; continue }
                if bytes[i] == UInt8(ascii: "}") { i += 1; return true }
                return false
            }
        }

        /// A JSON string, decoded (escapes through JSONSerialization).
        mutating func string() -> String? {
            guard i < bytes.count, bytes[i] == UInt8(ascii: "\"") else { return nil }
            let start = i
            i += 1
            while i < bytes.count, bytes[i] != UInt8(ascii: "\"") {
                i += bytes[i] == UInt8(ascii: "\\") ? 2 : 1
            }
            guard i < bytes.count else { return nil }
            i += 1
            let raw = Data(bytes[start..<i])
            return (try? JSONSerialization.jsonObject(with: raw, options: .fragmentsAllowed)) as? String
        }

        mutating func skipValue() -> Bool {
            skipSpace()
            guard i < bytes.count else { return false }
            switch bytes[i] {
            case UInt8(ascii: "\""): return string() != nil
            case UInt8(ascii: "{"): return object { _, s in s.skipValue() }
            case UInt8(ascii: "["):
                i += 1
                skipSpace()
                if i < bytes.count, bytes[i] == UInt8(ascii: "]") { i += 1; return true }
                while true {
                    guard skipValue() else { return false }
                    skipSpace()
                    guard i < bytes.count else { return false }
                    if bytes[i] == UInt8(ascii: ",") { i += 1; continue }
                    if bytes[i] == UInt8(ascii: "]") { i += 1; return true }
                    return false
                }
            default: // number, true, false, null
                while i < bytes.count, !",}] \t\r\n".utf8.contains(bytes[i]) { i += 1 }
                return true
            }
        }
    }
}
