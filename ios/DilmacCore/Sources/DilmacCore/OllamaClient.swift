import Foundation

/// Whether the local model can answer: Ollama running with the model pulled.
public enum OllamaStatus: Equatable, Sendable {
    case ready
    case down
    case modelMissing(String)
}

/// Ollama answered with an HTTP status other than 200.
public struct OllamaError: Error, CustomStringConvertible {
    public let status: Int
    public let body: String
    public var description: String { "Yerel model HTTP \(status): \(body.prefix(200))" }
}

/// The local model through Ollama's chat API, as pc/translation.py `_local`: the reply is forced into a JSON array
/// of exactly n strings by a schema, the model is kept loaded for an hour, and "think" is turned off (dropped for
/// good once a model rejects it).
public actor OllamaClient {
    public let base: URL
    public let model: String
    private let session: URLSession
    private var think = true

    public init(base: URL = URL(string: "http://127.0.0.1:11434")!, model: String, session: URLSession = .shared) {
        self.base = base
        self.model = model
        self.session = session
    }

    /// Translates the prompt's sentences: n Turkish strings in the same order.
    public func translate(prompt: String, count n: Int) async throws -> [String] {
        var body: [String: Any] = [
            "model": model,
            "messages": [["role": "system", "content": LLMPrompt.system], ["role": "user", "content": prompt]],
            "stream": false,
            "keep_alive": "60m",
            "format": ["type": "array", "items": ["type": "string"], "minItems": n, "maxItems": n] as [String: Any],
            "options": ["temperature": 0, "num_predict": 160 + 120 * n],
        ]
        var connectionRetried = false
        while true {
            if think { body["think"] = false } else { body.removeValue(forKey: "think") }
            var req = URLRequest(url: base.appendingPathComponent("api/chat"), timeoutInterval: 180)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
            let data: Data, status: Int
            do {
                let (d, resp) = try await session.data(for: req)
                data = d
                status = (resp as? HTTPURLResponse)?.statusCode ?? 0
            } catch let e as URLError where !connectionRetried && e.code != .cancelled {
                connectionRetried = true // a dropped keep-alive connection: once more
                continue
            }
            let text = String(decoding: data, as: UTF8.self)
            if status == 400, think, text.contains("think") {
                think = false
                continue
            }
            guard status == 200 else { throw OllamaError(status: status, body: text) }
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let content = (object?["message"] as? [String: Any])?["content"] as? String ?? ""
            return try LLMPrompt.replySeq(content, count: n)
        }
    }

    /// GET /api/tags: down if Ollama does not answer, modelMissing if the model is not pulled.
    public func check() async -> OllamaStatus {
        let req = URLRequest(url: base.appendingPathComponent("api/tags"), timeoutInterval: 5)
        guard let (data, resp) = try? await session.data(for: req), (resp as? HTTPURLResponse)?.statusCode == 200
        else { return .down }
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let names = ((object?["models"] as? [[String: Any]]) ?? []).flatMap { m in
            [m["name"] as? String, m["model"] as? String].compactMap { $0 }
        }
        return names.contains(model) || names.contains(model + ":latest") ? .ready : .modelMissing(model)
    }
}
