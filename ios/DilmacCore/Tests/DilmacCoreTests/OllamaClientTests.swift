// OllamaClient against a stubbed HTTP layer (URLProtocol): the request body the PC sends, the "think" fallback, the
// model check and errors.
@testable import DilmacCore
import Foundation
import XCTest

final class StubProtocol: URLProtocol, @unchecked Sendable {
    struct Reply { var status = 200; var body = Data(); var fail: URLError.Code? }
    nonisolated(unsafe) static var handler: (URLRequest, Data) -> Reply = { _, _ in Reply() }
    nonisolated(unsafe) static var requests: [(URLRequest, Data)] = []

    static func session() -> URLSession {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [StubProtocol.self]
        return URLSession(configuration: c)
    }

    static func json(_ data: Data) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        var body = request.httpBody ?? Data()
        if body.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            var buf = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let n = stream.read(&buf, maxLength: buf.count)
                if n <= 0 { break }
                body.append(buf, count: n)
            }
            stream.close()
        }
        Self.requests.append((request, body))
        let r = Self.handler(request, body)
        if let code = r.fail {
            client?.urlProtocol(self, didFailWithError: URLError(code))
            return
        }
        let resp = HTTPURLResponse(url: request.url!, statusCode: r.status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: r.body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

final class OllamaClientTests: XCTestCase {
    private func chat(_ content: String) -> Data {
        try! JSONSerialization.data(withJSONObject: ["message": ["role": "assistant", "content": content]])
    }

    override func setUp() {
        StubProtocol.requests = []
        StubProtocol.handler = { _, _ in StubProtocol.Reply() }
    }

    func testRequestBody() async throws {
        StubProtocol.handler = { _, _ in .init(body: self.chat(#"["Bir", "İki"]"#)) }
        let c = OllamaClient(model: "gemma4:e4b-it-qat", session: StubProtocol.session())
        let out = try await c.translate(prompt: "PROMPT", count: 2)
        XCTAssertEqual(out, ["Bir", "İki"])
        let (req, data) = StubProtocol.requests[0]
        XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:11434/api/chat")
        XCTAssertEqual(req.httpMethod, "POST")
        let body = StubProtocol.json(data)
        XCTAssertEqual(body["model"] as? String, "gemma4:e4b-it-qat")
        XCTAssertEqual(body["stream"] as? Bool, false)
        XCTAssertEqual(body["keep_alive"] as? String, "60m")
        XCTAssertEqual(body["think"] as? Bool, false)
        let format = body["format"] as? [String: Any]
        XCTAssertEqual(format?["type"] as? String, "array")
        XCTAssertEqual((format?["items"] as? [String: Any])?["type"] as? String, "string")
        XCTAssertEqual(format?["minItems"] as? Int, 2)
        XCTAssertEqual(format?["maxItems"] as? Int, 2)
        let options = body["options"] as? [String: Any]
        XCTAssertEqual(options?["temperature"] as? Int, 0)
        XCTAssertEqual(options?["num_predict"] as? Int, 400)
        let messages = body["messages"] as? [[String: String]]
        XCTAssertEqual(messages?.map { $0["role"]! }, ["system", "user"])
        XCTAssertEqual(messages?[0]["content"], LLMPrompt.system)
        XCTAssertEqual(messages?[1]["content"], "PROMPT")
    }

    func testThinkRetriedWithout() async throws {
        StubProtocol.handler = { _, body in
            StubProtocol.json(body)["think"] != nil
                ? .init(status: 400, body: Data(#"{"error":"\"m\" does not support thinking (think)"}"#.utf8))
                : .init(body: self.chat(#"["Tamam."]"#))
        }
        let c = OllamaClient(model: "m", session: StubProtocol.session())
        let first = try await c.translate(prompt: "p", count: 1)
        XCTAssertEqual(first, ["Tamam."])
        XCTAssertEqual(StubProtocol.requests.count, 2)
        _ = try await c.translate(prompt: "p", count: 1)
        XCTAssertEqual(StubProtocol.requests.count, 3) // no "think" sent again
        XCTAssertNil(StubProtocol.json(StubProtocol.requests[2].1)["think"])
    }

    func testConnectionErrorRetriedOnce() async {
        StubProtocol.handler = { _, _ in .init(fail: .cannotConnectToHost) }
        let c = OllamaClient(model: "m", session: StubProtocol.session())
        do {
            _ = try await c.translate(prompt: "p", count: 1)
            XCTFail("should throw")
        } catch {
            XCTAssertEqual((error as? URLError)?.code, .cannotConnectToHost)
        }
        XCTAssertEqual(StubProtocol.requests.count, 2)
    }

    func testMissingModel() async {
        StubProtocol.handler = { req, _ in
            XCTAssertEqual(req.url?.path, "/api/tags")
            return .init(body: Data(#"{"models":[{"name":"gemma4:e4b-it-qat"},{"name":"llama3:latest"}]}"#.utf8))
        }
        let ready = await OllamaClient(model: "gemma4:e4b-it-qat", session: StubProtocol.session()).check()
        XCTAssertEqual(ready, .ready)
        let latest = await OllamaClient(model: "llama3", session: StubProtocol.session()).check()
        XCTAssertEqual(latest, .ready)
        let missing = await OllamaClient(model: "qwen3:14b", session: StubProtocol.session()).check()
        XCTAssertEqual(missing, .modelMissing("qwen3:14b"))
        StubProtocol.handler = { _, _ in .init(fail: .cannotConnectToHost) }
        let down = await OllamaClient(model: "qwen3:14b", session: StubProtocol.session()).check()
        XCTAssertEqual(down, .down)
    }

    func testNon200Throws() async {
        StubProtocol.handler = { _, _ in .init(status: 500, body: Data("boom".utf8)) }
        let c = OllamaClient(model: "m", session: StubProtocol.session())
        do {
            _ = try await c.translate(prompt: "p", count: 1)
            XCTFail("should throw")
        } catch let e as OllamaError {
            XCTAssertEqual(e.status, 500)
            XCTAssertEqual(e.body, "boom")
        } catch {
            XCTFail("wrong error \(error)")
        }
    }

    func testBadReplyThrowsReplyError() async {
        StubProtocol.handler = { _, _ in .init(body: self.chat("Sure, here you go")) }
        let c = OllamaClient(model: "m", session: StubProtocol.session())
        do {
            _ = try await c.translate(prompt: "p", count: 2)
            XCTFail("should throw")
        } catch {
            XCTAssertTrue(error is LLMPrompt.ReplyError)
        }
    }
}
